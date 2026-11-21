const labels = {
  DRAFT: '草稿',
  READY: '待执行',
  REVIEW: '待复核',
  COMPLETED: '已完成',
  PENDING: '待确认',
  APPROVED: '人工已确认',
  DETERMINISTIC: '程序确定',
  REVERSED: '已撤销',
  REVERSAL_PENDING: '撤销待审',
  NEW: '待调查',
  ASSIGNED: '已分派',
  INVESTIGATING: '调查中',
  WAITING_EVIDENCE: '待补材料',
  READY_REVIEW: '待主管复核',
  CLOSED: '已关闭',
  BANK_ONLY: '银行单边',
  BOOK_ONLY: '账簿单边',
  AMBIGUOUS: '多候选歧义',
  AMOUNT_DIFFERENCE: '金额差异',
  DATE_OUTSIDE: '日期超窗',
  DIRECTION_MISMATCH: '方向差异',
  FEE_REVIEW: '手续费待查',
  L1_REFERENCE: '编号精确',
  L2_EXACT: '多字段精确',
  L3_FUZZY: '文本建议',
  L4_ONE_TO_MANY: '一对多',
  L4_MANY_TO_ONE: '多对一',
  Admin: '企业管理员',
  Accountant: '财务操作',
  Reviewer: '财务审核',
  Auditor: '内部审计',
  // Case & attachment event actions
  NOTE: '保存备注',
  SUBMIT: '提交独立复核',
  APPROVE: '审批通过',
  RETURN: '退回调查',
  REJECT: '已退回',
  REQUEST_EVIDENCE: '要求补充材料',
  CREATE: '创建异常',
  ASSIGN: '分派处理',
  INVESTIGATE: '生成调查建议',
  VERIFY: '材料核验通过',
  UPLOAD: '上传材料',
  WAIVE: '豁免材料',
  REOPEN: '重新打开',
  CLOSE: '关闭异常',
  CANCEL: '已取消',
  SUBMITTED: '待核验',
  VERIFIED: '已独立核验',
  REJECTED: '已退回',
  OTHER: '其他已核实差异',
  DATA_ERROR: '数据错误',
  BOOK_RECEIPT: '企收银未收',
  BOOK_PAYMENT: '企付银未付',
  BANK_RECEIPT: '银收企未收',
  BANK_PAYMENT: '银付企未付',
  NONE: '无'
};

function label(s) {
  if (!s) return '无';
  return labels[s] || s;
}

function shortId(value, len = 8) {
  if (!value) return '—';
  const s = String(value);
  return s.length > len ? s.slice(0, len) + '…' : s;
}

module.exports = { label, shortId };
