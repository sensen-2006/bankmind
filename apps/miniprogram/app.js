const config = require('./config');

App({
  globalData: { config, user: null, splashShown: false },
  onLaunch() {
    if (config.cloudEnv && wx.cloud && typeof wx.cloud.init === 'function') {
      try {
        wx.cloud.init({ env: config.cloudEnv });
      } catch (_) {}
    }
    this.globalData.user = wx.getStorageSync('bankmind-user') || null;
  }
});
