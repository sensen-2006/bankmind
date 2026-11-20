const {workspacePage}=require('../../utils/workspace');
const {request, requireAuth, approve} = require('../../utils/api');
const {download} = require('../../utils/files');

const writer = ['Admin', 'Accountant', 'Reviewer'];
const reviewer = ['Admin', 'Reviewer'];
const statusNames = {DRAFT: '草稿', SIGNED: '已签署', STALE: '已失效'};
const sides = ['BANK', 'BOOK'];
const balanceNames = {PASS: '已核验', FAIL: '有差额'};

workspacePage({
  data: {
    taskId: '', task: null, user: null, reports: [], detail: null, balance: [], sources: [],
    sourceForm: null, sourceSelection: null, sourceReason: '',
    editing: false, signing: false, reason: '', loading: true, busy: false, error: '',
    canDraft: false, canBalance: false, canSign: false, canDownload: false, ownDraft: false,
    shareAvailable: typeof wx.shareFileMessage === 'function'
  },
  onLoad(query) { this.setData({taskId: query.taskId || ''}); },
  onShow() { if (requireAuth()) this.load(); },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()); },
  async load() {
    const id = this.data.taskId;
    if (!id) {
      this.setData({loading: false, error: '请先选择对账任务。'});
      return;
    }
    this.setData({loading: true});
    try {
      const path = '/tasks/' + encodeURIComponent(id);
      const [workspace, reports, user, sources] = await Promise.all([
        request(path + '/workbench'), request(path + '/reports'), request('/me'), request(path + '/source-controls')
      ]);
      const {task, adjustment} = workspace;
      const open = task.status !== 'COMPLETED';
      this.setData({
        task, user, reports: reports.map(item => ({
          ...item, statusLabel: statusNames[item.status] || item.status,
          dateLabel: item.created_at.slice(0, 16).replace('T', ' ')
        })),
        adjustment,
        controls: sides.map(id => ({
          id, label: id === 'BANK' ? '银行流水' : '企业账簿',
          status: balanceNames[task.summary.balance_control[id]?.status] || '待核验'
        })),
        canDraft: open && Boolean(task.current_run) && writer.includes(user.role),
        canBalance: open && ['Admin', 'Accountant'].includes(user.role),
        sources: sources.map(item => ({
          ...item, canReview: open && reviewer.includes(user.role) && item.creator_id !== user.id && item.status === 'SUBMITTED',
          statusLabel: item.status === 'VERIFIED' ? (item.scope_current ? '已核验' : '范围已变更')
            : item.status === 'REJECTED' ? '已退回' : '待独立核验'
        })),
        error: ''
      });
    } catch (error) {
      this.setData({error: error.message});
    } finally {
      this.setData({loading: false});
    }
  },
  async perform(action) {
    if (this.data.busy) return;
    this.setData({busy: true, error: ''});
    try { await action(); }
    catch (error) {
      this.setData({error: error.message});
      wx.showToast({title: error.message.slice(0, 35), icon: 'none'});
    }
    finally { this.setData({busy: false}); }
  },
  view(event) {
    const id = event.currentTarget.dataset.id;
    return this.perform(() => this.readDetail(id));
  },
  async readDetail(id) {
    const record = await request('/reports/' + encodeURIComponent(id));
    const {snapshot, ...report} = record;
    const {user, task} = this.data;
    const own = report.creator_id === user.id;
    this.setData({
      detail: {...report, statusLabel: statusNames[report.status] || report.status,
        disclaimer: snapshot.disclaimer, counts: snapshot.summary.sides},
      signing: false, reason: '',
      canSign: report.status === 'DRAFT' && task.status !== 'COMPLETED'
        && reviewer.includes(user.role) && !own && task.creator_id !== user.id,
      canDownload: reviewer.includes(user.role) || user.role === 'Auditor',
      ownDraft: user.role === 'Accountant' && own && report.status === 'DRAFT' && report.run_id === task.current_run
    });
    wx.pageScrollTo?.({selector: '#report-detail', duration: 200});
  },
  close() { this.setData({detail: null, signing: false, reason: ''}); },
  draft() {
    return this.perform(async () => {
      const result = await request('/tasks/' + this.data.taskId + '/reports?reuse_current=true', 'POST');
      await this.load();
      await this.readDetail(result.id);
    });
  },
  editBalances() {
    const balances = this.data.task.balances;
    this.setData({
      detail: null, editing: true, reason: '',
      balance: sides.map(id => ({
        id, label: id === 'BANK' ? '银行流水' : '企业账簿',
        opening: balances[id]?.opening || '', closing: balances[id]?.closing || ''
      }))
    });
  },
  balanceInput(event) {
    const {index, field} = event.currentTarget.dataset;
    this.setData({['balance[' + index + '].' + field]: event.detail.value});
  },
  reasonInput(event) { this.setData({reason: event.detail.value}); },
  cancelBalance() { this.setData({editing: false, reason: ''}); },
  saveBalance() {
    if (this.data.reason.trim().length < 4) {
      this.setData({error: '请填写至少 4 字的余额核对依据。'});
      return;
    }
    return this.perform(async () => {
      const values = Object.fromEntries(this.data.balance.map(row => [row.id, {
        opening: row.opening.trim() || null, closing: row.closing.trim() || null
      }]));
      await request('/tasks/' + this.data.taskId + '/balances', 'PATCH', {
        version: this.data.task.version, reason: this.data.reason.trim(), ...values
      });
      this.cancelBalance();
      await this.load();
    });
  },
  editSource() {
    this.setData({sourceSelection: null, sourceForm: {
      rows: sides.map(id => ({id, label: id === 'BANK' ? '银行流水' : '企业账簿', count: '', net: ''})),
      reference: '', statement: ''
    }});
  },
  editSourceValue(event) {
    const {field, index} = event.currentTarget.dataset;
    const location = index === undefined ? field : 'rows[' + index + '].' + field;
    this.setData({['sourceForm.' + location]: event.detail.value});
  },
  cancelSource() { this.setData({sourceForm: null, sourceSelection: null, sourceReason: ''}); },
  submitSource() {
    const form = this.data.sourceForm;
    if (!form || form.reference.trim().length < 4 || form.statement.trim().length < 12
      || form.rows.some(row => !/^\d+$/.test(row.count.trim()) || !row.net.trim())) {
      this.setData({error: '请填写两侧原始笔数、净额、来源编号与至少 12 字核验说明。'});
      return;
    }
    return this.perform(async () => {
      const values = Object.fromEntries(form.rows.map(row => [row.id, {
        rows: Number(row.count.trim()), net_amount: row.net.trim()
      }]));
      await request('/tasks/' + this.data.taskId + '/source-controls', 'POST', {
        source_reference: form.reference.trim(), statement: form.statement.trim(), ...values
      });
      this.cancelSource();
      await this.load();
    });
  },
  selectSource(event) {
    const source = this.data.sources.find(item => item.id === event.currentTarget.dataset.id);
    if (!source?.canReview) return;
    this.setData({sourceSelection: source, sourceReason: '', sourceForm: null});
  },
  sourceReason(event) { this.setData({sourceReason: event.detail.value}); },
  decideSource(event) {
    const {sourceSelection, sourceReason} = this.data;
    if (!sourceSelection?.canReview || sourceReason.trim().length < 4) {
      this.setData({error: '请填写至少 4 字的独立核验意见。'});
      return;
    }
    return this.perform(async () => {
      await approve('/source-controls/' + sourceSelection.id + '/decision', sourceSelection.version,
        event.currentTarget.dataset.action, sourceReason.trim());
      this.cancelSource();
      await this.load();
    });
  },
  startSign() { this.setData({signing: true, reason: ''}); },
  cancelSign() { this.setData({signing: false, reason: ''}); },
  sign() {
    const {detail, reason, canSign} = this.data;
    if (!detail || !canSign || reason.trim().length < 4) return;
    return this.perform(async () => {
      await approve('/reports/' + detail.id + '/sign', detail.version, 'APPROVE', reason.trim());
      await this.load();
      await this.readDetail(detail.id);
    });
  },
  download(event) {
    const format = event.currentTarget.dataset.format;
    return this.perform(async () => {
      const {detail, ownDraft} = this.data;
      const query = '?format=' + format + (ownDraft ? '&own_draft=true' : '');
      const filePath = await download('/reports/' + detail.id + '/export' + query, format);
      if (format === 'xlsx') {
        wx.openDocument({filePath, fileType: 'xlsx', showMenu: true,
          fail: () => this.setData({error: '当前设备未能打开 Excel。'})});
      } else {
        wx.shareFileMessage({filePath, fileName: 'BankMind-' + detail.id.slice(0, 8) + '.csv'});
      }
    });
  }
});
