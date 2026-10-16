// 后端 HTTP 接口的客户端。含 SSE（text/event-stream）的逐帧解析。
using System.Net;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text.Json;
using frontend.Models;

namespace frontend.Services;

/// <summary>流式回答过程中的事件。</summary>
public abstract record ChatEvent;

/// <summary>本轮命中的参考片段。</summary>
public sealed record ChatSourcesEvent(IReadOnlyList<ChatSourceDto> Items) : ChatEvent;

/// <summary>回答的一小段文本。</summary>
public sealed record ChatTokenEvent(string Text) : ChatEvent;

/// <summary>回答结束，带用量。</summary>
public sealed record ChatDoneEvent(ChatUsageDto Usage, double Seconds) : ChatEvent;

/// <summary>出错（后端只发一帧 error，连接随后关闭）。</summary>
public sealed record ChatErrorEvent(string Message) : ChatEvent;

/// <summary>rag-lite 后端的客户端。</summary>
public sealed class ApiClient : IDisposable
{
    private readonly HttpClient _http;

    public ApiClient(Uri baseAddress)
    {
        // 问答要流式返回，不能设总超时；连接层超时交给后端自己的参数控制
        _http = new HttpClient { BaseAddress = baseAddress, Timeout = Timeout.InfiniteTimeSpan };
    }

    public async Task<HealthDto> GetHealthAsync(CancellationToken cancellationToken = default)
    {
        await using var stream = await _http.GetStreamAsync("/api/health", cancellationToken);
        var body = await new StreamReader(stream).ReadToEndAsync(cancellationToken);
        return Json.Parse(body, ApiJson.Default.HealthDto);
    }

    public async Task<IReadOnlyList<SourceDto>> ListSourcesAsync(CancellationToken cancellationToken = default)
    {
        var body = await _http.GetStringAsync("/api/sources", cancellationToken);
        return Json.Parse(body, ApiJson.Default.IReadOnlyListSourceDto);
    }

    /// <summary>登记一个 MinerU 产物目录，返回后台任务 id（调用方轮询进度）。</summary>
    public async Task<string> AddSourceAsync(string path, CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/sources")
        {
            Content = JsonContent.Create(new SourceCreateDto(path), ApiJson.Default.SourceCreateDto),
        };
        var response = await _http.SendAsync(request, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(await DescribeErrorAsync(response, cancellationToken));
        }
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        using var document = JsonDocument.Parse(body);
        return document.RootElement.GetProperty("job_id").GetString()!;
    }

    public async Task<JobDto> GetJobAsync(string jobId, CancellationToken cancellationToken = default)
    {
        var body = await _http.GetStringAsync($"/api/jobs/{jobId}", cancellationToken);
        return Json.Parse(body, ApiJson.Default.JobDto);
    }

    public async Task DeleteSourceAsync(string sourceId, CancellationToken cancellationToken = default)
    {
        var response = await _http.DeleteAsync($"/api/sources/{sourceId}?purge=true", cancellationToken);
        if (!response.IsSuccessStatusCode && response.StatusCode != HttpStatusCode.NotFound)
        {
            throw new InvalidOperationException(await DescribeErrorAsync(response, cancellationToken));
        }
    }

    /// <summary>读当前生效的配置（密钥是打码后的提示）。</summary>
    public async Task<SettingsDto> GetSettingsAsync(CancellationToken cancellationToken = default)
    {
        var body = await _http.GetStringAsync("/api/settings", cancellationToken);
        return Json.Parse(body, ApiJson.Default.SettingsDto);
    }

    /// <summary>改配置：后端会写进 .env 并立即生效（留空的项不动）。</summary>
    public async Task<SettingsDto> SaveSettingsAsync(
        SettingsUpdateDto update, CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Put, "/api/settings")
        {
            Content = JsonContent.Create(update, ApiJson.Default.SettingsUpdateDto),
        };
        var response = await _http.SendAsync(request, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(await DescribeErrorAsync(response, cancellationToken));
        }
        return Json.Parse(body, ApiJson.Default.SettingsDto);
    }

    /// <summary>清空对话上下文（历史不再随下一次提问发出）。</summary>
    public async Task<ChatClearDto> ClearChatAsync(CancellationToken cancellationToken = default)
    {
        var response = await _http.PostAsync("/api/chat/clear", null, cancellationToken);
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw new InvalidOperationException(body);
        }
        return Json.Parse(body, ApiJson.Default.ChatClearDto);
    }

    /// <summary>
    /// 提问并按帧读回 SSE：依次是 sources（一次）→ token（多次）→ done（一次），出错则是 error。
    /// </summary>
    public async IAsyncEnumerable<ChatEvent> StreamChatAsync(
        string question,
        [EnumeratorCancellation] CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, "/api/chat")
        {
            Content = JsonContent.Create(new ChatRequest(question), ApiJson.Default.ChatRequest),
        };
        using var response = await _http.SendAsync(
            request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            yield return new ChatErrorEvent(await DescribeErrorAsync(response, cancellationToken));
            yield break;
        }

        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var reader = new StreamReader(stream);

        while (await reader.ReadLineAsync(cancellationToken) is { } line)
        {
            // SSE 格式：每帧是 "data: {...}"，空行分隔；这里一帧一行，够用也简单
            if (!line.StartsWith("data: ", StringComparison.Ordinal))
            {
                continue;
            }

            ChatFrameDto? frame = null;
            string? parseError = null;
            try
            {
                frame = JsonSerializer.Deserialize(line[6..], ApiJson.Default.ChatFrameDto);
            }
            catch (JsonException ex)
            {
                // 迭代器里不能在 catch 里 yield，先记下来，出了 try 再发
                parseError = $"无法解析后端返回的一帧：{ex.Message}";
            }
            if (parseError is not null)
            {
                yield return new ChatErrorEvent(parseError);
                continue;
            }
            if (frame is null)
            {
                continue;
            }

            switch (frame.Type)
            {
                case "sources":
                    yield return new ChatSourcesEvent(frame.Items ?? []);
                    break;
                case "token":
                    yield return new ChatTokenEvent(frame.Text ?? "");
                    break;
                case "done":
                    yield return new ChatDoneEvent(
                        frame.Usage ?? new ChatUsageDto(0, 0, 0), frame.Seconds ?? 0);
                    break;
                case "error":
                    yield return new ChatErrorEvent(frame.Message ?? "未知错误");
                    break;
            }
        }
    }

    /// <summary>把后端的错误响应翻译成人话：优先取 {"detail": "..."}。</summary>
    private static async Task<string> DescribeErrorAsync(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        var body = await response.Content.ReadAsStringAsync(cancellationToken);
        try
        {
            var error = JsonSerializer.Deserialize(body, ApiJson.Default.ErrorDto);
            if (!string.IsNullOrWhiteSpace(error?.Detail))
            {
                return error.Detail;
            }
        }
        catch (JsonException)
        {
            // 不是标准错误体就原样返回
        }
        return $"HTTP {(int)response.StatusCode}：{body}";
    }

    public void Dispose() => _http.Dispose();
}
