const config=require('../config');
const noService='体验服务尚未开通，请联系邀请人获取新版本。';
function errorText(detail){
 if(typeof detail==='string')return detail;
 if(detail&&detail.message)return [detail.message,...(detail.mismatches||[]).map(item=>
  [item.code,item.page&&'第 '+item.page+' 页',item.row_no&&'第 '+item.row_no+' 行'].filter(Boolean).join(' · '))].join('；');
 return '请求未完成，请检查参数或刷新';
}
function request(path,method='GET',data,timeout=25000){
 if(!config.cloud && !config.apiBase)return Promise.reject(new Error(noService));
 return new Promise((resolve,reject)=>{
  const header={Authorization:'Bearer '+(wx.getStorageSync('bankmind-token')||''),'content-type':'application/json'};
  const success=res=>{
   if(res.statusCode>=200&&res.statusCode<300)return resolve(res.data);
   if(res.statusCode===401&&!['/auth/login','/auth/wechat','/auth/logout'].includes(path)){
    clearSession();
    wx.reLaunch({url:'/pages/login/index'});
   }
   reject(new Error(errorText(res.data&&res.data.detail)));
  };
  const fail=error=>{
   const message=error?.errMsg||error?.message||'';
   if(/timeout/i.test(message))return reject(new Error('请求超时，请稍后重试。'));
   if(/url not in domain list|合法域名/i.test(message))return reject(new Error('体验版域名还未生效，请联系邀请人。'));
   reject(new Error(config.local
    ? '本机服务未连接，请运行「启动联调后端.cmd」。'
    : '暂时无法连接体验服务，请稍后重试或联系邀请人。'));
  };
  if(config.cloud){
   if(!wx.cloud?.callContainer)return reject(new Error('请更新微信后重新打开体验版。'));
   const {env,service}=config.cloud;
   wx.cloud.callContainer({
    config:{env},path:'/api'+path,method:method==='PATCH'?'POST':method,data,timeout,
    header:{...header,'X-WX-SERVICE':service}
   }).then(success,fail);
  }else{
   wx.request({url:config.apiBase+path,method,data,timeout,header,success,fail});
  }
 });
}
function key(){return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2);}
function clearSession(){
 wx.removeStorageSync('bankmind-token');
 wx.removeStorageSync('bankmind-user');
 wx.removeStorageSync('bankmind-review-task');
 const app=typeof getApp==='function' ? getApp() : null;
 if(app?.globalData)app.globalData.user=null;
}
function requireAuth(){if(!wx.getStorageSync('bankmind-token')){wx.reLaunch({url:'/pages/login/index'});return false;}return true;}
function failure(error){wx.showToast({title:error.message||'操作失败',icon:'none',duration:3500});}
const receipts=new Map();
function approve(path,version,action,reason){const signature=JSON.stringify([path,version,action,reason]);if(!receipts.has(signature))receipts.set(signature,key());return request(path,'POST',{version,action,reason,idempotency_key:receipts.get(signature)});}
module.exports={request,key,requireAuth,failure,approve,errorText,noService,clearSession};
