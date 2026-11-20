const config=require('../config');
function errorText(detail){
 if(typeof detail==='string')return detail;
 if(detail&&detail.message)return [detail.message,...(detail.mismatches||[]).map(item=>
  [item.code,item.page&&'第 '+item.page+' 页',item.row_no&&'第 '+item.row_no+' 行'].filter(Boolean).join(' · '))].join('；');
 return '请求未完成，请检查参数或刷新';
}
function request(path,method='GET',data){
 return new Promise((resolve,reject)=>wx.request({url:config.apiBase+path,method,data,timeout:25000,header:{Authorization:'Bearer '+(wx.getStorageSync('bankmind-token')||''),'content-type':'application/json'},success(res){if(res.statusCode>=200&&res.statusCode<300)return resolve(res.data);if(res.statusCode===401){wx.removeStorageSync('bankmind-token');wx.reLaunch({url:'/pages/login/index'});}const detail=res.data&&res.data.detail;reject(new Error(typeof detail==='string'?detail:'请求未完成，请检查参数或刷新'));},fail(){reject(new Error('无法连接后端，请检查API地址、网络与合法域名配置'));}}));
}
function key(){return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2);}
function requireAuth(){if(!wx.getStorageSync('bankmind-token')){wx.reLaunch({url:'/pages/login/index'});return false;}return true;}
function failure(error){wx.showToast({title:error.message||'操作失败',icon:'none',duration:3500});}
const receipts=new Map();
function approve(path,version,action,reason){const signature=JSON.stringify([path,version,action,reason]);if(!receipts.has(signature))receipts.set(signature,key());return request(path,'POST',{version,action,reason,idempotency_key:receipts.get(signature)});}
function upload(path,filePath,formData){
 return new Promise((resolve,reject)=>wx.uploadFile({
  url:config.apiBase+path,filePath,name:'file',formData,timeout:60000,
  header:{Authorization:'Bearer '+(wx.getStorageSync('bankmind-token')||'')},
  success(res){
   if(res.statusCode===401){wx.removeStorageSync('bankmind-token');wx.reLaunch({url:'/pages/login/index'});}
   try{
    const body=JSON.parse(res.data);
    if(res.statusCode>=200&&res.statusCode<300)resolve(body);
    else reject(new Error(errorText(body.detail)));
   }catch(error){reject(new Error('服务器未返回可用的上传状态'));}
  },
  fail(){reject(new Error('上传中断。重新选择同一文件可续传。'));}
 }));
}
module.exports={request,key,requireAuth,failure,approve,upload,errorText};
