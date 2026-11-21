const config = require('../config');

let cloudReady = false;
function ensureCloudInit() {
  if (cloudReady) return;
  if (config.cloudEnv && wx.cloud && typeof wx.cloud.init === 'function') {
    try {
      wx.cloud.init({ env: config.cloudEnv });
      cloudReady = true;
    } catch (_) {}
  }
}

function errorText(detail) {
  if (typeof detail === 'string') return detail;
  if (detail && detail.message) {
    return [
      detail.message,
      ...(detail.mismatches || []).map((item) =>
        [item.code, item.page && '第 ' + item.page + ' 页', item.row_no && '第 ' + item.row_no + ' 行']
          .filter(Boolean)
          .join(' · ')
      )
    ].join('；');
  }
  return '请求未完成，请检查参数或刷新';
}

function handleResponse(res, resolve, reject) {
  let body = res.data;
  if (typeof body === 'string') {
    try {
      body = JSON.parse(body);
    } catch (_) {}
  }
  if (res.statusCode >= 200 && res.statusCode < 300) {
    return resolve(body);
  }
  if (res.statusCode === 401) {
    wx.removeStorageSync('bankmind-token');
    wx.reLaunch({ url: '/pages/login/index' });
  }
  const detail = body && body.detail;
  reject(new Error(typeof detail === 'string' ? detail : errorText(detail)));
}

function request(path, method = 'GET', data) {
  const token = wx.getStorageSync('bankmind-token') || '';
  const useCloud = Boolean(
    config.cloudEnv &&
      config.cloudService &&
      typeof wx !== 'undefined' &&
      wx.cloud &&
      typeof wx.cloud.callContainer === 'function'
  );

  if (useCloud) {
    ensureCloudInit();
    return new Promise((resolve, reject) => {
      const options = {
        config: { env: config.cloudEnv },
        path: '/api' + path,
        method,
        timeout: 25000,
        header: {
          'X-WX-SERVICE': config.cloudService,
          Authorization: 'Bearer ' + token,
          'content-type': 'application/json'
        },
        success(res) {
          handleResponse(res, resolve, reject);
        },
        fail(err) {
          const msg = (err && err.errMsg) || '';
          reject(
            new Error(
              msg
                ? '云托管连接失败：' + msg
                : '无法连接云托管后端，请确认小程序已绑定环境并检查网络'
            )
          );
        }
      };
      if (data !== undefined) options.data = data;
      wx.cloud.callContainer(options);
    });
  }

  return new Promise((resolve, reject) =>
    wx.request({
      url: config.apiBase + path,
      method,
      data,
      timeout: 25000,
      header: {
        Authorization: 'Bearer ' + token,
        'content-type': 'application/json'
      },
      success(res) {
        handleResponse(res, resolve, reject);
      },
      fail() {
        reject(new Error('无法连接后端，请检查API地址、网络与合法域名配置'));
      }
    })
  );
}

function key() {
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}

function requireAuth() {
  if (!wx.getStorageSync('bankmind-token')) {
    wx.reLaunch({ url: '/pages/login/index' });
    return false;
  }
  return true;
}

function failure(error) {
  wx.showToast({ title: error.message || '操作失败', icon: 'none', duration: 3500 });
}

const receipts = new Map();
function approve(path, version, action, reason) {
  const signature = JSON.stringify([path, version, action, reason]);
  if (!receipts.has(signature)) receipts.set(signature, key());
  return request(path, 'POST', { version, action, reason, idempotency_key: receipts.get(signature) });
}

function upload(path, filePath, formData) {
  return new Promise((resolve, reject) =>
    wx.uploadFile({
      url: config.apiBase + path,
      filePath,
      name: 'file',
      formData,
      timeout: 60000,
      header: { Authorization: 'Bearer ' + (wx.getStorageSync('bankmind-token') || '') },
      success(res) {
        if (res.statusCode === 401) {
          wx.removeStorageSync('bankmind-token');
          wx.reLaunch({ url: '/pages/login/index' });
        }
        try {
          const body = JSON.parse(res.data);
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(body);
          else reject(new Error(errorText(body.detail)));
        } catch (error) {
          reject(new Error('服务器未返回可用的上传状态'));
        }
      },
      fail() {
        reject(new Error('上传中断。重新选择同一文件可续传。'));
      }
    })
  );
}

module.exports = { request, key, requireAuth, failure, approve, upload, errorText };
