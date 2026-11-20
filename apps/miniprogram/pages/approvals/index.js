const {workspacePage}=require('../../utils/workspace');
const {request, requireAuth} = require('../../utils/api');
const {label} = require('../../utils/labels');

const pageSize = 25;
workspacePage({
  data: {
    matches: [], cases: [], matchTotal: 0, loading: true, error: '',
    scope: null, page: 1, hasNext: false
  },
  onShow() {
    if (!requireAuth()) return;
    const scope = wx.getStorageSync('bankmind-review-task') || null;
    this.setData({scope, page: 1, matches: [], cases: []});
    this.load();
    this.timer = setInterval(() => this.load(true), 15000);
  },
  onHide() { clearInterval(this.timer); },
  onUnload() { clearInterval(this.timer); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load(quiet = false) {
    const requestId = this.requestId = (this.requestId || 0) + 1;
    const {page, scope} = this.data;
    if (!quiet) this.setData({loading: true});
    try {
      const filter = scope ? '&task_id=' + encodeURIComponent(scope.id) : '';
      const response = await request('/approvals?page=' + page + '&page_size=' + pageSize + filter);
      if (requestId !== this.requestId) return;
      const total = response.match_total ?? response.matches.length;
      if (!response.matches.length && page > 1 && total <= (page - 1) * pageSize) {
        this.setData({page: Math.max(1, Math.ceil(total / pageSize))});
        await this.load();
        return;
      }
      this.setData({
        matches: response.matches.map(item => ({
          ...item, statusLabel: label(item.status), methodLabel: label(item.method),
          bankFirst: item.bank[0], bookFirst: item.book[0]
        })),
        cases: response.cases, matchTotal: total, hasNext: page * pageSize < total, error: ''
      });
    } catch (error) {
      if (requestId === this.requestId) this.setData({error: error.message});
    } finally {
      if (requestId === this.requestId) this.setData({loading: false});
    }
  },
  allTasks() {
    wx.removeStorageSync('bankmind-review-task');
    this.setData({scope: null, page: 1, matches: [], cases: []});
    return this.load();
  },
  pageStep(event) {
    const change = Number(event.currentTarget.dataset.change);
    if (this.data.loading || (change > 0 && !this.data.hasNext) || (change < 0 && this.data.page <= 1)) return;
    this.setData({page: this.data.page + change});
    return this.load();
  },
  match(event) { wx.navigateTo({url: '/pages/evidence/index?id=' + event.currentTarget.dataset.id}); },
  detail(event) { wx.navigateTo({url: '/pages/detail/index?id=' + event.currentTarget.dataset.id}); }
});
