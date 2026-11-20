const {workspacePage}=require('../../utils/workspace');
const {request, requireAuth, failure, clearSession} = require('../../utils/api');
const {confirmCall, providerError} = require('../../utils/ai');
const {label} = require('../../utils/labels');
const config = require('../../config');

workspacePage({
  data: {
    user: null, accounts: [], error: '',
    apiBase: config.cloud ? '微信云托管 · ' + config.cloud.service : config.apiBase, mode: config.authMode,
    providers: [], testResults: {}, testing: '', canTest: false,
    switching: false, showWechatBinding: config.local || config.authMode === 'wechat'
  },
  onShow() {
    if (requireAuth()) this.load();
  },
  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
  },
  async load() {
    try {
      const [user, accounts, ai] = await Promise.all([
        request('/me'), request('/accounts'), request('/ai/config')
      ]);
      this.setData({
        user: {...user, roleLabel: label(user.role), initial: (user.name || user.username || 'U').slice(0, 1)},
        accounts, providers: ai.providers || [], canTest: ['Admin', 'Accountant', 'Reviewer'].includes(user.role),
        error: ''
      });
    } catch (error) {
      this.setData({error: error.message});
    }
  },
  async testProvider(event) {
    const id = event.currentTarget.dataset.id;
    const provider = this.data.providers.find(item => item.id === id);
    if (this.data.testing || !this.data.canTest || !provider?.ready) return;
    this.setData({testing: id});
    const resultKey = 'testResults.' + id;
    try {
      if (!await confirmCall(provider, true)) return;
      this.setData({[resultKey]: ''});
      const result = await request('/ai/probe/' + id + '?confirm_external=true', 'POST', undefined, 95000);
      const message = result.status === 'SUCCEEDED'
        ? `测试通过 · ${(result.elapsed_ms / 1000).toFixed(1)} 秒`
        : providerError(result.error);
      this.setData({[resultKey]: message});
    } catch (error) {
      this.setData({[resultKey]: error.message});
    } finally {
      this.setData({testing: ''});
    }
  },
  async endSession(title) {
    if (this.data.switching) return;
    this.setData({switching: true});
    try {
      const confirmed = await new Promise(resolve => wx.showModal({
        title,
        content: '当前账号会退出工作台。',
        success: result => resolve(result.confirm),
        fail: () => resolve(false)
      }));
      if (!confirmed) return;
      await request('/auth/logout', 'POST', undefined, 5000).catch(() => {});
      clearSession();
      wx.reLaunch({url: '/pages/login/index'});
    } finally {
      this.setData({switching: false});
    }
  },
  switchAccount() {
    return this.endSession('切换账号');
  },
  logout() {
    return this.endSession('退出登录');
  },
  bindWechat() {
    wx.login({
      success: async result => {
        try {
          await request('/auth/wechat/bind', 'POST', {code: result.code});
          wx.showToast({title: '微信身份已绑定'});
        } catch (error) {
          failure(error);
        }
      },
      fail: () => failure(new Error('微信登录未完成'))
    });
  }
});
