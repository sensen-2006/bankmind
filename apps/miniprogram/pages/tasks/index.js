const { request, requireAuth, failure } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');

function defaultDates() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  return {
    name: `${y}年${Number(m)}月 · 对账任务`,
    start_date: `${y}-${m}-01`,
    end_date: `${y}-${m}-${d}`
  };
}

Page({
  data: {
    theme: getTheme(),
    canWork: false,
    tasks: [],
    filteredTasks: [],
    accounts: [],
    accountLabels: [],
    showCreate: false,
    creating: false,
    form: {
      ...defaultDates(),
      accountIndex: 0
    },
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
      const canWork = ['Admin', 'Accountant'].includes(user.role);
      let accounts = this.data.accounts;
      if (canWork && !accounts.length) {
        try {
          accounts = await request('/accounts');
        } catch (_) {
          accounts = [];
        }
      }
      this.setData({
        canWork,
        accounts,
        accountLabels: accounts.map((a) => `${a.bank} · ${a.name} (${a.currency})`),
        tasks: tasks.map((t) => {
          const valid = t.summary.sides.BANK.valid || 0;
          const matched = t.summary.sides.BANK.matched || 0;
          const pending = t.summary.pending_matches || 0;
          const confirmedPct = valid ? Math.min(100, Math.round((matched / valid) * 100)) : 0;
          const pendingPct = valid ? Math.min(100 - confirmedPct, Math.round((pending / valid) * 100)) : 0;
          return {
            ...t,
            statusLabel: label(t.status),
            bank: t.summary.sides.BANK,
            book: t.summary.sides.BOOK,
            confirmedPct,
            pendingPct
          };
        }),
        error: ''
      });
      this.applyFilters();
    } catch (e) {
      this.setData({ error: e.message });
    } finally {
      this.setData({ loading: false });
    }
  },
  toggleCreate() {
    haptic();
    const next = !this.data.showCreate;
    playSound(next ? 'open' : 'click');
    this.setData({ showCreate: next });
  },
  onFormInput(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ ['form.' + field]: e.detail.value });
  },
  onAccountPick(e) {
    playSound('click');
    this.setData({ 'form.accountIndex': Number(e.detail.value) });
  },
  onDatePick(e) {
    playSound('click');
    const field = e.currentTarget.dataset.field;
    this.setData({ ['form.' + field]: e.detail.value });
  },
  async submitCreate() {
    if (this.data.creating) return;
    const { form, accounts } = this.data;
    const name = (form.name || '').trim();
    const account = accounts[form.accountIndex];
    if (name.length < 2) {
      playSound('error');
      return failure(new Error('请填写至少 2 字的任务名称'));
    }
    if (!account) {
      playSound('error');
      return failure(new Error('暂无可用的授权银行账户'));
    }
    if (form.start_date > form.end_date) {
      playSound('error');
      return failure(new Error('开始日期不能晚于结束日期'));
    }
    haptic();
    this.setData({ creating: true });
    try {
      const created = await request('/tasks', 'POST', {
        name,
        account_id: account.id,
        currency: account.currency,
        start_date: form.start_date,
        end_date: form.end_date
      });
      playSound('success');
      this.setData({
        showCreate: false,
        form: { ...defaultDates(), accountIndex: form.accountIndex }
      });
      wx.showToast({ title: '任务已创建' });
      await this.load(true);
      if (created && created.id) {
        wx.navigateTo({ url: '/pages/work/index?taskId=' + encodeURIComponent(created.id) });
      }
    } catch (e) {
      playSound('error');
      failure(e);
    } finally {
      this.setData({ creating: false });
    }
  },
  filter(e) {
    haptic();
    playSound('click');
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
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/work/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  matches(e) {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/evidence/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  },
  alerts(e) {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/alerts/index?taskId=' + encodeURIComponent(e.currentTarget.dataset.id) });
  }
});
