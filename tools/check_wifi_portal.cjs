#!/usr/bin/env node
'use strict';
// Run the page that users operate, with a local synthetic server and Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const outputIndex = process.argv.indexOf('--output');
const output = path.resolve(outputIndex === -1 ? path.join(require('node:os').tmpdir(), 'family-radio-wifi-ui') : process.argv[outputIndex + 1]);
fs.mkdirSync(output, {recursive: true});
const root = path.resolve(__dirname, '..');
const radioFixture = process.argv.includes('--radio-fixture');
const fixtureCode = `
import sys
sys.path.insert(0, ${JSON.stringify(root)})
from host.wifi_portal import DemoNetwork, PortalServer, Setup
class RadioFixture(DemoNetwork):
    def status(self):
        value = super().status()
        value['demo'] = False
        if value['state'] == 'connected': value['message'] = 'Synthetic radio join complete. No real networking changed.'
        return value
with PortalServer(('127.0.0.1', 0), Setup(RadioFixture()), real=True) as server:
    print(server.origin, flush=True)
    server.serve_forever()
`;
const server = spawn('python3', radioFixture ? ['-B', '-c', fixtureCode] : [path.join(root, 'host/wifi_portal.py'), '--port', '0'], {stdio: ['ignore', 'pipe', 'pipe']});
let serverErrors = '';
server.stderr.on('data', chunk => { serverErrors += chunk; });
let browser;
async function main() {
  const address = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Demo server startup timed out')), 10000);
    let text = '';
    server.stdout.on('data', chunk => {
      text += chunk;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    server.on('error', reject);
    server.on('exit', code => { clearTimeout(timer); reject(new Error(`Demo server exited: ${code}`)); });
  });
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1000, height: 1050}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => { errors.push('Unexpected script dialog'); dialog.dismiss(); });
  await page.goto(address);
  await page.locator('.network').last().waitFor();
  assert.equal(await page.locator('.network').count(), 3);
  assert.equal(await page.locator('#networks img').count(), 0, 'SSID text must not create markup');
  await page.screenshot({path: path.join(output, '01-initial.png'), fullPage: true});
  await page.locator('.network').first().click();
  assert.equal(await page.locator('#ssid').inputValue(), 'Demo Home Network');
  await page.locator('#password').fill('short');
  await page.locator('#connect').click();
  await page.locator('#status[data-state=failed]').waitFor();
  assert.match(await page.locator('#status-message').innerText(), /Check the network name, security and password/);
  for (const selector of ['#ssid', '#security', '#password', '#connect', '#refresh', '.network']) {
    assert.equal(await page.locator(selector).first().isEnabled(), true, `${selector} must allow correction after rejection`);
  }
  assert.equal(await page.locator('#password').inputValue(), '');
  assert.equal((await (await page.request.get(address + '/api/status')).json()).state, 'ready');
  await page.screenshot({path: path.join(output, '09-rejected-password.png'), fullPage: true});
  await page.locator('#password').fill('wrong-password');
  await page.locator('#show-password').check();
  assert.equal(await page.locator('#password').getAttribute('type'), 'text');
  await page.locator('#connect').click();
  await page.locator('#status[data-state=connecting]').waitFor();
  assert.equal(await page.locator('#connect').isDisabled(), true);
  assert.equal(await page.locator('#password').inputValue(), '');
  assert.equal(await page.locator('#password').getAttribute('type'), 'password');
  await page.screenshot({path: path.join(output, '02-connecting.png'), fullPage: true});
  await page.locator('#status[data-state=failed]').waitFor();
  await page.screenshot({path: path.join(output, '03-retry.png'), fullPage: true});
  await page.locator('#password').fill('synthetic-only');
  await page.locator('#connect').click();
  await page.locator('#status[data-state=connected]').waitFor();
  assert.match(await page.locator('#status').innerText(), radioFixture ? /Wi-Fi connected/ : /Demo connection complete/);
  await page.screenshot({path: path.join(output, '04-connected.png'), fullPage: true});
  await page.setViewportSize({width: 360, height: 900});
  await page.locator('#ssid').fill('Hidden synthetic network');
  await page.locator('#security').selectOption('open');
  assert.equal(await page.locator('#password-group').isVisible(), false);
  assert.equal(await page.locator('#open-warning').isVisible(), true);
  await page.screenshot({path: path.join(output, '05-mobile-manual.png'), fullPage: true});
  await page.locator('#connect').click();
  await page.locator('#status[data-state=connected]').waitFor();
  const status = await (await page.request.get(address + '/api/status')).json();
  assert.equal(status.ssid, 'Hidden synthetic network');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Mobile layout must fit');
  await page.locator('.network').first().click();
  await page.locator('#password').fill('wrong-password');
  await page.locator('#connect').click();
  await page.locator('#status[data-state=connecting]').waitFor();
  await page.screenshot({path: path.join(output, '06-mobile-connecting.png'), fullPage: true});
  await page.locator('#status[data-state=failed]').waitFor();
  await page.screenshot({path: path.join(output, '07-mobile-retry.png'), fullPage: true});
  await page.route('**/api/networks', route => route.fulfill({json: {version: 3, demo: !radioFixture, networks: [{ssid: '<img src=x onerror=alert(1)>', security: 'wpa2'}]}}));
  await page.locator('#refresh').click();
  await page.locator('.network').filter({hasText: '<img src=x onerror=alert(1)>'}).waitFor();
  assert.equal(await page.locator('#networks img').count(), 0);
  await page.locator('.network').last().click();
  assert.equal(await page.locator('#ssid').inputValue(), '<img src=x onerror=alert(1)>');
  assert.equal(await page.locator('#password-group').isVisible(), true);
  if (radioFixture) {
    assert.match(await page.locator('.badge').innerText(), /RADIO WI-FI SETUP/);
    await page.unroute('**/api/networks');
    await page.locator('#refresh').click();
    await page.locator('.network').first().click();
    await page.route('**/api/status', route => route.abort());
    await page.locator('#password').fill('synthetic-only');
    await page.locator('#connect').click();
    await page.getByText('Reconnect your phone', {exact: true}).waitFor();
    assert.equal(await page.locator('#status').getAttribute('data-state'), 'connecting');
    await page.screenshot({path: path.join(output, '08-expected-disconnection.png'), fullPage: true});
  }
  assert.deepEqual(errors, []);
  assert.equal(serverErrors, '', 'Server must not log credentials or requests');
  console.log(`Wi-Fi portal browser checks passed. Screenshots: ${output}`);
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  server.kill();
});
