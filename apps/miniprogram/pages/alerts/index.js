const { request, requireAuth } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme } = require('../../utils/theme');

Page({
  data: {
    theme: 'light',
    items: [],
    total: 0,
    page: 1,
    loading: true,
    error: '',
    status: '',
    taskId: ''
  },
  onLoad(q) {
    this.setData({ theme: getTheme(), taskId: q.taskId || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(this.data.items.length > 0);
    }
  },
  onPullDownRefresh() {
    this.setData({ page: 1 });
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const d = await request(
        '/cases?task_id=' + encodeURIComponent(this.data.taskId) + '&status=' + encodeURIComponent(this.data.status) + '&page=' + this.data.page
      );
      this.setData({
        items: d.items.map((c) => ({
          ...c,
          statusLabel: label(c.status),
          kindLabel: label(c.kind)
        })),
        total: d.total,
        error: ''
      });
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  filter(e) {
    this.setData({ status: e.currentTarget.dataset.status, page: 1 });
    this.load();
  },
  next() {
    this.setData({ page: this.data.page + 1 });
    this.load();
  },
  prev() {
    this.setData({ page: Math.max(1, this.data.page - 1) });
    this.load();
  },
  detail(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + encodeURIComponent(e.currentTarget.dataset.id) });
  }
});
