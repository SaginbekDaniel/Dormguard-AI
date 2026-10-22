'use strict';
// Real local API/browser integration against explicitly disposable fixtures.
// No production systems, external messages, or real camera footage are used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
if (process.env.DORMGUARD_BROWSER_RESULTS) fs.writeFileSync(process.env.DORMGUARD_BROWSER_RESULTS, JSON.stringify({status: 'RUNNING', started_at_utc: new Date().toISOString(), scope: 'Explicit disposable local API/browser verification'}) + '\n');
const {chromium} = require(process.env.DORMGUARD_PLAYWRIGHT_PATH || 'playwright');
const access = JSON.parse(fs.readFileSync(process.env.DORMGUARD_BROWSER_ACCESS || '/private/tmp/dormguard-browser-access.json', 'utf8'));
const origin = new URL(access.origin);
const screenshotDir = process.env.DORMGUARD_SCREENSHOT_DIR || '/private/tmp';
fs.mkdirSync(screenshotDir, {recursive: true});
if (access.disposable !== true || origin.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(origin.hostname)) throw new Error('Explicit disposable loopback fixture required.');
(async () => {
  const started = performance.now(); const browser = await chromium.launch({headless: true, ...(process.env.DORMGUARD_BROWSER_CHANNEL ? {channel: process.env.DORMGUARD_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage(); const errors = []; const csp = []; const failures = []; let checks = 0;
  const measurements = {scope: 'Local Chrome desktop with mobile viewport emulation; generated media; not real device, WAN, Telegram or field latency', incident_page_seconds: [], preview_startup_seconds: [], evidence_metadata_seconds: []};
  page.on('pageerror', e => errors.push(e.message)); page.on('console', msg => {if (/violates.*Content Security Policy|Refused to.*Content Security Policy/i.test(msg.text())) csp.push(msg.text());});
  async function check(name, fn) {await fn(); checks++; console.log(`PASS ${name}`);}
  async function nav(name) {if (await page.getByRole('button', {name: 'Open navigation'}).isVisible()) await page.getByRole('button', {name: 'Open navigation'}).click(); await page.getByRole('navigation', {name: 'Main navigation'}).getByRole('button', {name, exact: true}).click(); await page.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'));}
  try {
    await page.setViewportSize({width: 390, height: 900});
    await page.goto(origin.href + '#incidents/' + access.incident_id); await page.getByLabel('Email address').fill(access.email); await page.getByLabel('Password', {exact: true}).fill(access.password); await page.getByRole('button', {name: 'Sign in securely'}).click();
    await check('mobile Telegram-style link survives real password login', async () => {await page.getByRole('dialog').filter({has: page.getByRole('heading', {name: /Suspected/})}).waitFor();});
    await page.keyboard.press('Escape'); await nav('Overview');
    await check('rapid repeated incident links reopen safely at 390, 768 and 1440 pixels', async () => {for (const width of [390, 768, 1440, 390, 768, 1440]) {await page.setViewportSize({width, height: 1000}); const before = performance.now(); await page.goto(origin.href + '#incidents/' + access.incident_id); await page.getByRole('dialog').filter({has: page.getByRole('heading', {name: /Suspected/})}).waitFor(); measurements.incident_page_seconds.push({width, seconds: (performance.now() - before) / 1000}); assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); await page.keyboard.press('Escape');} await nav('Overview');});
    await check('real API session signs in and displays overview', async () => {await page.getByRole('heading', {name: 'Overview', exact: true}).waitFor(); await page.getByText('AI detection is not operational').waitFor(); assert.equal(await page.getByText('We could not load this view').count(), 0);});
    await page.screenshot({path: '/tmp/dormguard-real-overview-1440.png', fullPage: true});
    await check('real API overview at mobile and tablet widths', async () => {for (const width of [390, 768]) {await page.setViewportSize({width, height: 900}); assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); await page.screenshot({path: `/tmp/dormguard-real-overview-${width}.png`, fullPage: true});} await page.setViewportSize({width: 1440, height: 1000});});
    await check('all seven real API screens work at mobile, tablet and desktop widths', async () => {
      for (const width of [390, 768, 1024, 1440]) {
        await page.setViewportSize({width, height: 1000});
        for (const name of ['Overview', 'Live Cameras', 'Incidents', 'AI Analytics', 'Activity Journal', 'System Health', 'Settings']) {
          await nav(name); await page.getByRole('heading', {name, exact: true}).waitFor();
          assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), name + ' overflow at ' + width);
          assert.equal(await page.getByText('We could not load this view').count(), 0);
          await page.screenshot({path: screenshotDir + '/mvp-' + name.toLowerCase().replaceAll(' ', '-') + '-' + width + '.png', fullPage: true});
        }
      }
    });
    await check('mobile menu supports keyboard escape and focus restoration', async () => {
      await page.setViewportSize({width: 390, height: 900});
      const menu = page.getByRole('button', {name: 'Open navigation'});
      await menu.focus(); await page.keyboard.press('Enter');
      assert.equal(await menu.getAttribute('aria-expanded'), 'true');
      await page.keyboard.press('Escape');
      assert.equal(await menu.getAttribute('aria-expanded'), 'false');
      assert.equal(await menu.evaluate(node => document.activeElement === node), true);
      assert.equal(await page.locator('#sidebar').evaluate(node => node.inert), true);
      await page.setViewportSize({width: 1440, height: 1000});
    });
    await check('incident server search and UTC dates preserve real results', async () => {
      await nav('Incidents');
      await page.getByLabel('Search incidents').fill(access.incident_id);
      await page.getByRole('button', {name: 'Apply filters'}).click();
      await page.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'));
      assert.equal(await page.locator('tbody tr').count(), 1);
      await page.getByLabel('Search incidents').fill('no-such-incident');
      await page.getByRole('button', {name: 'Apply filters'}).click(); await page.getByText('No incidents match this view', {exact: true}).waitFor();
      await page.getByLabel('Search incidents').fill(''); await page.getByRole('button', {name: 'Apply filters'}).click();
      await page.locator('tbody tr').first().waitFor();
    });
    await check('journal reads actual append-only camera and access events', async () => {
      await nav('Activity Journal'); await page.getByLabel('Search activity journal').fill('camera.connected');
      await page.getByRole('button', {name: 'Search journal'}).click(); await page.getByText('Camera connected', {exact: true}).first().waitFor();
      await page.getByText('Audit chain verified', {exact: true}).waitFor();
      assert.equal(await page.getByRole('button', {name: /delete/i}).count(), 0);
    });
    await check('overview spotlight renders only an explicitly opened generated preview', async () => {
      await nav('Overview'); assert.equal(await page.locator('.camera-preview img').count(), 0);
      await page.getByRole('button', {name: 'Open live preview'}).click();
      await page.waitForFunction(() => {const img = document.querySelector('.camera-preview img'); return img && img.naturalWidth > 0;});
      await page.screenshot({path: screenshotDir + '/mvp-overview-protected-preview-1440.png', fullPage: true});
    });
    await nav('Live Cameras');
    const previewStarted = performance.now(); await page.getByRole('button', {name: 'Open live preview'}).first().click();
    await check('protected MJPEG renders actual generated video frames', async () => {await page.locator('.camera-preview img').first().waitFor({state: 'attached', timeout: 10000}); await page.waitForFunction(() => {const img = document.querySelector('.camera-preview img'); return img && img.naturalWidth > 0;}, null, {timeout: 15000});}).catch(error => {failures.push('protected MJPEG: ' + error.message); console.log('FAIL protected MJPEG; continuing independent incident checks');});
    measurements.preview_startup_seconds.push((performance.now() - previewStarted) / 1000);
    await check('fullscreen preserves protected preview and can exit', async () => {
      await page.getByRole('button', {name: 'Fullscreen camera'}).first().click();
      await page.waitForFunction(() => Boolean(document.fullscreenElement));
      await page.evaluate(() => document.exitFullscreen());
      await page.waitForFunction(() => !document.fullscreenElement);
    });
    await page.screenshot({path: '/tmp/dormguard-real-cameras-1440.png', fullPage: true});
    await nav('Incidents');
    await page.locator('tbody .row-link').first().click();
    await check('real persisted incident shows audited timeline and model provenance', async () => {await page.getByRole('dialog').filter({has: page.getByRole('heading', {name: /Suspected/})}).waitFor(); await page.getByText('CONTROLLED_BROWSER_FIXTURE_NOT_MODEL_INFERENCE', {exact: true}).waitFor(); await page.getByRole('heading', {name: 'Incident timeline', exact: true}).waitFor();});
    const evidenceStarted = performance.now(); await page.getByRole('button', {name: 'Play clip', exact: true}).first().click();
    await check('native evidence player decodes authorized derivative', async () => {await page.waitForFunction(() => {const video = document.querySelector('#detail-dialog video'); return video && video.readyState >= 1 && video.videoWidth > 0;}, null, {timeout: 15000});});
    measurements.evidence_metadata_seconds.push((performance.now() - evidenceStarted) / 1000);
    await page.getByRole('button', {name: 'Verify integrity', exact: true}).first().click();
    await check('real evidence SHA-256 verification passes', async () => {await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.').waitFor();});
    const noteText = `Browser integration review note ${Date.now()}`; await page.getByLabel('Reviewer note').fill(noteText); await page.getByRole('button', {name: 'Add note', exact: true}).click();
    await check('review note persists in SQL-backed timeline', async () => {await page.getByText(noteText, {exact: true}).waitFor(); assert.equal(await page.getByLabel('Reviewer note').inputValue(), '');});
    await page.getByRole('button', {name: 'Acknowledge', exact: true}).click(); await page.getByLabel('Reason for this action').fill('Controlled local browser integration: acknowledged for review.'); await page.getByRole('dialog', {name: 'Acknowledge', exact: true}).getByRole('button', {name: 'Acknowledge', exact: true}).click();
    await check('real legal transition is persisted with review reason', async () => {await page.getByRole('button', {name: 'Confirm incident', exact: true}).waitFor(); await page.getByText('Controlled local browser integration: acknowledged for review.', {exact: true}).waitFor();});
    await page.screenshot({path: '/tmp/dormguard-real-detail-1440.png', fullPage: true});
    await check('incident View camera opens its protected source', async () => {await page.getByRole('button', {name: 'View camera', exact: true}).click(); try {await page.waitForFunction(() => {const img = document.querySelector('.camera-preview img'); return img && img.naturalWidth > 0;}, null, {timeout: 15000});} catch (error) {await page.screenshot({path: '/private/tmp/dormguard-preview-failure.png', fullPage: true}); const diagnostics = await page.evaluate(() => ({headings: Array.from(document.querySelectorAll('h1,h2')).map(n => n.textContent), previews: Array.from(document.querySelectorAll('.camera-preview')).map(n => ({text: n.textContent, pending: n.hasAttribute('data-opening-stream'), image: Boolean(n.querySelector('img')), width: n.querySelector('img')?.naturalWidth})), busy: document.getElementById('main')?.getAttribute('aria-busy'), toast: document.getElementById('toast')?.textContent})); console.log('CONTROLLED_FIXTURE_PREVIEW_DIAGNOSTICS', JSON.stringify(diagnostics)); console.log('CONTROLLED_FIXTURE_CAMERA_HEALTH', JSON.stringify((await (await context.request.get(origin.origin + '/api/v1/cameras')).json()).items.map(c => c.health))); throw error;}});
    await nav('Settings');
    await check('real integration unavailability and audit integrity are explicit', async () => {await page.getByText('Telegram is not configured on the server.', {exact: false}).waitFor(); await page.getByText('Audit chain verified', {exact: true}).waitFor(); assert.equal(await page.getByRole('button', {name: 'Link Telegram account'}).count(), 0);});
    const cameraTarget = (await (await context.request.get(origin.origin + '/api/v1/cameras')).json()).items[0].id;
    await page.getByRole('button', {name: 'Sign out', exact: true}).click();
    await check('real session logout returns to protected login', async () => {await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor(); const response = await context.request.get(origin.origin + '/api/v1/auth/me'); assert.equal(response.status(), 401);});
    await check('mobile protected camera link survives real password login', async () => {
      await page.setViewportSize({width: 390, height: 900}); await page.goto(origin.href + '#cameras/' + cameraTarget);
      await page.getByLabel('Email address').fill(access.email); await page.getByLabel('Password', {exact: true}).fill(access.password); await page.getByRole('button', {name: 'Sign in securely'}).click();
      await page.waitForFunction(() => {const img = document.querySelector('.camera-preview img'); return img && img.naturalWidth > 0;}, null, {timeout: 15000});
      assert.equal(await page.getByLabel('Select camera').inputValue(), cameraTarget);
      await page.getByRole('button', {name: 'Sign out', exact: true}).click();
    });
    await check('no uncaught JavaScript errors or CSP violations', async () => {assert.deepEqual(errors, []); assert.deepEqual(csp, []);});
    console.log(`PASS ${checks} real local API browser checks in ${((performance.now() - started) / 1000).toFixed(2)}s; generated media and controlled incident fixture, no model/provider claims.`);
    if (process.env.DORMGUARD_BROWSER_RESULTS) fs.writeFileSync(process.env.DORMGUARD_BROWSER_RESULTS, JSON.stringify({status: failures.length ? 'FAIL' : 'PASS', checks, failures, runtime_seconds: (performance.now() - started) / 1000, measurements}, null, 2) + '\n');
    if (failures.length) throw new Error(failures.join('\n'));
  } finally {await context.close(); await browser.close();}
})().catch(error => {if (process.env.DORMGUARD_BROWSER_RESULTS) fs.writeFileSync(process.env.DORMGUARD_BROWSER_RESULTS, JSON.stringify({status: 'FAIL', finished_at_utc: new Date().toISOString(), error_type: error.name, reason: 'Browser acceptance did not complete; inspect the current command output.'}, null, 2) + '\n'); console.error(error); process.exitCode = 1;});
