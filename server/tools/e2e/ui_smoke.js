// Browser test of Firmware -> Update Device I/O and Generate config.h against a running server + simulated meter
// (run.sh starts both). Needs Node.js and Playwright:  npm i -g playwright && npx playwright install chromium
//   NODE_PATH=$(npm root -g) node ui_smoke.js http://127.0.0.1:5055
const { chromium } = require('playwright');
const base = process.argv[2] || 'http://127.0.0.1:5055';
let failures = 0;
function check(ok, what) { console.log((ok ? 'ok    ' : 'FAIL  ') + what); if (!ok) failures++; }

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1100, height: 1500 } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('dialog', d => d.accept());

  await page.goto(base + '/login');
  await page.fill('input[name=username]', 'admin');
  await page.fill('input[name=password]', 'Str0ng!Passw0rd');
  await page.press('input[name=password]', 'Enter');
  await page.waitForTimeout(800);

  await page.goto(base + '/firmware');
  check(await page.locator('text=Update Device I/O').count() === 1, 'Firmware page has the Update Device I/O button');

  await page.goto(base + '/firmware/io');
  const options = await page.locator('#meterSel option').allTextContents();
  check(options.length === 2, 'one multi-channel meter is listed: ' + options[1]);
  await page.selectOption('#meterSel', { index: 1 });
  await page.waitForSelector('#io');
  check(await page.locator('#channels tr').count() === 2 && await page.locator('#relays tr').count() === 1, 'editor shows 2 channels, 1 relay board');
  check((await page.textContent('#relays tr')).includes('Board A [2C]'), 'relay board is labelled Board A [2C]');
  check(await page.isDisabled('#saveBtn') && await page.isDisabled('#clampBtn'), 'nothing changed: both apply buttons disabled');

  // scan
  await page.click('#scanBtn');
  await page.waitForSelector('#scanCard:not(.d-none)');
  await page.waitForTimeout(500);
  const scan = await page.textContent('#scanOut');
  check(scan.includes('0x50') && scan.includes('rbAmp confirmed') && scan.includes('0x21'), 'scan lists the rbAmp modules and the expanders');

  // live clamp change
  await page.selectOption('[data-ca="0"]', '100');
  check(!(await page.isDisabled('#clampBtn')) && await page.isDisabled('#saveBtn'), 'clamp-only change enables Update Clamp Model only');
  await page.click('#clampBtn');
  await page.waitForTimeout(4000);
  check((await page.textContent('#stateBadge')) === 'Active' && (await page.textContent('#stateText')).includes('v1'), 'clamp change applied live (profile v1 active)');

  // structure: a second rbAmp board, relay Board B (MCP23017, 4 outputs), 2 more channels, CH3 on Board B relay 3
  await page.click('#addBoard');
  await page.click('#addRelay');
  await page.selectOption('[data-rdrv="1"]', 'mcp23017');
  await page.selectOption('[data-raddr="1"]', '33');
  await page.fill('[data-rn="1"]', '4');
  await page.dispatchEvent('[data-rn="1"]', 'change');
  await page.click('#addCh'); await page.click('#addCh');
  await page.selectOption('[data-crb="2"]', '2');
  await page.selectOption('[data-cro="2"]', '3');
  check((await page.locator('#relays tr').nth(1).textContent()).includes('Board B [4C]'), 'second relay board is Board B [4C]');
  check((await page.textContent('#auxNote')).includes('Board B relay 1'), 'AUX relays are listed as "Board B relay N"');

  // Board A removed: Board B stays Board B and keeps its channel
  await page.click('[data-delrelay="0"]');
  check((await page.locator('#relays tr').first().textContent()).includes('Board B [4C]'), 'after removing Board A, Board B is still Board B');
  check(await page.locator('[data-crb="2"]').inputValue() === '2', 'the channel on Board B still points at Board B');
  await page.click('#addRelay');   // gets the free letter A again
  check((await page.locator('#relays tr').first().textContent()).includes('Board A'), 'a new relay board takes the free letter A');
  await page.click('[data-delrelay="0"]');   // keep the test short: remove it again

  await page.fill('#delay', '10');
  await page.click('#saveBtn');
  await page.waitForTimeout(1500);
  await page.waitForTimeout(9000);
  check((await page.textContent('#stateBadge')) === 'Active' && (await page.textContent('#stateText')).includes('v2'), 'profile v2 applied after the restart (confirmed)');
  check(await page.locator('#channels tr').count() === 4, 'the meter now reports 4 channels');
  await page.screenshot({ path: process.env.SHOT || 'update_device_io.png', fullPage: true });

  // generator
  await page.goto(base + '/firmware/config');
  check(await page.locator('#gFrom').count() === 1, 'generator offers "Start from the hardware of a meter"');
  await page.selectOption('#gFrom', { index: 1 });
  await page.waitForTimeout(800);
  check(await page.locator('.g-board').count() === 2 && await page.locator('#gChannels tr').count() === 4, 'generator form filled from the meter');
  await page.click('#genForm button[type=submit]');
  await page.waitForSelector('#gOut:not(.d-none)');
  const text = await page.inputValue('#gText');
  check(/\{\s*1,\s+3,\s+SCT_30A,\s+1,\s+3 \}/.test(text), 'config.h has the relay board + output columns (CH3 = relay board 1, output 3)');

  check(errors.length === 0, 'no browser errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
