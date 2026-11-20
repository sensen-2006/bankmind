const { request, requireAuth } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { formatMoney } = require('../../utils/money');
const { getTheme, syncPageTheme } = require('../../utils/theme');

Page({
  data: {
    theme: 'light',
    matches: [],
    cases: [],
    matchTotal: 0,
    truncated: false,
    loading: true,
    error: ''
  },
  onLoad() {
    this.setData({ theme: getTheme() });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(this.data.matches.length > 0 || this.data.cases.length > 0);
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
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const d = await request('/approvals?page=1&page_size=100');
      const total = d.match_total || d.matches.length;
      this.setData({
        matches: d.matches.map((m) => {
          const bankFirst = m.bank[0] || {};
          return {
            ...m,
            statusLabel: label(m.status),
            methodLabel: label(m.method),
            bankFirst,
            bookFirst: m.book[0],
            displayAmount: formatMoney(bankFirst.amount, bankFirst.currency)
          };
        }),
        cases: d.cases,
        matchTotal: total,
        truncated: total > d.matches.length,
        error: ''
      });
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  match(e) {
    wx.navigateTo({ url: '/pages/evidence/index?id=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  detail(e) {
    wx.navigateTo({ url: '/pages/detail/index?id=' + encodeURIComponent(e.currentTarget.dataset.id) });
  }
});
