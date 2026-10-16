// 界面程序没有控制台，出问题时得有个地方看——统一写这个日志文件。
using System.Text;

namespace frontend.Services;

/// <summary>极简文件日志：追加写 exe 同级的 rag-lite-client.log。</summary>
public static class AppLog
{
    private static readonly Lock Gate = new();

    public static string FilePath { get; } = Path.Combine(AppContext.BaseDirectory, "rag-lite-client.log");

    public static void Write(string message)
    {
        try
        {
            lock (Gate)
            {
                File.AppendAllText(FilePath, $"{DateTime.Now:HH:mm:ss.fff} {message}{Environment.NewLine}", Encoding.UTF8);
            }
        }
        catch
        {
            // 日志本身不该把程序搞崩（比如目录只读），直接忽略
        }
    }

    public static void Error(Exception exception, string context)
    {
        Write($"[错误] {context}: {exception}");
    }
}
