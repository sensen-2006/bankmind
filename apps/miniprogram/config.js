// Development account authentication is real enterprise API auth, NOT fake WeChat auth.
// Uses WeChat Cloud Run (wx.cloud.callContainer) automatically when available so no custom domain is required.
module.exports = {
  apiBase: 'https://bankmind-trial-351039-7-1536999294.sh.run.tcloudbase.com/api',
  cloudEnv: 'bankmind-test-d3g7hhtet60584503',
  cloudService: 'bankmind-trial',
  authMode: 'enterprise-dev'
};
