'use strict';
const $ = id => document.getElementById(id);
const token = location.hash.slice(1) || sessionStorage.getItem('notiontool-token') || '';
if (token) sessionStorage.setItem('notiontool-token', token);
history.replaceState(null, '', location.pathname);
let state = null, selected = localStorage.getItem('notiontool-chat') || '', view = 'chat', details = {messages:[],tasks:[]};
let renderKey = '', pollBusy = false, modalHandler = null, toastTimer, disconnected = false;
let seenJobs=null;
let messageLimit=100, detailMarker='';
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const titleCase = text => String(text || '').replaceAll('_',' ').replace(/^./, c=>c.toUpperCase());
const stages = ['approach','planning','executing','reviewing','fixing','integrating'];

async function api(path, body) {
  let response;
  try{response = await fetch('/api/'+path, {method:body===undefined?'GET':'POST',
    headers:{'X-NotionTool-Token':token,'Content-Type':'application/json'}, body:body===undefined?undefined:JSON.stringify(body)});}
  catch(error){setHostBanner(true);throw new Error('NotionTool host is not reachable. Nothing was resent; the state is checked again after reconnecting.');}
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function setHostBanner(down){const b=$('host-banner');if(b)b.classList.toggle('hidden',!down);}
function requestId(key){let id=sessionStorage.getItem(key);if(!id){id=(crypto.randomUUID?crypto.randomUUID():String(Date.now())+Math.random());sessionStorage.setItem(key,id);}return id;}
async function reconcilePending(){
  // After a reconnect: ask the host what happened instead of resending.
  const send=JSON.parse(sessionStorage.getItem('pending-send')||'null');
  if(send){try{const r=await api('request?id='+encodeURIComponent(send.request_id));
    if(r.found){sessionStorage.removeItem('pending-send');} else {await api('send',send);sessionStorage.removeItem('pending-send');}}catch{}}
  for(const key of ['create-main']){const id=sessionStorage.getItem(key);if(!id)continue;
    const op=(state?.operations||[]).find(o=>o.request_id===id);if(op&&['done','failed'].includes(op.state))sessionStorage.removeItem(key);}
}
function toast(text, error=false) {
  $('toast').textContent=text; $('toast').className='toast'+(error?' error':'');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),error?7000:3500);
}
async function action(path, body, note='Saved') {
  try { const result=await api(path,body); if(note)toast(result.job_id?'Operation started':note); await refresh(); return result; }
  catch(error){toast(error.message,true);throw error;}
}
function markdown(source) {
  // Escape every source character before producing our own limited markup.
  const blocks=[];
  let text=String(source||'').replace(/```[^\n]*\n([\s\S]*?)```/g,(_,code)=>{
    blocks.push('<pre><code>'+escape(code)+'</code></pre>');return '\u0000'+(blocks.length-1)+'\u0000';
  });
  text=escape(text).replace(/^### (.+)$/gm,'<h3>$1</h3>').replace(/^## (.+)$/gm,'<h2>$1</h2>').replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/\*\*([^\n]+?)\*\*/g,'<strong>$1</strong>').replace(/`([^`\n]+)`/g,'<code>$1</code>')
    .replace(/\n/g,'<br>');
  return text.replace(/\u0000(\d+)\u0000/g,(_,index)=>blocks[Number(index)]||'');
}
function currentChat(){return state?.chats.find(c=>c.id===selected);}
function currentRun(){return state?.runs.filter(r=>r.chat_id===selected).at(-1);}
function showView(name){view=name;for(const n of ['chat','accounts','settings','mcp'])$(n+'-view').classList.toggle('hidden',name!==n);render();}
function selectChat(id){selected=id;messageLimit=100;localStorage.setItem('notiontool-chat',id);renderKey='';details={messages:[],tasks:[]};showView('chat');refresh();}

function tree(){
  const search=$('search').value.toLowerCase();
  const chatRows=folder=>state.chats.filter(c=>(c.folder_id||'')===folder&&c.title.toLowerCase().includes(search)).map(c=>{
    const active=state.runs.some(r=>r.chat_id===c.id&&stages.includes(r.state));
    return `<button class="chat-row ${c.id===selected?'active':''}" data-chat="${escape(c.id)}"><span>▤</span><span class="chat-title">${escape(c.title)}</span>${active?'<span class="mini-dot"></span>':''}</button>`;
  }).join('');
  function folderRows(parent,depth=0){
    if(depth>20)return '';
    return state.folders.filter(f=>(f.parent_id||'')===parent).map(f=>`<div class="folder-group"><div class="folder-row" style="padding-left:${8+depth*8}px"><span>▱</span>${escape(f.name)}<button data-folder-edit="${escape(f.id)}" title="Edit folder">···</button></div>${chatRows(f.id)}${folderRows(f.id,depth+1)}</div>`).join('');
  }
  $('chat-tree').innerHTML=chatRows('')+folderRows('') || '<div class="empty-tree">Your main chats will appear here.<br>Create a folder to organize your work.</div>';
}
function render(){
  if(!state)return;
  document.body.classList.toggle('light',state.settings.theme==='light');
  tree();
  $('account-count').textContent=state.harness_account_id?1:0;
  $('metric-running').textContent=state.stats.running;
  $('metric-ready').textContent=state.stats.ready;
  $('metric-recovering').textContent=state.stats.recovering;
  $('team-total').textContent=state.workers.filter(w=>w.enabled).length;
  $('composer-capacity').textContent=state.stats.ready+' workers ready';
  const chat=currentChat(),run=currentRun();
  const modelTasks=state.tasks.filter(t=>t.run_id===run?.id);
  const mainEvidence=modelTasks.filter(t=>['planner','integrator'].includes(t.role)).at(-1);
  $('model-status').textContent=modelTasks.some(t=>t.model_verification==='mismatch')?'Model mismatch · blocked':
    mainEvidence?.model_verification==='fallback'?'Opus 5.5 unavailable · fallback model':mainEvidence?.model_verification==='verified'?'Opus 5.5 · verified':'Opus 5.5 · verification required';
  $('breadcrumb-folder').textContent=view==='chat'?(state.folders.find(f=>f.id===chat?.folder_id)?.name||'Workspace'):'Workspace';
  $('breadcrumb-chat').textContent=view==='chat'?(chat?.title||'New conversation'):view==='accounts'?'Account & workers':'Settings';
  $('project-label').textContent=chat?.project_path?chat.project_path.split(/[\\/]/).at(-1):'No project folder';
  $('send').disabled=!chat;
  $('message').placeholder=chat?'Message your team…':'Connect an account and create a main chat to begin';
  const stageIndex=stages.indexOf(run?.state==='paused'?run?.resume_state:run?.state);
  $('pipeline').innerHTML=stages.filter(s=>s!=='fixing'||run?.fixes).map((s,displayIndex)=>{
    const n=stages.indexOf(s),complete=run?.state==='done'||(stageIndex>n),current=stageIndex===n;
    return `<div class="pipeline-row ${complete?'complete':current?'current':''}"><span class="pipeline-dot">${complete?'✓':current?'•':displayIndex+1}</span>${({approach:'Explore approaches',planning:'Main chat plans',executing:'Execute in parallel',reviewing:'Check the results',fixing:'Repair remaining issues',integrating:'Bring it together'})[s]}${current?'<span class="stage-detail">Active</span>':''}</div>`;
  }).join('');
  $('worker-grid').innerHTML=state.workers.filter(w=>w.enabled||w.state==='recovering').map(w=>`<button class="worker-cell ${escape(w.state)}" data-worker-report="${escape(w.id)}" title="${escape(w.label)} · ${escape(w.state)}"></button>`).join('')||'<p class="help">No workers connected yet.</p>';
  const tasks=state.tasks.filter(t=>t.run_id===run?.id);
  $('task-list').innerHTML=tasks.slice(-24).map(t=>`<button class="task-row ${escape(t.state)}" data-report="${escape(t.id)}"><strong>${escape(t.title)}</strong><small><span class="task-state">${escape(t.state==='done'&&t.role==='executor'&&run?.state!=='done'?'Reported':titleCase(t.state))}</span> · ${escape(titleCase(t.specialty||t.role))} · ${escape(titleCase(t.model||'opus'))} 5.5 ${({verified:'verified',mismatch:'mismatch',fallback:'fallback model',turn_error:'turn error'})[t.model_verification]||'requested'}${t.remote_error?' · <span class=warn>Notion error: '+escape(t.remote_error)+(t.state==='done'?' (retried)':'')+'</span>':''}${t.worker_id?' · '+escape(state.accounts.find(a=>a.id===state.workers.find(w=>w.id===t.worker_id)?.account_id)?.label||''):''}</small></button>`).join('');
  $('run-bar').classList.toggle('hidden',!run);
  if(run){
    const done=tasks.filter(t=>t.state==='done').length;
    const active=stages.includes(run.state);
    $('run-bar').innerHTML=`<div><strong class="run-state">${escape(titleCase(run.state))}</strong> <span>· ${done}/${tasks.length} assignments finished</span><small>${escape(run.error||run.summary||'Your team is preparing the approach.')}</small></div><div>${active?`<button data-run-pause="${escape(run.id)}">Pause</button><button data-run-stop="${escape(run.id)}">Stop</button>`:run.state==='paused'?`<button data-run-resume="${escape(run.id)}">Resume</button>`:['blocked','failed'].includes(run.state)?`<button data-run-retry="${escape(run.id)}">Retry resolved tasks</button>`:''}</div>`;
  }
  if(chat){
      const key=JSON.stringify([details.messages.map(m=>[m.id,m.text]),details.has_more,run?.id,run?.state]);
    if(renderKey!==key){
      const container=$('conversation'),atBottom=container.scrollHeight-container.scrollTop-container.clientHeight<90;
      container.innerHTML=(details.has_more&&messageLimit<10000?'<button data-older-messages>Load 100 earlier messages</button>':'')+details.messages.map(m=>`<article class="message ${escape(m.role)}"><div class="avatar">${m.role==='user'?'U':'✦'}</div><div class="message-content"><div class="message-label">${m.role==='user'?'You':'NotionTool'}<span>${new Date(m.created*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}</span></div><div class="message-body">${markdown(m.text)}</div></div></article>`).join('')||`<div class="welcome"><div class="welcome-mark">✦</div><div class="eyebrow">${escape(chat.title.toUpperCase())}</div><h1>Ready when you are.</h1><p>Give your main chat a goal. The team will plan, execute, and check it.</p></div>`;
      if(atBottom||!renderKey)container.scrollTop=container.scrollHeight;
      renderKey=key;
    }
  }
  if(view==='accounts')renderAccounts();
  if(view==='settings')renderSettings();
  if(view==='mcp')renderMcp();
}
function workerHints(w){const h=[];if(w.compress_requested)h.push('Compress queued'+(w.compress_reason?': '+w.compress_reason:''));if(w.context_fallback)h.push(w.context_fallback);if(String(w.cleanup_state||'').startsWith('deferred'))h.push('Cleanup waits: '+String(w.cleanup_state).slice(9).trim());return h.map(x=>'<small class=warn>'+escape(x)+'</small>').join('');}
function renderAccounts(){
  $('accounts-summary').textContent='1 account slot · '+state.workers.filter(w=>w.purpose==='worker'&&w.enabled).length+' enabled Coworkers';
  const choices=state.accounts.length>1?`<div class="settings-card"><h3>Harness account</h3><p>Existing accounts are preserved. Only the selected account runs this harness.</p>${state.accounts.map(a=>`<button data-select-account="${escape(a.id)}" ${a.id===state.harness_account_id?'disabled':''}>${escape(a.email)}${a.id===state.harness_account_id?' · Selected':''}</button>`).join('')}</div>`:'';
  $('account-list').innerHTML=choices+state.accounts.filter(a=>a.id===state.harness_account_id).map(a=>{
    const canOpen=a.state==='ready'||Boolean(a.setup?.chat_ready);
    a={...a,error:a.error||a.setup_warning||a.bridge_error||''};
    if(a.state==='ready'&&a.setup&&!a.setup.complete)a={...a,state:a.setup.stage};
    const workers=state.workers.filter(w=>w.account_id===a.id);
    return `<section class="account-card"><div class="account-heading"><div><h3>Harness account</h3><p>${escape(a.email)}</p>${a.model_fallback?'<p class="help warn">'+escape(a.model_fallback)+'</p>':''}</div><span class="account-state ${escape(a.state)}">${escape(titleCase(a.state))}</span></div>${a.error?'<p class="error-text">'+escape(a.error)+'</p>':''}<div class="button-row"><button data-add-worker="${escape(a.id)}">＋ Coworker</button><button data-provision="${escape(a.id)}">+ Welcome chats</button><button data-team-account="${escape(a.id)}">Prepare Main & Coworkers</button><button data-refresh-account="${escape(a.id)}">Refresh chats</button>${canOpen?`<button data-open-notion="${escape(a.id)}">Open Edge · robots.txt ↗</button>`:''}</div><p class="help">One account, several Main chats, Coworkers created through the existing Welcome chat creation. Context renewal waits for confirmed assignment boundaries.</p><table class="worker-table"><tbody>${workers.map(w=>`<tr><td><input type="checkbox" data-worker-enable="${escape(w.id)}" ${w.enabled?'checked':''} ${w.purpose==='main'?'disabled':''} aria-label="Enable ${escape(w.label)}"></td><td class="worker-name">${escape(w.purpose==='main'?(w.main_number?'Main '+w.main_number:'Main'):'Coworker · '+String(w.id).slice(-6))}${w.purpose==='main'?' <small>MAIN</small>':''}<small>Generation ${Number(w.generation||0)+1} · ~${Number(w.context_usage?.estimated_tokens||0).toLocaleString()} tokens · ${Number(w.context_usage?.turns||0)} turns</small>${workerHints(w)}</td><td>${escape(titleCase(w.state))}</td></tr>`).join('')}</tbody></table></section>`;
  }).join('')||'<div class="empty-note">Sign in to Notion once to connect your account.</div>';
  $('jobs-list').innerHTML=state.jobs.slice(-6).reverse().map(j=>`<div class="job-row ${escape(j.state)}">${escape(j.title)} · ${escape(titleCase(j.state))}${j.error?' — '+escape(j.error):j.state==='done'?' · '+escape(JSON.stringify(j.result)):''}</div>`).join('');
}
function renderSettings(){
  if(!document.querySelector('#settings-form :focus')){
    for(const element of $('settings-form').elements)if(element.name&&state.settings[element.name]!==undefined)element.value=state.settings[element.name];
  }
  $('local-mcp').textContent=state.bridge.local_url;
  if($('ops-list'))$('ops-list').innerHTML=(state.operations||[]).slice(-8).reverse().map(o=>`<div class="event-row"><strong>${escape(o.title||titleCase(o.kind))}</strong> · ${escape(titleCase(o.state))}${o.error?' · '+escape(o.error):''}${o.next_step?'<br><small>Next: '+escape(o.next_step)+'</small>':''}</div>`).join('')||'<p class="help">No Main creations or context switches yet.</p>';
  if($('limits-list')&&state.limits)$('limits-list').innerHTML=['confirmed','observed','configured'].map(k=>`<div class="event-row"><strong>${titleCase(k)}</strong> · ${escape(state.limits[k])}</div>`).join('');
  $('activity-log').innerHTML=state.events.slice(-35).reverse().map(e=>`<div class="event-row"><time>${new Date(e.ts*1000).toLocaleTimeString()}</time>${escape(e.message||titleCase(e.kind))}${e.error?' · '+escape(e.error):''}</div>`).join('')||'<p class="help">Your team activity will appear here.</p>';
}
async function refresh(){
  if(pollBusy)return;
  pollBusy=true;
  try{
    state=await api('state');
    if(!state.chats.some(c=>c.id===selected))selected=state.chats[0]?.id||'';
    const chatState=state.chats.find(c=>c.id===selected);
    const marker=chatState?[selected,messageLimit,chatState.message_marker,chatState.active_run,state.tasks?.length||0].join('|'):'';
    if(selected&&(marker!==detailMarker||chatState?.active_run)){details=await api('chat?id='+encodeURIComponent(selected)+'&limit='+messageLimit);detailMarker=marker;}
    render();
    const jobKey=j=>j.id+'|'+j.state+'|'+(j.error||'');if(seenJobs===null)seenJobs=new Set(state.jobs.filter(j=>j.state!=='running').map(jobKey));
    for(const job of state.jobs){
      if(job.state==='running'||seenJobs.has(jobKey(job)))continue;
      seenJobs.add(jobKey(job));
      if(job.state==='failed'){toast(job.title+': '+job.error,true);continue;}
      else if(job.result.chat_verified||job.result.verified){toast(job.result.setup?.complete?'Account setup verified: six chats and PC tools':'Six chats verified. Account setup is incomplete: PC tools required.');showView('chat');}
    }
    setHostBanner(false);
    if(disconnected){toast('Reconnected to NotionTool. Operation states were re-read.');disconnected=false;}
    await reconcilePending();
  }catch(error){if(!disconnected)toast(error.message,true);disconnected=true;setHostBanner(true);}
  finally{pollBusy=false;}
}
function modal(title,fields,handler,submit='Save'){
  $('modal-title').textContent=title;$('modal-fields').innerHTML=fields;$('modal-error').textContent='';
  $('modal-submit').textContent=submit;$('modal-submit').classList.remove('hidden');
  modalHandler=handler;$('modal').showModal();
  setTimeout(()=>$('modal-fields').querySelector('input,select,textarea')?.focus(),50);
}
function folderOptions(value=''){
  return '<option value="">Workspace</option>'+state.folders.map(f=>`<option value="${escape(f.id)}" ${f.id===value?'selected':''}>${escape(f.name)}</option>`).join('');
}
function newFolder(id=''){
  const existing=state.folders.find(f=>f.id===id);
  modal(existing?'Edit folder':'New folder',`<label>Name<input name="name" required value="${escape(existing?.name||'')}"></label><label>Parent folder<select name="parent_id">${folderOptions(existing?.parent_id)}</select></label>`,async data=>action('folder',{...data,id:id||undefined}));
}
async function newChat(){
  if(!state)return;
  if(!state.accounts.some(a=>a.state==='ready')){showView('accounts');toast('Connect your Notion account first');return;}
  const button=$('new-chat');if(button.disabled)return;button.disabled=true;
  try{
    // One click = one request_id; retries reuse it, so the host creates exactly one Main.
    const op=await api('main/create',{request_id:requestId('create-main')});
    toast(op.duplicate?op.title+' is already being created':'Creating '+op.title+'…');
    for(let i=0;i<120;i++){const cur=await api('operation?id='+encodeURIComponent(op.id));
      if(cur.state==='done'){sessionStorage.removeItem('create-main');await refresh();selectChat(cur.chat_id);toast(cur.title+' created');return;}
      if(cur.state==='failed'||cur.state==='unconfirmed'){sessionStorage.removeItem('create-main');toast(cur.title+': '+(cur.error||cur.state)+' · '+(cur.next_step||''),true);return;}
      await new Promise(r=>setTimeout(r,1500));}
  }catch(error){toast(error.message,true);}
  finally{button.disabled=false;refresh();}
}
async function compressChat(){
  const chat=currentChat();if(!chat)return;
  const reason=prompt('Compress '+chat.title+'? It summarizes the context and continues in the same chat at the next message. Reason:','Context too long');if(reason===null)return;
  try{const r=await api('chat/compress',{chat_id:chat.id,reason});toast('Compress queued: '+r.reason);}catch(error){toast(error.message,true);}
}
function chatDetails(){
  const chat=currentChat();if(!chat)return newChat();
  modal('Chat settings',`<label>Name<input name="title" required value="${escape(chat.title)}"></label><label>Folder<select name="folder_id">${folderOptions(chat.folder_id)}</select></label><label>PC project folder<input name="project_path" value="${escape(chat.project_path)}" placeholder="C:\Your\Project"></label><button type="button" data-pick-folder>Choose a folder…</button>`,async data=>action('chat/update',{...data,id:chat.id}));
}
async function report(taskId){
  try{
    const data=await api('report?id='+encodeURIComponent(taskId));
    let content=data.result||data.partial||data.task.error||'Waiting for an assignment.';
    try{content=JSON.stringify(JSON.parse(content),null,2);}catch{}
    modal(data.task.title,`<p class="help">${escape(titleCase(data.task.state))} · ${escape(titleCase(data.task.role))} · Attempt ${data.task.attempts}</p>${data.task.remote_unknown?`<button type="button" data-task-reconcile="${escape(taskId)}">Check upstream completion again</button>`:''}<pre class="report-text">${escape(content)}</pre>`,async()=>{});
    $('modal-submit').classList.add('hidden');$('modal-cancel').textContent='Close';
  }catch(error){toast(error.message,true);}
}

$('modal-close').onclick=()=>$('modal').close();
$('modal-cancel').onclick=()=>$('modal').close();
$('modal').addEventListener('close',()=>{$('modal-cancel').textContent='Cancel';$('modal').querySelectorAll('input[type="password"]').forEach(input=>{input.value='';});});
$('modal-form').onsubmit=async event=>{
  event.preventDefault();const data=Object.fromEntries(new FormData(event.target));
  $('modal-submit').disabled=true;
  try{await modalHandler(data);$('modal').close();}catch(error){$('modal-error').textContent=error.message;}
  finally{$('modal-submit').disabled=false;}
};
$('new-chat').onclick=newChat;if($('compress-chat'))$('compress-chat').onclick=compressChat;$('new-folder').onclick=()=>newFolder();$('chat-details').onclick=chatDetails;
$('search').oninput=()=>state&&tree();$('nav-accounts').onclick=()=>showView('accounts');$('welcome-accounts').onclick=()=>showView('accounts');$('nav-settings').onclick=()=>showView('settings');
$('toggle-team').onclick=()=>document.body.classList.toggle('team-hidden');
$('sign-in-notion').onclick=()=>action('account/browser-login',{},'Notion sign-in opened. Log in, then close the window.').catch(()=>{});$('add-account').onclick=()=>action('account/browser-login',{},'Sign in with the additional account. It is added without replacing the current one.').catch(()=>{});
$('remove-account').onclick=async()=>{const list=state?.accounts||[];const acc=list.find(x=>x.id===state.harness_account_id)||list[0];if(!acc){toast('No account connected',true);return;}if(!confirm('Remove '+acc.email+' from NotionTool? The saved sign-in and the local chat list are deleted. Nothing is deleted in Notion.'))return;action('account/remove',{account_id:acc.id},'Account removed').catch(()=>{});};
async function copyText(t){if(!t){toast('Nothing to copy yet',true);return;}try{await navigator.clipboard.writeText(t);}catch{const x=document.createElement('textarea');x.value=t;document.body.appendChild(x);x.select();document.execCommand('copy');x.remove();}toast('Copied');}
async function hostKey(rotate){const r=await api('host/key',{rotate:!!rotate});$('remote-mcp-key').value=r.api_key||'';return r.api_key||'';}
$('remote-mcp-start').onclick=()=>action('bridge/tunnel',{},'Starting remote access').catch(()=>{});
$('remote-mcp-rotate').onclick=async()=>{if(!confirm('Rotate the token? Existing MCP clients lose access.'))return;try{await hostKey(true);toast('Token rotated');}catch(e){toast(e.message,true);}};
$('remote-mcp-show').onclick=async()=>{const k=$('remote-mcp-key');if(!k.value)await hostKey(false).catch(e=>toast(e.message,true));k.type=k.type==='password'?'text':'password';$('remote-mcp-show').textContent=k.type==='password'?'Show':'Hide';};
$('remote-mcp-copy-url').onclick=()=>copyText(state?.bridge?.public_url||'');
$('remote-mcp-copy-key').onclick=async()=>{try{copyText($('remote-mcp-key').value||await hostKey(false));}catch(e){toast(e.message,true);}};
$('automatic-account').onclick=()=>{
  const account=state.accounts.find(a=>a.id===state.harness_account_id);
  if(!account){$('sign-in-notion').click();return;}
  action('account/add-worker',{account_id:account.id},'Adding Coworker chat').catch(()=>{});
};
$('start-tunnel').onclick=()=>action('bridge/tunnel',{}).catch(()=>{});
$('settings-form').onsubmit=async event=>{event.preventDefault();await action('settings',Object.fromEntries(new FormData(event.target))).catch(()=>{});};
$('composer').onsubmit=async event=>{
  event.preventDefault();const message=$('message'),text=message.value.trim();if(!text||!selected)return;
  $('send').disabled=true;
  const payload={chat_id:selected,text,request_id:(crypto.randomUUID?crypto.randomUUID():String(Date.now()))};
  sessionStorage.setItem('pending-send',JSON.stringify(payload));
  try{await action('send',payload,'');sessionStorage.removeItem('pending-send');message.value='';message.style.height='auto';await refresh();}
  catch{}finally{$('send').disabled=!currentChat();}
};
$('message').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('composer').requestSubmit();}});
$('message').addEventListener('input',()=>{$('message').style.height='auto';$('message').style.height=Math.min(180,$('message').scrollHeight)+'px';});
document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.key.toLowerCase()==='k'){event.preventDefault();$('search').focus();}if(event.ctrlKey&&event.key.toLowerCase()==='n'){event.preventDefault();newChat();}});
document.addEventListener('change',event=>{const id=event.target.dataset.workerEnable;if(id)action('worker',{id,enabled:event.target.checked},'Worker pool updated').catch(()=>{});});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;const d=button.dataset;
  try{
    if(d.chat)selectChat(d.chat);
    if(d.folderEdit)newFolder(d.folderEdit);
    if(d.report)await report(d.report);
    if(d.workerReport){const task=state.tasks.filter(t=>t.worker_id===d.workerReport).at(-1);if(task)await report(task.id);else toast('Ready for an assignment');}
    if('olderMessages' in d){messageLimit=Math.min(10000,messageLimit+100);await refresh();}
    if(d.selectAccount){await action('account/select',{account_id:d.selectAccount},'Harness account selected');selected='';}
    if(d.addWorker)await action('account/add-worker',{account_id:d.addWorker},'Adding worker and completing setup');
    if(d.openNotion)await action('account/open',{account_id:d.openNotion},'Opening your signed-in Notion account');
    if(d.refreshAccount)await action('account/refresh',{account_id:d.refreshAccount});
    if(d.setupAccount)await action('account/setup',{account_id:d.setupAccount});
    if(d.teamAccount)await action('account/team',{account_id:d.teamAccount});
    if(d.enableAccount)await action('workers/enable',{account_id:d.enableAccount,enabled:true},'Available worker chats enabled');
    if(d.bridgeAccount)await action('bridge/connect',{account_id:d.bridgeAccount});
    if(d.provision)modal('Create Welcome worker chats','<label>Number of chats<input type="number" name="count" min="1" max="40" value="5" required></label><p class="help">These use the Welcome to Notion protocol and are renamed Cowork joined. Account limits apply.</p>',async data=>action('account/provision',{account_id:d.provision,count:Number(data.count)}),'Create chats');
    if(d.runPause)await action('run/pause',{id:d.runPause,paused:true},'Paused new assignments');
    if(d.runResume)await action('run/pause',{id:d.runResume,paused:false},'Resumed');
    if(d.runStop)await action('run/cancel',{id:d.runStop},'Stopped locally; upstream requests are reconciled before reuse');
    if(d.runRetry)await action('run/retry',{id:d.runRetry});
    if(d.taskReconcile){await action('task/reconcile',{id:d.taskReconcile},'Checking upstream completion');$('modal').close();}
    if('pickFolder' in d){const result=await api('project/pick',{});if(result.path)$('modal-fields').querySelector('[name=project_path]').value=result.path;}
  }catch(error){if(!error.message.includes('Request failed'))toast(error.message,true);}
});
refresh();setInterval(()=>{if(!document.hidden)refresh();},1500);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});

function renderMcp(){
  if($('remote-mcp-url'))$('remote-mcp-url').value=state.bridge?.public_url||'';if($('remote-mcp-key')&&!$('remote-mcp-key').value&&!window.__keyLoading){window.__keyLoading=true;hostKey(false).catch(()=>{}).finally(()=>{window.__keyLoading=false;});}
  $('mcp-server-list').innerHTML=(state.mcp_servers||[]).map(s=>`<div class="settings-card"><h3>${escape(s.name)}</h3><p>${escape(s.url)}</p><small>${s.toggle ? `<label class="toggle"><input type="checkbox" data-toggle="${escape(s.toggle)}" data-label="${escape(s.label||s.name)}" ${s.enabled?'checked':''}> ${s.enabled?'On':'Off'}</label> ${escape(s.hint||'')}` : s.builtin ? (s.installed ? 'Studio MCP installed. Open Studio and enable MCP to use its tools.' : 'Install Roblox Studio and enable MCP in Studio settings.') : state.accounts.filter(a=>a.mcp_connections?.[s.id]?.connected).length+' accounts connected'}</small></div>`).join('')||'<p class="help">No custom MCP servers yet.</p>';
  document.querySelectorAll('#mcp-server-list [data-toggle]').forEach(el=>el.onchange=()=>action('settings',{[el.dataset.toggle]:el.checked},(el.dataset.label||'MCP')+(el.checked?' on':' off')).catch(()=>{}));
}
$('nav-mcp').onclick=()=>showView('mcp');
$('mcp-form').onsubmit=async e=>{e.preventDefault();try{const values=Object.fromEntries(new FormData(e.target));await action('mcp/add',{...values,url:values.mcp_url,token:values.mcp_bearer_token},'Connecting MCP server');e.target.reset();}catch{}};
$('mcp-refresh').onclick=async()=>{const b=$('mcp-refresh'),st=$('mcp-refresh-status');b.disabled=true;st.textContent='Refreshing tools…';try{const r=await action('mcp/refresh',{},'MCP tools refreshed');const on=((r&&r.servers)||[]).filter(s=>s.enabled);st.textContent=on.map(s=>s.name+': '+(s.ok?s.count+' tools':'error – '+s.error)).join(' · ')||'No local MCP server is switched on.';}catch(e){st.textContent='Refresh failed';}finally{b.disabled=false;}};

// Remote MCP fields are filled independently of render(), so a render error can never leave them empty.
function fillRemote(){try{const u=$('remote-mcp-url');const url=(typeof state!=='undefined'&&state&&state.bridge&&state.bridge.public_url)||'';if(u&&url&&u.value!==url)u.value=url;const k=$('remote-mcp-key');if(k&&!k.value&&!window.__keyLoading){window.__keyLoading=true;Promise.resolve(hostKey(false)).catch(()=>{}).finally(()=>{window.__keyLoading=false;});}}catch(e){}}
setInterval(fillRemote,1500);
// Keep Settings readable: remote access first, everything else under Advanced settings.
(function tidySettings(){const card=$('remote-mcp-card');if(!card||card.dataset.tidy)return;card.dataset.tidy='1';const view=card.parentElement;const d=document.createElement('details');d.className='settings-card advanced-settings';const s=document.createElement('summary');s.textContent='Advanced settings';d.appendChild(s);[...view.children].filter(el=>el!==card&&!el.classList.contains('page-heading')).forEach(el=>d.appendChild(el));view.appendChild(d);})();
