const el = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'等待排队',submitting:'提交中',submitted:'ComfyUI 排队中',running:'GPU 计算中',waiting:'等待恢复',complete:'生成完成',failed:'失败',interrupted:'已中断',attention:'需管理员核查'};
const failed = ['failed','interrupted','attention'];
let member = null, profiles = [], tasks = [], enabled = false, previewUrls = [], formVersion = '', taskSignature = '';
const chosen = () => profiles.find(p => p.id === el('kind').value && p.enabled);
async function api(url, options={}) {
  const r = await fetch(url, options);
  if(r.status===401){location.href='/login';throw new Error('请先登录');}
  const data = await r.json();
  if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'请求参数不正确');
  return data;
}
function view(name) {
  document.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view===name));
  el('workspace').hidden = name==='library';
  el('library-panel').hidden = name!=='library';
  el('creation-panel').hidden = name==='tasks';
  el('workspace').classList.toggle('tasks-only',name==='tasks');
  history.replaceState(null,'','#'+name);
  el('page-title').textContent = {create:'创建资产',tasks:'任务中心',library:'工作流设置'}[name];
}
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => view(b.dataset.view));
function renderForm() {
  const p = chosen();
  el('submit').disabled = !enabled || !p?.ready;
  el('workflow-description').textContent = p?.description || '请先在工作流设置启用一个工作流。';
  el('workflow-status').textContent = !enabled ? '本机计算服务尚未启用' : p ? (p.ready?'● 工作流就绪 · v'+p.version:p.message) : '没有已启用的工作流';
  el('workflow-status').classList.toggle('error', !p?.ready || !enabled);
  const signature = p ? JSON.stringify([p.id,p.version,p.inputs,p.parameters]) : '';
  if(signature===formVersion)return;
  formVersion=signature;
  previewUrls.forEach(URL.revokeObjectURL);previewUrls=[];
  el('submit-message').textContent='';
  const fields=p?.inputs || [];
  const multi=fields.length>1;
  el('image-inputs').className=multi?'upload-grid':'';
  el('image-inputs').innerHTML=fields.map((f,i)=>`<label class="upload-slot"><span>${esc(f.label)}</span><input id="image-${i}" data-key="${esc(f.key)}" type="file" accept="image/png,image/jpeg,image/webp" ${multi?'':'multiple'} required><small>${multi?'选择这一方向的图片':'可批量选择图片'} · 最多 20MB / 张</small><div class="input-previews"></div></label>`).join('');
  el('view-hint').hidden=p?.id!=='multiview';
  el('input-title').textContent=multi?'上传各方向参考图':'上传参考图';
  el('parameter-fields').innerHTML=(p?.parameters || []).map((f,i)=>{
    let control;
    if(f.type==='text')control=`<textarea rows="4" maxlength="8000" placeholder="留空使用工作流默认内容">${esc(f.default||'')}</textarea>`;
    else if(f.type==='choice')control=`<select>${f.choices.map(c=>`<option ${c===f.default?'selected':''}>${esc(c)}</option>`).join('')}</select>`;
    else if(f.type==='boolean')control=`<input type="checkbox" ${f.default?'checked':''}>`;
    else control=`<input type="number" step="${f.type==='integer'?'1':'any'}" ${f.min!==undefined?`min="${f.min}"`:''} ${f.max!==undefined?`max="${f.max}"`:''} value="${esc(f.default??'')}">`;
    return `<label class="field" data-parameter="${i}">${esc(f.label)}${control}${f.key==='fov'?'<small>默认 20°，适合标准多视图；实际照片可按拍摄视场角调整。</small>':''}</label>`;
  }).join('');
  document.querySelectorAll('[data-parameter]').forEach(label=>{const f=p.parameters[Number(label.dataset.parameter)];label.querySelector('input,select,textarea').required=!!f.required;});
  document.querySelectorAll('[data-key]').forEach(input=>input.onchange=()=>{
    const box=input.parentElement.querySelector('.input-previews');
    box.querySelectorAll('img').forEach(img=>{URL.revokeObjectURL(img.src);previewUrls=previewUrls.filter(u=>u!==img.src);});
    const urls=[...input.files].slice(0,8).map(f=>URL.createObjectURL(f));previewUrls.push(...urls);
    box.innerHTML=urls.map(u=>`<img src="${u}" alt="待上传参考图">`).join('');
  });
}
function renderLibrary() {
  el('workflow-cards').innerHTML=profiles.map((p,i)=>`<article class="workflow-card"><p class="workflow-meta">${p.builtin?'内置工作流':'自定义工作流'} · v${esc(p.version)}</p><h3>${esc(p.label)}</h3><p class="helper">${esc(p.description||'自定义图片或模型工作流')}</p><p class="helper">${(p.inputs||[]).length} 个图片输入 → ${Object.values(p.outputs||{}).includes('model')?'3D 模型':'图片结果'} · ${p.enabled?'已启用':'未启用'}</p><p class="status-line ${p.ready?'':'error'}">${p.ready?'● 依赖就绪':esc(p.message)}</p><div class="task-actions">${p.enabled?`<button class="button" data-use="${esc(p.id)}">使用工作流</button>`:''}${member?.admin?`<a class="button" href="/api/local/workflows/${encodeURIComponent(p.id)}/export?version=${encodeURIComponent(p.version)}">导出配置</a><button class="button" data-toggle="${i}">${p.enabled?'停用':'检查并启用'}</button>`:''}</div></article>`).join('');
  document.querySelectorAll('[data-use]').forEach(b=>b.onclick=()=>{el('kind').value=b.dataset.use;renderForm();view('create');window.scrollTo(0,0);});
  document.querySelectorAll('[data-toggle]').forEach(b=>b.onclick=async()=>{
    const p=profiles[Number(b.dataset.toggle)];b.disabled=true;
    try{await api(`/api/local/workflows/${encodeURIComponent(p.id)}/enabled`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:p.version,enabled:!p.enabled})});el('library-message').textContent='工作流状态已更新；已有任务继续使用提交时的版本。';await health();}
    catch(e){el('library-message').textContent=e.message;b.disabled=false;}
  });
}
async function health() {
  el('check').disabled=true;el('library-refresh').disabled=true;
  try{
    const data=await api('/api/local/status');profiles=data.profiles;enabled=data.enabled;
    const previous=el('kind').value;
    el('kind').innerHTML=profiles.filter(p=>p.enabled).map(p=>`<option value="${esc(p.id)}">${esc(p.label)} · v${esc(p.version)}</option>`).join('');
    if(profiles.some(p=>p.id===previous&&p.enabled))el('kind').value=previous;
    el('service').innerHTML=profiles.map(p=>`<div class="${p.ready?'':'error'}">${esc(p.label)}：${p.ready?'就绪':esc(p.message)}</div>`).join('');
    renderForm();renderLibrary();
  }catch(e){el('service').textContent=e.message;el('library-message').textContent=e.message;el('submit').disabled=true;}
  finally{el('check').disabled=false;el('library-refresh').disabled=false;}
}
function picture(url,label) {return `<figure><a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${esc(label)}" loading="lazy"></a><figcaption>${esc(label)}</figcaption></figure>`;}
function renderTasks() {
  const query=el('task-search').value.toLowerCase(),filter=el('task-filter').value;
  const visible=tasks.filter(t=>(filter==='all'||(filter==='complete'?t.status==='complete':filter==='failed'?failed.includes(t.status):!['complete',...failed].includes(t.status)))&&[t.label,t.owner,t.name,t.id].some(v=>String(v).toLowerCase().includes(query)));
  const signature=JSON.stringify([visible,member?.username,query,filter]);
  if(signature===taskSignature)return;taskSignature=signature;
  el('counts').textContent=String(visible.length);
  el('tasks').innerHTML=visible.length?visible.map(t=>{
    const base=`/api/local/tasks/${encodeURIComponent(t.id)}`;
    const inputs=t.inputs?.length?t.inputs.map(f=>picture(`${base}/inputs/${encodeURIComponent(f.key)}`,f.label)).join(''):picture(base+'/files/source','参考图');
    const artifacts=t.artifacts?.length?t.artifacts:[...(t.has_image?[{role:'image',url:base+'/files/image'}]:[]),...(t.has_model?[{role:'model',url:base+'/files/model'}]:[])];
    const url=a=>a.url||`${base}/artifacts/${encodeURIComponent(a.id)}`;
    const images=artifacts.filter(a=>a.role==='image').map((a,i)=>picture(url(a),'生成图片 '+(i+1))).join('');
    const models=artifacts.filter(a=>a.role==='model').map((a,i)=>`<button class="button" data-model="${url(a)}">旋转查看模型${i?' '+(i+1):''}</button><a class="button primary" href="${url(a)}">下载 GLB${i?' '+(i+1):''}</a>`).join('');
    return `<article class="task-card"><header><div><h3>${esc(t.title||t.name||t.label||t.kind)}</h3><small>${esc(t.label||t.kind)}</small><small class="task-owner">${esc(t.owner)} · ${esc(new Date(t.created).toLocaleString())} · v${esc(t.version||'1.0.0')}</small></div><span class="task-status ${esc(t.status)}">${esc(labels[t.status]||t.status)}</span></header><p class="task-id">${esc(t.id)}</p><p class="task-message">${esc(t.message||'任务已保存，等待执行')}</p>${t.prompt?`<p class="task-message">${esc(t.prompt)}</p>`:''}<div class="task-images">${inputs}${images}</div><div class="task-actions">${models}<a class="button" href="/?task=${encodeURIComponent(t.id)}">查看关联资产</a>${['failed','interrupted','waiting'].includes(t.status)&&(member?.admin||t.owner===member?.username)?`<button class="button" data-retry="${esc(t.id)}">重试任务</button>`:''}</div></article>`;
  }).join(''):'<div class="empty-state"><strong>这里会记录每一次创作</strong><span>提交新任务，或调整筛选条件查看已有结果。</span></div>';
  document.querySelectorAll('[data-model]').forEach(b=>b.onclick=()=>{el('model-error').textContent='';el('model-preview').src=b.dataset.model;el('model-dialog').showModal();});
  document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api(`/api/local/tasks/${b.dataset.retry}/retry`,{method:'POST'});await refresh();}catch(e){el('connection').textContent=e.message;b.disabled=false;}});
}
async function refresh() {
  try{
    const data=await api('/api/local/tasks');tasks=data.tasks;el('connection').textContent='';
    el('total-count').textContent=tasks.length;
    el('active-count').textContent=tasks.filter(t=>!['complete',...failed].includes(t.status)).length;
    el('complete-count').textContent=tasks.filter(t=>t.status==='complete').length;
    el('failed-count').textContent=tasks.filter(t=>failed.includes(t.status)).length;
    renderTasks();
  }catch(e){el('connection').textContent='连接暂时中断，将继续刷新：'+e.message;}
}
el('create-task').onsubmit=async event=>{
  event.preventDefault();const p=chosen();if(!p?.ready||!enabled)return;
  const body=new FormData(),keys=[],params={};let count=0;
  for(const input of document.querySelectorAll('[data-key]')){
    for(const f of input.files){if(f.size>20*1024*1024){el('submit-message').textContent='单张图片最多 20MB';return;}body.append('files',f);keys.push(input.dataset.key);count++;}
  }
  if(!count||count>50){el('submit-message').textContent='每批需要 1–50 张图片';return;}
  for(const label of document.querySelectorAll('[data-parameter]')){
    const f=p.parameters[Number(label.dataset.parameter)],input=label.querySelector('input,select,textarea');
    if(['integer','number'].includes(f.type)&&input.value==='')continue;
    params[f.key]=f.type==='boolean'?input.checked:['integer','number'].includes(f.type)?Number(input.value):input.value;
  }
  body.append('title',el('asset-name').value);body.append('kind',p.id);body.append('version',p.version);body.append('parameters',JSON.stringify(params));
  if(p.inputs.length>1)body.append('input_keys',JSON.stringify(keys));
  el('submit').disabled=true;el('submit-message').textContent='上传并保存任务…';
  try{const job=await api('/api/local/tasks',{method:'POST',body});el('submit-message').textContent=`已保存 ${job.progress.total} 项任务。关闭页面不影响生成。`;await refresh();window.dispatchEvent(new Event('assets-updated'));}
  catch(e){el('submit-message').textContent=e.message;}
  finally{el('submit').disabled=!chosen()?.ready||!enabled;}
};
el('kind').onchange=renderForm;el('check').onclick=health;el('library-refresh').onclick=health;el('refresh').onclick=refresh;
el('task-search').oninput=renderTasks;el('task-filter').onchange=renderTasks;
el('import-workflow').onsubmit=async event=>{
  event.preventDefault();const file=el('bundle-file').files[0];if(!file)return;
  if(file.size>5*1024*1024){el('library-message').textContent='工作流包最多 5MB';return;}
  const body=new FormData();body.append('file',file);el('import-button').disabled=true;
  try{const result=await api('/api/local/workflows/import',{method:'POST',body});el('library-message').textContent=`已导入 ${result.id} v${result.version}。请检查依赖后启用。`;await health();}
  catch(e){el('library-message').textContent=e.message;}finally{el('import-button').disabled=false;}
};
el('add-member').onsubmit=async event=>{event.preventDefault();try{await api('/api/team/members',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:el('new-name').value,password:el('new-password').value})});el('new-password').value='';el('member-message').textContent='成员已保存，请将账号交给同事。';}catch(e){el('member-message').textContent=e.message;}};

el('close-model').onclick=()=>el('model-dialog').close();el('model-dialog').addEventListener('close',()=>el('model-preview').removeAttribute('src'));
el('model-preview').addEventListener('error',()=>{el('model-error').textContent='浏览器无法预览此模型，可以下载 GLB 后检查。';});
(async()=>{member=await api('/api/team/me');el('task-search').value=new URLSearchParams(location.search).get('task')||'';el('admin-panel').hidden=!member.admin;view(['create','tasks','library'].includes(location.hash.slice(1))?location.hash.slice(1):'create');await Promise.all([refresh(),health()]);async function poll(){await refresh();setTimeout(poll,5000);}setTimeout(poll,5000);})().catch(e=>el('connection').textContent=e.message);
