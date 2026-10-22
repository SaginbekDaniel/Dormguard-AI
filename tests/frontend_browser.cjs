'use strict';
// Controlled browser API doubles only. This verifies client behavior, not real
// authentication, camera ingest, model quality, provider delivery, or evidence.
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DORMGUARD_PLAYWRIGHT_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
let role = 'director';
let signedIn = false;
let state = 'DETECTED';
let noteCount = 0;
let sse;
let systemFailure = false;
let telegramLinked = false;
let streamClosed = 0;
let verificationRequest;
const writes = [];
const now = new Date().toISOString();
const camera = {id: 'cam-1', name: 'North hall <img src=x onerror=alert(1)>', location: 'First floor', kind: 'test', enabled: true, health: {state: 'active', detail: 'Controlled frontend test source'}};
const incident = () => ({id: 'inc-1', camera_id: camera.id, camera_name: camera.name, location: camera.location, status: state, score: 0.843, detected_at: now, model: 'controlled-test-double', events: Array.from({length: noteCount + 1}, (_, i) => ({kind: i ? 'note' : 'detection', new_state: i ? null : 'DETECTED', actor_id: i ? 'reviewer-2' : null, reason: i ? 'External reviewer update <script>test</script>' : null, created_at: now})), evidence: [{id: 'ev-1', incident_id: 'inc-1', size_bytes: 1024, sha256: 'a'.repeat(64), created_at: now}]});
const config = {inference_fps: 2, max_inference_queue: 4, model_device: 'cpu', detection_threshold: 0.8, incident_cooldown_seconds: 60, model_configured: false, model_rights_confirmed: false};
const server = http.createServer(async (req, res) => {
  if (req.url === '/' || req.url.startsWith('/?')) {res.setHeader('Content-Type', 'text/html'); res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'"); return res.end(fs.readFileSync(path.join(root, 'dormguard/index.html')));}
  if (req.url === '/assets/app.js' || req.url === '/assets/style.css') {res.setHeader('Content-Type', req.url.endsWith('.js') ? 'text/javascript' : 'text/css'); return res.end(fs.readFileSync(path.join(root, 'dormguard', path.basename(req.url))));}
  const url = new URL(req.url, 'http://localhost'); const p = url.pathname.replace('/api/v1', '');
  res.setHeader('Content-Type', 'application/json');
  function send(value, status = 200) {res.statusCode = status; res.end(JSON.stringify(value));}
  if (p === '/auth/config') return send({mode: 'local', configured: true, signup_enabled: false, evaluation: true});
  if (p === '/auth/login') {signedIn = true; return send({ok: true});}
  if (!signedIn) return send({detail: 'Authentication required'}, 401);
  if (p === '/auth/me') return send({id: 'user-1', email: 'director@example.test', role, csrf_token: 'controlled-csrf-token'});
  if (req.method !== 'GET') {let body = ''; for await (const chunk of req) body += chunk; writes.push({path: p, method: req.method, csrf: req.headers['x-csrf-token'], body: body ? JSON.parse(body) : null}); if (req.headers['x-csrf-token'] !== 'controlled-csrf-token') return send({detail: 'CSRF required'}, 403);}
  if (p === '/auth/logout') {signedIn = false; return send({ok: true});}
  if (p === '/overview') return send({open_incidents: 1, cameras_total: 1, cameras_active: 1, total_incidents: 1, recent_incidents: [incident()], trends: [{date: '2026-10-20', count: 1}], workers: []});
  if (p === '/system') return systemFailure ? send({detail: 'Controlled system failure'}, 503) : send({auth_mode: 'local', configuration: config, workers: [{name: 'camera', state: 'stale', detail: 'Controlled stale worker'}]});
  if (p === '/cameras') return send(req.method === 'POST' ? {...camera, id: 'cam-2'} : {items: [camera]});
  if (p.endsWith('/stream-session')) return send({url: '/api/v1/streams/test-ticket', expires_at: Date.now() / 1000 + 300});
  if (p === '/streams/test-ticket') {res.setHeader('Content-Type', 'multipart/x-mixed-replace; boundary=frame'); res.write('--frame\r\n'); req.on('close', () => streamClosed++); return;}
  if (p === '/incidents') return send({items: [incident()]});
  if (p === '/incidents/inc-1') return send(incident());
  if (p.startsWith('/incidents/inc-1/')) {const action = p.split('/').at(-1); if (action === 'opened') return send({recorded: true}); if (action === 'notes') noteCount++; else state = {review: 'PENDING_REVIEW', acknowledge: 'ACKNOWLEDGED', respond: 'RESPONDING', confirm: 'CONFIRMED', 'false-positive': 'FALSE_POSITIVE', resolve: 'RESOLVED', close: 'CLOSED'}[action]; return send(incident());}
  if (p === '/evidence') return send({items: incident().evidence});
  if (p === '/evidence/ev-1/verify') {if (verificationRequest) return verificationRequest(() => send({valid: true})); return send({valid: true});}
  if (p === '/integrations') return send({telegram: {configured: true, linked: telegramLinked, linked_at: telegramLinked ? now : null, bot_username: 'test_bot'}});
  if (p === '/integrations/telegram/test') return send({accepted: true, device_delivery: 'unverified'});
  if (p === '/integrations/telegram/link') return send({token: 'CONTROLLED-LINK-TOKEN', url: 'https://t.me/test_bot?start=controlled', expires_at: now});
  if (p === '/audit') return send({items: [], integrity: true});
  if (p === '/notifications') return send({items: []});
  if (p === '/events') {res.setHeader('Content-Type', 'text/event-stream'); res.setHeader('Cache-Control', 'no-cache'); res.write(': test-double connection\n\n'); sse = res; req.on('close', () => {if (sse === res) sse = null;}); return;}
  return send({detail: 'Controlled route not configured'}, 404);
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, ...(process.env.DORMGUARD_BROWSER_CHANNEL ? {channel: process.env.DORMGUARD_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage(); const errors = []; const csp = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', msg => {if (msg.text().includes('Content Security Policy') || msg.text().includes('violates')) csp.push(msg.text());});
  let checks = 0; let retiredPreview;
  async function check(name, fn) {await fn(); checks++; process.stdout.write(`PASS ${name}\n`);}
  async function navigate(name) {if (name === 'Evidence') {await navigate('Incidents'); await page.getByRole('button', {name: 'Evidence library'}).click(); await page.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy')); return;} if (await page.getByRole('button', {name: 'Open navigation'}).isVisible()) await page.getByRole('button', {name: 'Open navigation'}).click(); await page.getByRole('navigation', {name: 'Main navigation'}).getByRole('button', {name, exact: true}).click(); await page.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'));}
  try {
    await page.goto(origin + '/#incidents/inc-1');
    await check('provision-only authentication screen', async () => {await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor(); assert.equal(await page.getByText('Access is by invitation').count(), 1); assert.equal(await page.getByRole('button', {name: /sign up/i}).count(), 0);});
    await page.screenshot({path: '/tmp/dormguard-login-1440.png', fullPage: true});
    await page.getByLabel('Email address').fill('director@example.test'); await page.getByLabel('Password', {exact: true}).fill('controlled-test-password'); await page.getByRole('button', {name: 'Sign in securely'}).click();
    await check('Telegram hash deep link survives sign-in and opens intended incident', async () => {await page.getByRole('dialog', {name: 'Suspected physical altercation'}).waitFor({timeout: 3000});});
    await check('incident URL survives reload and close removes its target', async () => {assert.equal(new URL(page.url()).hash, '#incidents/inc-1'); await page.reload(); await page.getByRole('dialog', {name: 'Suspected physical altercation'}).waitFor(); await page.keyboard.press('Escape'); await page.waitForFunction(() => location.hash === '#incidents');});
    await page.keyboard.press('Escape'); await navigate('Overview');
    await check('authoritative overview and explicit model unavailability', async () => {await page.getByRole('heading', {name: 'Overview', exact: true}).waitFor(); await page.getByText('AI detection is not operational').waitFor(); assert.equal(await page.locator('img[onerror]').count(), 0); assert.equal(await page.getByText(camera.name, {exact: true}).count() > 0, true);});
    await page.screenshot({path: '/tmp/dormguard-overview-1440.png', fullPage: true});
    await check('responsive 390px and 768px without page overflow', async () => {for (const width of [390, 768]) {await page.setViewportSize({width, height: 900}); assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); await page.screenshot({path: `/tmp/dormguard-overview-${width}.png`, fullPage: true});} await page.setViewportSize({width: 1440, height: 1000});});
    await check('seven destinations render at every supported width', async () => {
      for (const width of [390, 768, 1440]) {
        await page.setViewportSize({width, height: 1000});
        for (const name of ['Overview', 'Live Cameras', 'Incidents', 'AI Analytics', 'Activity Journal', 'System Health', 'Settings']) {
          await navigate(name); await page.getByRole('heading', {name, exact: true}).waitFor();
          assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), name + ' overflow at ' + width);
          assert.equal(await page.getByText('We could not load this view').count(), 0);
        }
      }
    });
    await check('AI and system screens preserve unknown measurements', async () => {
      await navigate('AI Analytics'); await page.getByText('No processing measurements yet').waitFor();
      await page.getByText('Field accuracy is not established').waitFor();
      await navigate('System Health'); assert.equal(await page.getByText('Unknown', {exact: true}).count() > 0, true);
    });
    await check('journal search uses authoritative filters', async () => {
      await navigate('Activity Journal'); await page.getByLabel('Search activity journal').fill('camera.connected');
      await page.getByRole('button', {name: 'Search journal'}).click();
      await page.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'));
      assert.equal(await page.getByLabel('Search activity journal').inputValue(), 'camera.connected');
    });
    await navigate('Incidents');
    await page.getByRole('button', {name: 'Suspected physical altercation', exact: true}).click();
    await page.getByRole('dialog', {name: 'Suspected physical altercation'}).waitFor();
    await page.getByLabel('Reviewer note').fill('Draft preserved during another reviewer update.');
    noteCount++; sse.write('data: {"revision":2}\n\n');
    await check('SSE refresh updates open detail and preserves draft', async () => {await page.getByText('External reviewer update <script>test</script>', {exact: true}).waitFor(); assert.equal(await page.getByLabel('Reviewer note').inputValue(), 'Draft preserved during another reviewer update.');});
    await page.getByRole('button', {name: 'Acknowledge', exact: true}).click();
    await page.getByLabel('Reason for this action').fill('On-site staff are reviewing the incident.');
    noteCount++; sse.write('data: {"revision":3}\n\n');
    await check('reason dialog survives background detail invalidation', async () => {await page.waitForTimeout(100); assert.equal(await page.getByLabel('Reason for this action').inputValue(), 'On-site staff are reviewing the incident.');});
    await page.getByRole('dialog', {name: 'Acknowledge', exact: true}).getByRole('button', {name: 'Acknowledge', exact: true}).click();
    await check('audited state transition carries CSRF and reason', async () => {await page.getByRole('button', {name: 'Confirm incident', exact: true}).waitFor(); assert(writes.some(w => w.path === '/incidents/inc-1/acknowledge' && w.csrf === 'controlled-csrf-token' && w.body.reason.includes('On-site staff')));});
    await page.screenshot({path: '/tmp/dormguard-detail-1440.png', fullPage: true});
    await page.getByRole('button', {name: 'Verify integrity', exact: true}).click();
    await check('evidence hash verification renders result', async () => {await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.').waitFor();});
    await check('pending evidence verification survives a background detail refresh', async () => {
      const received = new Promise(resolve => {verificationRequest = resolve;});
      await page.getByRole('button', {name: 'Verify integrity', exact: true}).click();
      const finish = await received; verificationRequest = null;
      try {
        noteCount++; sse.write('data: {"revision":41}\n\n');
        await page.waitForFunction(expected => document.querySelectorAll('.timeline li').length === expected, noteCount + 1);
        assert.equal(await page.getByRole('button', {name: 'Verify integrity', exact: true}).isDisabled(), true);
        await page.getByText('Checking the stored original against its recorded hash…').waitFor({timeout: 2000});
      } finally {finish();}
      await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.').waitFor({timeout: 2000});
      assert.equal(await page.getByRole('button', {name: 'Verify integrity', exact: true}).isEnabled(), true);
    });
    await check('completed evidence verification survives later detail refreshes', async () => {
      noteCount++; sse.write('data: {"revision":42}\n\n');
      await page.waitForFunction(expected => document.querySelectorAll('.timeline li').length === expected, noteCount + 1);
      await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.').waitFor({timeout: 2000});
    });
    await check('playback errors after a detail refresh target the current feedback', async () => {
      await page.getByRole('button', {name: 'Play clip', exact: true}).click();
      await page.locator('#detail-dialog video').evaluate(video => video.dispatchEvent(new Event('error')));
      await page.getByText('Playback is unavailable.', {exact: false}).waitFor({timeout: 2000});
    });
    await page.keyboard.press('Escape');
    await navigate('Settings');
    await page.getByRole('button', {name: 'Link Telegram account'}).click();
    await check('private Telegram binding is functional', async () => {await page.getByText('CONTROLLED-LINK-TOKEN', {exact: true}).waitFor(); assert.equal(await page.getByRole('link', {name: 'Open Telegram to complete binding'}).getAttribute('href'), 'https://t.me/test_bot?start=controlled');});
    await check('Telegram test requires a separate explicit confirmation', async () => {
      telegramLinked = true; await page.getByRole('button', {name: 'Check connection', exact: true}).click();
      await page.getByText('Telegram connected.', {exact: false}).waitFor();
      const before = writes.filter(w => w.path === '/integrations/telegram/test').length;
      await page.getByRole('button', {name: 'Send test notification'}).click();
      assert.equal(writes.filter(w => w.path === '/integrations/telegram/test').length, before);
      await page.getByRole('button', {name: 'Send my test'}).click();
      await page.getByText('Telegram API accepted the test.', {exact: false}).waitFor();
      assert.equal(writes.filter(w => w.path === '/integrations/telegram/test').length, before + 1);
      await page.keyboard.press('Escape');
    });
    await page.getByRole('button', {name: 'Register camera', exact: true}).click();
    await page.getByLabel('Camera name').fill('Approved test source'); await page.getByLabel('Location', {exact: true}).fill('Evaluation room'); await page.getByLabel('Source type').selectOption('test'); await page.getByLabel('Server environment reference').fill('DORMGUARD_CAMERA_TEST'); await page.getByRole('dialog', {name: 'Register camera', exact: true}).getByRole('button', {name: 'Register camera', exact: true}).click();
    await check('camera registration sends private environment reference', async () => {await page.getByRole('dialog', {name: 'Register camera', exact: true}).waitFor({state: 'hidden'}); assert(writes.some(w => w.path === '/cameras' && w.body.source_ref === 'DORMGUARD_CAMERA_TEST' && w.body.kind === 'test'));});
    await navigate('Live Cameras');
    await check('test camera is labeled and not opened automatically', async () => {await page.locator('#main').getByText('Prerecorded test source', {exact: true}).waitFor(); assert.equal(await page.locator('.camera-preview img').count(), 0);});
    await page.getByRole('button', {name: 'Open live preview'}).click();
    await check('protected stream accepts epoch ticket and stops on view switch', async () => {await page.locator('.camera-preview img').waitFor({state: 'attached'}); retiredPreview = await page.locator('.camera-preview img').elementHandle(); await navigate('Evidence'); assert.equal(await page.locator('.camera-preview img').count(), 0); await page.waitForFunction(() => !document.querySelector('.camera-preview img')); await new Promise(resolve => setTimeout(resolve, 100)); assert(streamClosed > 0);});
    await navigate('Live Cameras'); await page.getByRole('button', {name: 'Open live preview'}).click(); await page.locator('.camera-preview img').waitFor({state: 'attached'});
    await check('late errors from a retired preview cannot clear the current preview', async () => {
      await retiredPreview.evaluate(img => img.dispatchEvent(new Event('error')));
      sse.write('data: {"revision":99}\n\n');
      await page.waitForTimeout(100);
      assert.equal(await page.locator('.camera-preview img').count(), 1);
    });
    await check('stale camera health removes the displayed frame', async () => {camera.health = {state: 'disconnected', last_frame_at: Date.now() / 1000 - 20}; sse.write('data: {"revision":4}\n\n'); await page.getByText('Recent frame is stale. Camera unavailable.').waitFor(); assert.equal(await page.locator('.camera-preview img').count(), 0); camera.health = {state: 'active'};});
    await check('protected camera hash target selects and opens its source', async () => {
      await page.goto(origin + '/#cameras/cam-1'); await page.locator('.camera-preview img').waitFor({state: 'attached'});
      assert.equal(await page.getByLabel('Select camera').inputValue(), 'cam-1');
    });
    await navigate('Live Cameras');
    systemFailure = true; await page.getByRole('button', {name: 'Refresh', exact: true}).click();
    await check('partial supporting API failure remains visible', async () => {await page.getByText('Some information is unavailable').waitFor();});
    systemFailure = false;
    await page.getByRole('button', {name: 'Sign out', exact: true}).click(); await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor();
    role = 'deputy_director'; await page.getByLabel('Email address').fill('deputy@example.test'); await page.getByLabel('Password', {exact: true}).fill('controlled-test-password'); await page.getByRole('button', {name: 'Sign in securely'}).click(); await page.getByRole('heading', {name: 'Overview', exact: true}).waitFor(); await navigate('Live Cameras');
    await check('deputy has no camera management controls', async () => {assert.equal(await page.getByRole('button', {name: 'Register camera', exact: true}).count(), 0); assert.equal(await page.getByRole('button', {name: 'Manage', exact: true}).count(), 0);});
    await check('session expiry clears protected detail media and text from the document', async () => {
      await navigate('Incidents'); await page.locator('tbody .row-link').first().click();
      await page.getByRole('button', {name: 'Play clip', exact: true}).click();
      await page.locator('#detail-dialog video').waitFor({state: 'attached'});
      sse.write('event: auth-expired\ndata: {}\n\n');
      await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor();
      assert.equal(await page.locator('#detail-dialog video').count(), 0);
      assert.equal(await page.locator('#detail-dialog').textContent(), '');
    });
    await check('no uncaught browser errors or CSP violations', async () => {assert.deepEqual(errors, []); assert.deepEqual(csp, []);});
    process.stdout.write(`PASS ${checks} browser checks with controlled API doubles\n`);
  } finally {await context.close(); await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error); process.exitCode = 1; server.closeAllConnections(); server.close();});
