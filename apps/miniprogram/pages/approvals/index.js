const { request, requireAuth } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { formatMoney } = require('../../utils/money');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

const PAGE_SIZE = 20;

Page({
  data: {
    theme: getTheme(),
    matches: [],
    filteredMatches: [],
    cases: [],
    filteredCases: [],
    query: '',
    matchTotal: 0,
    page: 1,
    totalPages: 1,
    jumpInput: '',
    loading: true,
    error: ''
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
  retry() {
    this.load(false);
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const page = this.data.page;
      const d = await request(`/approvals?page=${page}&page_size=${PAGE_SIZE}`);
      const total = d.match_total != null ? d.match_total : d.matches.length;
      const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      this.setData({
        matches: (d.matches || []).map((m) => {
          const bankFirst = (m.bank && m.bank[0]) || {};
          return {
            ...m,
            statusLabel: label(m.status),
            methodLabel: label(m.method),
            bankFirst,
            bookFirst: (m.book && m.book[0]) || {},
            displayAmount: formatMoney(bankFirst.amount, bankFirst.currency || 'CNY')
          };
        }),
        cases: d.cases || [],
        matchTotal: total,
        totalPages,
        error: ''
      });
      this.applySearch();
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  onSearch(e) {
    this.setData({ query: e.detail.value });
    this.applySearch();
  },
  applySearch() {
    const terms = String(this.data.query || '')
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    if (!terms.length) {
      this.setData({
        filteredMatches: this.data.matches,
        filteredCases: this.data.cases
      });
      return;
    }
    const filteredMatches = this.data.matches.filter((m) => {
      const hay = [
        m.bankFirst && m.bankFirst.counterparty,
        m.bankFirst && m.bankFirst.date,
        m.bankFirst && m.bankFirst.amount,
        m.displayAmount,
        m.methodLabel,
        m.statusLabel
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
    const filteredCases = this.data.cases.filter((c) => {
      const hay = [c.title, c.kind, c.status].filter(Boolean).join(' ').toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
    this.setData({ filteredMatches, filteredCases });
  },
  prevPage() {
    if (this.data.page <= 1) return;
    haptic();
    playSound('click');
    this.setData({ page: this.data.page - 1 });
    this.load(false);
    wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },
  nextPage() {
    if (this.data.page >= this.data.totalPages) return;
    haptic();
    playSound('click');
    this.setData({ page: this.data.page + 1, jumpInput: '' });
    this.load(false);
    wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },
  onJumpInput(e) {
    this.setData({ jumpInput: e.detail.value });
  },
  jumpToPage() {
    const raw = String(this.data.jumpInput || '').trim();
    const target = Number(raw);
    if (!Number.isInteger(target) || target < 1 || target > this.data.totalPages) {
      playSound('error');
      wx.showToast({ title: `请输入 1 至 ${this.data.totalPages} 的页码`, icon: 'none' });
      return;
    }
    if (target === this.data.page) {
      this.setData({ jumpInput: '' });
      return;
    }
    haptic();
    playSound('click');
    this.setData({ page: target, jumpInput: '' });
    this.load(false);
    wx.pageScrollTo({ scrollTop: 0, duration: 200 });
  },
  match(e) {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/evidence/index?id=' + e.currentTarget.dataset.id });
  },
  detail(e) {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/detail/index?id=' + e.currentTarget.dataset.id });
  }
});
