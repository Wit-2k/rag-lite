// 设置窗口的 ViewModel：把后端配置映射成可编辑字段，保存前做基本校验。
//
// 约定：**留空 = 不修改**（密钥因此不必重敲）。数字项按文本编辑，保存时解析并给出中文提示。
using CommunityToolkit.Mvvm.ComponentModel;
using frontend.Models;

namespace frontend.ViewModels;

public partial class SettingsViewModel : ObservableObject
{
    /// <summary>可选维度（实例属性，便于 XAML 绑定）。</summary>
    public IReadOnlyList<int> AllowedDims { get; } = [256, 512, 1024, 2048];

    private readonly SettingsDto _current;

    public SettingsViewModel(SettingsDto current)
    {
        _current = current;
        ApiKey = "";
        EmbedBaseUrl = current.EmbedBaseUrl;
        EmbedModel = current.EmbedModel;
        EmbedDim = current.EmbedDim;
        EmbedApiKey = "";
        RerankBaseUrl = current.RerankBaseUrl;
        RerankModel = current.RerankModel;
        RerankApiKey = "";
        ChatBaseUrl = current.ChatBaseUrl;
        ChatModel = current.ChatModel;
        ChatApiKey = "";
        RetrieveTopK = current.RetrieveTopK.ToString();
        ContextN = current.ContextN.ToString();

        // 只读信息：让用户知道值写在哪、库是几维、哪些钥匙已经配过
        Location =
            $"配置文件：{current.EnvFile}\n数据目录：{current.DataDir}\n向量库维度：{current.StoreDim} 维（与嵌入维度必须一致）";
        // 密钥只回固定圆点：即使后端以后误带片段，界面也不显示任何真实字符
        ApiKeyHint = current.ApiKeySet ? "已配置：••••••••（留空则不修改）" : "未配置（必填，否则无法导入与问答）";
        EmbedApiKeyHint = current.EmbedApiKeySet ? "已配置：••••••••" : "留空则用上面那把共享密钥";
        RerankApiKeyHint = current.RerankApiKeySet ? "已配置：••••••••" : "留空则用上面那把共享密钥";
        ChatApiKeyHint = current.ChatApiKeySet ? "已配置：••••••••" : "留空则用上面那把共享密钥";
    }

    [ObservableProperty]
    public partial string ApiKey { get; set; }

    [ObservableProperty]
    public partial string EmbedBaseUrl { get; set; }

    [ObservableProperty]
    public partial string EmbedModel { get; set; }

    [ObservableProperty]
    public partial int EmbedDim { get; set; }

    [ObservableProperty]
    public partial string EmbedApiKey { get; set; }

    [ObservableProperty]
    public partial string RerankBaseUrl { get; set; }

    [ObservableProperty]
    public partial string RerankModel { get; set; }

    [ObservableProperty]
    public partial string RerankApiKey { get; set; }

    [ObservableProperty]
    public partial string ChatBaseUrl { get; set; }

    [ObservableProperty]
    public partial string ChatModel { get; set; }

    [ObservableProperty]
    public partial string ChatApiKey { get; set; }

    [ObservableProperty]
    public partial string RetrieveTopK { get; set; }

    [ObservableProperty]
    public partial string ContextN { get; set; }

    [ObservableProperty]
    public partial string Error { get; set; } = "";

    public bool HasError => Error.Length > 0;

    partial void OnErrorChanged(string value) => OnPropertyChanged(nameof(HasError));

    // —— 只读信息 ——

    public string Location { get; }

    public string ApiKeyHint { get; }

    public string EmbedApiKeyHint { get; }

    public string RerankApiKeyHint { get; }

    public string ChatApiKeyHint { get; }

    public int StoreDim => _current.StoreDim;

    public bool DimChanged => EmbedDim != _current.StoreDim;

    /// <summary>保存前校验；通过时返回要提交的 DTO，否则把原因写进 Error 并返回 null。</summary>
    public SettingsUpdateDto? TryBuildUpdate()
    {
        Error = "";
        if (!int.TryParse(RetrieveTopK.Trim(), out var topK) || topK < 1)
        {
            Error = "召回条数要是 ≥1 的整数";
            return null;
        }
        if (!int.TryParse(ContextN.Trim(), out var contextN) || contextN < 1)
        {
            Error = "送给大模型的片段数要是 ≥1 的整数";
            return null;
        }
        if (contextN > topK)
        {
            Error = $"送给大模型的片段数（{contextN}）不能大于召回条数（{topK}）";
            return null;
        }
        foreach (var (label, url) in new[] { ("嵌入", EmbedBaseUrl), ("重排", RerankBaseUrl), ("回答", ChatBaseUrl) })
        {
            if (!url.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !url.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                Error = $"{label}接入点要以 http:// 或 https:// 开头";
                return null;
            }
        }
        if (EmbedModel.Trim().Length == 0 || RerankModel.Trim().Length == 0 || ChatModel.Trim().Length == 0)
        {
            Error = "三个模型 ID 都不能为空";
            return null;
        }

        return new SettingsUpdateDto(
            ApiKey: ApiKey.Trim(),
            EmbedBaseUrl: EmbedBaseUrl.Trim(),
            EmbedModel: EmbedModel.Trim(),
            EmbedDim: EmbedDim,
            EmbedApiKey: EmbedApiKey.Trim(),
            RerankBaseUrl: RerankBaseUrl.Trim(),
            RerankModel: RerankModel.Trim(),
            RerankApiKey: RerankApiKey.Trim(),
            ChatBaseUrl: ChatBaseUrl.Trim(),
            ChatModel: ChatModel.Trim(),
            ChatApiKey: ChatApiKey.Trim(),
            RetrieveTopK: topK,
            ContextN: contextN);
    }
}
