// 分组管理窗口：打开时拉一次分组列表，其余改动都在 ViewModel 里直接打后端。
using Avalonia.Controls;
using Avalonia.Interactivity;
using frontend.ViewModels;

namespace frontend.Views;

public partial class GroupsWindow : Window
{
    public GroupsWindow()
    {
        InitializeComponent();
        Opened += async (_, _) =>
        {
            if (DataContext is GroupsViewModel viewModel)
            {
                await viewModel.LoadAsync();
            }
        };
    }

    private void OnClose(object? sender, RoutedEventArgs e) => Close();
}
