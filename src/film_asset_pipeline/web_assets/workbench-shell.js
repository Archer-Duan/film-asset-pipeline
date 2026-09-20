// Shared navigation and account control for generation and asset review pages.
(() => {
  const generation = location.pathname === '/objects';
  const stages = [
    ['all','全部资产','total'],['source_frames','原始素材','source-frames'],
    ['awaiting_2d','待处理图片','awaiting-2d'],['image_generation','图片生成中','image-generation'],
    ['image_review','图片待审核','image-review'],['ready_for_3d','待生成模型','ready-3d'],
    ['model_generation','模型生成中','model-generation'],['model_review','模型待审核','model-review'],
    ['approved','已通过模型','approved'],['rejected','审核未通过','rejected'],['generation_failed','生成失败','generation-failed'],
  ];
  const sidebar = document.querySelector('.sidebar');
  sidebar.innerHTML = `<a class="shell-brand" href="/objects"><span class="shell-logo">F</span><span>FILM ASSET<small>电影数字资产工作台</small></span></a><p class="shell-section">工作空间</p><nav class="shell-primary" aria-label="工作台导航">${[['create','＋','创建资产'],['tasks','▤','任务中心']].map(([v,i,l])=>generation?`<button class="nav-item" data-view="${v}">${i}　${l}</button>`:`<a class="nav-item" href="/objects#${v}">${i}　${l}</a>`).join('')}<a class="nav-item ${generation?'':'active'}" href="/">▦　资产库</a>${generation?'<button class="nav-item" data-view="library">◇　工作流设置</button>':'<a class="nav-item" href="/objects#library">◇　工作流设置</a>'}</nav><p class="shell-section">资产阶段</p><nav class="stage-nav" aria-label="资产阶段">${stages.map(([key,label,id])=>generation?`<a class="stage-link" href="/?stage=${key}"><span>${label}</span><b id="nav-${id}">—</b></a>`:`<button class="stage-link ${key==='all'?'is-active':''}" data-stage="${key}"><span>${label}</span><b id="nav-${id}">0</b></button>`).join('')}</nav><div class="sidebar-foot"><span class="service-dot"></span><div><strong>共享工作空间</strong><span id="service-status">本地 ComfyUI 算力</span></div></div>`;
  const account = document.getElementById('account-slot');
  if(account) {
    account.innerHTML='<a class="button" href="/login">用户登录</a>';
    fetch('/api/team/me').then(async r=>r.ok?r.json():null).then(me=>{
      if(!me?.username)return;
      account.innerHTML='<details class="account-menu"><summary class="button"><span class="account-avatar">●</span><span id="account-name"></span> ▾</summary><div class="account-options"><small id="account-role"></small><a href="/login">切换账号</a><button id="account-signout" type="button">退出登录</button></div></details>';
      document.getElementById('account-name').textContent=me.admin?'主机管理员':me.username;
      document.getElementById('account-role').textContent=me.admin?'本机自动登录 · 管理员':'团队成员';
      document.getElementById('account-signout').hidden=me.admin;
      document.getElementById('account-signout').onclick=async()=>{await fetch('/api/team/logout',{method:'POST'});location.href='/login';};
    }).catch(()=>{});
  }
  if(generation) {
    async function counts() {
      try {
        const r=await fetch('/api/workspace');if(!r.ok)return;
        const data=await r.json();
        for(const [key,,id] of stages)document.getElementById('nav-'+id).textContent=data.summary[key==='all'?'total':key]||0;
      } catch (_) { /* Current counts remain visible during reconnect. */ }
    }
    counts();window.addEventListener('assets-updated',counts);
    async function poll(){await counts();setTimeout(poll,15000);}setTimeout(poll,15000);
  }
})();
