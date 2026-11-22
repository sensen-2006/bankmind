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

function canUseCloud() {
  return Boolean(
    config.cloudEnv &&
      config.cloudService &&
      typeof wx !== 'undefined' &&
      wx.cloud &&
      typeof wx.cloud.callContainer === 'function'
  );
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

function friendlyCloudError(err) {
  const msg = String((err && (err.errMsg || err.message)) || '');
  if (msg.includes('102002') || msg.includes('超时') || /timeout/i.test(msg)) {
    return '云托管服务正在冷启动或网络较慢，请稍候轻触重试';
  }
  if (msg.includes('abort') || msg.includes('app hide')) {
    return '页面切换导致请求中断，请轻触重试';
  }
  return '无法连接云托管后端，请检查网络后重试';
}

function handleResponse(path, res, resolve, reject) {
  let body = res.data;
  if (typeof body === 'string') {
    try {
      body = JSON.parse(body);
    } catch (_) {}
  }
  if (res.statusCode >= 200 && res.statusCode < 300) {
    return resolve(body);
  }
  const isAuthEndpoint = String(path || '').startsWith('/auth/');
  if (res.statusCode === 401) {
    wx.removeStorageSync('bankmind-token');
    if (!isAuthEndpoint) {
      wx.reLaunch({ url: '/pages/login/index' });
    }
  }
  const detail = body && body.detail;
  const fallback =
    res.statusCode === 401
      ? '账号密码无效或服务未授权（401），请检查登录凭据'
      : '请求未完成（HTTP ' + res.statusCode + '），请检查服务状态';
  reject(new Error(typeof detail === 'string' ? detail : detail ? errorText(detail) : fallback));
}

function callCloudOnce(path, method, data, token) {
  ensureCloudInit();
  return new Promise((resolve, reject) => {
    const options = {
      config: { env: config.cloudEnv },
      path: '/api' + path,
      method,
      timeout: 45000,
      header: {
        'X-WX-SERVICE': config.cloudService,
        Authorization: 'Bearer ' + token,
        'content-type': 'application/json'
      },
      success(res) {
        handleResponse(path, res, resolve, reject);
      },
      fail(err) {
        reject(err);
      }
    };
    if (data !== undefined) options.data = data;
    wx.cloud.callContainer(options);
  });
}

async function request(path, method = 'GET', data) {
  const token = wx.getStorageSync('bankmind-token') || '';
  if (canUseCloud()) {
    try {
      return await callCloudOnce(path, method, data, token);
    } catch (err) {
      if (err instanceof Error) throw err;
      const msg = String((err && err.errMsg) || '');
      const isTimeout = msg.includes('102002') || msg.includes('超时') || /timeout/i.test(msg);
      if (method === 'GET' && isTimeout) {
        try {
          return await callCloudOnce(path, method, data, token);
        } catch (retryErr) {
          if (retryErr instanceof Error) throw retryErr;
          throw new Error(friendlyCloudError(retryErr));
        }
      }
      throw new Error(friendlyCloudError(err));
    }
  }

  return new Promise((resolve, reject) =>
    wx.request({
      url: config.apiBase + path,
      method,
      data,
      timeout: 30000,
      header: {
        Authorization: 'Bearer ' + token,
        'content-type': 'application/json'
      },
      success(res) {
        handleResponse(path, res, resolve, reject);
      },
      fail() {
        reject(new Error('无法连接后端，请检查API地址、网络与合法域名配置'));
      }
    })
  );
}

function asciiBytes(str) {
  const arr = new Uint8Array(str.length);
  for (let i = 0; i < str.length; i++) arr[i] = str.charCodeAt(i) & 0xff;
  return arr;
}

function buildMultipart(chunkBuffer, boundary) {
  const head = asciiBytes(
    `--${boundary}\r\n` +
      'Content-Disposition: form-data; name="file"; filename="chunk.bin"\r\n' +
      'Content-Type: application/octet-stream\r\n\r\n'
  );
  const body = new Uint8Array(chunkBuffer);
  const tail = asciiBytes(`\r\n--${boundary}--\r\n`);
  const merged = new Uint8Array(head.length + body.length + tail.length);
  merged.set(head, 0);
  merged.set(body, head.length);
  merged.set(tail, head.length + body.length);
  return merged.buffer;
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

const MOBILE_UPLOAD_PART = 48 * 1024;

function readLocalFile(filePath) {
  return new Promise((resolve, reject) => {
    wx.getFileSystemManager().readFile({
      filePath,
      success(res) {
        resolve(res.data);
      },
      fail() {
        reject(new Error('读取本地分块失败，请重新选择文件'));
      }
    });
  });
}

async function uploadViaMobileFiles(path, filePath, formData) {
  const buffer = await readLocalFile(filePath);
  const size = buffer.byteLength;
  if (!size) throw new Error('文件分块为空，请重新选择文件');
  const form = {};
  if (formData && typeof formData === 'object') {
    for (const [k, v] of Object.entries(formData)) {
      if (v !== undefined && v !== null) form[k] = String(v);
    }
  }
  const transfer = await request('/mobile-files/uploads', 'POST', { path, size, form });
  try {
    for (let offset = 0; offset < size; offset += MOBILE_UPLOAD_PART) {
      const slice = buffer.slice(offset, Math.min(size, offset + MOBILE_UPLOAD_PART));
      const content = wx.arrayBufferToBase64(slice);
      await request('/mobile-files/uploads/' + transfer.id + '/part', 'POST', { offset, content });
    }
    return await request('/mobile-files/uploads/' + transfer.id + '/complete', 'POST');
  } catch (err) {
    request('/mobile-files/' + transfer.id + '/release', 'POST').catch(() => {});
    throw err;
  }
}

function upload(path, filePath, formData) {
  const token = wx.getStorageSync('bankmind-token') || '';
  if (
    canUseCloud() &&
    typeof wx.getFileSystemManager === 'function' &&
    typeof wx.arrayBufferToBase64 === 'function'
  ) {
    return uploadViaMobileFiles(path, filePath, formData);
  }

  return new Promise((resolve, reject) =>
    wx.uploadFile({
      url: config.apiBase + path,
      filePath,
      name: 'file',
      formData,
      timeout: 60000,
      header: { Authorization: 'Bearer ' + token },
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
