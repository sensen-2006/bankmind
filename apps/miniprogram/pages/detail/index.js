const { request, requireAuth, failure, approve } = require('../../utils/api');
const { label } = require('../../utils/labels');
const { formatMoney } = require('../../utils/money');
const { getTheme, syncPageTheme, shortId } = require('../../utils/theme');
const config = require('../../config');

Page({
  data: {
    theme: 'light',
    expandedCandidates: false,
    expandedHistory: false,
    id: '',
    detail: null,
    loading: true,
    error: '',
    reason: '',
    verifyDraftId: '',
    verifyDraftAction: '',
    verifyDraftReason: '',
    busy: false,
    canApprove: false,
    resolutions: ['其他已核实差异', '数据错误', '企收银未收', '企付银未付', '银收企未收', '银付企未付'],
    resolution: 0,
    attachments: [],
    requirements: [],
    aiConfig: null
  },
  onLoad(q) {
    this.setData({ theme: getTheme(), id: q.id || '' });
  },
  onShow() {
    syncPageTheme(this);
    if (requireAuth()) {
      this.load(Boolean(this.data.detail));
    }
  },
  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },
  async load(quiet = false) {
    if (!quiet) this.setData({ loading: true });
    try {
      const [d, u, attachments, requirements, aiConfig] = await Promise.all([
        request('/cases/' + encodeURIComponent(this.data.id)),
        request('/me'),
        request('/cases/' + encodeURIComponent(this.data.id) + '/attachments'),
        request('/cases/' + encodeURIComponent(this.data.id) + '/evidence-requirements'),
        request('/ai/config')
      ]);
      d.transactions = (d.transactions || []).map((t) => ({
        ...t,
        shortId: shortId(t.id),
        displayAmount: formatMoney(t.amount, t.currency)
      }));
      d.candidates = (d.candidates || []).map((t) => ({
        ...t,
        displayAmount: formatMoney(t.amount, t.currency)
      }));
      d.events = (d.events || []).map((ev) => ({
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
          kindLabel: label(d.kind)
        },
        user: u,
        attachments: attachments.map((a) => ({
          ...a,
          shortSha: shortId(a.sha256),
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
    wx.setClipboardData({
      data: String(value),
      success: () => wx.showToast({ title: '已复制', icon: 'none' })
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
  verifyDraftInput(e) {
    this.setData({ verifyDraftReason: e.detail.value });
  },
  cancelVerifyDraft() {
    this.setData({ verifyDraftId: '', verifyDraftAction: '', verifyDraftReason: '' });
  },
  resolution(e) {
    this.setData({ resolution: Number(e.detail.value) });
  },
  source(e) {
    wx.navigateTo({ url: '/pages/evidence/index?transactionId=' + encodeURIComponent(e.currentTarget.dataset.id) });
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
              '。仅发送脱敏后的内部编号、方向、日期、币种与金额，不发送账号、户名与摘要。可能产生模型调用费用，请确认已获授权。'
            : '当前后端为本地规则辅助模式，本次不发送第三方请求，不产生额外费用。',
          success: (r) => resolve(r.confirm),
          fail: () => resolve(false)
        })
      );
      if (!confirmed) return;
      await request('/cases/' + encodeURIComponent(this.data.id) + '/investigate', 'POST');
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
          url: config.apiBase + '/attachments/' + encodeURIComponent(id) + '/original',
          header: { Authorization: 'Bearer ' + wx.getStorageSync('bankmind-token') },
          success: (r) => {
            if (r.statusCode !== 200) return reject(new Error('下载失败或权限已变化，请刷新'));
            const a = this.data.attachments.find((x) => x.id === id);
            if (a && a.filename.toLowerCase().endsWith('.pdf')) {
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
                  const text = String(data.data);
                  wx.showModal({
                    title: '材料原件',
                    content: text.slice(0, 1800) + (text.length > 1800 ? '…完整内容请交接至电脑端查看' : ''),
                    showCancel: false
                  });
                  resolve();
                },
                fail: () => reject(new Error('无法读取文本，请交接至电脑端核验'))
              });
            }
          },
          fail: () => reject(new Error('下载网络失败，请检查网络连接后重试'))
        })
      );
    } catch (e) {
      failure(e);
    } finally {
      this.setData({ busy: false });
    }
  },
  async verifyAttachment(e) {
    const id = e.currentTarget.dataset.id;
    const action = e.currentTarget.dataset.action;
    const a = this.data.attachments.find((x) => x.id === id);
    if (!a || !a.canVerify) return;

    const effectiveReason =
      this.data.verifyDraftId === id && this.data.verifyDraftAction === action && this.data.verifyDraftReason.trim().length >= 4
        ? this.data.verifyDraftReason.trim()
        : this.data.reason.trim();

    if (effectiveReason.length < 4) {
      this.setData({
        verifyDraftId: id,
        verifyDraftAction: action,
        verifyDraftReason: this.data.verifyDraftReason || this.data.reason || ''
      });
      return;
    }

    const ok = await new Promise((resolve) =>
      wx.showModal({
        title: action === 'APPROVE' ? '确认材料已独立核验' : '退回材料',
        content: '请先下载并核对原件。此操作仅处理当前材料版本，不自动结案；版本冲突将自动刷新。',
        success: (r) => resolve(r.confirm)
      })
    );
    if (!ok) return;
    this.setData({ busy: true });
    try {
      await approve('/attachments/' + a.id + '/decision', a.version, action, effectiveReason);
      this.setData({ verifyDraftId: '', verifyDraftAction: '', verifyDraftReason: '' });
      wx.showToast({ title: '材料复核已同步' });
    } catch (e) {
      failure(e);
    } finally {
      await this.load(true);
      this.setData({ busy: false });
    }
  },
  handoff() {
    const url = config.apiBase.replace(/\/api\/?$/, '') + '/?case=' + encodeURIComponent(this.data.id);
    const note = this.data.reason.trim();
    const lines = [
      '【银账智核 · 电脑端交接】',
      '异常标题：' + (this.data.detail ? this.data.detail.title : ''),
      '异常编号：' + this.data.id + '（v' + (this.data.detail ? this.data.detail.version : 1) + '）',
      note ? '当前处理意见：' + note : '当前处理意见：暂无',
      '电脑端直达链接：' + url
    ];
    wx.setClipboardData({
      data: lines.join('\n'),
      success: () => wx.showToast({ title: '交接信息已复制' })
    });
  },
  async save(e) {
    if (this.data.reason.trim().length < 4) return failure(new Error('请填写至少4字处理意见'));
    const action = e.currentTarget.dataset.action;
    this.setData({ busy: true });
    try {
      await request('/cases/' + encodeURIComponent(this.data.id), 'PATCH', {
        version: this.data.detail.version,
        action,
        reason: this.data.reason,
        resolution:
          action === 'SUBMIT'
            ? ['OTHER', 'DATA_ERROR', 'BOOK_RECEIPT', 'BOOK_PAYMENT', 'BANK_RECEIPT', 'BANK_PAYMENT'][this.data.resolution]
            : ''
      });
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
      content: '将记录当前版本、处理原因与审核人，结果将同步至电脑端。',
      success: async (res) => {
        if (!res.confirm) return;
        this.setData({ busy: true });
        try {
          await approve('/cases/' + encodeURIComponent(this.data.id) + '/decision', this.data.detail.version, action, this.data.reason);
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
