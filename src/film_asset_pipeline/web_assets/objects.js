const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'等待排队',submitting:'提交中',submitted:'ComfyUI 排队中',running:'GPU 计算中',waiting:'等待恢复',complete:'生成完成',failed:'失败',interrupted:'已中断',attention:'需管理员核查'};
const kinds = {edit:'补全优化图片',model:'单图生模',full:'补图并生模',multiview:'多视图生模'};
let member = null;
let readiness = {};
let previewUrls = [];
async function api(url, options={}) {
  const r=await fetch(url,options);
  if(r.status===401){location.href='/login';throw new Error('请先登录');}
  const data=await r.json();
  if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'请求参数不正确');
  return data;
}
async function health(){
  el('service').textContent='正在检查…';
  try{const data=await api('/api/local/status');readiness=Object.fromEntries(data.profiles.map(p=>[p.id,p]));
    el('service').innerHTML=data.profiles.map(p=>`<div class="${p.ready?'':'error'}">${esc(p.label)}：${p.ready?'就绪':esc(p.message)}</div>`).join('');
  }catch(e){el('service').textContent=e.message;}
}
async function refresh(){
  try{const data=await api('/api/local/tasks');el('connection').textContent='';
    el('counts').textContent=`${data.tasks.length} 项`;
    el('tasks').innerHTML=data.tasks.length?data.tasks.map(t=>{
      const base=`/api/local/tasks/${encodeURIComponent(t.id)}/files/`;
      return `<article class="task-card"><header><div><h3>${esc(kinds[t.kind])}</h3><small>${esc(t.owner)} · ${esc(new Date(t.created).toLocaleString())}</small></div><span class="task-status ${esc(t.status)}">${esc(labels[t.status]||t.status)}</span></header><p class="task-id">${esc(t.id)}</p><p class="task-message">${esc(t.message||'任务已保存，等待执行')}</p>${t.prompt?`<p class="task-message">提示词：${esc(t.prompt)}</p>`:''}<div class="task-images"><figure><a href="${base}source" target="_blank" rel="noopener"><img src="${base}source" alt="参考图" loading="lazy"></a><figcaption>参考图</figcaption></figure>${t.has_image?`<figure><a href="${base}image" target="_blank" rel="noopener"><img src="${base}image" alt="补全结果" loading="lazy"></a><figcaption>补全结果</figcaption></figure>`:''}</div><div class="task-actions">${t.has_image?`<a class="button" href="/">进入图片审核</a>`:''}${t.has_model?`<button class="button" data-model="${base}model">旋转查看模型</button><a class="button button-primary" href="${base}model">下载 GLB</a><a class="button" href="/">模型审核</a>`:''}${['failed','interrupted','waiting'].includes(t.status)&&(member?.admin||t.owner===member?.username)?`<button class="button" data-retry="${esc(t.id)}">重试任务</button>`:''}</div></article>`;
    }).join(''):'<div class="empty-state"><strong>开始制作第一件物体资产</strong><span>上传参考图，选择补图或生模，结果会保存在这里。</span></div>';
    document.querySelectorAll('[data-model]').forEach(b=>b.onclick=()=>{el('model-error').textContent='';el('model-preview').src=b.dataset.model;el('model-dialog').showModal();});
    document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api(`/api/local/tasks/${b.dataset.retry}/retry`,{method:'POST'});await refresh();}catch(e){el('connection').textContent=e.message;}});
  }catch(e){el('connection').textContent='连接暂时中断，将继续刷新：'+e.message;}
}
el('create-task').onsubmit=async(event)=>{
  event.preventDefault();const kind=el('kind').value;const files=[...el('files').files];
  if(files.length>50||files.some(f=>f.size>20*1024*1024)){el('submit-message').textContent='每批最多 50 张，单张最多 20MB';return;}
  if(readiness[kind]&&!readiness[kind].ready){el('submit-message').textContent=readiness[kind].message;return;}
  const body=new FormData();files.forEach(f=>body.append('files',f));body.append('kind',kind);body.append('prompt',kind==='model'?'':el('prompt').value);
  el('submit').disabled=true;el('submit-message').textContent='上传并保存任务…';
  try{const job=await api('/api/local/tasks',{method:'POST',body});el('submit-message').textContent=`任务已保存：${job.job_id}。关闭页面不影响生成。`;await refresh();}catch(e){el('submit-message').textContent=e.message;}finally{el('submit').disabled=false;}
};
el('files').onchange=()=>{previewUrls.forEach(URL.revokeObjectURL);previewUrls=[...el('files').files].slice(0,12).map(f=>URL.createObjectURL(f));el('input-previews').innerHTML=previewUrls.map(u=>`<img src="${u}" alt="待上传图片">`).join('');};
el('kind').onchange=()=>{el('prompt-field').hidden=el('kind').value==='model';};
el('refresh').onclick=refresh;el('check').onclick=health;
el('logout').onclick=async()=>{await api('/api/team/logout',{method:'POST'});location.href='/login';};
el('add-member').onsubmit=async(e)=>{e.preventDefault();try{await api('/api/team/members',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:el('new-name').value,password:el('new-password').value})});el('new-password').value='';el('member-message').textContent='成员已保存，请将账号交给同事。';}catch(e){el('member-message').textContent=e.message;}};
(async()=>{member=await api('/api/team/me');el('member').textContent=member.admin?'主机管理员':member.username;el('admin-panel').hidden=!member.admin;el('logout').hidden=member.admin;await refresh();health();setInterval(refresh,5000);})().catch(e=>{el('connection').textContent=e.message;});

el('close-model').onclick=()=>{el('model-dialog').close();el('model-preview').removeAttribute('src');};
el('model-preview').addEventListener('error',()=>{el('model-error').textContent='浏览器无法预览此模型，可以下载 GLB 后检查。';});
