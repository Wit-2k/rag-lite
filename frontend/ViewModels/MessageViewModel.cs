// 对话里的一条消息。助手那条的正文是流式长出来的，所以 Content 是可变的。
using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using frontend.Models;

namespace frontend.ViewModels;

public partial class MessageViewModel : ObservableObject
{
    public MessageViewModel(bool isUser, string content)
    {
        IsUser = isUser;
        Content = content;
    }

    public bool IsUser { get; }

    public bool IsAssistant => !IsUser;

    [ObservableProperty]
    public partial string Content { get; set; }

    /// <summary>正在流式接收中（界面上可以显示个小提示）。</summary>
    [ObservableProperty]
    public partial bool IsStreaming { get; set; }

    /// <summary>回答结束后的用量/耗时等小字信息。</summary>
    [ObservableProperty]
    public partial string Meta { get; set; } = "";

    public bool HasMeta => Meta.Length > 0;

    partial void OnMetaChanged(string value) => OnPropertyChanged(nameof(HasMeta));

    /// <summary>本轮命中的参考片段。</summary>
    public ObservableCollection<ChatSourceDto> Sources { get; } = [];

    public bool HasSources => Sources.Count > 0;

    /// <summary>把命中来源压成一行，显示在气泡上方。</summary>
    public string SourcesText { get; private set; } = "";

    public void SetSources(IEnumerable<ChatSourceDto> items)
    {
        Sources.Clear();
        foreach (var item in items)
        {
            Sources.Add(item);
        }
        SourcesText = string.Join(
            " · ",
            Sources.Select(source => $"[来源 {source.Rank}] {source.Name}／{Trim(source.Heading)}"));
        OnPropertyChanged(nameof(HasSources));
        OnPropertyChanged(nameof(SourcesText));
    }

    private static string Trim(string heading) =>
        string.IsNullOrEmpty(heading) ? "（前言）" : heading.Length <= 24 ? heading : heading[..24] + "…";
}
