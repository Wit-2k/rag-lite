// 把 Python 后端当作子进程拉起来，并等它打印握手行。
//
// 为什么要握手行：端口可能被占用而顺延（后端会自己找空闲端口），所以不能假定固定端口，
// 得由后端告诉我们「我起来了，在哪个端口」。
using System.Diagnostics;
using System.Text.Json;

namespace frontend.Services;

/// <summary>后端进程：启动、读握手行、退出时清理。</summary>
public sealed class BackendProcess : IAsyncDisposable
{
    private const string HandshakePrefix = "RAGLITE_READY";

    /// <summary>让后端盯着界面进程：界面被硬杀时后端自己退出（作业对象之外的第二道保险）。</summary>
    private const string WatchParentArgument = "--watch-parent";

    private Process? _process;
    private ChildJob? _job;

    /// <summary>后端地址，`StartAsync` 成功后才有值。</summary>
    public Uri? BaseAddress { get; private set; }

    /// <summary>后端启动命令（日志里打出来，出问题好排查）。</summary>
    public string CommandLine { get; private set; } = "";

    /// <summary>
    /// 启动后端并等握手行。超时会抛异常（消息里带后端最近输出，便于定位）。
    /// </summary>
    public async Task<Uri> StartAsync(TimeSpan? timeout = null, CancellationToken cancellationToken = default)
    {
        var (fileName, arguments, workingDirectory) = ResolveBackend();
        CommandLine = $"{fileName} {arguments}";
        AppLog.Write($"[后端] 启动：{CommandLine}（工作目录 {workingDirectory}）");

        var info = new ProcessStartInfo(fileName, arguments)
        {
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };

        _process = Process.Start(info) ?? throw new InvalidOperationException($"无法启动后端：{CommandLine}");
        // 两道保险防孤儿进程：① 作业对象（系统级，界面一死立刻杀掉整棵树）；
        // ② 后端自己的看门狗（读 stdin，界面一死读到 EOF 就退出）——①建不出来时还有②档着
        _job = ChildJob.TryCreate();
        if (_job is not null && !_job.Assign(_process))
        {
            AppLog.Write("[后端] 提示：没能把后端放进作业对象（错误码见上），本次只靠看门狗兜底");
        }
        var deadline = DateTime.UtcNow + (timeout ?? TimeSpan.FromSeconds(90));
        var recent = new Queue<string>();
        var ready = new TaskCompletionSource<Uri>(TaskCreationOptions.RunContinuationsAsynchronously);

        // 后端 stderr（uvicorn 日志）转发到日志文件，方便排查启动失败
        _ = Task.Run(async () =>
        {
            try
            {
                while (await _process.StandardError.ReadLineAsync(cancellationToken) is { } line)
                {
                    AppLog.Write($"[后端] {line}");
                }
            }
            catch (Exception ex)
            {
                AppLog.Error(ex, "读取后端 stderr");
            }
        }, cancellationToken);

        // 后端 stdout：只认握手行，其余（uvicorn 的启动提示）照旧记日志
        _ = Task.Run(async () =>
        {
            try
            {
                while (await _process.StandardOutput.ReadLineAsync(cancellationToken) is { } line)
                {
                    if (line.StartsWith(HandshakePrefix, StringComparison.Ordinal))
                    {
                        var payload = line[HandshakePrefix.Length..].Trim();
                        using var document = JsonDocument.Parse(payload);
                        var host = document.RootElement.GetProperty("host").GetString() ?? "127.0.0.1";
                        var port = document.RootElement.GetProperty("port").GetInt32();
                        ready.TrySetResult(new Uri($"http://{host}:{port}"));
                    }
                    else if (line.Length > 0)
                    {
                        AppLog.Write($"[后端] {line}");
                    }
                    lock (recent)
                    {
                        recent.Enqueue(line);
                        while (recent.Count > 20)
                        {
                            recent.Dequeue();
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                AppLog.Error(ex, "读取后端 stdout");
            }
            ready.TrySetException(new InvalidOperationException("后端进程已经退出（没等到握手行）"));
        }, cancellationToken);

        while (DateTime.UtcNow < deadline)
        {
            if (ready.Task.IsCompleted)
            {
                break;
            }
            if (_process.HasExited)
            {
                throw new InvalidOperationException(
                    $"后端启动失败（退出码 {_process.ExitCode}）：{CommandLine}\n最近输出：\n{RecentOutput(recent)}");
            }
            await Task.WhenAny(ready.Task, Task.Delay(200, cancellationToken));
        }

        if (!ready.Task.IsCompleted)
        {
            throw new TimeoutException($"等后端握手超时（{timeout?.TotalSeconds ?? 90:0} 秒）：{CommandLine}\n最近输出：\n{RecentOutput(recent)}");
        }

        BaseAddress = await ready.Task;
        AppLog.Write($"[后端] 就绪：{BaseAddress}");
        return BaseAddress;
    }

    private static string RecentOutput(Queue<string> recent)
    {
        lock (recent)
        {
            return string.Join(Environment.NewLine, recent);
        }
    }

    /// <summary>
    /// 找后端在哪、怎么启动：
    /// 1）环境变量 RAGLITE_BACKEND 指定的可执行文件；
    /// 2）便携布局：exe 同级 backend/raglite-backend.exe；
    /// 3）开发布局：往上找到含 backend/main.py 的仓库根，用 backend/.venv 里的 python 跑它。
    /// </summary>
    private static (string FileName, string Arguments, string WorkingDirectory) ResolveBackend()
    {
        var overridePath = Environment.GetEnvironmentVariable("RAGLITE_BACKEND");
        if (!string.IsNullOrWhiteSpace(overridePath) && File.Exists(overridePath))
        {
            return (overridePath, WatchParentArgument, Path.GetDirectoryName(overridePath)!);
        }

        var portable = Path.Combine(AppContext.BaseDirectory, "backend", "raglite-backend.exe");
        if (File.Exists(portable))
        {
            return (portable, WatchParentArgument, Path.GetDirectoryName(portable)!);
        }

        var repoRoot = FindRepoRoot();
        if (repoRoot is null)
        {
            throw new FileNotFoundException(
                "没找到后端：既没有 backend/raglite-backend.exe，也没找到含 backend/main.py 的仓库目录。" +
                "可以用环境变量 RAGLITE_BACKEND 指定后端可执行文件。");
        }

        var backendDir = Path.Combine(repoRoot, "backend");
        var venvPython = Path.Combine(backendDir, ".venv", "Scripts", "python.exe");
        var python = File.Exists(venvPython) ? venvPython : "python";
        return (python, $"main.py {WatchParentArgument}", backendDir);
    }

    /// <summary>从 exe 所在目录往上找仓库根（开发期 exe 在 frontend/bin/Debug/net10.0/）。</summary>
    private static string? FindRepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (var depth = 0; depth < 8 && dir is not null; depth++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "backend", "main.py")))
            {
                return dir.FullName;
            }
        }
        return null;
    }

    /// <summary>退出时连同子进程一起结束，别留下孤儿后端占着端口。</summary>
    public ValueTask DisposeAsync()
    {
        if (_process is { HasExited: false })
        {
            try
            {
                _process.Kill(entireProcessTree: true);
                AppLog.Write("[后端] 已停止");
            }
            catch (Exception ex)
            {
                AppLog.Error(ex, "停止后端");
            }
        }
        _process?.Dispose();
        _process = null;
        // 关掉作业句柄：即使上面那次 Kill 失败，系统也会把作业里的残余进程清掉
        _job?.Dispose();
        _job = null;
        return ValueTask.CompletedTask;
    }
}
