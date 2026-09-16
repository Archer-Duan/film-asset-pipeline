// Read-only acceptance against a running workbench containing a completed model.
const {chromium}=require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const fs=require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:"msedge"});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:8765/objects');
    await page.getByText('主机管理员',{exact:true}).waitFor();
    await page.getByRole('button',{name:'旋转查看模型'}).first().waitFor();
    await page.getByText('补全优化图片：就绪',{exact:true}).waitFor();
    await page.screenshot({path:'output/local-workbench.png',fullPage:true});
    await page.getByRole('button',{name:'旋转查看模型'}).first().click();
    await page.waitForFunction(()=>document.querySelector('model-viewer').loaded,{},{timeout:60000});
    const model=await page.locator('model-viewer').evaluate(el=>({loaded:el.loaded,dimensions:el.getDimensions(),materials:el.model.materials.length}));
    await page.screenshot({path:'output/local-model-preview.png'});
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.goto('http://127.0.0.1:8765/');
    await page.getByRole('link',{name:'物体资产 · 本地生成'}).waitFor();
    const workspace=await page.evaluate(async()=>await (await fetch('/api/workspace')).json());
    const ids=workspace.assets.map(a=>a.asset_id);
    if(new Set(ids).size!==ids.length)throw new Error('Duplicate asset ids');
    let lanLoginRedirect = null;
    if(process.env.OFFICE_URL){const remote=await browser.newPage();await remote.goto(process.env.OFFICE_URL+"/objects");await remote.waitForURL("**/login");lanLoginRedirect=true;}
    const result={model,assets:ids.length,lanLoginRedirect,page_errors:errors};
    fs.writeFileSync('output/browser-local-check.json',JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
    if(errors.length)throw new Error(errors.join('; '));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
