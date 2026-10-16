// 接口返回的数据形状。字段名与后端 backend/app/api/models.py 一一对应，
// 后端用 snake_case，这里用 JsonSourceGenerationOptions 统一转换，不必逐个写特性。
using System.Text.Json;
using System.Text.Json.Serialization;

namespace frontend.Models;

/// <summary>就绪探测结果。</summary>
public sealed record HealthDto(
    string Status,
    int Chunks,
    int Sources,
    int SourcesStale,
    bool Configured,
    IReadOnlyList<string> MissingKeys,
    int EmbedDim,
    string EmbedModel,
    string EmbedBaseUrl,
    string RerankModel,
    string RerankBaseUrl,
    string ChatModel,
    string ChatBaseUrl,
    string DataDir,
    string EnvFile);

/// <summary>一个数据源（MinerU 产物目录）。</summary>
public sealed record SourceDto(
    string SourceId,
    string Name,
    string Path,
    int ChunkCount,
    int ImageCount,
    string AddedAt,
    string EmbeddedAt,
    bool Missing,
    string EmbedModel,
    int EmbedDim,
    bool Stale,
    IReadOnlyList<string> GroupIds);   // 它属于哪些分组（一个源可以同时在多个分组里）

/// <summary>后台任务状态（导入进度）。</summary>
public sealed record JobDto(
    string Id,
    string Kind,
    string State,
    int Total,
    int Done,
    string Message,
    string Error,
    JobResultDto? Result);

/// <summary>导入任务跑完后的结果摘要。</summary>
public sealed record JobResultDto(
    string SourceId,
    string Name,
    int ChunkCount,
    int ImageCount,
    bool Skipped,
    string Reason,
    double CostUsd,
    double Seconds,
    IReadOnlyList<string> Warnings);

/// <summary>添加数据源的请求体。</summary>
public sealed record SourceCreateDto(string Path, string? Name = null);

/// <summary>一个会话（会话栏里的一行）。</summary>
public sealed record SessionDto(
    string SessionId,
    string Title,
    string CreatedAt,
    string UpdatedAt,
    int MessageCount,
    string? GroupId);   // 检索范围：绑定的分组；null = 全部数据源

/// <summary>新建会话的请求体（标题留空则由后端用默认名，首问后自动命名）。</summary>
public sealed record SessionCreateDto(string? Title = null, string? GroupId = null);

/// <summary>设置某个会话的检索范围（GroupId 为 null 就是「全部数据源」；
/// 源生成器配了 WhenWritingNull，所以 null 会整个字段不写，正好等于后端要的 null）。</summary>
public sealed record SessionGroupDto(string? GroupId);

/// <summary>一个数据源分组。</summary>
public sealed record GroupDto(
    string GroupId,
    string Name,
    string CreatedAt,
    int SourceCount);

/// <summary>新建 / 重命名分组。</summary>
public sealed record GroupNameDto(string Name);

/// <summary>把一个数据源的所属分组整体替换成这些（空列表 = 移出所有分组）。</summary>
public sealed record SourceGroupsDto(IReadOnlyList<string> GroupIds);

/// <summary>重命名会话。</summary>
public sealed record SessionRenameDto(string Title);

/// <summary>一条聊天记录（切换会话时用它渲染）。</summary>
public sealed record ChatMessageDto(
    int MessageId,
    string Role,
    string Content,
    IReadOnlyList<ChatSourceDto> Sources,
    string CreatedAt);

/// <summary>提问请求（必须带会话 id：历史存库里、按会话取）。</summary>
public sealed record ChatRequest(string Question, string SessionId);

/// <summary>清空某个会话的消息。</summary>
public sealed record ChatClearRequest(string SessionId);

/// <summary>当前生效的配置（密钥只有打码提示，没有明文）。</summary>
public sealed record SettingsDto(
    string EnvFile,
    string DataDir,
    string DbUri,
    int StoreDim,
    bool ApiKeySet,
    string ApiKeyHint,
    string EmbedBaseUrl,
    string EmbedModel,
    int EmbedDim,
    bool EmbedApiKeySet,
    string EmbedApiKeyHint,
    string RerankBaseUrl,
    string RerankModel,
    bool RerankApiKeySet,
    string RerankApiKeyHint,
    string ChatBaseUrl,
    string ChatModel,
    bool ChatApiKeySet,
    string ChatApiKeyHint,
    int RetrieveTopK,
    int ContextN);

/// <summary>
/// 要改的配置。**留空表示不修改**（所以密钥框可以只填新值，不必重敲旧密钥）。
/// </summary>
public sealed record SettingsUpdateDto(
    string? ApiKey = null,
    string? EmbedBaseUrl = null,
    string? EmbedModel = null,
    int? EmbedDim = null,
    string? EmbedApiKey = null,
    string? RerankBaseUrl = null,
    string? RerankModel = null,
    string? RerankApiKey = null,
    string? ChatBaseUrl = null,
    string? ChatModel = null,
    string? ChatApiKey = null,
    int? RetrieveTopK = null,
    int? ContextN = null);

/// <summary>清空上下文的结果。</summary>
public sealed record ChatClearDto(int Cleared, int Turns);

/// <summary>命中的参考片段（SSE 的 sources 帧里的一项）。</summary>
public sealed record ChatSourceDto(
    int Rank,
    string Name,
    string Heading,
    double Score,
    double Distance,
    int Start,
    int End,
    IReadOnlyList<string> Images,
    string Preview);

/// <summary>回答用量。</summary>
public sealed record ChatUsageDto(int PromptTokens, int CompletionTokens, double CostUsd);

/// <summary>
/// SSE 的一帧。四种 type 共用这一个形状，用到的字段以外的都是 null：
/// sources（items）/ token（text）/ done（usage、seconds）/ error（message）。
/// </summary>
public sealed record ChatFrameDto(
    string Type,
    IReadOnlyList<ChatSourceDto>? Items,
    string? Text,
    ChatUsageDto? Usage,
    double? Seconds,
    string? Message);

/// <summary>通用的错误体（后端 HTTPException 返回 {"detail": "..."}）。</summary>
public sealed record ErrorDto(string Detail);

/// <summary>
/// System.Text.Json 的源生成上下文：不走反射，启动快、也便于将来裁剪。
/// snake_case 让 C# 的 PascalCase 属性直接对上后端的 source_id / chunk_count。
/// </summary>
[JsonSourceGenerationOptions(
    PropertyNamingPolicy = JsonKnownNamingPolicy.SnakeCaseLower,
    PropertyNameCaseInsensitive = true,
    DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull)]
[JsonSerializable(typeof(HealthDto))]
[JsonSerializable(typeof(SourceDto))]
[JsonSerializable(typeof(GroupDto))]
[JsonSerializable(typeof(IReadOnlyList<GroupDto>))]
[JsonSerializable(typeof(GroupNameDto))]
[JsonSerializable(typeof(SourceGroupsDto))]
[JsonSerializable(typeof(SessionGroupDto))]
[JsonSerializable(typeof(IReadOnlyList<SourceDto>))]
[JsonSerializable(typeof(JobDto))]
[JsonSerializable(typeof(SourceCreateDto))]
[JsonSerializable(typeof(ChatRequest))]
[JsonSerializable(typeof(ChatClearRequest))]
[JsonSerializable(typeof(ChatClearDto))]
[JsonSerializable(typeof(SessionDto))]
[JsonSerializable(typeof(IReadOnlyList<SessionDto>))]
[JsonSerializable(typeof(SessionCreateDto))]
[JsonSerializable(typeof(SessionRenameDto))]
[JsonSerializable(typeof(ChatMessageDto))]
[JsonSerializable(typeof(IReadOnlyList<ChatMessageDto>))]
[JsonSerializable(typeof(SettingsDto))]
[JsonSerializable(typeof(SettingsUpdateDto))]
[JsonSerializable(typeof(ChatFrameDto))]
[JsonSerializable(typeof(ErrorDto))]
public partial class ApiJson : JsonSerializerContext;

/// <summary>把 JSON 解析成指定类型的便捷方法，失败时抛出带原文的异常，方便在日志里定位。</summary>
public static class Json
{
    public static T Parse<T>(string text, System.Text.Json.Serialization.Metadata.JsonTypeInfo<T> typeInfo)
    {
        try
        {
            return JsonSerializer.Deserialize(text, typeInfo)
                   ?? throw new InvalidOperationException($"解析结果为空：{Trim(text)}");
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException($"解析 JSON 失败：{Trim(text)}（{ex.Message}）", ex);
        }
    }

    private static string Trim(string text) => text.Length <= 300 ? text : text[..300] + "…";
}
