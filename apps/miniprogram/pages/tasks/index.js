const { request, requireAuth } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme } = require('../../utils/theme');

Page({
  data: {
    theme: 'light',
    canWork: false,
    tasks: [],
    filteredTasks: [],
    status: '',
    query: '',
    loading: true,
    error: ''
  },
  onLoad() {
    this.setData({ theme: getTheme() });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(this.data.tasks.length > 0);
    }
  },
  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const [tasks, user] = await Promise.all([request('/tasks'), request('/me')]);
      this.setData({
        canWork: ['Admin', 'Accountant'].includes(user.role),
        tasks: tasks.map((t) => ({
          ...t,
          statusLabel: label(t.status),
          bank: t.summary.sides.BANK,
          book: t.summary.sides.BOOK,
          confirmedPct: t.summary.sides.BANK.valid
            ? Math.round((t.summary.sides.BANK.matched / t.summary.sides.BANK.valid) * 100)
            : 0
        })),
        error: ''
      });
      this.applyFilters();
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  filter(e) {
    this.setData({ status: e.currentTarget.dataset.status });
    this.applyFilters();
  },
  search(e) {
    this.setData({ query: e.detail.value });
    this.applyFilters();
  },
  applyFilters() {
    const q = this.data.query.trim().toLowerCase();
    this.setData({
      filteredTasks: this.data.tasks.filter(
        (t) =>
          (!this.data.status || t.status === this.data.status) &&
          (!q || (t.name + ' ' + t.account_name).toLowerCase().includes(q))
      )
    });
  },
  openWork(e) {
    wx.navigateTo({ url: '/pages/work/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  matches(e) {
    wx.navigateTo({ url: '/pages/evidence/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  alerts(e) {
    wx.navigateTo({ url: '/pages/alerts/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  }
});
