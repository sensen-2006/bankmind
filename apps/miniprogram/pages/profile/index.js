const { request, requireAuth, failure } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { isSoundEnabled, setSoundEnabled, playSound } = require('../../utils/sound');
const config = require('../../config');

Page({
  data: {
    theme: getTheme(),
    soundEnabled: isSoundEnabled(),
    user: null,
    accounts: [],
    error: '',
    apiBase: config.apiBase,
    mode: config.authMode
  },
  onShow() {
    syncPageTheme(this);
    this.setData({ soundEnabled: isSoundEnabled() });
    if (requireAuth()) this.load();
  },
  onThemeChange(e) {
    this.setData({ theme: e.detail.theme });
  },
  toggleSound(e) {
    haptic();
    const enabled = setSoundEnabled(e.detail.value);
    this.setData({ soundEnabled: enabled });
    if (enabled) playSound('success', true);
  },
  previewSound() {
    haptic();
    playSound('success', true);
  },
  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
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
    haptic();
    playSound('click');
    wx.showModal({
      title: '退出企业工作空间',
      content: '退出后需要重新登录企业账号。',
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
    haptic();
    playSound('click');
    wx.login({
      success: async (r) => {
        try {
          await request('/auth/wechat/bind', 'POST', { code: r.code });
          playSound('success');
          wx.showToast({ title: '微信身份已绑定' });
        } catch (e) {
          playSound('error');
          failure(e);
        }
      },
      fail: () => {
        playSound('error');
        failure(new Error('微信登录未完成'));
      }
    });
  }
});
