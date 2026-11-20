const { request, requireAuth, failure } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme } = require('../../utils/theme');
const config = require('../../config');

Page({
  data: {
    theme: 'light',
    user: null,
    accounts: [],
    error: '',
    apiBase: config.apiBase,
    mode: config.authMode
  },
  onLoad() {
    this.setData({ theme: getTheme() });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) this.load();
  },
  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
  },
  onThemeChange(e) {
    this.setData({ theme: e.detail.theme });
  },
  async load() {
    try {
      const [u, accounts] = await Promise.all([request('/me'), request('/accounts')]);
      this.setData({
        user: {
          ...u,
          roleLabel: label(u.role),
          initial: (u.name || u.username || 'U').slice(0, 1)
        },
        accounts,
        error: ''
      });
    } catch (e) {
      this.setData({ error: e.message });
    }
  },
  logout() {
    wx.showModal({
      title: '退出登录',
      content: '退出后需重新登录。',
      success: async (r) => {
        if (!r.confirm) return;
        await request('/auth/logout', 'POST').catch(() => {});
        wx.removeStorageSync('bankmind-token');
        wx.removeStorageSync('bankmind-user');
        wx.reLaunch({ url: '/pages/login/index' });
      }
    });
  },
  bindWechat() {
    wx.login({
      success: async (r) => {
        try {
          await request('/auth/wechat/bind', 'POST', { code: r.code });
          wx.showToast({ title: '微信身份已绑定' });
        } catch (e) {
          failure(e);
        }
      },
      fail: () => failure(new Error('微信登录未完成'))
    });
  }
});
