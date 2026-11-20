const {workspacePage}=require('../../utils/workspace');
const {request, noService} = require('../../utils/api');
const config = require('../../config');

workspacePage({
  data: {
    username: '',
    password: '',
    accounts: [],
    quickFill: false,
    checking: false,
    connectionError: '',
    available: Boolean(config.cloud || config.apiBase),
    unavailableMessage: noService,
    busy: false,
    error: '',
    mode: config.authMode
  },
  onLoad() {
    if (this.data.available && wx.getStorageSync('bankmind-token')) {
      wx.switchTab({url: '/pages/home/index'});
    } else if (this.data.available) {
      this.loadOptions();
    }
  },
  async loadOptions() {
    if (this.data.checking) return;
    this.setData({checking: true, connectionError: ''});
    try {
      const options = await request('/auth/options', 'GET', undefined, 6500);
      this.setData({
        accounts: options.accounts || [],
        quickFill: config.local && options.quick_fill === true
      });
    } catch (error) {
      this.setData({connectionError: error.message});
    } finally {
      this.setData({checking: false});
    }
  },
  chooseAccount(event) {
    if (this.data.busy) return;
    const username = event.currentTarget.dataset.username;
    if (!this.data.accounts.some(account => account.username === username)) return;
    this.setData({
      username,
      password: this.data.quickFill ? 'BankMind@2026' : '',
      error: ''
    });
  },
  input(event) {
    this.setData({[event.currentTarget.dataset.field]: event.detail.value});
  },
  async login() {
    if (!this.data.available || this.data.busy) return;
    const username = this.data.username.trim();
    const password = this.data.password;
    if (!username || !password) {
      this.setData({error: '请输入账号和密码。'});
      return;
    }
    this.setData({busy: true, error: ''});
    try {
      await this.finish(await request('/auth/login', 'POST', {username, password}));
    } catch (error) {
      this.setData({error: error.message});
    } finally {
      this.setData({busy: false});
    }
  },
  async finish(result) {
    wx.setStorageSync('bankmind-token', result.token);
    try {
      const user = await request('/me');
      wx.setStorageSync('bankmind-user', user);
      getApp().globalData.user = user;
      wx.switchTab({url: '/pages/home/index'});
    } catch (error) {
      wx.removeStorageSync('bankmind-token');
      wx.removeStorageSync('bankmind-user');
      throw error;
    }
  },
  wechat() {
    if (!this.data.available || this.data.busy) return;
    this.setData({busy: true, error: ''});
    wx.login({
      success: async result => {
        try {
          await this.finish(await request('/auth/wechat', 'POST', {code: result.code}));
        } catch (error) {
          this.setData({error: error.message});
        } finally {
          this.setData({busy: false});
        }
      },
      fail: () => this.setData({busy: false, error: '微信身份校验未完成'})
    });
  }
});
