const {workspacePage}=require('../../utils/workspace');
const {request, requireAuth} = require('../../utils/api');
const {formatMoney} = require('../../utils/money');

const filters = [
  {id: 'QUARANTINED', label: '隔离'}, {id: 'VALID', label: '有效'},
  {id: 'EXCLUDED', label: '排除'}, {id: '', label: '全部'}
];
const pageSize = 25;
workspacePage({
  data: {
    taskId: '', filters, quality: 'QUARANTINED', items: [], total: 0,
    page: 1, more: false, loading: true, error: ''
  },
  onLoad(query) { this.setData({taskId: query.taskId || ''}); },
  onShow() { if (requireAuth()) this.load(); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    if (!this.data.taskId) {
      this.setData({loading: false, error: '请从任务概览选择对账任务。'});
      return;
    }
    const run = this.run = (this.run || 0) + 1;
    const {taskId, page, quality} = this.data;
    this.setData({loading: true, error: ''});
    try {
      const result = await request('/tasks/' + encodeURIComponent(taskId) + '/transactions?quality='
        + quality + '&page=' + page + '&page_size=' + pageSize);
      if (run !== this.run) return;
      this.setData({
        items: result.items.map(item => ({
          ...item, qualityLabel: filters.find(filter => filter.id === item.quality)?.label || item.quality,
          displayAmount: formatMoney(item.amount, item.currency)
        })),
        total: result.total, more: page * pageSize < result.total
      });
    } catch (error) {
      if (run === this.run) this.setData({error: error.message});
    } finally {
      if (run === this.run) this.setData({loading: false});
    }
  },
  filter(event) {
    this.setData({quality: event.currentTarget.dataset.quality, page: 1, items: []});
    return this.load();
  },
  changePage(event) {
    const step = Number(event.currentTarget.dataset.step);
    if (this.data.loading || (step > 0 && !this.data.more) || (step < 0 && this.data.page <= 1)) return;
    this.setData({page: this.data.page + step});
    return this.load();
  },
  open(event) { wx.navigateTo({url: '/pages/evidence/index?transactionId=' + event.currentTarget.dataset.id}); }
});
