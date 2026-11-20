const {workspacePage}=require('../../utils/workspace');
const {confirmCall,providerError}=require('../../utils/ai');
const {request,requireAuth,failure,approve}=require('../../utils/api');
const {upload,download}=require('../../utils/files');const {label}=require('../../utils/labels');const {formatMoney}=require('../../utils/money');const config=require('../../config');
workspacePage({data:{expandedCandidates:false,expandedHistory:false,id:'',detail:null,loading:true,error:'',reason:'',busy:false,canApprove:false,resolutions:['其他已核实差异','数据错误','企收银未收','企付银未付','银收企未收','银付企未付'],resolution:0,attachments:[],attachmentFile:null,materialNote:'',requirements:[],aiConfig:null,aiChoices:[],aiIndex:0,aiError:''},onLoad(q){this.setData({id:q.id||''});},onShow(){if(requireAuth())this.load();},onPullDownRefresh(){this.load().finally(()=>wx.stopPullDownRefresh());},async load(){this.setData({loading:true});try{const [d,u,attachments,requirements,aiConfig]=await Promise.all([request('/cases/'+this.data.id),request('/me'),request('/cases/'+this.data.id+'/attachments'),request('/cases/'+this.data.id+'/evidence-requirements'),request('/ai/config')]);const selectedId=this.data.aiChoices[this.data.aiIndex]?.id;
const aiChoices=(aiConfig.providers||[]).filter(provider=>provider.ready);
const aiIndex=Math.max(0,aiChoices.findIndex(provider=>provider.id===selectedId));
d.transactions=d.transactions.map(t=>({...t,displayAmount:formatMoney(t.amount,t.currency)}));d.candidates=d.candidates.map(t=>({...t,displayAmount:formatMoney(t.amount,t.currency)}));this.setData({detail:{...d,statusLabel:label(d.status),kindLabel:label(d.kind)},user:u,attachments:attachments.map(a=>({...a,canVerify:['Admin','Reviewer'].includes(u.role)&&a.uploader_id!==u.id&&a.current_evidence&&a.status==='SUBMITTED'&&d.status!=='CLOSED',statusLabel:a.status==='VERIFIED'?'已独立核验':a.status==='REJECTED'?'已退回':'待核验'})),requirements,aiConfig,aiChoices,aiIndex,canApprove:['Admin','Reviewer'].includes(u.role)&&d.submitter_id!==u.id&&d.status==='READY_REVIEW',error:''});}catch(e){this.setData({error:e.message});}finally{this.setData({loading:false});}},toggleCandidates(){this.setData({expandedCandidates:!this.data.expandedCandidates});},toggleHistory(){this.setData({expandedHistory:!this.data.expandedHistory});},input(e){this.setData({reason:e.detail.value});},resolution(e){this.setData({resolution:Number(e.detail.value)});},source(e){wx.navigateTo({url:'/pages/evidence/index?transactionId='+e.currentTarget.dataset.id});},chooseProvider(event){
  this.setData({aiIndex:Number(event.detail.value),aiError:''});
},
async investigate(){
  const provider=this.data.aiChoices[this.data.aiIndex];
  if(this.data.busy||!provider)return;
  this.setData({busy:true,aiError:''});
  try{
    if(!await confirmCall(provider))return;
    const path='/cases/'+this.data.id+'/investigate?provider='+provider.id+'&confirm_external=true';
    const result=await request(path,'POST',undefined,95000);
    this.setData({aiError:result.status==='FALLBACK'?providerError(result.failure_reason):''});
    await this.load();
  }catch(error){
    this.setData({aiError:error.message});
  }finally{
    this.setData({busy:false});
  }
},
chooseMaterial(){
  wx.chooseMessageFile({count:1,type:'file',extension:['pdf','txt'],
    success:result=>{
      const file=result.tempFiles[0];
      if(!file||file.size>5*1024*1024)return failure(new Error('请选择不超过 5 MB 的材料'));
      this.setData({attachmentFile:file,materialNote:''});
    }});
},
materialNote(event){this.setData({materialNote:event.detail.value});},
async uploadMaterial(){
  const {attachmentFile,materialNote,busy,id}=this.data;
  if(busy||!attachmentFile)return;
  if(materialNote.trim().length<4)return failure(new Error('请填写至少 4 字材料说明'));
  this.setData({busy:true});
  try{
    await upload('/cases/'+id+'/attachments',attachmentFile.path,
      {note:materialNote.trim(),purpose:'SUPPORT',necessary:'true',original_filename:attachmentFile.name});
    this.setData({attachmentFile:null,materialNote:''});
    await this.load();
    wx.showToast({title:'材料待独立核验'});
  }catch(error){failure(error);}
  finally{this.setData({busy:false});}
},
async attachment(event) {
  const id = event.currentTarget.dataset.id;
  const attachment = this.data.attachments.find(item => item.id === id);
  if (!attachment) return;
  this.setData({busy:true});
  try {
    const type = attachment.filename.toLowerCase().endsWith('.pdf') ? 'pdf' : 'txt';
    const filePath = await download('/attachments/' + id + '/original', type);
    await new Promise((resolve,reject) => {
      const fail = () => reject(new Error('无法打开材料原件，请交接网页核验。'));
      if (type === 'pdf') {
        wx.openDocument({filePath,fileType:type,showMenu:true,success:resolve,fail});
      } else {
        wx.getFileSystemManager().readFile({filePath,encoding:'utf8',success: result => {
          const text = String(result.data);
          wx.showModal({title:'材料原件（UTF-8）',content:text.slice(0,1800)+(text.length>1800?'…完整内容请交接网页查看':''),showCancel:false});
          resolve();
        }, fail});
      }
    });
  } catch (error) {
    failure(error);
  } finally {
    this.setData({busy:false});
  }
},
async verifyAttachment(e){if(this.data.reason.trim().length<4)return failure(new Error('先填写至少4字材料核验意见'));const a=this.data.attachments.find(x=>x.id===e.currentTarget.dataset.id);if(!a||!a.canVerify)return;const action=e.currentTarget.dataset.action;const ok=await new Promise(resolve=>wx.showModal({title:action==='APPROVE'?'确认材料已独立核验':'退回材料',content:'请先下载并核对原件。此操作仅处理当前材料版本，不自动结案；版本冲突将刷新。',success:r=>resolve(r.confirm)}));if(!ok)return;this.setData({busy:true});try{await approve('/attachments/'+a.id+'/decision',a.version,action,this.data.reason);wx.showToast({title:'材料复核已同步'});}catch(e){failure(e);}finally{await this.load();this.setData({busy:false});}},handoff(){wx.setClipboardData({data:JSON.stringify({web:config.apiBase ? config.apiBase.replace(/\/api\/?$/,'')+'/?case='+encodeURIComponent(this.data.id) : undefined,case_id:this.data.id,version:this.data.detail.version,resume:'在Web异常调查中按case_id检索，保留当前处理意见：'+this.data.reason}),success:()=>wx.showToast({title:'交接信息已复制'})});},async save(e){if(this.data.reason.trim().length<4)return failure(new Error('请填写至少4字处理意见'));const action=e.currentTarget.dataset.action;this.setData({busy:true});try{await request('/cases/'+this.data.id,'PATCH',{version:this.data.detail.version,action,reason:this.data.reason,resolution:action==='SUBMIT'?['OTHER','DATA_ERROR','BOOK_RECEIPT','BOOK_PAYMENT','BANK_RECEIPT','BANK_PAYMENT'][this.data.resolution]:''});wx.showToast({title:'处理已保存'});await this.load();}catch(e){failure(e);await this.load();}finally{this.setData({busy:false});}},decide(e){if(this.data.reason.trim().length<4)return failure(new Error('请填写至少4字审批意见'));const action=e.currentTarget.dataset.action;wx.showModal({title:'确认审批操作',content:'将记录当前版本、处理原因与审核人，操作同步至Web。',success:async res=>{if(!res.confirm)return;this.setData({busy:true});try{await approve('/cases/'+this.data.id+'/decision',this.data.detail.version,action,this.data.reason);wx.showToast({title:'审批已同步'});await this.load();}catch(e){failure(e);await this.load();}finally{this.setData({busy:false});}}});}});
