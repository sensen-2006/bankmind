const { request, requireAuth, errorText } = require('../../utils/api');
const { submit, change, root, uploadChunks } = require('../../utils/jobs');
const { getTheme, syncPageTheme, formatBytes, haptic } = require('../../utils/theme');
const { playSound } = require('../../utils/sound');
const config = require('../../config');

const sides = ['银行流水', '企业账簿'];
const directionLabels = ['请选择', '正数流入', '正数流出', '借方流入', '贷方流入'];
const directions = ['signed_in', 'signed_out', 'debit_in', 'credit_in'];
const formats = ['ISO', 'DMY', 'MDY'];
const fields = {
  date: '交易日期',
  amount: '有符号金额',
  debit: '借方金额',
  credit: '贷方金额',
  account: '账号',
  currency: '币种',
  reference: '流水号',
  description: '摘要',
  counterparty: '交易对方',
  group_key: '批次号',
  balance: '逐笔余额'
};
const statusNames = {
  UPLOADING: '等待续传',
  QUEUED: '排队或执行中',
  SUCCEEDED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消'
};
const kindNames = {
  PREVIEW: '解析预览',
  PUBLISH: '校验并发布',
  MATCH: '运行匹配'
};
const totals = [
  ['count', '交易笔数'],
  ['receipts', '收入合计'],
  ['payments', '支出合计'],
  ['opening', '期初余额'],
  ['closing', '期末余额'],
  ['note', '控制数在原件中的位置']
];
const pageFields = [
  ['count', '笔数'],
  ['receipts', '收入'],
  ['payments', '支出'],
  ['references', '原件流水号，每行一个']
];

function displayRows(rows, columns) {
  return rows.map((row) => ({
    row_no: row.row_no,
    page: row.page,
    cells: columns.map((key) => ({ key, value: row.raw[key] }))
  }));
}

function parseIsoDate(raw) {
  if (!raw) return '';
  const m = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/.exec(String(raw).trim());
  if (!m) return '';
  const y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return '';
  return `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function monthEnd(isoDate) {
  const [y, m] = isoDate.split('-').map(Number);
  const lastDay = new Date(y, m, 0).getDate();
  return `${y}-${String(m).padStart(2, '0')}-${String(lastDay).padStart(2, '0')}`;
}

function scanBatchDates(batches) {
  let min = '', max = '';
  for (const b of batches) {
    const dateCol = b.suggested_mapping && b.suggested_mapping.date;
    if (!dateCol || !Array.isArray(b.preview)) continue;
    for (const row of b.preview) {
      const iso = parseIsoDate(row.raw && row.raw[dateCol]);
      if (!iso) continue;
      if (!min || iso < min) min = iso;
      if (!max || iso > max) max = iso;
    }
  }
  return min && max ? { min, max } : null;
}

function scanCsvDates(filePath) {
  return new Promise((resolve) => {
    if (typeof wx.getFileSystemManager !== 'function') return resolve(null);
    wx.getFileSystemManager().readFile({
      filePath,
      encoding: 'utf8',
      success(res) {
        const text = String(res.data || '');
        const matches = text.match(/\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b/g);
        if (!matches || !matches.length) return resolve(null);
        let min = '', max = '';
        for (let i = 0; i < matches.length; i++) {
          const iso = parseIsoDate(matches[i]);
          if (!iso) continue;
          if (!min || iso < min) min = iso;
          if (!max || iso > max) max = iso;
        }
        resolve(min && max ? { min, max } : null);
      },
      fail() {
        resolve(null);
      }
    });
  });
}

function computeMismatch(range, task) {
  if (!range || !task) return null;
  if (range.min >= task.start_date && range.max <= task.end_date) return null;
  const suggestedStart = range.min.slice(0, 7) + '-01';
  const suggestedEnd = monthEnd(range.max);
  return {
    min: range.min,
    max: range.max,
    suggestedStart,
    suggestedEnd
  };
}

function guessDirection(suggested) {
  if (suggested && suggested.amount) return 0; // signed_in
  if (suggested && suggested.debit && suggested.credit) return 2; // debit_in
  return -1;
}

function guessFormat(batch) {
  const dateCol = batch && batch.suggested_mapping && batch.suggested_mapping.date;
  const firstVal = dateCol && batch.preview && batch.preview[0] && batch.preview[0].raw && batch.preview[0].raw[dateCol];
  if (firstVal && /^\d{4}[-/]\d{1,2}[-/]\d{1,2}/.test(String(firstVal).trim())) return 0; // ISO
  return 0; // default ISO for standard CSV/Excel
}

function makeDraft(batch) {
  const visual = batch.quality.encoding === 'document-ai';
  const choices = ['未映射', ...batch.columns];
  const isDoc = visual || /\.pdf$/i.test(batch.filename);
  return {
    batch,
    visual,
    document: isDoc,
    choices,
    fields: Object.entries(fields).map(([id, title]) => ({
      id,
      title,
      index: Math.max(0, choices.indexOf(batch.suggested_mapping[id]))
    })),
    totals: totals.map(([id, title]) => ({ id, title, value: '' })),
    pages: visual
      ? Array.from({ length: batch.quality.pdf_pages }, (_, i) => ({
          number: i + 1,
          fields: pageFields.map(([id, title]) => ({ id, title, value: '' }))
        }))
      : [],
    direction: isDoc ? -1 : guessDirection(batch.suggested_mapping),
    format: isDoc ? -1 : guessFormat(batch),
    order: 0,
    confirmed: false,
    sourceVerified: false,
    noReference: false,
    rows: displayRows(batch.preview, batch.columns),
    extractionPage: 0,
    hasNext: false
  };
}

function mapping(draft, task) {
  const columns = Object.fromEntries(
    draft.fields.filter((field) => field.index).map((field) => [field.id, draft.choices[field.index]])
  );
  const direction = directions[draft.direction];
  if (
    !draft.confirmed ||
    !columns.date ||
    !direction ||
    !formats[draft.format] ||
    (direction.startsWith('signed_') ? !columns.amount : !columns.debit || !columns.credit)
  ) {
    throw new Error('请选择日期、金额方向与列，确认后再提交');
  }
  let controls = null;
  if (draft.document) {
    if (!draft.sourceVerified) throw new Error('请核对原件并确认控制数');
    const amounts = /^-?(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,3})?$/;
    const getValues = (items) => Object.fromEntries(items.map((item) => [item.id, item.value]));
    const checkedTotal = (input, allowZero) => {
      if (
        !/^\d{1,5}$/.test(input.count) ||
        Number(input.count) < (allowZero ? 0 : 1) ||
        !amounts.test(input.receipts) ||
        input.receipts.startsWith('-') ||
        !amounts.test(input.payments) ||
        input.payments.startsWith('-')
      ) {
        throw new Error('请填写原件笔数及非负收支金额');
      }
      return {
        transaction_count: Number(input.count),
        receipts: input.receipts,
        payments: input.payments
      };
    };
    const input = getValues(draft.totals);
    if (
      !amounts.test(input.opening) ||
      !amounts.test(input.closing) ||
      input.note.trim().length < 4 ||
      (columns.balance && !draft.order)
    ) {
      throw new Error('请填写期初、期末、控制数来源和所需的逐笔余额顺序');
    }
    if (
      draft.visual &&
      (Boolean(columns.reference) === draft.noReference ||
        (draft.batch.suggested_mapping.reference && !columns.reference))
    ) {
      throw new Error('请映射流水号或确认原件确无该列');
    }
    const pages = draft.pages.map((page) => {
      const source = getValues(page.fields);
      const row = { page: page.number, ...checkedTotal(source, true) };
      if (columns.reference) {
        row.references = row.transaction_count ? source.references.split(/\r?\n/) : [];
        if (row.references.length !== row.transaction_count) {
          throw new Error('第 ' + page.number + ' 页流水号数量与原件声明笔数不符');
        }
      }
      return row;
    });
    controls = {
      ...checkedTotal(input, false),
      opening_balance: input.opening,
      closing_balance: input.closing,
      source_note: input.note.trim(),
      pages,
      source_has_no_reference: draft.visual && draft.noReference,
      balance_order: columns.balance ? ['', 'oldest_first', 'newest_first'][draft.order] : null
    };
  }
  return {
    fields: columns,
    direction,
    date_format: formats[draft.format],
    confirmed: true,
    default_currency: task.currency,
    default_account: '__TASK_ACCOUNT_CONFIRMED__',
    source_verified: draft.sourceVerified,
    statement_controls: controls
  };
}

Page({
  data: {
    theme: 'light',
    taskId: '',
    task: null,
    jobs: [],
    batches: [],
    dismissedBatchIds: [],
    fileDateRange: null,
    dateMismatch: null,
    draft: null,
    file: null,
    side: 0,
    sheet: '',
    header: '1',
    busy: false,
    error: '',
    progress: '',
    enabled: false,
    canMatch: false,
    sides,
    directionLabels,
    formatLabels: ['请选择', ...formats],
    orders: ['请选择', '从旧到新', '从新到旧']
  },
  onLoad(query) {
    this.setData({ theme: getTheme(), taskId: query.taskId || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.visible = true;
      this.poll();
    }
  },
  onHide() {
    this.visible = false;
    clearTimeout(this.timer);
  },
  onUnload() {
    this.onHide();
  },
  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
  },
  async poll() {
    clearTimeout(this.timer);
    await this.load();
    if (this.visible) this.timer = setTimeout(() => this.poll(), 3500);
  },
  async load() {
    if (this.loading || !this.data.taskId) return;
    this.loading = true;
    try {
      const id = this.data.taskId;
      const result = await request('/tasks/' + encodeURIComponent(id) + '/workbench');
      const task = result.task;
      const jobs = result.background_jobs ? await request(root(id)) : [];
      const dismissed = new Set(this.data.dismissedBatchIds || []);
      const batches = result.batches.filter((batch) => batch.status === 'PREVIEWED' && !dismissed.has(batch.id));
      const quickPublishable = batches.filter(
        (b) =>
          b.quality.encoding !== 'document-ai' &&
          !/\.pdf$/i.test(b.filename) &&
          b.suggested_mapping &&
          b.suggested_mapping.date &&
          (b.suggested_mapping.amount || (b.suggested_mapping.debit && b.suggested_mapping.credit))
      );
      const batchRange = scanBatchDates(batches);
      const activeRange = this.data.fileDateRange || batchRange;
      const dateMismatch = !task.current_run ? computeMismatch(activeRange, task) : null;
      const draft = this.data.draft;
      this.setData({
        task,
        enabled: result.background_jobs,
        batches,
        dateMismatch,
        canQuickPublish: !task.current_run && quickPublishable.length > 0,
        canMatch: !task.current_run && task.summary.sides.BANK.valid > 0 && task.summary.sides.BOOK.valid > 0,
        draft: draft && !batches.some((batch) => batch.id === draft.batch.id) ? null : draft,
        jobs: jobs.map((job) => ({
          ...job,
          title: kindNames[job.kind],
          displayStatus: statusNames[job.status],
          progressText: job.total_bytes ? formatBytes(job.received_bytes, job.total_bytes) : '',
          canCancel: ['FAILED', 'QUEUED', 'UPLOADING'].includes(job.status),
          errorText: job.error ? errorText(job.error.detail) : ''
        }))
      });
    } catch (error) {
      this.setData({ error: error.message });
    } finally {
      this.loading = false;
    }
  },
  chooseFile() {
    wx.chooseMessageFile({
      count: 1,
      type: 'file',
      extension: ['csv', 'xls', 'xlsx', 'pdf', 'html', 'htm', 'md', 'txt', 'tsv', 'sta', 'mt940', 'xml', 'ofx', 'qfx'],
      success: async (result) => {
        const picked = result.tempFiles[0];
        if (!picked) return;
        let fileDateRange = null;
        if (/\.csv$/i.test(picked.name)) {
          fileDateRange = await scanCsvDates(picked.path);
        }
        const dateMismatch = computeMismatch(fileDateRange, this.data.task);
        if (dateMismatch) playSound('error');
        else playSound('click');
        this.setData({ file: picked, fileDateRange, dateMismatch, error: '', progress: '' });
      }
    });
  },
  clearMismatch() {
    haptic();
    playSound('click');
    const hidden = [...(this.data.dismissedBatchIds || []), ...this.data.batches.map((b) => b.id)];
    this.setData({
      file: null,
      fileDateRange: null,
      dateMismatch: null,
      draft: null,
      dismissedBatchIds: hidden,
      batches: [],
      canQuickPublish: false,
      error: '',
      progress: ''
    });
    wx.showToast({ title: '已清除，请重新选择文件', icon: 'none' });
  },
  createMatchingTask() {
    const { task, dateMismatch } = this.data;
    if (!task || !dateMismatch) return;
    return this.runAction(async () => {
      const startYm = dateMismatch.suggestedStart.slice(0, 7);
      const endYm = dateMismatch.suggestedEnd.slice(0, 7);
      const labelPeriod = startYm === endYm ? startYm : `${startYm}至${endYm}`;
      const created = await request('/tasks', 'POST', {
        name: `${labelPeriod} · 自动匹配期间对账`,
        account_id: task.account_id,
        currency: task.currency,
        start_date: dateMismatch.suggestedStart,
        end_date: dateMismatch.suggestedEnd
      });
      haptic();
      playSound('success');
      wx.showToast({ title: '已按文件日期建好新任务' });
      if (created && created.id) {
        wx.redirectTo({ url: '/pages/work/index?taskId=' + encodeURIComponent(created.id) });
      }
    });
  },
  selectSide(event) {
    if (this.data.busy) return;
    const side = Number(event.currentTarget.dataset.side);
    if (side === this.data.side) return;
    haptic();
    playSound('click');
    this.setData({ side, error: '' });
  },
  setting(event) {
    const name = event.currentTarget.dataset.name;
    this.setData({ [name]: name === 'side' ? Number(event.detail.value) : event.detail.value });
  },
  async runAction(action) {
    if (this.data.busy) return;
    this.setData({ busy: true, error: '' });
    try {
      await action();
    } catch (error) {
      playSound('error');
      this.setData({ error: error.message });
    } finally {
      await this.load();
      this.setData({ busy: false });
    }
  },
  enqueueFile() {
    return this.runAction(async () => {
      const { taskId, file, side, sheet, header } = this.data;
      if (!file) throw new Error('请先选择对账文件');
      if (!/^[0-9]+$/.test(header) || +header < 1 || +header > 20) {
        throw new Error('表头行号须在 1 至 20 之间');
      }
      const job = await uploadChunks(taskId, side === 0 ? 'BANK' : 'BOOK', file, sheet.trim(), +header, (j) => {
        this.setData({ progress: formatBytes(j.received_bytes, j.total_bytes) });
      });
      haptic();
      playSound('success');
      wx.showToast({ title: '上传成功，请点步骤2核对', icon: 'none' });
      this.setData({ file: null, fileDateRange: null, progress: statusNames[job.status] });
    });
  },
  selectBatch(event) {
    const batch = this.data.batches.find((item) => item.id === event.currentTarget.dataset.id);
    if (batch) {
      haptic();
      playSound('open');
      this.setData({ draft: makeDraft(batch), error: '' });
    }
  },
  edit(event) {
    const { name, index, page } = event.currentTarget.dataset;
    const value = event.detail.value;
    const values = { order: Number(value), direction: Number(value) - 1, format: Number(value) - 1 };
    const keys = {
      field: 'fields[' + index + '].index',
      total: 'totals[' + index + '].value',
      page: 'pages[' + page + '].fields[' + index + '].value'
    };
    const update = {
      ['draft.' + (keys[name] || name)]: name === 'field' ? Number(value) : name in values ? values[name] : value
    };
    if (!['sourceVerified', 'confirmed'].includes(name)) update['draft.sourceVerified'] = false;
    if (['direction', 'format', 'field'].includes(name)) update['draft.confirmed'] = false;
    this.setData(update);
  },
  extraction(event) {
    return this.runAction(async () => {
      const draft = this.data.draft;
      const page = draft.extractionPage + Number(event.currentTarget.dataset.step) || 1;
      const result = await request('/imports/' + draft.batch.id + '/extraction?page=' + page);
      this.setData({
        'draft.rows': displayRows(result.rows, draft.batch.columns),
        'draft.extractionPage': page,
        'draft.hasNext': page * 50 < result.total
      });
    });
  },
  original() {
    return this.runAction(async () => {
      const batch = this.data.draft.batch;
      const downloaded = await new Promise((resolve, reject) =>
        wx.downloadFile({
          url: config.apiBase + '/imports/' + batch.id + '/original',
          header: { Authorization: 'Bearer ' + wx.getStorageSync('bankmind-token') },
          success: (r) => (r.statusCode === 200 ? resolve(r.tempFilePath) : reject(new Error('原件不可用或权限已变化'))),
          fail: () => reject(new Error('原件下载失败'))
        })
      );
      const type = batch.filename.split('.').pop().toLowerCase();
      await new Promise((resolve, reject) => {
        const fail = () => reject(new Error('设备无法打开此文件，可在电脑端下载完整原件'));
        if (['png', 'jpg', 'jpeg'].includes(type)) wx.previewImage({ urls: [downloaded], success: resolve, fail });
        else if (type === 'csv')
          wx.getFileSystemManager().readFile({
            filePath: downloaded,
            encoding: 'utf8',
            position: 0,
            length: 1500,
            success: (r) => {
              wx.showModal({ title: 'CSV 原件开头', content: r.data, showCancel: false });
              resolve();
            },
            fail
          });
        else wx.openDocument({ filePath: downloaded, fileType: type, showMenu: false, success: resolve, fail });
      });
    });
  },
  publish() {
    return this.runAction(async () => {
      const { task, draft } = this.data;
      const checked = mapping(draft, task);
      const job = await submit(task.id, {
        kind: 'PUBLISH',
        version: task.version,
        selections: [{ batch_id: draft.batch.id, mapping: checked }]
      });
      haptic();
      playSound('success');
      wx.showToast({ title: '已提交校验并发布', icon: 'none' });
      this.setData({ progress: statusNames[job.status] });
    });
  },
  publishAll() {
    return this.runAction(async () => {
      const { task, batches } = this.data;
      const eligible = batches.filter(
        (b) =>
          b.quality.encoding !== 'document-ai' &&
          !/\.pdf$/i.test(b.filename) &&
          b.suggested_mapping &&
          b.suggested_mapping.date &&
          (b.suggested_mapping.amount || (b.suggested_mapping.debit && b.suggested_mapping.credit))
      );
      // Backend allows at most one preview batch per side (BANK / BOOK) per publish job.
      const bySide = new Map();
      for (const b of eligible) {
        if (!bySide.has(b.side)) bySide.set(b.side, b);
      }
      const targets = Array.from(bySide.values());
      if (!targets.length) throw new Error('没有可一键发布的表格批次，请手动核对');
      const sideText = targets.map((b) => (b.side === 'BANK' ? '银行流水' : '企业账簿')).join(' + ');
      const confirmed = await new Promise((resolve) =>
        wx.showModal({
          title: '一键校验并发布',
          content: `将按系统识别的字段映射发布最新 ${sideText}（共 ${targets.length} 份）。确认继续？`,
          success: (r) => resolve(r.confirm),
          fail: () => resolve(false)
        })
      );
      if (!confirmed) return;
      const selections = targets.map((b) => {
        const d = makeDraft(b);
        d.confirmed = true;
        return { batch_id: b.id, mapping: mapping(d, task) };
      });
      const job = await submit(task.id, {
        kind: 'PUBLISH',
        version: task.version,
        selections
      });
      haptic();
      playSound('success');
      wx.showToast({ title: '正在后台校验并发布', icon: 'none' });
      this.setData({ draft: null, progress: statusNames[job.status] });
    });
  },
  openMatches() {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/evidence/index?taskId=' + encodeURIComponent(this.data.taskId) });
  },
  openAlerts() {
    haptic();
    playSound('click');
    wx.navigateTo({ url: '/pages/alerts/index?taskId=' + encodeURIComponent(this.data.taskId) });
  },
  match() {
    return this.runAction(async () => {
      const confirmed = await new Promise((resolve) =>
        wx.showModal({
          title: '提交后台匹配',
          content: '请先核对两侧输入与隔离行。匹配完成后会冻结本任务输入。',
          success: (r) => resolve(r.confirm),
          fail: () => resolve(false)
        })
      );
      if (confirmed) {
        const { task } = this.data;
        const job = await submit(task.id, { kind: 'MATCH', version: task.version, require_both: true });
        haptic();
        playSound('success');
        wx.showToast({ title: '已提交后台自动匹配', icon: 'none' });
        this.setData({ progress: statusNames[job.status] });
      }
    });
  },
  jobAction(event) {
    const { id, action } = event.currentTarget.dataset;
    return this.runAction(() => change(this.data.taskId, id, action));
  }
});
