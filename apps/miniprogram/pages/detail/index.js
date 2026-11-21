const { request, requireAuth, failure, approve } = require('../../utils/api');
const { label, shortId } = require('../../utils/labels');
const { formatMoney } = require('../../utils/money');
const { getTheme, syncPageTheme, haptic } = require('../../utils/theme');
const config = require('../../config');

Page({
  data: {
    theme: getTheme(),
    expandedCandidates: false,
    expandedHistory: false,
    id: '',
    detail: null,
    loading: true,
    error: '',
    reason: '',
    busy: false,
    canApprove: false,
    resolutions: ['其他已核实差异', '数据错误', '企收银未收', '企付银未付', '银收企未收', '银付企未付'],
    resolution: 0,
    attachments: [],
    requirements: [],
    aiConfig: null
  },
  onLoad(q) {
    this.setData({ id: q.id || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) this.load(!!this.data.detail);
  },
  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const [d, u, attachments, requirements, aiConfig] = await Promise.all([
        request('/cases/' + this.data.id),
        request('/me'),
        request('/cases/' + this.data.id + '/attachments'),
        request('/cases/' + this.data.id + '/evidence-requirements'),
        request('/ai/config')
      ]);
      d.transactions = (d.transactions || []).map((t) => ({
        ...t,
        shortId: shortId(t.id),
        displayAmount: formatMoney(t.amount, t.currency)
      }));
      d.candidates = (d.candidates || []).map((t) => ({
        ...t,
        shortId: shortId(t.id),
        displayAmount: formatMoney(t.amount, t.currency)
      }));
      if (d.analysis && Array.isArray(d.analysis.hypotheses)) {
        d.analysis = {
          ...d.analysis,
          hypotheses: d.analysis.hypotheses.map((h) => ({
            ...h,
            evidenceList: (h.evidence_ids || []).map((eid) => ({ id: eid, shortId: shortId(eid) }))
          }))
        };
      }
      const events = (d.events || []).map((ev) => ({
        ...ev,
        actionLabel: label(ev.action),
        beforeLabel: label(ev.before),
        afterLabel: label(ev.after)
      }));
      this.setData({
        detail: {
          ...d,
          shortId: shortId(d.id),
          statusLabel: label(d.status),
          kindLabel: label(d.kind),
          events
        },
        user: u,
        attachments: (attachments || []).map((a) => ({
          ...a,
          shortHash: shortId(a.sha256),
          canVerify:
            ['Admin', 'Reviewer'].includes(u.role) &&
            a.uploader_id !== u.id &&
            a.current_evidence &&
            a.status === 'SUBMITTED' &&
            d.status !== 'CLOSED',
          statusLabel: a.status === 'VERIFIED' ? '已独立核验' : a.status === 'REJECTED' ? '已退回' : '待核验'
        })),
        requirements,
        aiConfig,
        canApprove: ['Admin', 'Reviewer'].includes(u.role) && d.submitter_id !== u.id && d.status === 'READY_REVIEW',
        error: ''
      });
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
  toggleCandidates() {
    this.setData({ expandedCandidates: !this.data.expandedCandidates });
  },
  toggleHistory() {
    this.setData({ expandedHistory: !this.data.expandedHistory });
  },
  input(e) {
    this.setData({ reason: e.detail.value });
  },
  resolution(e) {
    this.setData({ resolution: Number(e.detail.value) });
  },
  source(e) {
    haptic();
    wx.navigateTo({ url: '/pages/evidence/index?transactionId=' + e.currentTarget.dataset.id });
  },
  async investigate() {
    if (this.data.busy) return;
    this.setData({ busy: true });
    try {
      const c = await request('/ai/config');
      this.setData({ aiConfig: c });
      const live = c.mode === 'live' && c.external_enabled;
      const confirmed = await new Promise((resolve) =>
        wx.showModal({
          title: live ? '第三方 AI 调查授权' : '本地规则辅助调查',
          content: live
            ? '供应商域名：' +
              c.provider_host +
              '；模型：' +
              c.model +
              '。仅发送内部ID、侧别、日期、币种、最小单位金额、质量及异常版本，不发送账号、名称、摘要或原始行。可能产生费用；超时也可能已发送或计费，不自动重试。请确认本次调用已获额度授权。'
            : '当前后端为本地规则辅助模式，本次不发送第三方请求，不产生模型费用。',
          success: (r) => resolve(r.confirm),
          fail: () => resolve(false)
        })
      );
      if (!confirmed) return;
      await request('/cases/' + this.data.id + '/investigate', 'POST');
      await this.load(true);
    } catch (e) {
      failure(e);
    } finally {
      this.setData({ busy: false });
    }
  },
  async attachment(e) {
    const id = e.currentTarget.dataset.id;
    this.setData({ busy: true });
    try {
      await new Promise((resolve, reject) =>
        wx.downloadFile({
          url: config.apiBase + '/attachments/' + id + '/original',
          header: { Authorization: 'Bearer ' + wx.getStorageSync('bankmind-token') },
          success: (r) => {
            if (r.statusCode !== 200) return reject(new Error('下载失败或权限已变化，请刷新'));
            const a = this.data.attachments.find((x) => x.id === id);
            if (a.filename.toLowerCase().endsWith('.pdf')) {
              wx.openDocument({
                filePath: r.tempFilePath,
                fileType: 'pdf',
                showMenu: true,
                success: resolve,
                fail: () => reject(new Error('无法打开 PDF，请交接至电脑端下载核验'))
              });
            } else {
              wx.getFileSystemManager().readFile({
                filePath: r.tempFilePath,
                encoding: 'utf8',
                success: (data) => {
                  wx.showModal({
                    title: '材料原件（UTF-8）',
                    content:
                      String(data.data).slice(0, 1800) +
                      (String(data.data).length > 1800 ? '…完整内容请交接至电脑端查看' : ''),
                    showCancel: false
                  });
                  resolve();
                },
                fail: () => reject(new Error('无法读取文本原件，请交接至电脑端核验'))
              });
            }
          },
          fail: () => reject(new Error('下载网络失败，请检查合法域名并重试'))
        })
      );
    } catch (e) {
      failure(e);
    } finally {
      this.setData({ busy: false });
    }
  },
  async verifyAttachment(e) {
    const a = this.data.attachments.find((x) => x.id === e.currentTarget.dataset.id);
    if (!a || !a.canVerify) return;
    const action = e.currentTarget.dataset.action;
    let finalReason = this.data.reason.trim();

    if (finalReason.length < 4) {
      const promptRes = await new Promise((resolve) =>
        wx.showModal({
          title: action === 'APPROVE' ? '填写核验通过意见' : '填写退回材料原因',
          editable: true,
          placeholderText: '至少4字，说明核对原件的结论',
          content: finalReason,
          confirmText: action === 'APPROVE' ? '核验通过' : '退回材料',
          success: (r) => resolve(r),
          fail: () => resolve({ confirm: false })
        })
      );
      if (!promptRes.confirm) return;
      finalReason = (promptRes.content || '').trim();
      if (finalReason.length < 4) {
        return failure(new Error('请填写至少4字材料核验意见'));
      }
      this.setData({ reason: finalReason });
    } else {
      const ok = await new Promise((resolve) =>
        wx.showModal({
          title: action === 'APPROVE' ? '确认材料已独立核验' : '退回材料',
          content: '请先下载并核对原件。此操作仅处理当前材料版本，不自动结案；版本冲突将刷新。',
          success: (r) => resolve(r.confirm),
          fail: () => resolve(false)
        })
      );
      if (!ok) return;
    }

    this.setData({ busy: true });
    try {
      await approve('/attachments/' + a.id + '/decision', a.version, action, finalReason);
      haptic();
      wx.showToast({ title: '材料复核已同步' });
    } catch (e) {
      failure(e);
    } finally {
      await this.load(true);
      this.setData({ busy: false });
    }
  },
  handoff() {
    haptic();
    const d = this.data.detail;
    const url = config.apiBase.replace(/\/api\/?$/, '') + '/?case=' + encodeURIComponent(this.data.id);
    const lines = [
      '【银账智核 · 异常调查交接】',
      '异常标题：' + (d ? d.title : '—'),
      '异常编号：' + this.data.id + (d ? '（v' + d.version + '）' : ''),
      '当前状态：' + (d ? d.statusLabel : '—'),
      '处理意见：' + (this.data.reason.trim() || '暂无'),
      '电脑端链接：' + url
    ];
    wx.setClipboardData({
      data: lines.join('\n'),
      success: () => wx.showToast({ title: '交接摘要已复制' })
    });
  },
  async save(e) {
    if (this.data.reason.trim().length < 4) return failure(new Error('请填写至少4字处理意见'));
    const action = e.currentTarget.dataset.action;
    this.setData({ busy: true });
    try {
      await request('/cases/' + this.data.id, 'PATCH', {
        version: this.data.detail.version,
        action,
        reason: this.data.reason,
        resolution:
          action === 'SUBMIT'
            ? ['OTHER', 'DATA_ERROR', 'BOOK_RECEIPT', 'BOOK_PAYMENT', 'BANK_RECEIPT', 'BANK_PAYMENT'][this.data.resolution]
            : ''
      });
      haptic();
      wx.showToast({ title: '处理已保存' });
      await this.load(true);
    } catch (e) {
      failure(e);
      await this.load(true);
    } finally {
      this.setData({ busy: false });
    }
  },
  decide(e) {
    if (this.data.reason.trim().length < 4) return failure(new Error('请填写至少4字审批意见'));
    const action = e.currentTarget.dataset.action;
    wx.showModal({
      title: '确认审批操作',
      content: '将记录当前版本、处理原因与审核人，操作同步至电脑端。',
      success: async (res) => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          await approve('/cases/' + this.data.id + '/decision', this.data.detail.version, action, this.data.reason);
          haptic();
          wx.showToast({ title: '审批已同步' });
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
