const config = require('./config');
const {removeOldDownloads} = require('./utils/files');

App({
  globalData: {config, user: null},
  onLaunch() {
    const backend = config.cloud ? 'wxcloud:' + config.cloud.env + '/' + config.cloud.service : config.apiBase;
    if (config.cloud) {
      wx.cloud?.init?.({env: config.cloud.env});
      removeOldDownloads();
    }
    if (wx.getStorageSync('bankmind-api-base') !== backend) {
      wx.removeStorageSync('bankmind-token');
      wx.removeStorageSync('bankmind-user');
      wx.removeStorageSync('bankmind-review-task');
      wx.setStorageSync('bankmind-api-base', backend);
    }
    this.globalData.user = wx.getStorageSync('bankmind-user') || null;
  }
});
