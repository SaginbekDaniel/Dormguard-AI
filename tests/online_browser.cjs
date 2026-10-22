'use strict';
// Actual HTTPS localhost service. The local self-signed certificate is a TEST exception.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require('playwright');
const access = JSON.parse(fs.readFileSync(process.env.DORMGUARD_ONLINE_ACCESS, 'utf8'));
if (new URL(access.origin).hostname !== '127.0.0.1') throw Error('Loopback acceptance only');
(async () => {
  const browser = await chromium.launch({headless:true, channel:'chrome'});
  const context = await browser.newContext({ignoreHTTPSErrors:true, viewport:{width:1440,height:1000}});
  const page = await context.newPage(), errors=[], external=[];
  page.on('pageerror', e=>errors.push(e.message));
  await context.route('**/*', route=>{
    if(new URL(route.request().url()).origin===access.origin) return route.continue();
    external.push(route.request().url());return route.abort();
  });
  const checks=[];
  async function check(name, fn) {await fn(); checks.push(name);console.log('PASS '+name);}
  async function nav(name) {
    if(await page.getByRole('button',{name:'Open navigation'}).isVisible()) await page.getByRole('button',{name:'Open navigation'}).click();
    await page.getByRole('navigation',{name:'Main navigation'}).getByRole('button',{name,exact:true}).click();
    await page.waitForFunction(()=>!document.getElementById('main').hasAttribute('aria-busy'));
  }
  try {
    await page.goto(access.origin);
    await check('online entry uses private server-authenticated credentials',async()=>{
      assert.equal(await page.getByRole('button',{name:'Enter as Demo Director'}).count(),0);
      await page.getByLabel('Email address').fill(access.email);
      await page.getByLabel('Password',{exact:true}).fill(access.password);
      await page.getByRole('button',{name:'Sign in securely'}).click();
      await page.getByRole('heading',{name:'Overview',exact:true}).waitFor();
      const cookie=(await context.cookies()).find(c=>c.name==='dormguard_session');
      assert(cookie.secure && cookie.httpOnly && cookie.sameSite==='Strict');
    });
    await check('seven online screens render at 390, 768 and 1440 pixels',async()=>{
      for(const width of [390,768,1440]) {
        await page.setViewportSize({width,height:1000});
        for(const name of ['Overview','Live Cameras','Incidents','AI Analytics','Activity Journal','System Health','Settings']) {
          await nav(name);await page.getByRole('heading',{name,exact:true}).waitFor();
          assert.equal(await page.getByText('We could not load this view').count(),0);
          assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
        }
      }
    });
    await check('actual incident evidence verifies and plays over HTTPS',async()=>{
      await page.goto(access.origin+'/#incidents/'+process.env.DORMGUARD_ONLINE_INCIDENT);
      const dialog=page.locator('#detail-dialog');await dialog.waitFor({state:'visible'});
      await dialog.getByRole('button',{name:'Verify integrity'}).first().click();
      await page.getByText('PASS · Stored original matches its recorded SHA-256 hash.',{exact:true}).first().waitFor();
      await dialog.getByRole('button',{name:'Play clip'}).first().click();
      await page.waitForFunction(()=>[...document.querySelectorAll('#detail-dialog video')].some(v=>v.readyState>=2),{},{timeout:15000});
      await page.screenshot({path:'/private/tmp/dormguard-online-evidence.png',fullPage:true});
    });
    await check('refresh retains the online session',async()=>{
      await page.reload();await page.locator('#detail-dialog').waitFor({state:'visible'});
      assert.equal((await page.request.get(access.origin+'/api/v1/auth/me')).status(),200);
    });
    assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    fs.writeFileSync('/private/tmp/dormguard-online-browser-results.json',JSON.stringify({status:'PASS',scope:'Real HTTPS loopback, self-signed test certificate; not public hosting',checks,page_errors:errors,external_requests:external},null,2));
  } finally {await browser.close();}
})();
