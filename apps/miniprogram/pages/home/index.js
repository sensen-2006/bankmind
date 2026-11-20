const {workspacePage, switchWorkspace} = require('../../utils/workspace');
const {request, requireAuth} = require('../../utils/api');
const {label} = require('../../utils/labels');

const steps = [
  {id: 'imports', name: '数据导入', page: 'work', symbol: '⇧'},
  {id: 'quality', name: '质量检查', page: 'quality', symbol: '◇'},
  {id: 'matches', name: '智能匹配', page: 'evidence', symbol: '⇄'},
  {id: 'cases', name: '异常调查', page: 'alerts', symbol: '◎'},
  {id: 'reviews', name: '人工复核', page: 'approvals', symbol: '✓'},
  {id: 'reports', name: '对账报告', page: 'reports', symbol: '▤'}
];

function overview(task, canWork) {
  const {BANK: bank, BOOK: book} = task.summary.sides;
  const balance = task.summary.balance_control || {};
  const balanceStatus = side => ({PASS: '已核验', FAIL: '有差额'})[balance[side]?.status] || '待核验';
  const next = !task.current_run
    ? (canWork ? {id: 'imports', name: '继续导入'} : {id: 'quality', name: '查看记录'})
    : task.summary.pending_matches ? {id: 'reviews', name: '继续复核'}
    : task.summary.open_cases ? {id: 'cases', name: '调查异常'}
    : {id: 'reports', name: '查看报告'};
  return {
    ...task, bank, book, next, statusLabel: label(task.status),
    percent: bank.valid ? Math.min(100, Math.round(100 * bank.matched / bank.valid)) : 0,
    bankBalance: balanceStatus('BANK'),
    bookBalance: balanceStatus('BOOK')
  };
}

workspacePage({
  data: {
    loading: true, error: '', dashboard: null, user: null, tasks: [],
    active: null, activeIndex: 0, canWork: false, steps
  },
  onShow() {
    if (!requireAuth()) return;
    this.timer = setInterval(() => this.load(true), 15000);
    return this.load();
  },
  onHide() { clearInterval(this.timer); },
  onUnload() { clearInterval(this.timer); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load(quiet = false) {
    if (this.loading) return;
    this.loading = true;
    if (!quiet) this.setData({loading: true});
    try {
      const [dashboard, user, tasks] = await Promise.all([
        request('/dashboard'), request('/me'), request('/tasks')
      ]);
      const canWork = ['Admin', 'Accountant'].includes(user.role);
      const activeId = wx.getStorageSync('bankmind-task-' + user.id);
      const activeIndex = Math.max(0, tasks.findIndex(task => task.id === activeId));
      this.setData({
        dashboard, user: {...user, roleLabel: label(user.role)}, tasks, canWork,
        activeIndex, error: '',
        active: tasks.length ? overview(tasks[activeIndex], canWork) : null
      });
    } catch (error) {
      this.setData({error: error.message});
    } finally {
      this.loading = false;
      this.setData({loading: false});
    }
  },
  pickTask(event) {
    const activeIndex = Number(event.detail.value);
    const task = this.data.tasks[activeIndex];
    if (!task) return;
    wx.setStorageSync('bankmind-task-' + this.data.user.id, task.id);
    this.setData({activeIndex, active: overview(task, this.data.canWork)});
  },
  toggleWorkspace() {
    switchWorkspace(this, this.data.workspaceMode === 'game' ? 'classic' : 'game');
  },
  go(event) {
    const step = steps.find(item => item.id === event.currentTarget.dataset.step);
    const task = this.data.active;
    if (!step || !task) return;
    if (step.id === 'reviews') {
      wx.setStorageSync('bankmind-review-task', {id: task.id, name: task.name});
      wx.switchTab({url: '/pages/approvals/index'});
      return;
    }
    wx.navigateTo({url: '/pages/' + step.page + '/index?taskId=' + encodeURIComponent(task.id)});
  },
  tasks() { wx.switchTab({url: '/pages/tasks/index'}); },
  allReviews() {
    wx.removeStorageSync('bankmind-review-task');
    wx.switchTab({url: '/pages/approvals/index'});
  },
  allCases() { wx.navigateTo({url: '/pages/alerts/index'}); },
  profile() { wx.switchTab({url: '/pages/profile/index'}); },
  create() { wx.navigateTo({url: '/pages/work/index'}); }
});
