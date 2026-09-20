// Opt-in real GPU acceptance: set RUN_GPU_SMOKE=1 and MULTIVIEW_INPUT_DIR.
const {chromium} = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  assert.equal(process.env.RUN_GPU_SMOKE, '1', 'Explicit RUN_GPU_SMOKE=1 required');
  assert.ok(process.env.MULTIVIEW_INPUT_DIR, 'MULTIVIEW_INPUT_DIR required');
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  try {
    const page = await browser.newPage({viewport: {width:1536,height:1100}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto((process.env.WORKBENCH_URL || 'http://127.0.0.1:8765')+'/objects');
    await page.waitForFunction(()=>[...document.querySelector('#kind').options].some(o=>o.value==='multiview'));
    await page.locator('#kind').selectOption('multiview');
    for(const key of ['front','left','back','right']) {
      await page.locator(`[data-key="${key}"]`).setInputFiles(path.resolve(process.env.MULTIVIEW_INPUT_DIR,key+'.png'));
    }
    assert.equal(await page.locator('#image-inputs img').count(),4);
    await page.screenshot({path:'output/v1.1-uploaded-views.png',fullPage:true});
    const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/local/tasks')&&r.request().method()==='POST');
    await page.locator('#submit').click();
    const response=await responsePromise;
    const result=await response.json();
    assert.equal(response.status(),202,JSON.stringify(result));
    assert.equal(result.progress.total,1);
    const all=await page.evaluate(async()=>await (await fetch('/api/local/tasks')).json());
    const task=all.tasks.find(t=>t.batch===result.job_id);
    assert.ok(task);
    assert.equal(task.kind,'multiview');
    assert.deepEqual(task.inputs.map(f=>f.key),['front','left','back','right']);
    assert.deepEqual(errors,[]);
    const report={...result,task_id:task.id,browser_errors:errors};
    fs.writeFileSync('output/multiview-browser-submission.json',JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
