// 分组选择用的两个小类型：左栏的「筛选」与对话区的「检索范围」共用同一套选项形状。
using frontend.Models;

namespace frontend.ViewModels;

/// <summary>下拉选项的种类。</summary>
public enum GroupScopeKind
{
    /// <summary>全部数据源（不限制检索范围）。</summary>
    All,

    /// <summary>未分组的数据源（只作为左栏的筛选条件；检索范围没有这一项）。</summary>
    Ungrouped,

    /// <summary>某个具体分组。</summary>
    Group,
}

/// <summary>下拉里的一项。</summary>
public sealed record GroupScopeOption(GroupScopeKind Kind, string? GroupId, string Name)
{
    /// <summary>
    /// 按当前分组列表生成选项。
    /// `includeUngrouped` 只有左栏筛选用 true——会话的检索范围只认「全部数据源」或某个具体分组
    /// （「未分组」不是后端能表达的范围）。
    /// </summary>
    public static List<GroupScopeOption> Build(IReadOnlyList<GroupDto> groups, bool includeUngrouped)
    {
        var options = new List<GroupScopeOption> { new(GroupScopeKind.All, null, "全部数据源") };
        if (includeUngrouped)
        {
            options.Add(new GroupScopeOption(GroupScopeKind.Ungrouped, null, "未分组"));
        }
        options.AddRange(groups.Select(group => new GroupScopeOption(
            GroupScopeKind.Group, group.GroupId, $"{group.Name}（{group.SourceCount}）")));
        return options;
    }

    /// <summary>这一行数据源是不是在当前筛选范围内。</summary>
    public bool Matches(SourceDto source) => Kind switch
    {
        GroupScopeKind.All => true,
        GroupScopeKind.Ungrouped => source.GroupIds.Count == 0,
        _ => source.GroupIds.Contains(GroupId!),
    };
}
