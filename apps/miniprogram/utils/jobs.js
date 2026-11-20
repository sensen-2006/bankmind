const {request,key,errorText}=require('./api');
const {upload,fsCall}=require('./files');
const sha256=require('./sha256');

const root=id=>'/tasks/'+encodeURIComponent(id)+'/jobs';
function digestText(text){
 const escaped=encodeURIComponent(text);
 const bytes=[];
 for(let i=0;i<escaped.length;i++){
  if(escaped[i]==='%'){bytes.push(parseInt(escaped.slice(i+1,i+3),16));i+=2;}
  else bytes.push(escaped.charCodeAt(i));
 }
 return sha256(Uint8Array.from(bytes).buffer);
}

async function submit(task,payload){
 const slot='bankmind-job-'+digestText(JSON.stringify([task,payload]));
 let requestKey=wx.getStorageSync(slot);
 if(!requestKey){requestKey=key();wx.setStorageSync(slot,requestKey);}
 const create=()=>request(root(task),'POST',{...payload,idempotency_key:requestKey});
 let job=await create();
 if(job.status==='CANCELLED'){
  requestKey=key();wx.setStorageSync(slot,requestKey);
  job=await create();
 }
 if(job.status==='FAILED')throw new Error(errorText(job.error.detail));
 return job;
}

const change=(task,id,action)=>request(root(task)+'/'+id+'/'+action,'POST');

async function uploadChunks(task,side,file,sheet,header,progress){
 if(!file.size||file.size>10*1024*1024)throw new Error('文件上限 10 MiB');
 const fs=wx.getFileSystemManager();
 const {data}=await fsCall(fs,'readFile',{filePath:file.path});
 let job=await submit(task,{kind:'PREVIEW',side,filename:file.name,size:data.byteLength,sha256:sha256(data),sheet,header_row:header});
 progress(job);
 const temp=wx.env.USER_DATA_PATH+'/bankmind-chunk-'+key();
 try{
  while(job.status==='UPLOADING'&&job.received_bytes<data.byteLength){
   const offset=job.received_bytes;
   await fsCall(fs,'writeFile',{filePath:temp,data:data.slice(offset,offset+job.chunk_bytes)});
   job=await upload(root(task)+'/'+job.id+'/chunks/'+offset,temp);
   progress(job);
  }
 }finally{
  fs.unlink({filePath:temp,fail(){}});
 }
 return job.status==='UPLOADING'?change(task,job.id,'complete'):job;
}
module.exports={submit,change,uploadChunks,root};
