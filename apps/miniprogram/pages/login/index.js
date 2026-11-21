const { request } = require('../../utils/api');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const config = require('../../config');

Page({
  data: {
    theme: getTheme(),
    username: 'reviewer',
    password: '',
    busy: false,
    error: '',
    mode: config.authMode
  },
  onLoad() {
    if (wx.getStorageSync('bankmind-token')) wx.switchTab({ url: '/pages/home/index' });
  },
  onShow() {
    syncPageTheme(this);
  },
  onThemeChange(e) {
    this.setData({ theme: e.detail.theme });
  },
  input(e) {
    this.setData({ [e.currentTarget.dataset.field]: e.detail.value });
  },
  async login() {
    haptic();
    this.setData({ busy: true, error: '' });
    try {
      const result = await request('/auth/login', 'POST', {
        username: this.data.username,
        password: this.data.password
      });
      await this.finish(result);
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ busy: false });
    }
  },
  async finish(result) {
    wx.setStorageSync('bankmind-token', result.token);
    const user = await request('/me');
    wx.setStorageSync('bankmind-user', user);
    getApp().globalData.user = user;
    wx.switchTab({ url: '/pages/home/index' });
  },
  wechat() {
    haptic();
    this.setData({ busy: true });
    wx.login({
      success: async (res) => {
        try {
          await this.finish(await request('/auth/wechat', 'POST', { code: res.code }));
        } catch (e) {
          this.setData({ error: e.message });
        } finally {
          this.setData({ busy: false });
        }
      },
      fail: () => this.setData({ busy: false, error: '微信身份校验未完成' })
    });
  }
});
