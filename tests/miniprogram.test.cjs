// Native page logic harness against an isolated real FastAPI server.
// This does not claim WeChat rendering/device certification.
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');const path=require('node:path');
const root=path.resolve(__dirname,'../apps/miniprogram');
const store=new Map();let pageDefinition;const navigation=[];
const apiBase=process.env.BANKMIND_TEST_API||'http://127.0.0.1:8001/api';
const config=require(path.join(root,'config'));config.apiBase=apiBase;
global.Page=o=>pageDefinition=o;
global.getApp=()=>({globalData:{config,user:store.get('bankmind-user')}});
global.wx={getStorageSync:k=>store.get(k),setStorageSync:(k,v)=>store.set(k,v),removeStorageSync:k=>store.delete(k),
 switchTab:o=>navigation.push(o.url),reLaunch:o=>navigation.push(o.url),navigateTo:o=>navigation.push(o.url),
 setClipboardData:o=>{assert.ok(o.data && (o.data.includes('case=') || o.data.includes('异常编号')));o.success();},showToast:()=>{},stopPullDownRefresh:()=>{},showModal:o=>o.success({confirm:true}),
 request(o){fetch(o.url,{method:o.method,headers:o.header,body:o.method==='GET'?undefined:JSON.stringify(o.data)}).then(async r=>o.success({statusCode:r.status,data:await r.json()})).catch(o.fail);}};
function page(name){
 const file=path.join(root,'pages',name,'index.js');delete require.cache[require.resolve(file)];require(file);
 const o=pageDefinition;
 const instance={...o,data:structuredClone(o.data),setData(values){
  for(const [key,value] of Object.entries(values)){
   const parts=key.replace(/\[(\d+)\]/g,'.$1').split('.');
   const field=parts.pop();
   parts.reduce((obj,k)=>obj[k],this.data)[field]=value;
  }
 }};
 for(const key in instance)if(typeof instance[key]==='function')instance[key]=instance[key].bind(instance);
 return instance;
}
const event=(dataset,value)=>({currentTarget:{dataset},detail:{value}});
async function waitForJob(work,kind,completed){
 for(let i=0;i<70;i++){
  await work.load();
  const jobs=work.data.jobs.filter(j=>j.kind===kind&&j.status==='SUCCEEDED');
  if(jobs.length===completed)return;
  const failed=work.data.jobs.find(j=>j.status==='FAILED');
  assert.ok(!failed,JSON.stringify(failed));
  await new Promise(resolve=>setTimeout(resolve,200));
 }
 assert.fail('Backend did not finish '+kind);
}
async function testWork(api){
 const account=(await api.request('/accounts'))[0];
 const task=await api.request('/tasks','POST',{name:'移动客户端恢复合成验收',account_id:account.id,currency:account.currency,
  start_date:'2026-10-01',end_date:'2026-10-31'});
 const work=page('work');work.onLoad({taskId:task.id});await work.load();
 assert.equal(work.data.enabled,true);
 const big=Buffer.from('date,amount,reference,description\n'+Array.from({length:3600},(_,i)=>'2026-10-04,'+(i+1)+',R'+i+','+'A'.repeat(300)).join('\n'));
 for(const content of [Buffer.alloc(0),Buffer.alloc(56,1),big]){
  const bytes=content.buffer.slice(content.byteOffset,content.byteOffset+content.byteLength);
  assert.equal(require(path.join(root,'utils/sha256'))(bytes),crypto.createHash('sha256').update(content).digest('hex'));
 }
 const pdf=fs.readFileSync(process.env.BANKMIND_TEST_MOBILE_STATEMENT);
 const fileMap=new Map([['BIG',big],['SMALL',pdf]]);
 wx.env={USER_DATA_PATH:'LOCAL'};
 wx.getFileSystemManager=()=>({
  readFile:o=>{
   const file=fileMap.get(o.filePath);
   if(!file)return o.fail();
   o.success({data:file.buffer.slice(file.byteOffset,file.byteOffset+file.byteLength)});
  },
  writeFile:o=>{fileMap.set(o.filePath,Buffer.from(o.data));o.success();},
  unlink:o=>fileMap.delete(o.filePath)
 });
 const sent=[];
 let interrupt=true;
 wx.uploadFile=o=>{
  const offset=Number(o.url.split('/').at(-1));
  if(offset===1048576&&interrupt){interrupt=false;return o.fail();}
  sent.push(offset);
  const data=new FormData();
  data.set('file',new Blob([fileMap.get(o.filePath)]),'chunk');
  fetch(o.url,{method:'POST',headers:o.header,body:data}).then(async r=>o.success({statusCode:r.status,data:await r.text()})).catch(o.fail);
 };
 const runFile=async (source,side)=>{
  work.data.side=side;
  work.data.file={path:source,name:source+(source==='BIG'?'.csv':'.pdf'),size:fileMap.get(source).length};
  await work.enqueueFile();
 };
 await runFile('BIG',0);
 assert.match(work.data.error,/上传中断/);
 assert.equal(work.data.jobs[0].received_bytes,1048576);
 const pendingId=work.data.jobs[0].id;
 await runFile('BIG',0);
 await waitForJob(work,'PREVIEW',1);
 assert.equal(work.data.jobs[0].id,pendingId);
 assert.deepEqual(sent,[0,1048576]);
 await runFile('SMALL',1);
 await waitForJob(work,'PREVIEW',2);
 for(const [index,batch] of work.data.batches.entries()){
  work.selectBatch(event({id:batch.id}));
  work.edit(event({name:'direction'},'1'));work.edit(event({name:'format'},'1'));
  if(batch.filename.endsWith('.pdf')){
   let opened=false;
   wx.downloadFile=o=>fetch(o.url,{headers:o.header}).then(async result=>{
    assert.equal(result.status,200);
    assert.deepEqual(Buffer.from(await result.arrayBuffer()),pdf);
    o.success({statusCode:200,tempFilePath:'OPEN_SOURCE'});
   }).catch(o.fail);
   wx.openDocument=o=>{assert.equal(o.filePath,'OPEN_SOURCE');assert.equal(o.fileType,'pdf');opened=true;o.success();};
   await work.original();
   assert.equal(opened,true);
   ['1','1.00','0','0','1.00','合成原件第一页'].forEach((value,index)=>work.edit(event({name:'total',index},value)));
   work.edit(event({name:'sourceVerified'},true));
  }
  work.edit(event({name:'confirmed'},true));
  await work.publish();
  await waitForJob(work,'PUBLISH',index+1);
 }
 assert.equal(work.data.canMatch,true);
 await work.match();
 await waitForJob(work,'MATCH',1);
 assert.equal(work.data.task.summary.sides.BANK.raw,3600);
 assert.equal(work.data.task.summary.sides.BOOK.raw,1);
}
async function main(){
 const app=JSON.parse(fs.readFileSync(path.join(root,'app.json'),'utf8'));
 assert.equal(app.pages.length,9);
 for(const p of app.pages)for(const ext of ['.js','.json','.wxml','.wxss'])assert.ok(fs.existsSync(path.join(root,p+ext)),p+ext);
 const login=page('login');login.data.username='reviewer';await login.login();assert.ok(store.get('bankmind-token'));assert.equal(store.get('bankmind-user').role,'Reviewer');
 const home=page('home');await home.load();assert.ok(home.data.dashboard.task_count>0);
 const tasks=page('tasks');await tasks.load();assert.ok(tasks.data.tasks.some(t=>t.bank.raw===1000&&t.book.raw===1000));
 assert.ok(home.data.recentTasks.length<=2);for(const t of home.data.recentTasks){assert.equal(t.confirmedPct,t.summary.sides.BANK.valid?Math.round(t.summary.sides.BANK.matched/t.summary.sides.BANK.valid*100):0);}
 tasks.filter({currentTarget:{dataset:{status:'REVIEW'}}});assert.ok(tasks.data.filteredTasks.length>0);assert.ok(tasks.data.filteredTasks.every(t=>t.status==='REVIEW'));
 tasks.search({detail:{value:'definitely-no-such-task'}});assert.equal(tasks.data.filteredTasks.length,0);
 tasks.search({detail:{value:''}});tasks.filter({currentTarget:{dataset:{status:''}}});assert.equal(tasks.data.filteredTasks.length,tasks.data.tasks.length);
 home.openTask({currentTarget:{dataset:{id:tasks.data.tasks[0].id}}});assert.ok(navigation.at(-1).includes(encodeURIComponent(tasks.data.tasks[0].id)));

 const approvals=page('approvals');await approvals.load();assert.ok(approvals.data.matches.length>0);
 const match=approvals.data.matches.find(m=>m.status==='PENDING');assert.ok(match);
 const evidence=page('evidence');evidence.onLoad({id:match.id});await evidence.load();assert.ok(evidence.data.canApprove);assert.equal(evidence.data.match.id,match.id);
 const api=require(path.join(root,'utils/api'));
 const result=await api.approve('/matches/'+match.id+'/decision',match.version,'APPROVE','小程序业务验收，已核对合成交易证据');assert.equal(result.status,'APPROVED');
 const retry=await api.approve('/matches/'+match.id+'/decision',match.version,'APPROVE','小程序业务验收，已核对合成交易证据');assert.deepEqual(retry,result);
 await evidence.load();assert.equal(evidence.data.match.status,'APPROVED');assert.equal(evidence.data.canApprove,false);
 const source=page('evidence');source.onLoad({transactionId:match.bank_ids[0]});await source.load();assert.ok(source.data.rawFields.length>0);assert.ok(source.data.rawFields.filter(f=>f.key.includes('账号')).every(f=>f.value.includes('••••')));
 const alerts=page('alerts');alerts.onLoad({});await alerts.load();assert.ok(alerts.data.items.length>0);
 const detail=page('detail');detail.onLoad({id:alerts.data.items.find(x=>x.status!=='CLOSED').id});await detail.load();assert.ok(detail.data.detail.transactions.length>0);
 // Mobile material list, authenticated read and independent review share the exact same API.
 const reviewerToken=store.get('bankmind-token');const c=detail.data.detail;const uploadLogin=await fetch(apiBase+'/auth/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:'accountant',password:'BankMind@2026'})}).then(r=>r.json());
 const form=new FormData();form.append('note','小程序合成材料核验测试，不代表真实凭证');form.append('purpose','SUPPORT');form.append('file',new Blob(['Synthetic mobile evidence'],{type:'text/plain'}),'mobile.txt');
 const attachment=await fetch(apiBase+'/cases/'+c.id+'/attachments',{method:'POST',headers:{Authorization:'Bearer '+uploadLogin.token},body:form}).then(r=>r.json());assert.ok(attachment.id,JSON.stringify(attachment));
 await detail.load();assert.equal(detail.data.aiConfig.mode,'mock');const a=detail.data.attachments.find(x=>x.id===attachment.id);assert.ok(a.canVerify);
 let downloaded=false;wx.downloadFile=o=>{assert.equal(o.header.Authorization,'Bearer '+reviewerToken);fetch(o.url,{headers:o.header}).then(async r=>{assert.equal(r.status,200);downloaded=true;o.success({statusCode:r.status,tempFilePath:'TEST_TEMP'});}).catch(o.fail);};wx.getFileSystemManager=()=>({readFile:o=>o.success({data:'Synthetic mobile evidence'})});
 await detail.attachment({currentTarget:{dataset:{id:a.id}}});assert.ok(downloaded);
 detail.data.reason='独立核对合成原始材料和交易范围后确认';await detail.verifyAttachment({currentTarget:{dataset:{id:a.id,action:'APPROVE'}}});assert.equal(detail.data.attachments.find(x=>x.id===a.id).status,'VERIFIED');
 // A stale version must be rejected and the controller must refresh server state.
 const stale=await fetch(apiBase+'/attachments/'+a.id+'/decision',{method:'POST',headers:{Authorization:'Bearer '+reviewerToken,'content-type':'application/json'},body:JSON.stringify({version:a.version,action:'APPROVE',reason:'旧版本应被拒绝且不重复批准',idempotency_key:'mobile-stale-'+Date.now()})});assert.equal(stale.status,409);
 detail.handoff();
 const profile=page('profile');await profile.load();assert.equal(profile.data.user.role,'Reviewer');
 const accountant=page('login');accountant.data.username='accountant';await accountant.login();const synced=await api.request('/matches/'+match.id);assert.equal(synced.status,'APPROVED');
 await testWork(api);
 console.log(JSON.stringify({passed:true,pages:9,checks:['enterprise login','dashboard','tasks','approvals','evidence','same-key network retry','source masking','alerts','case detail','profile','cross-client sync','material download auth','independent evidence review','stale material rejection','resumable Web handoff','mobile chunk interruption and resume','mobile publish and match']}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
