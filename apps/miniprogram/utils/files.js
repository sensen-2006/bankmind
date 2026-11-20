const config = require('../config');
const {request, key, clearSession, errorText, noService} = require('./api');

const uploadPartBytes = 48 * 1024;
const downloadPrefix = 'bankmind-cloud-';
const transfers = '/mobile-files';

function fsCall(fs, method, params) {
  return new Promise((resolve, reject) => fs[method]({
    ...params, success: resolve, fail: () => reject(new Error('无法读取或保存本地文件，请重试。'))
  }));
}

function release(id) {
  return request(transfers + '/' + id + '/release', 'POST', undefined, 5000).catch(() => {});
}

function removeOldDownloads() {
  if (!config.cloud || !wx.env?.USER_DATA_PATH) return;
  const fs = wx.getFileSystemManager();
  const dirPath = wx.env.USER_DATA_PATH;
  fs.readdir({
    dirPath, success: ({files}) => files.filter(file => file.startsWith(downloadPrefix))
      .forEach(file => fs.unlink({filePath: dirPath + '/' + file, fail() {}})),
    fail() {}
  });
}

async function uploadThroughCloud(path, filePath, form) {
  const fs = wx.getFileSystemManager();
  const {data} = await fsCall(fs, 'readFile', {filePath});
  const {id} = await request(transfers + '/uploads', 'POST', {path, form, size: data.byteLength});
  const transfer = transfers + '/uploads/' + id;
  try {
    for (let offset = 0; offset < data.byteLength; offset += uploadPartBytes) {
      const part = data.slice(offset, offset + uploadPartBytes);
      await request(transfer + '/part', 'POST', {offset, content: wx.arrayBufferToBase64(part)});
    }
    return await request(transfer + '/complete', 'POST', undefined, 60000);
  } catch (error) {
    await release(id);
    throw error;
  }
}

function upload(path, filePath, formData) {
  if (config.cloud) return uploadThroughCloud(path, filePath, formData);
  if (!config.apiBase) return Promise.reject(new Error(noService));
  return new Promise((resolve, reject) => wx.uploadFile({
    url: config.apiBase + path, filePath, name: 'file', formData, timeout: 60000,
    header: {Authorization: 'Bearer ' + (wx.getStorageSync('bankmind-token') || '')},
    success(res) {
      if (res.statusCode === 401) {
        clearSession();
        wx.reLaunch({url:'/pages/login/index'});
      }
      try {
        const body = JSON.parse(res.data);
        if (res.statusCode >= 200 && res.statusCode < 300) resolve(body);
        else reject(new Error(errorText(body.detail)));
      } catch (error) {
        reject(new Error('服务器未返回可用的上传状态'));
      }
    },
    fail: () => reject(new Error('上传中断。重新选择同一文件可续传。'))
  }));
}

async function downloadThroughCloud(path, extension) {
  const {id, size} = await request(transfers + '/downloads', 'POST', {path}, 60000);
  const fs = wx.getFileSystemManager();
  const filePath = wx.env.USER_DATA_PATH + '/' + downloadPrefix + key() + '.' + extension;
  try {
    await fsCall(fs, 'writeFile', {filePath, data: ''});
    let offset = 0;
    while (offset < size) {
      const part = await request(transfers + '/downloads/' + id + '?offset=' + offset);
      const data = wx.base64ToArrayBuffer(part.content);
      if (part.next <= offset || part.next > size || part.next - offset !== data.byteLength) {
        throw new Error('文件分块不完整，请重新下载。');
      }
      await fsCall(fs, 'appendFile', {filePath, data});
      offset = part.next;
    }
    return filePath;
  } catch (error) {
    fs.unlink({filePath, fail() {}});
    throw error;
  } finally {
    await release(id);
  }
}

function download(path, extension) {
  if (config.cloud) return downloadThroughCloud(path, extension);
  if (!config.apiBase) return Promise.reject(new Error(noService));
  return new Promise((resolve, reject) => wx.downloadFile({
    url: config.apiBase + path,
    header: {Authorization: 'Bearer ' + (wx.getStorageSync('bankmind-token') || '')},
    success: result => result.statusCode === 200
      ? resolve(result.tempFilePath) : reject(new Error('文件不可用或权限已变化，请刷新。')),
    fail: () => reject(new Error('下载中断，请重试。'))
  }));
}

module.exports = {upload, download, fsCall, removeOldDownloads};
