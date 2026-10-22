'use strict';
// Actual pretrained worker in scripts/run_demo.py inference mode. No API/model doubles.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.DORMGUARD_PLAYWRIGHT_PATH || 'playwright');
const access = JSON.parse(fs.readFileSync(process.env.DORMGUARD_BROWSER_ACCESS, 'utf8'));
const origin = new URL(access.origin);
if (access.disposable !== true || access.mode !== 'inference' || origin.hostname !== '127.0.0.1') throw new Error('Explicit disposable inference demo required');
const output = process.env.DORMGUARD_BROWSER_RESULTS;
if (output) fs.writeFileSync(output, JSON.stringify({status: 'RUNNING'}) + '\n');
(async () => {
  const started = performance.now(); const checks = []; const errors = [];
  const browser = await chromium.launch({headless: true, ...(process.env.DORMGUARD_BROWSER_CHANNEL ? {channel: process.env.DORMGUARD_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage(); page.on('pageerror', e => errors.push(e.message));
  async function check(name, fn) {await fn(); checks.push(name); console.log('PASS ' + name);}
  try {
    assert.equal((await context.request.get(origin.origin + '/api/v1/incidents')).status(), 401);
    await page.goto(origin.href); await page.getByLabel('Email address').fill(access.email); await page.getByLabel('Password', {exact: true}).fill(access.password); await page.getByRole('button', {name: 'Sign in securely'}).click();
    await check('real-inference demo is visibly labeled and authenticates', async () => {await page.getByText('Prerecorded Demo — Real Model Inference', {exact: false}).waitFor();});
    await check('actual model worker reports processed windows', async () => {
      await page.waitForFunction(async () => {const response = await fetch('/api/v1/system'); const data = await response.json(); return data.workers.some(w => w.name === 'inference' && Object.values(w.metrics.per_camera || {}).some(c => c.analyzed_windows > 0));}, null, {timeout: 60000});
    });
    const response = await context.request.get(origin.origin + '/api/v1/incidents'); const incidents = (await response.json()).items;
    if (!incidents.length) {
      const result = {status: 'PASS_INFERENCE_NO_DETECTION', checks, runtime_seconds: (performance.now() - started) / 1000, scope: 'Actual pretrained execution; no synthetic incident substituted; downstream workflow separately verified by controlled fixture'};
      if (output) fs.writeFileSync(output, JSON.stringify(result, null, 2) + '\n'); return;
    }
    const incident = incidents[0]; assert.equal(incident.model, 'jaranohaal-vit-frame-screening');
    await page.goto(origin.href + '#incidents/' + incident.id);
    await check('actual pretrained incident opens in browser', async () => {await page.getByText('jaranohaal-vit-frame-screening', {exact: true}).waitFor();});
    await page.getByRole('button', {name: 'Play clip', exact: true}).first().click();
    await check('real model evidence decodes and verifies its SHA-256', async () => {
      await page.waitForFunction(() => document.querySelector('#detail-dialog video')?.videoWidth > 0);
      await page.getByRole('button', {name: 'Verify integrity', exact: true}).first().click(); await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.').first().waitFor();
    });
    await page.getByLabel('Reviewer note').fill('Reviewed public-domain boxing demo. Consensual sport; not a dormitory conflict.'); await page.getByRole('button', {name: 'Add note', exact: true}).click();
    await check('human review note persists for actual model event', async () => {await page.getByText('Reviewed public-domain boxing demo. Consensual sport; not a dormitory conflict.', {exact: true}).waitFor();});
    await page.getByRole('button', {name: 'Acknowledge', exact: true}).click(); await page.getByLabel('Reason for this action').fill('Acknowledged for demo review, no disciplinary conclusion.');
    await page.getByRole('dialog', {name: 'Acknowledge', exact: true}).getByRole('button', {name: 'Acknowledge', exact: true}).click();
    await check('actual model event has human acknowledgement and valid audit chain', async () => {await page.getByRole('button', {name: 'Confirm incident', exact: true}).waitFor(); assert.equal((await (await context.request.get(origin.origin + '/api/v1/audit')).json()).integrity.valid, true);});
    await page.screenshot({path: process.env.DORMGUARD_SCREENSHOT_DIR + '/final-real-inference-detail-1440.png', fullPage: true});
    await page.keyboard.press('Escape'); await page.getByRole('button', {name: 'Sign out', exact: true}).click();
    await check('real-inference session logs out and all protected media is removed', async () => {await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor(); assert.equal(await page.locator('video').count(), 0); assert.equal((await context.request.get(origin.origin + '/api/v1/incidents')).status(), 401);});
    assert.deepEqual(errors, []);
    if (output) fs.writeFileSync(output, JSON.stringify({status: 'PASS', checks, incident_model: incident.model, actual_score: incident.score, runtime_seconds: (performance.now() - started) / 1000, scope: 'Actual pinned ViT, public-domain boxing sports proxy, real local API/SQL/media/Chrome; no field quality or external provider claim'}, null, 2) + '\n');
  } finally {await context.close(); await browser.close();}
})().catch(error => {if (output) fs.writeFileSync(output, JSON.stringify({status: 'FAIL', error_type: error.name, message: error.message}, null, 2) + '\n'); console.error(error); process.exitCode = 1;});
