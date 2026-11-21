const { request } = require('../../utils/api');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');
const config = require('../../config');

Page({
  data: {
    theme: getTheme(),
    username: 'reviewer',
    password: '',
    showPassword: false,
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
  togglePassword() {
    this.setData({ showPassword: !this.data.showPassword });
  },
  async login() {
    haptic();
    const username = (this.data.username || '').trim();
    const password = this.data.password || '';
    if (!username || !password) {
      this.setData({ error: '请输入企业账号和密码' });
      return;
    }
    this.setData({ busy: true, error: '' });
    try {
      const result = await request('/auth/login', 'POST', { username, password });
      playSound('success');
      await this.finish(result);
    } catch (e) {
      playSound('error');
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
