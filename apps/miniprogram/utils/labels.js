const labels = {
  // 任务状态
  DRAFT: '草稿',
  READY: '待执行',
  REVIEW: '待复核',
  COMPLETED: '已完成',
  // 匹配状态
  PENDING: '待确认',
  APPROVED: '人工已确认',
  DETERMINISTIC: '程序确定',
  REVERSED: '已撤销',
  REVERSAL_PENDING: '撤销待审',
  // 异常状态
  NEW: '待调查',
  ASSIGNED: '已分派',
  INVESTIGATING: '调查中',
  WAITING_EVIDENCE: '待补材料',
  READY_REVIEW: '待主管复核',
  CLOSED: '已关闭',
  // 异常类型
  BANK_ONLY: '银行单边',
  BOOK_ONLY: '账簿单边',
  AMBIGUOUS: '多候选歧义',
  AMOUNT_DIFFERENCE: '金额差异',
  DATE_OUTSIDE: '日期超窗',
  DIRECTION_MISMATCH: '方向差异',
  FEE_REVIEW: '手续费待查',
  // 匹配方式
  L1_REFERENCE: '编号精确',
  L2_EXACT: '多字段精确',
  L3_FUZZY: '文本建议',
  L4_ONE_TO_MANY: '一对多',
  L4_MANY_TO_ONE: '多对一',
  // 角色
  Admin: '企业管理员',
  Accountant: '财务操作',
  Reviewer: '财务审核',
  Auditor: '内部审计',
  // 处理动作
  CREATE: '创建异常',
  NOTE: '保存备注',
  SUBMIT: '提交复核',
  APPROVE: '复核通过',
  REJECT: '已拒绝',
  RETURN: '退回调查',
  REQUEST_EVIDENCE: '要求补料',
  ASSIGN: '分派调查',
  INVESTIGATE: '生成建议',
  CLOSE: '关闭异常',
  REOPEN: '重新开启',
  VERIFY: '核验通过',
  UPLOAD: '上传材料'
};

function label(s) {
  return labels[s] || s;
}

module.exports = { label };
