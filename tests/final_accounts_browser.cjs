'use strict';
// Real local SQL/password/API/Chrome checks. Only explicitly disposable loopback fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.DORMGUARD_PLAYWRIGHT_PATH || 'playwright');
const access = JSON.parse(fs.readFileSync(process.env.DORMGUARD_BROWSER_ACCESS, 'utf8'));
const origin = new URL(access.origin);
if (access.disposable !== true || origin.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(origin.hostname)) throw new Error('Disposable loopback fixture required');
const output = process.env.DORMGUARD_BROWSER_RESULTS;
const screenshotDir = process.env.DORMGUARD_SCREENSHOT_DIR || '/private/tmp';
if (output) fs.writeFileSync(output, JSON.stringify({status: 'RUNNING'}) + '\n');
(async () => {
  const started = performance.now();
  const browser = await chromium.launch({headless: true, ...(process.env.DORMGUARD_BROWSER_CHANNEL ? {channel: process.env.DORMGUARD_BROWSER_CHANNEL} : {})});
  const context = await browser.newContext({viewport: {width: 390, height: 1000}, reducedMotion: 'reduce'});
  const adminContext = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
  const page = await context.newPage(); const admin = await adminContext.newPage(); const errors = []; const passed = [];
  for (const current of [page, admin]) current.on('pageerror', e => errors.push(e.message));
  async function check(name, fn) {await fn(); passed.push(name); console.log('PASS ' + name);}
  async function login(current, email) {await current.goto(origin.href); await current.getByLabel('Email address').fill(email); await current.getByLabel('Password', {exact: true}).fill(access.password); await current.getByRole('button', {name: 'Sign in securely'}).click(); await current.getByRole('heading', {name: 'Overview', exact: true}).waitFor();}
  async function nav(current, name) {const menu = current.getByRole('button', {name: 'Open navigation'}); if (await menu.isVisible()) await menu.click(); await current.getByRole('navigation', {name: 'Main navigation'}).getByRole('button', {name, exact: true}).click(); await current.waitForFunction(() => !document.getElementById('main').hasAttribute('aria-busy'));}
  try {
    await page.goto(origin.href);
    await check('password visibility is accessible and Enter submits real credentials', async () => {
      await page.getByLabel('Email address').fill(access.director_email || access.email);
      const password = page.getByLabel('Password', {exact: true}); await password.fill(access.password);
      await page.getByRole('button', {name: 'Show password', exact: true}).click(); assert.equal(await password.getAttribute('type'), 'text');
      await page.getByRole('button', {name: 'Hide password', exact: true}).click(); assert.equal(await password.getAttribute('type'), 'password');
      await password.press('Enter'); await page.getByRole('heading', {name: 'Overview', exact: true}).waitFor();
    });
    await nav(page, 'Settings');
    const display = 'Alex <reviewer>';
    await check('display name persists after reload and is escaped', async () => {
      await page.getByLabel('Display name', {exact: true}).fill(display);
      await page.getByRole('button', {name: 'Save display name'}).click(); await page.getByText('Display name saved.', {exact: true}).waitFor();
      await page.reload(); await page.getByRole('heading', {name: 'Overview', exact: true}).waitFor();
      await page.getByText('Welcome back, ' + display + '.', {exact: true}).waitFor();
      assert.equal(await page.locator('reviewer').count(), 0);
      assert.equal((await (await context.request.get(origin.origin + '/api/v1/auth/me')).json()).role, 'director');
    });
    await login(admin, access.super_email);
    await check('super administrator account panel works and officials cannot list accounts', async () => {
      await nav(admin, 'Settings'); await admin.getByRole('heading', {name: 'Authorized accounts', exact: true}).waitFor();
      assert.equal((await context.request.get(origin.origin + '/api/v1/users')).status(), 403);
      const users = await (await adminContext.request.get(origin.origin + '/api/v1/users')).json(); assert(users.items.some(user => user.role === 'super_admin'));
    });
    await page.goto(origin.href + '#incidents/' + access.incident_id);
    await page.getByRole('button', {name: 'Play clip', exact: true}).first().click();
    await page.waitForFunction(() => document.querySelector('#detail-dialog video')?.videoWidth > 0);
    await check('revoking an official ends protected playback and clears all detail content', async () => {
      const row = admin.locator('.audit-event').filter({has: admin.getByText(access.director_email || access.email, {exact: false})}).first();
      await row.getByRole('button', {name: 'Revoke access', exact: true}).click();
      await admin.getByRole('dialog', {name: 'Revoke account access', exact: true}).getByRole('button', {name: 'Revoke account access', exact: true}).click();
      await page.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor({timeout: 15000});
      assert.equal(await page.locator('video').count(), 0); assert.equal(await page.locator('#detail-dialog').textContent(), '');
      assert.equal((await context.request.get(origin.origin + '/api/v1/incidents')).status(), 401);
      const current = await (await adminContext.request.get(origin.origin + '/api/v1/users')).json();
      const official = current.items.find(user => user.email === (access.director_email || access.email)); assert.equal(official.enabled, 0);
      const me = await (await adminContext.request.get(origin.origin + '/api/v1/auth/me')).json();
      assert.equal((await adminContext.request.patch(origin.origin + '/api/v1/users/' + official.id, {headers: {'X-CSRF-Token': me.csrf_token}, data: {enabled: true}})).status(), 200);
    });
    await login(page, access.director_email || access.email);
    const second = await context.newPage(); await second.goto(origin.href); await second.getByRole('heading', {name: 'Overview', exact: true}).waitFor();
    await check('logout clears another tab immediately and old protected access is denied', async () => {
      await page.getByRole('button', {name: 'Sign out', exact: true}).click();
      await second.getByRole('heading', {name: 'Sign in to your workspace'}).waitFor({timeout: 3000});
      assert.equal((await context.request.get(origin.origin + '/api/v1/auth/me')).status(), 401);
    });
    await second.close();
    await check('login and super-admin settings fit 390/768/1024/1440 pixels', async () => {
      for (const width of [390, 768, 1024, 1440]) {
        await page.setViewportSize({width, height: 1000}); await admin.setViewportSize({width, height: 1000});
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        assert(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.screenshot({path: screenshotDir + '/final-login-' + width + '.png', fullPage: true});
        await admin.screenshot({path: screenshotDir + '/final-super-settings-' + width + '.png', fullPage: true});
      }
    });
    await check('no uncaught JavaScript errors', async () => assert.deepEqual(errors, []));
    if (output) fs.writeFileSync(output, JSON.stringify({status: 'PASS', checks: passed, runtime_seconds: (performance.now() - started) / 1000, scope: 'Actual disposable local API/SQL/password/media/Chrome; no hosted or external provider claim'}, null, 2) + '\n');
  } finally {await context.close(); await adminContext.close(); await browser.close();}
})().catch(error => {if (output) fs.writeFileSync(output, JSON.stringify({status: 'FAIL', error_type: error.name, message: error.message}, null, 2) + '\n'); console.error(error); process.exitCode = 1;});
