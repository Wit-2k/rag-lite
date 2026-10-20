// 分组选择用的两个小类型：左栏的「筛选」与对话区的「检索范围」共用同一套选项形状。
using frontend.Models;

namespace frontend.ViewModels;

/// <summary>下拉选项的种类。</summary>
public enum GroupScopeKind
{
    /// <summary>全部数据源（不限制检索范围）。</summary>
    All,

    /// <summary>不使用任何数据源（显式空范围）。</summary>
    NoSources,

    /// <summary>未分组的数据源（只作为左栏的筛选条件；检索范围没有这一项）。</summary>
    Ungrouped,

    /// <summary>某个具体分组。</summary>
    Group,
}

/// <summary>下拉里的一项。</summary>
public sealed record GroupScopeOption(GroupScopeKind Kind, string? GroupId, string Name)
{
    /// <summary>后端识别的「不使用数据源」哨兵值。</summary>
    public const string NoSourcesGroupId = "__no_sources__";

    /// <summary>
    /// 按当前分组列表生成选项。
    /// `includeUngrouped` 只有左栏筛选用 true；`includeNoSources` 只有会话检索范围用 true。
    /// </summary>
    public static List<GroupScopeOption> Build(
        IReadOnlyList<GroupDto> groups,
        bool includeUngrouped,
        bool includeNoSources = false)
    {
        var options = new List<GroupScopeOption> { new(GroupScopeKind.All, null, "全部数据源") };
        if (includeNoSources)
        {
            options.Add(new GroupScopeOption(GroupScopeKind.NoSources, NoSourcesGroupId, "不使用数据源"));
        }
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
        GroupScopeKind.NoSources => false,
        GroupScopeKind.Ungrouped => source.GroupIds.Count == 0,
        _ => source.GroupIds.Contains(GroupId!),
    };
}
