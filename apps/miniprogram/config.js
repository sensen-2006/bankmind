// The simulator uses the local API. Real devices prefer WeChat Cloud Run.
const cloudEnv = 'bankmind-test-d3g7hhtet60584503';
const cloudService = 'bankmind-trial';
const mobileApiBase = '';
const debugApiBase = '';
const localApiBase = 'http://127.0.0.1:8026/api';

function platform() {
  if (typeof wx === 'undefined') return 'devtools';
  if (typeof wx.getDeviceInfo === 'function') return wx.getDeviceInfo().platform;
  return typeof wx.getSystemInfoSync === 'function' ? wx.getSystemInfoSync().platform : '';
}

const local = platform() === 'devtools';
const appVersion = typeof wx !== 'undefined' && typeof wx.getAccountInfoSync === 'function'
  ? wx.getAccountInfoSync().miniProgram?.envVersion : '';
const configuredMobileUrl = (appVersion === 'develop' && debugApiBase ? debugApiBase : mobileApiBase)
  .trim().replace(/\/+$/, '');
const httpsApi = /^https:\/\/(?:[a-z0-9-]+\.)+[a-z]{2,}\/api$/i;
module.exports = {
  apiBase: local ? localApiBase : httpsApi.test(configuredMobileUrl) ? configuredMobileUrl : '',
  cloud: !local && cloudEnv && !(appVersion === 'develop' && debugApiBase)
    ? {env: cloudEnv, service: cloudService} : null,
  local,
  authMode: 'enterprise-dev'
};
