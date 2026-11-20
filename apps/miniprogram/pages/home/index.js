const { label } = require('../../utils/labels');
const { request, requireAuth } = require('../../utils/api');
const { getTheme, syncPageTheme } = require('../../utils/theme');

Page({
  data: {
    theme: 'light',
    loading: true,
    error: '',
    dashboard: null,
    user: null,
    recentTasks: []
  },
  onLoad() {
    this.setData({ theme: getTheme() });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(Boolean(this.data.dashboard));
      this.timer = setInterval(() => this.load(true), 15000);
    }
  },
  onHide() {
    clearInterval(this.timer);
  },
  onUnload() {
    clearInterval(this.timer);
  },
  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  onThemeChange(e) {
    this.setData({ theme: e.detail.theme });
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const [d, u] = await Promise.all([request('/dashboard'), request('/me')]);
      this.setData({
        dashboard: d,
        user: u,
        recentTasks: (d.recent_tasks || []).slice(0, 2).map((t) => ({
          ...t,
          statusLabel: label(t.status),
          confirmedPct: t.summary.sides.BANK.valid
            ? Math.round((t.summary.sides.BANK.matched / t.summary.sides.BANK.valid) * 100)
            : 0
        })),
        error: ''
      });
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  openTask(e) {
    wx.navigateTo({ url: '/pages/evidence/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  tasks() {
    wx.switchTab({ url: '/pages/tasks/index' });
  },
  approvals() {
    wx.switchTab({ url: '/pages/approvals/index' });
  },
  alerts() {
    wx.navigateTo({ url: '/pages/alerts/index' });
  }
});
