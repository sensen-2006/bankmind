const { request, requireAuth } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

const SORT_OPTIONS = [
  { key: '', label: '最新建立' },
  { key: 'oldest', label: '最早建立' },
  { key: 'task', label: '按所属任务' },
  { key: 'difference', label: '按差额绝对值' }
];

Page({
  data: {
    theme: getTheme(),
    items: [],
    total: 0,
    page: 1,
    loading: true,
    error: '',
    status: '',
    taskId: '',
    search: '',
    sortIndex: 0,
    sortLabels: SORT_OPTIONS.map((o) => o.label),
    tasks: [],
    taskLabels: ['全部任务'],
    taskIndex: 0
  },
  onLoad(q) {
    this.setData({ taskId: q.taskId || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.loadTasks();
      this.load(this.data.items.length > 0);
    }
  },
  onPullDownRefresh() {
    this.setData({ page: 1 });
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async loadTasks() {
    if (this.data.tasks.length) return;
    try {
      const tasks = await request('/tasks');
      const taskLabels = ['全部任务', ...tasks.map((t) => `${t.name} (${t.start_date})`)];
      const idx = this.data.taskId ? tasks.findIndex((t) => t.id === this.data.taskId) + 1 : 0;
      this.setData({ tasks, taskLabels, taskIndex: Math.max(0, idx) });
    } catch (_) {}
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const sort = SORT_OPTIONS[this.data.sortIndex]?.key || '';
      const q = [
        'task_id=' + encodeURIComponent(this.data.taskId || ''),
        'status=' + encodeURIComponent(this.data.status || ''),
        'sort=' + encodeURIComponent(sort),
        'search=' + encodeURIComponent((this.data.search || '').trim()),
        'page=' + this.data.page
      ].join('&');
      const d = await request('/cases?' + q);
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
    haptic();
    playSound('click');
    this.setData({ status: e.currentTarget.dataset.status, page: 1 });
    this.load();
  },
  onSearchInput(e) {
    this.setData({ search: e.detail.value, page: 1 });
    clearTimeout(this._st);
    this._st = setTimeout(() => this.load(true), 280);
  },
  onSortPick(e) {
    haptic();
    playSound('click');
    this.setData({ sortIndex: Number(e.detail.value), page: 1 });
    this.load();
  },
  onTaskPick(e) {
    haptic();
    playSound('click');
    const idx = Number(e.detail.value);
    const taskId = idx > 0 && this.data.tasks[idx - 1] ? this.data.tasks[idx - 1].id : '';
    this.setData({ taskIndex: idx, taskId, page: 1 });
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
    haptic();
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.currentTarget.dataset.id });
  }
});
