function confirmCall(provider, probe = false) {
  const external = provider.id !== 'mock';
  const destination = provider.host || provider.label;
  const content = probe
    ? `向 ${destination} 发送一组合成测试数据，模型调用可能产生费用。`
    : external
      ? `将向 ${destination} 发送交易内部 ID、侧别、日期、币种、最小单位金额、质量状态和异常版本。原始行、账号、名称和摘要留在本机。调用可能消耗额度，建议由人工核验。`
      : '在电脑上使用本地规则生成建议。';
  return new Promise(resolve => wx.showModal({
    title: probe ? `测试 ${provider.label}` : `使用 ${provider.label} 调查`,
    content,
    confirmText: probe ? '开始测试' : '生成建议',
    success: result => resolve(result.confirm),
    fail: () => resolve(false)
  }));
}

const errors = {
  PROVIDER_NOT_CONFIGURED: '后端尚未配置',
  CODEX_LOGIN_REQUIRED: '请在电脑上登录 Codex CLI',
  CODEX_AUTH_EXPIRED: 'Codex 登录已过期，请在电脑上重新登录后再试',
  CODEX_EXEC_FAILED: 'Codex CLI 执行失败，请检查电脑上的 CLI',
  PROVIDER_BUSY: '模型调用繁忙，请稍后重试',
  PROVIDER_TIMEOUT: '模型响应超时',
  HTTP_401: 'API 凭据未通过',
  HTTP_402: 'API 账户余额或配额不足',
  HTTP_403: 'API 或模型访问被拒绝',
  HTTP_429: 'API 限流，请稍后重试',
  ValidationError: '模型返回未通过结构校验',
  JSONDecodeError: '模型返回的 JSON 无效',
  EMPTY_MODEL_OUTPUT: '模型未返回调查内容，请稍后重试',
  OUTPUT_TRUNCATED: '模型返回被截断，请稍后重试',
  INVALID_EVIDENCE_ID: '模型引用了无效证据',
  UNVERIFIED_NUMERIC_ASSERTION: '模型输出包含未经核验的数字'
};

function providerError(code) {
  return errors[code] || (code ? '模型调用失败（' + code + '）' : '模型调用失败');
}

module.exports = {confirmCall, providerError};
