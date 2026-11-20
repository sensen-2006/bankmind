const themes = {
  game: {
    title: '游戏工作台', foreground: '#ffffff', navigation: '#14252d', background: '#101c22',
    tabColor: '#aebeb7', selectedColor: '#dfc48b', borderStyle: 'black'
  },
  classic: {
    title: '经典工作台', foreground: '#000000', navigation: '#fafbf4', background: '#eeefe8',
    tabColor: '#63776a', selectedColor: '#286a50', borderStyle: 'white'
  }
};
const tabs = new Set(['pages/home/index', 'pages/tasks/index', 'pages/approvals/index', 'pages/profile/index']);
let activeMode = 'game';

function refreshWorkspace(page) {
  const theme = themes[activeMode];
  const route = page.route;
  const updateChrome = () => applyChrome(theme, route);
  if (page.data.workspaceMode === activeMode) updateChrome();
  else page.setData({workspaceMode: activeMode}, updateChrome);
}

function applyChrome(theme, route) {
  wx.setNavigationBarColor?.({frontColor: theme.foreground, backgroundColor: theme.navigation});
  wx.setBackgroundColor?.({
    backgroundColor: theme.background, backgroundColorTop: theme.background,
    backgroundColorBottom: theme.background
  });
  wx.setBackgroundTextStyle?.({textStyle: theme.foreground === '#ffffff' ? 'light' : 'dark'});
  if (tabs.has(route)) {
    wx.setTabBarStyle?.({
      color: theme.tabColor, selectedColor: theme.selectedColor,
      backgroundColor: theme.navigation, borderStyle: theme.borderStyle
    });
  }
  if (route === 'pages/home/index') wx.setNavigationBarTitle?.({title: theme.title});
}

function workspacePage(definition) {
  const show = definition.onShow;
  Page({
    ...definition,
    data: {...definition.data, workspaceMode: activeMode},
    onShow() {
      refreshWorkspace(this);
      return show?.call(this);
    }
  });
}

function switchWorkspace(page, mode) {
  if (mode !== 'classic' && mode !== 'game') return;
  activeMode = mode;
  refreshWorkspace(page);
}

module.exports = {workspacePage, switchWorkspace};
