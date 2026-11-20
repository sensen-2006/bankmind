const {workspacePage}=require('../../utils/workspace');
const {request,requireAuth,errorText}=require('../../utils/api');
const {submit,change,root,uploadChunks}=require('../../utils/jobs');
const {download}=require('../../utils/files');

const sides=['银行流水','企业账簿'];
const directionLabels=['请选择','正数流入','正数流出','借方流入','贷方流入'];
const directions=['signed_in','signed_out','debit_in','credit_in'];
const formats=['ISO','DMY','MDY'];
const fields={date:'交易日期',amount:'有符号金额',debit:'借方金额',credit:'贷方金额',
 account:'账号',currency:'币种',reference:'流水号',description:'摘要',counterparty:'交易对方',group_key:'批次号',balance:'逐笔余额'};
const statusNames={UPLOADING:'等待续传',QUEUED:'排队或执行中',SUCCEEDED:'已完成',FAILED:'失败',CANCELLED:'已取消'};
const kindNames={PREVIEW:'解析预览',PUBLISH:'校验并发布',MATCH:'运行匹配'};
const totals=[
 ['count','交易笔数'],['receipts','收入合计'],['payments','支出合计'],['opening','期初余额'],
 ['closing','期末余额'],['note','控制数在原件中的位置']
];
const pageFields=[['count','笔数'],['receipts','收入'],['payments','支出'],['references','原件流水号，每行一个']];

function displayRows(rows,columns){
 return rows.map(row=>({row_no:row.row_no,page:row.page,cells:columns.map(key=>({key,value:row.raw[key]}))}));
}

function makeDraft(batch){
 const visual=batch.quality.encoding==='document-ai';
 const choices=['未映射',...batch.columns];
 return {batch,visual,document:visual||/\.pdf$/i.test(batch.filename),choices,
  fields:Object.entries(fields).map(([id,title])=>({id,title,index:Math.max(0,choices.indexOf(batch.suggested_mapping[id]))})),
  totals:totals.map(([id,title])=>({id,title,value:''})),
  pages:visual?Array.from({length:batch.quality.pdf_pages},(_,i)=>({number:i+1,fields:pageFields.map(([id,title])=>({id,title,value:''}))})):[],
  direction:-1,format:-1,order:0,confirmed:false,sourceVerified:false,noReference:false,
  rows:displayRows(batch.preview,batch.columns),extractionPage:0,hasNext:false};
}

function mapping(draft,task){
 const columns=Object.fromEntries(draft.fields.filter(field=>field.index).map(field=>[field.id,draft.choices[field.index]]));
 const direction=directions[draft.direction];
 if(!draft.confirmed||!columns.date||!direction||!formats[draft.format]||
    (direction.startsWith('signed_')?!columns.amount:!columns.debit||!columns.credit))throw new Error('请选择日期、金额方向与列，确认后再提交');
 let controls=null;
 if(draft.document){
  if(!draft.sourceVerified)throw new Error('请核对原件并确认控制数');
  const amounts=/^-?(?:0|[1-9][0-9]{0,16})(?:\.[0-9]{1,3})?$/;
  const getValues=items=>Object.fromEntries(items.map(item=>[item.id,item.value]));
  const checkedTotal=(input,allowZero)=> {
   if(!/^\d{1,5}$/.test(input.count)||Number(input.count)<(allowZero?0:1)||
      !amounts.test(input.receipts)||input.receipts.startsWith('-')||!amounts.test(input.payments)||input.payments.startsWith('-')){
    throw new Error('请填写原件笔数及非负收支金额');
   }
   return {transaction_count:Number(input.count),receipts:input.receipts,payments:input.payments};
  };
  const input=getValues(draft.totals);
  if(!amounts.test(input.opening)||!amounts.test(input.closing)||input.note.trim().length<4||
     (columns.balance&&!draft.order))throw new Error('请填写期初、期末、控制数来源和所需的逐笔余额顺序');
  if(draft.visual&&(Boolean(columns.reference)===draft.noReference||
     (draft.batch.suggested_mapping.reference&&!columns.reference)))throw new Error('请映射流水号或确认原件确无该列');
  const pages=draft.pages.map(page=>{
   const source=getValues(page.fields);
   const row={page:page.number,...checkedTotal(source,true)};
   if(columns.reference){
    row.references=row.transaction_count?source.references.split(/\r?\n/):[];
    if(row.references.length!==row.transaction_count)throw new Error('第 '+page.number+' 页流水号数量与原件声明笔数不符');
   }
   return row;
  });
  controls={...checkedTotal(input,false),opening_balance:input.opening,closing_balance:input.closing,
   source_note:input.note.trim(),pages,source_has_no_reference:draft.visual&&draft.noReference,
   balance_order:columns.balance?['','oldest_first','newest_first'][draft.order]:null};
 }
 return {fields:columns,direction,date_format:formats[draft.format],confirmed:true,default_currency:task.currency,
  default_account:'__TASK_ACCOUNT_CONFIRMED__',source_verified:draft.sourceVerified,statement_controls:controls};
}

workspacePage({
 data:{taskId:'',task:null,jobs:[],batches:[],draft:null,file:null,side:0,sheet:'',header:'1',busy:false,
  createReady:false,accounts:[],accountIndex:0,newName:'',startDate:'',endDate:'',
  error:'',progress:'',enabled:false,canMatch:false,sides,directionLabels,formatLabels:['请选择',...formats],orders:['请选择','从旧到新','从新到旧']},
 onLoad(query){this.setData({taskId:query.taskId||''});},
 onShow(){if(requireAuth()){this.visible=true;this.data.taskId?this.poll():this.loadCreate();}},
 onHide(){this.visible=false;clearTimeout(this.timer);},
 onUnload(){this.onHide();},
 onPullDownRefresh(){(this.data.taskId?this.load():this.loadCreate()).finally(()=>wx.stopPullDownRefresh());},
 async loadCreate(){
  try{
   const [user,accounts]=await Promise.all([request('/me'),request('/accounts')]);
   if(!['Admin','Accountant'].includes(user.role))throw new Error('当前账号没有新建任务权限');
   this.setData({accounts:accounts.map(account=>({...account,label:account.bank+' · '+account.number_masked+' · '+account.currency})),
    createReady:true,error:''});
  }catch(error){this.setData({error:error.message});}
 },
 editTask(event){
  const field=event.currentTarget.dataset.field;
  this.setData({[field]:field==='accountIndex'?Number(event.detail.value):event.detail.value});
 },
 async createTask(){
  if(this.data.busy)return;
  const {accounts,accountIndex,newName,startDate,endDate}=this.data;
  const account=accounts[accountIndex];
  if(!account||newName.trim().length<2||!startDate||!endDate||endDate<startDate){
   this.setData({error:'请填写任务名称、账户和有效的对账期间。'});
   return;
  }
  this.setData({busy:true,error:''});
  try{
   const task=await request('/tasks','POST',{name:newName.trim(),account_id:account.id,
    currency:account.currency,start_date:startDate,end_date:endDate});
   this.setData({taskId:task.id,task});
   await this.poll();
  }catch(error){this.setData({error:error.message});}
  finally{this.setData({busy:false});}
 },
 async poll(){
  clearTimeout(this.timer);
  await this.load();
  if(this.visible)this.timer=setTimeout(()=>this.poll(),3500);
 },
 async load(){
  if(this.loading||!this.data.taskId)return;
  this.loading=true;
  try{
   const id=this.data.taskId;
   const path='/tasks/'+encodeURIComponent(id)+'/workbench';
   const initial=await request(path);
   const jobs=initial.background_jobs&&initial.can_modify?await request(root(id)):[];
   // A worker can publish after the first task read but before the job list arrives.
   const applied=jobs.some(job=>job.status==='SUCCEEDED'&&!this.data.jobs.some(
    previous=>previous.id===job.id&&previous.status==='SUCCEEDED'));
   const result=applied?await request(path):initial;
   const task=result.task;
   const batches=result.batches.filter(batch=>batch.status==='PREVIEWED');
   const draft=this.data.draft;
   this.setData({task,enabled:result.background_jobs,canModify:result.can_modify,batches,
    canMatch:!task.current_run&&task.summary.sides.BANK.valid>0&&task.summary.sides.BOOK.valid>0,
    canRerun:Boolean(task.current_run)&&task.status!=='COMPLETED',
    draft:draft&&!batches.some(batch=>batch.id===draft.batch.id)?null:draft,
    jobs:jobs.map(job=>({...job,title:kindNames[job.kind],displayStatus:statusNames[job.status],
     canCancel:['FAILED','QUEUED','UPLOADING'].includes(job.status),errorText:job.error?errorText(job.error.detail):''}))});
  }catch(error){this.setData({error:error.message});}
  finally{this.loading=false;}
 },
 chooseFile(){
  wx.chooseMessageFile({count:1,type:'file',extension:['csv','xls','xlsx','pdf'],
   success:result=>{if(result.tempFiles[0])this.setData({file:result.tempFiles[0],error:'',progress:''});}});
 },
 setting(event){
  const name=event.currentTarget.dataset.name;
  this.setData({[name]:name==='side'?Number(event.detail.value):event.detail.value});
 },
 async runAction(action){
  if(this.data.busy)return;
  this.setData({busy:true,error:''});
  try{await action();}
  catch(error){this.setData({error:error.message});}
  finally{await this.load();this.setData({busy:false});}
 },
 enqueueFile(){return this.runAction(async()=>{
  const {taskId,file,side,sheet,header}=this.data;
  if(!file)throw new Error('请先选择对账文件');
  if(!/^[0-9]+$/.test(header)||+header<1||+header>20)throw new Error('表头行号须在 1 至 20 之间');
  const job=await uploadChunks(taskId,side===0?'BANK':'BOOK',file,sheet.trim(),+header,job=>{
   this.setData({progress:job.received_bytes+' / '+job.total_bytes+' 字节'});
  });
  this.setData({file:null,progress:statusNames[job.status]});
 });},
 selectBatch(event){
  const batch=this.data.batches.find(item=>item.id===event.currentTarget.dataset.id);
  if(batch)this.setData({draft:makeDraft(batch),error:''});
 },
 edit(event){
  const {name,index,page}=event.currentTarget.dataset;
  const value=event.detail.value;
  const values={order:Number(value),direction:Number(value)-1,format:Number(value)-1};
  const keys={field:'fields['+index+'].index',total:'totals['+index+'].value',
   page:'pages['+page+'].fields['+index+'].value'};
  const update={['draft.'+(keys[name]||name)]:name==='field'?Number(value):(name in values?values[name]:value)};
  if(!['sourceVerified','confirmed'].includes(name))update['draft.sourceVerified']=false;
  if(['direction','format','field'].includes(name))update['draft.confirmed']=false;
  this.setData(update);
 },
 extraction(event){return this.runAction(async()=>{
  const draft=this.data.draft;
  const page=draft.extractionPage+Number(event.currentTarget.dataset.step)||1;
  const result=await request('/imports/'+draft.batch.id+'/extraction?page='+page);
  this.setData({'draft.rows':displayRows(result.rows,draft.batch.columns),'draft.extractionPage':page,'draft.hasNext':page*50<result.total});
 });},
 original(){return this.runAction(async()=>{
  const batch=this.data.draft.batch;
  const type=batch.filename.split('.').pop().toLowerCase();
  const downloaded=await download('/imports/'+batch.id+'/original', type);
  await new Promise((resolve,reject)=>{
   const fail=()=>reject(new Error('设备无法打开此文件，可在网页下载完整原件'));
   if(['png','jpg','jpeg'].includes(type))wx.previewImage({urls:[downloaded],success:resolve,fail});
   else if(type==='csv')wx.getFileSystemManager().readFile({filePath:downloaded,encoding:'utf8',position:0,length:1500,
    success:r=>{wx.showModal({title:'CSV 原件开头',content:r.data,showCancel:false});resolve();},fail});
   else wx.openDocument({filePath:downloaded,fileType:type,showMenu:false,success:resolve,fail});
  });
 });},
 publish(){return this.runAction(async()=>{
  const {task,draft}=this.data;
  const checked=mapping(draft,task);
  const job=await submit(task.id,{kind:'PUBLISH',version:task.version,selections:[{batch_id:draft.batch.id,mapping:checked}]});
  this.setData({progress:statusNames[job.status]});
 });},
 match(event){return this.runAction(async()=>{
  const rerun=Boolean(event?.currentTarget.dataset.rerun);
  const confirmed=await new Promise(resolve=>wx.showModal({title:rerun?'重新运行匹配':'提交后台匹配',
   content:rerun?'新运行会保留历史批次，并重新生成待复核结果。':'请先核对两侧输入与隔离行。匹配完成后会冻结本任务输入。',
   success:r=>resolve(r.confirm),fail:()=>resolve(false)}));
  if(confirmed){
   const {task}=this.data;
   const job=await submit(task.id,{kind:'MATCH',version:task.version,require_both:true,rerun});
   this.setData({progress:statusNames[job.status]});
  }
 });},
 jobAction(event){
  const {id,action}=event.currentTarget.dataset;
  return this.runAction(()=>change(this.data.taskId,id,action));
 }
});
