const { label } = require('../../utils/labels');
const { request, requireAuth } = require('../../utils/api');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

function buildTaskMetrics(t) {
  const valid = (t.summary && t.summary.sides && t.summary.sides.BANK && t.summary.sides.BANK.valid) || 0;
  const matched = (t.summary && t.summary.sides && t.summary.sides.BANK && t.summary.sides.BANK.matched) || 0;
  const pending = (t.summary && t.summary.pending_matches) || 0;
  const confirmedPct = valid ? Math.min(100, Math.round((matched / valid) * 100)) : 0;
  const pendingPct = valid ? Math.min(100 - confirmedPct, Math.round((pending / valid) * 100)) : 0;
  return {
    ...t,
    statusLabel: label(t.status),
    confirmedPct,
    pendingPct
  };
}

Page({
  data: {
    theme: getTheme(),
    loading: true,
    error: '',
    dashboard: null,
    user: null,
    recentTasks: []
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(!!this.data.dashboard);
      this.timer = setInterval(() => this.load(true), 15000);
    }
  },
  onThemeChange(e) {
    this.setData({ theme: e.detail.theme });
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
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const [d, u] = await Promise.all([request('/dashboard'), request('/me')]);
      this.setData({
        dashboard: d,
        user: u,
        recentTasks: (d.recent_tasks || []).slice(0, 2).map(buildTaskMetrics),
        error: ''
      });
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  openTask(e) {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/evidence/index?taskId=' + e.currentTarget.dataset.id });
  },
  tasks() {
    haptic();
    playSound('click');
    wx.switchTab({ url: '/pages/tasks/index' });
  },
  approvals() {
    haptic();
    playSound('click');
    wx.switchTab({ url: '/pages/approvals/index' });
  },
  alerts() {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/alerts/index' });
  },
  onShareAppMessage() {
    return {
      title: '银账智核 BankMind · 智能银企对账工作台',
      path: '/pages/home/index',
      imageUrl: '/assets/covers/bankmind-share-5x4.jpg'
    };
  }
});
