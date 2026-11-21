const { request, requireAuth, failure, approve } = require('../../utils/api');
const { label, shortId } = require('../../utils/labels');
const { formatMoney } = require('../../utils/money');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

Page({
  data: {
    theme: getTheme(),
    id: '',
    taskId: '',
    transactionId: '',
    match: null,
    transaction: null,
    rawFields: [],
    items: [],
    total: 0,
    page: 1,
    loading: true,
    error: '',
    reason: '',
    busy: false,
    canApprove: false
  },
  onLoad(q) {
    this.setData({ id: q.id || '', taskId: q.taskId || '', transactionId: q.transactionId || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) this.load(!!(this.data.match || this.data.transaction || this.data.items.length));
  },
  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      if (this.data.transactionId) {
        const t = await request('/transactions/' + this.data.transactionId);
        this.setData({
          transaction: {
            ...t,
            shortHash: shortId(t.source && t.source.sha256),
            displayAmount: formatMoney(t.amount, t.currency)
          },
          rawFields: Object.keys(t.raw || {}).map((key) => ({ key, value: t.raw[key] }))
        });
      } else if (this.data.id) {
        const [m, u] = await Promise.all([request('/matches/' + this.data.id), request('/me')]);
        this.setData({
          match: {
            ...m,
            shortId: shortId(m.id),
            bank: (m.bank || []).map((t) => ({ ...t, displayAmount: formatMoney(t.amount, t.currency) })),
            book: (m.book || []).map((t) => ({ ...t, displayAmount: formatMoney(t.amount, t.currency) })),
            statusLabel: label(m.status),
            methodLabel: label(m.method)
          },
          canApprove:
            ['Admin', 'Reviewer'].includes(u.role) &&
            m.creator_id !== u.id &&
            m.active &&
            ['PENDING', 'DETERMINISTIC', 'REVERSAL_PENDING'].includes(m.status)
        });
      } else if (this.data.taskId) {
        const d = await request('/tasks/' + this.data.taskId + '/matches?page=' + this.data.page);
        this.setData({
          items: (d.items || []).map((m) => {
            const first = (m.bank && m.bank[0]) || {};
            return {
              ...m,
              statusLabel: label(m.status),
              methodLabel: label(m.method),
              first,
              displayAmount: formatMoney(first.amount, first.currency || 'CNY')
            };
          }),
          total: d.total
        });
      }
      this.setData({ error: '' });
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  copyText(e) {
    const value = e.currentTarget.dataset.value;
    if (!value) return;
    haptic();
    wx.setClipboardData({
      data: String(value),
      success: () => wx.showToast({ title: '已复制到剪贴板', icon: 'none' })
    });
  },
  open(e) {
    haptic();
    wx.navigateTo({ url: '/pages/evidence/index?id=' + e.currentTarget.dataset.id });
  },
  source(e) {
    haptic();
    wx.navigateTo({ url: '/pages/evidence/index?transactionId=' + e.currentTarget.dataset.id });
  },
  input(e) {
    this.setData({ reason: e.detail.value });
  },
  next() {
    this.setData({ page: this.data.page + 1 });
    this.load();
  },
  prev() {
    this.setData({ page: Math.max(1, this.data.page - 1) });
    this.load();
  },
  decide(e) {
    if (this.data.reason.trim().length < 4) return failure(new Error('请填写至少4字审批原因'));
    const action = e.currentTarget.dataset.action;
    wx.showModal({
      title: '确认独立复核',
      content: '审批前重新验证权限、证据版本与占用；本人提交不可自审。',
      success: async (r) => {
        if (!r.confirm) return;
        this.setData({ busy: true });
        try {
          await approve('/matches/' + this.data.id + '/decision', this.data.match.version, action, this.data.reason);
          haptic();
          playSound('success');
          wx.showToast({ title: '已同步审批结果' });
          await this.load(true);
        } catch (e) {
          failure(e);
          await this.load(true);
        } finally {
          this.setData({ busy: false });
        }
      }
    });
  }
});
