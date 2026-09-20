// Read-only live acceptance. Optional renders provide coherent references for GPU smoke tests.
const {chromium} = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const errors = [];
  try {
    const page = await browser.newPage({viewport: {width: 1536, height: 1100}});
    page.on('pageerror', e => errors.push(e.message));
    await page.goto((process.env.WORKBENCH_URL || 'http://127.0.0.1:8765') + '/objects');
    await page.getByText('主机管理员', {exact: true}).waitFor();
    await page.waitForFunction(() => [...document.querySelector('#kind').options].some(o => o.value === 'multiview'));
    await page.locator('#kind').selectOption('multiview');
    assert.equal(await page.locator('[data-key]').count(), 4);
    assert.deepEqual(await page.locator('[data-key]').evaluateAll(nodes => nodes.map(n => n.dataset.key)), ['front','left','back','right']);
    assert.equal(await page.locator('#submit').isDisabled(), false);
    await page.screenshot({path: 'output/v1.1-create.png', fullPage: true});
    await page.locator('[data-view="library"]').click();
    assert.equal(await page.locator('.workflow-card').count() >= 4, true);
    assert.equal(await page.locator('#admin-panel').isVisible(), true);
    await page.screenshot({path: 'output/v1.1-workflows.png', fullPage: true});
    await page.locator('[data-view="tasks"]').click();
    await page.locator('#task-search').fill('no-such-task-acceptance');
    assert.equal(await page.locator('.task-card').count(), 0);
    await page.locator('#task-search').fill('');
    await page.locator('#task-filter').selectOption('complete');
    const previewButton = page.getByRole('button', {name: '旋转查看模型', exact: true}).first();
    await previewButton.click();
    await page.waitForFunction(() => document.querySelector('model-viewer').loaded, {}, {timeout: 60000});
    const model = await page.locator('model-viewer').evaluate(m => ({loaded: m.loaded, dimensions: m.getDimensions(), materials: m.model.materials.length}));
    if(process.env.RENDER_TEST_VIEWS === '1') {
      if(process.env.TEST_MODEL_URL) {
        await page.locator('model-viewer').evaluate((m,url) => new Promise((resolve,reject) => {m.addEventListener('load',resolve,{once:true});m.addEventListener('error',reject,{once:true});m.src=url;}),process.env.TEST_MODEL_URL);
      }
      const folder = path.resolve(process.env.RENDER_OUTPUT || 'output/multiview-smoke-inputs');
      fs.mkdirSync(folder, {recursive: true});
      const references = {};
      await page.locator('model-viewer').evaluate(m => {
        m.style.cssText = 'width:768px;height:768px;background:#000';
        m.setAttribute('shadow-intensity','0');
        m.setAttribute('field-of-view','20deg');
        m.setAttribute('interaction-prompt','none');
        m.setAttribute('max-camera-orbit','auto auto 100m');
        m.setAttribute('min-camera-orbit','auto auto 0.01m');
      });
      for(const [key, angle] of [['front',0],['left',90],['back',180],['right',270]]) {
        await page.locator('model-viewer').evaluate(async (m, angle) => {
          const d=m.getDimensions();
          const radius=Math.hypot(d.x,d.y,d.z)/2/Math.tan(Math.PI/18)*1.15;
          m.cameraOrbit = `${angle}deg 90deg ${radius}m`;
          await m.updateComplete;
          m.jumpCameraToGoal();
          for(let i=0;i<6;i++) await new Promise(requestAnimationFrame);
        }, angle);
        const file = path.join(folder, key+'.png');
        await page.locator('model-viewer').screenshot({path: file});
        references[key] = key+'.png';
      }
      fs.writeFileSync(path.join(folder,'inputs.json'), JSON.stringify(references,null,2));
    }
    await page.screenshot({path: 'output/v1.1-model.png'});
    await page.getByRole('button', {name:'关闭',exact:true}).click();
    await page.locator('[data-view="create"]').click();
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path:'output/v1.1-mobile.png',fullPage:true});
    assert.deepEqual(errors, []);
    const result = {model, fourViewInputs: true, library: true, filters: true, mobileNoOverflow: true, errors};
    fs.writeFileSync('output/browser-workflow-check.json',JSON.stringify(result,null,2));
    console.log(JSON.stringify(result));
  } finally {await browser.close();}
})().catch(e => {console.error(e);process.exitCode=1;});
