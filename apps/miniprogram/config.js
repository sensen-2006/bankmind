// Development account authentication is real enterprise API auth, NOT fake WeChat auth.
// A physical phone needs a reachable LAN or authorized HTTPS API URL.
module.exports = {
  apiBase: 'https://bankmind-trial-351039-7-1536999294.sh.run.tcloudbase.com/api',
  authMode: 'enterprise-dev'
};
