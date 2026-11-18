#!/usr/bin/env node
'use strict';
// Drive the parent page a parent uses, on a synthetic demo library, with Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const outputIndex = process.argv.indexOf('--output');
const output = path.resolve(outputIndex === -1 ? path.join(os.tmpdir(), 'family-radio-dashboard-ui') : process.argv[outputIndex + 1]);
fs.mkdirSync(output, {recursive: true});
const root = path.resolve(__dirname, '..');
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'family-dashboard-check-'));
const data = path.join(work, 'data');
const media = path.join(work, 'media');
fs.mkdirSync(media);
execFileSync('python3', [path.join(root, 'host/dashboard.py'), 'demo', '--data-dir', data], {stdio: 'inherit'});
const song = path.join(media, 'Synthetic lullaby.mp3');
execFileSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=520:duration=1', '-metadata', 'title=Synthetic lullaby', song]);
const cover = path.join(media, 'cover.png');
execFileSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'color=c=0xe0a040:s=300x300', '-frames:v', '1', cover]);

const server = spawn('python3', [path.join(root, 'host/dashboard.py'), 'serve', '--data-dir', data, '--port', '0'],
  {stdio: ['ignore', 'pipe', 'pipe']});
let serverErrors = '';
server.stderr.on('data', chunk => { serverErrors += chunk; });
let browser;

async function waitForLogin() {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Dashboard startup timed out')), 10000);
    let text = '';
    server.stdout.on('data', chunk => {
      text += chunk;
      const match = text.match(/listening on http:\/\/127\.0\.0\.1:(\d+)/);
      if (match) {
        clearTimeout(timer);
        assert.doesNotMatch(text, /token=/, 'a non-interactive server keeps login links out of its log');
        resolve(execFileSync('python3', [path.join(root, 'host/dashboard.py'), 'login-url', '--data-dir', data, '--port', match[1]]).toString().trim());
      }
    });
    server.on('exit', code => { clearTimeout(timer); reject(new Error(`Dashboard exited: ${code}`)); });
  });
}

function watch(page, errors) {
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', entry => {
    if (entry.type() === 'error' && !/^Failed to load resource: .*\((Unauthorized|Conflict)\)$/.test(entry.text())) errors.push(entry.text());
  });
}

async function clear(page) {
  await page.evaluate(() => { document.getElementById('status').textContent = ''; });
}

async function settled(page, pattern) {
  await page.locator('#status').filter({hasText: pattern}).waitFor();
}

async function main() {
  const loginUrl = await waitForLogin();
  const origin = new URL(loginUrl).origin;
  browser = await chromium.launch({headless: true});
  const errors = [];

  const stranger = await browser.newPage({viewport: {width: 1000, height: 800}});
  watch(stranger, errors);
  await stranger.goto(origin + '/');
  await stranger.locator('#login').waitFor();
  assert.equal(await stranger.locator('#editor').isVisible(), false, 'no editor without a session');
  await stranger.screenshot({path: path.join(output, '01-login-required.png'), fullPage: true});

  const context = await browser.newContext({viewport: {width: 1000, height: 1100}});
  const page = await context.newPage();
  watch(page, errors);
  page.on('dialog', dialog => dialog.accept());
  await page.goto(loginUrl);
  await page.locator('#songs .song').nth(2).waitFor();
  assert.equal(new URL(page.url()).search, '', 'login token leaves the address bar');
  assert.equal(await page.locator('#songs .song').count(), 3);
  assert.match(await page.locator('#playlists').innerText(), /Synthetic family playlist/);
  assert.match(await page.locator('#playlists').innerText(), /Refreshed 5 min ago/);
  assert.match(await page.locator('#artists').innerText(), /Synthetic bedtime artist/);
  await page.screenshot({path: path.join(output, '02-desktop-overview.png'), fullPage: true});

  await page.locator('#add-approval [name=kind]').selectOption('playlist');
  await page.locator('#add-approval [name=identity]').fill('https://open.spotify.com/playlist/' + 'B'.repeat(22) + '?si=synthetic');
  await page.locator('#add-approval [name=label]').fill('Car songs');
  await clear(page);
  await page.locator('#add-approval button').click();
  await settled(page, /reach the radio/);
  assert.match(await page.locator('#playlists').innerText(), /Car songs/);
  assert.equal(await page.locator('#playlist-count').innerText(), '(2)');

  const artistRow = page.locator('#artists .item').first();
  await artistRow.locator('summary').click();
  await artistRow.locator('[name=label]').fill('Bedtime artist');
  await clear(page);
  await artistRow.getByRole('button', {name: 'Save label'}).click();
  await settled(page, /Saved/);
  assert.match(await page.locator('#artists').innerText(), /Bedtime artist/);
  await page.locator('#artists .item').first().locator('summary').click();
  await page.locator('#artists .item').first().getByRole('button', {name: 'Remove'}).click();
  await page.locator('#artists .empty').waitFor();

  await clear(page);
  await page.locator('#audio-files').setInputFiles(song);
  await settled(page, /1 songs added/);
  assert.equal(await page.locator('#songs .song').count(), 4);
  const added = page.locator('#songs .song').nth(3);
  assert.match(await added.innerText(), /Synthetic lullaby/);
  await clear(page);
  await added.getByRole('button', {name: /Move up/}).click();
  await settled(page, /Saved/);
  assert.match(await page.locator('#songs .song').nth(2).innerText(), /Synthetic lullaby/, 'moved up one place');

  const moved = page.locator('#songs .song').nth(2);
  await moved.locator('summary').click();
  await moved.locator('[name=title]').fill('Bedtime Lullaby');
  await moved.locator('[name=artist]').fill('Synthetic dad');
  await clear(page);
  await moved.getByRole('button', {name: 'Save song'}).click();
  await settled(page, /Saved/);
  const edited = page.locator('#songs .song').nth(2);
  assert.match(await edited.innerText(), /Bedtime Lullaby/);
  assert.match(await edited.innerText(), /Synthetic dad/);
  await edited.locator('summary').click();
  await clear(page);
  await edited.locator('input[type=file]').setInputFiles(cover);
  await settled(page, /Cover saved/);
  const art = page.locator('#songs .song').nth(2).locator('.cover img');
  await art.waitFor();
  assert.ok(await art.evaluate(image => image.complete && image.naturalWidth === 112), 'cover thumbnail decodes');
  await page.screenshot({path: path.join(output, '03-after-edits.png'), fullPage: true});

  const other = await context.newPage();
  watch(other, errors);
  await other.goto(origin + '/');
  await other.locator('#songs .song').nth(3).waitFor();
  await clear(other);
  await other.locator('#songs .song').nth(0).getByRole('button', {name: /Move down/}).click();
  await settled(other, /Saved/);
  await clear(page);
  await page.locator('#songs .song').nth(1).getByRole('button', {name: /Move down/}).click();
  await settled(page, /changed in another window/);
  assert.match(await page.locator('#songs .song').nth(0).innerText(), /Synthetic rain/, 'stale window reloaded the newer order');
  await other.close();

  await page.route('**/api/spotify/playlists', route => route.fulfill({json: {items: [
    {id: 'C'.repeat(22), name: 'Synthetic kitchen dance party', playable: true},
    {id: 'D'.repeat(22), name: 'Synthetic followed mix', playable: false}]}}));
  await page.route('**/api/state', async route => {
    const response = await route.fetch();
    route.fulfill({response, json: {...await response.json(), spotify_authorized: true}});
  });
  await page.getByRole('button', {name: 'Refresh music lists'}).click();
  await page.locator('#pickers').waitFor();
  await page.getByRole('button', {name: 'Show your playlists'}).click();
  await page.locator('#playlist-results .item').nth(1).waitFor();
  assert.equal(await page.locator('#playlist-results button').nth(1).isDisabled(), true, 'followed playlist cannot be added');
  await clear(page);
  await page.locator('#playlist-results').getByRole('button', {name: 'Add'}).click();
  await settled(page, /reach the radio/);
  assert.equal(await page.locator('#playlist-count').innerText(), '(3)');
  assert.match(await page.locator('#playlists').innerText(), /Synthetic kitchen dance party/, 'picker keeps the Spotify name');
  await page.locator('#pickers').screenshot({path: path.join(output, '04-spotify-picker.png')});
  await page.unrouteAll();

  for (const scheme of ['light', 'dark']) {
    const phone = await browser.newPage({viewport: {width: 360, height: 780}, colorScheme: scheme});
    watch(phone, errors);
    await phone.context().addCookies(await context.cookies());
    await phone.goto(origin + '/');
    await phone.locator('#songs .song').nth(3).waitFor();
    await phone.locator('#songs .song').nth(0).locator('summary').click();
    const overflow = await phone.evaluate(() => document.scrollingElement.scrollWidth - innerWidth);
    assert.ok(overflow <= 0, `no sideways scrolling at 360px (${scheme}): ${overflow}px`);
    const small = await phone.locator('button:visible').evaluateAll(nodes =>
      nodes.filter(node => node.getBoundingClientRect().height < 44).map(node => node.textContent));
    assert.deepEqual(small, [], `touch targets are at least 44px tall (${scheme})`);
    await phone.screenshot({path: path.join(output, `05-phone-${scheme}.png`), fullPage: true});
    await phone.close();
  }

  assert.deepEqual(errors, [], 'no page errors or CSP violations');
  console.log('Parent dashboard browser check passed. Screenshots: ' + output);
}

main().catch(error => {
  console.error(error);
  if (serverErrors) console.error(serverErrors);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  fs.rmSync(work, {recursive: true, force: true});
});
