const HOME='http://127.0.0.1:3788';
const HELPER_VERSION='2.5';
let pumping=false;
const lovartJobs={};          // jobId -> {id, lease}：页面出图后靠它把结果回传给工作台
async function local(path,body){
  const state=await (await fetch(HOME+'/api/bridge/state')).json();
  const response=await fetch(HOME+'/api/bridge/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok)throw Error(value.error||'工作台连接失败');return value;
}
function targetId(url){const u=new URL(url);const asin=u.pathname.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})/i);const offer=u.pathname.match(/\/offer\/(\d+)\.html/);return u.hostname+':'+(asin?.[1]||offer?.[1]||u.pathname)}
async function capture(job){
  const u=new URL(job.url);
  if(u.protocol!=='https:'||!(/(^|\.)1688\.com$/.test(u.hostname)||/(^|\.)amazon\.(de|co\.uk|com|fr|it|es|nl|se|pl)$/.test(u.hostname)))throw Error('采集网址不在允许的商品网站范围内');
  const tabs=await chrome.tabs.query({});
  let tab=tabs.find(t=>{try{return targetId(t.url)===targetId(job.url)}catch{return false}});
  if(!tab)tab=await chrome.tabs.create({url:job.url,active:false});
  const started=Date.now();
  while(Date.now()-started<45000){tab=await chrome.tabs.get(tab.id);if(tab.status==='complete')break;await new Promise(r=>setTimeout(r,500))}
  await new Promise(r=>setTimeout(r,1800));
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['capture.js']});
  const [{result}]=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(kind,skus,options)=>{try{return {data:await globalThis.collectProductPage(kind,skus,options)}}catch(e){return {error:e.message}}},args:[job.kind,job.skus,{subset:!!job.subset}]});
  if(result.error){await chrome.tabs.update(tab.id,{active:true});throw Error(result.error)}
  return result.data;
}

/* ------------------------------------------------------------ Lovart 网页出图 */
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const isLovart=value=>{try{return /(^|\.)lovart\.ai$/.test(new URL(value).hostname)}catch(e){return false}};
const dataUrlOf=blob=>new Promise(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=()=>resolve('');reader.readAsDataURL(blob)});
async function fetchAsDataUrl(url){
  try{const response=await fetch(url);if(!response.ok)return '';return await dataUrlOf(await response.blob())}catch(e){return ''}
}
async function waitTabReady(tabId,limit=90000){
  const started=Date.now();
  while(Date.now()-started<limit){
    try{const tab=await chrome.tabs.get(tabId);if(tab.status==='complete')return true}catch(e){return false}
    await sleep(600);
  }
  return false;
}
function lovartRelay(jobId){
  window.addEventListener('message',event=>{
    const data=event.data;
    if(!data||typeof data.__lovart!=='string')return;
    if(data.jobId&&data.jobId!==jobId)return;
    try{
      if(data.__lovart==='result')chrome.runtime.sendMessage({type:'lovart-result',jobId,payload:data.payload});
      else if(data.__lovart==='progress')chrome.runtime.sendMessage({type:'lovart-progress',jobId,message:data.message});
    }catch(e){}
  });
}
/* 只复用“同一个画布项目”的标签页；新项目一律开新标签，绝不落到首页。 */
async function lovartTab(job){
  if(!isLovart(job.url))throw Error('Lovart 网址无效');
  const want=(new URL(job.url)).searchParams.get('projectId')||'';
  let tab=null;
  if(want){
    const tabs=await chrome.tabs.query({});
    tab=tabs.find(item=>{
      try{
        const url=new URL(item.url||'');
        return isLovart(url.href)&&url.pathname.includes('/canvas')
          &&(url.searchParams.get('projectId')||'')===want;
      }catch(e){return false}
    })||null;
  }
  if(!tab)tab=await chrome.tabs.create({url:job.url,active:false});
  else if(tab.url!==job.url)await chrome.tabs.update(tab.id,{url:job.url});
  await waitTabReady(tab.id);
  await sleep(4000);
  const current=await chrome.tabs.get(tab.id);
  if(!/\/canvas/.test(new URL(current.url||'').pathname)){
    await chrome.tabs.update(tab.id,{url:job.url,active:false});
    await waitTabReady(tab.id);
    await sleep(3500);
  }
  // 所有 Lovart 作业都先装好“消息转发器”：页面里的进度和结果才能回到工作台。
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['lovart.js'],world:'MAIN'});
  await chrome.scripting.executeScript({target:{tabId:tab.id},world:'ISOLATED',func:lovartRelay,args:[job.id]});
  return tab;
}
async function startLovart(job){
  const tab=await lovartTab(job);
  const files=[];
  for(const file of (job.files||[])){
    const dataUrl=await fetchAsDataUrl(file.url);
    if(dataUrl)files.push({name:file.name,dataUrl});
  }
  const [{result}]=await chrome.scripting.executeScript({
    target:{tabId:tab.id},world:'MAIN',
    func:options=>globalThis.lovartStart(options),
    args:[{jobId:job.id,prompt:job.prompt,skill:job.skill,answer:job.answer,project:job.project,
           files,timeout_sec:job.timeout_sec||780}],
  });
  if(!result||!result.ok)throw Error((result&&result.error)||'Lovart 页面没有开始工作');
  lovartJobs[job.id]={id:job.id,lease:job.lease};
  try{await chrome.storage.session.set({['lovart:'+job.id]:lovartJobs[job.id]})}catch(e){}
  return {progress:'已挂上 '+result.uploaded+' 张参考图、已发送提示词，等待 Lovart 出图…',
          project_url:result.project_url,waiting:true};
}
async function probeLovart(job){
  const tab=await lovartTab(job);
  // 探针作业也要登记：页面脚本的进度（比如“已点开上传按钮，请填文件窗口”）要靠它回传。
  lovartJobs[job.id]={id:job.id,lease:job.lease};
  try{await chrome.storage.session.set({['lovart:'+job.id]:lovartJobs[job.id]})}catch(e){}
  const files=[];
  if(job.mode==='upload'){
    for(const file of (job.files||[])){
      const dataUrl=await fetchAsDataUrl(file.url);
      if(dataUrl)files.push({name:file.name,dataUrl});
    }
  }
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['lovart.js'],world:'MAIN'});
  // 后台只负责“准备好文件和标签页”，具体做什么交给页面脚本自己判断——
  // 这样以后改 Lovart 的行为只需要更新 lovart.js，不用再重新加载扩展。
  const [{result}]=await chrome.scripting.executeScript({target:{tabId:tab.id},world:'MAIN',
    func:(payload)=>globalThis.lovartRun(payload),
    args:[{...job, files}]});
  if(!result)throw Error('Lovart 页面没有响应');
  return result;
}
async function forwardLovart(message){
  let record=lovartJobs[message.jobId];
  if(!record){
    try{
      const stored=await chrome.storage.session.get('lovart:'+message.jobId);
      record=stored['lovart:'+message.jobId];
    }catch(e){}
  }
  if(!record)return;
  if(message.type==='lovart-progress'){
    await local('result',{id:record.id,lease:record.lease,data:{progress:String(message.message||'').slice(0,180)}}).catch(()=>{});
    return;
  }
  try{await local('result',{id:record.id,lease:record.lease,data:message.payload})}catch(e){}
  delete lovartJobs[message.jobId];
  try{await chrome.storage.session.remove('lovart:'+message.jobId)}catch(e){}
}

async function pump(){
  if(pumping)return;pumping=true;let job;
  const heartbeat=setInterval(()=>local('heartbeat',{version:HELPER_VERSION}).catch(()=>{}),15000);
  try{
    await local('heartbeat',{version:HELPER_VERSION});
    while((job=await local('next',{})).id){
      try{
        const data=job.kind==='lovart'?await startLovart(job)
          :(job.kind==='lovart-probe'?await probeLovart(job):await capture(job));
        await local('result',{id:job.id,lease:job.lease,data});
      }catch(e){
        await local('result',{id:job.id,lease:job.lease,error:e.message});
      }
    }
  }catch{}finally{clearInterval(heartbeat);pumping=false}
}
chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create('workbench',{periodInMinutes:0.5});pump()});
chrome.runtime.onStartup.addListener(()=>{chrome.alarms.create('workbench',{periodInMinutes:0.5});pump()});
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name==='workbench')pump()});
chrome.runtime.onMessage.addListener((message,sender,reply)=>{
  if(message?.type==='workbench-pump'){pump();reply({connected:true});return}
  if(message?.type==='lovart-result'||message?.type==='lovart-progress'){forwardLovart(message).catch(()=>{});reply({ok:true});return}
});
