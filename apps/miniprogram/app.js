const config = require('./config');
const { getTheme, applyThemeChrome } = require('./utils/theme');

App({
  globalData: {
    config,
    user: null,
    theme: 'light'
  },
  onLaunch() {
    this.globalData.user = wx.getStorageSync('bankmind-user') || null;
    const theme = getTheme();
    this.globalData.theme = theme;
    applyThemeChrome(theme);
  }
});
