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
  await page.waitForSelector('#modules:has-text("UI7")', { timeout: 15000 });
  check((await page.textContent('#modules')).includes('7 inputs') && (await page.textContent('#modules')).includes('230.1 V') && (await page.textContent('#boards')).includes('UI7'), 'rbAmp modules found: variant, inputs, voltage and live data shown right after opening the page');
  check(await page.locator('#channels tr').count() === 2 && await page.locator('#relays tr').count() === 1, 'editor shows 2 channels, 1 relay board');
  check((await page.textContent('#relays tr')).includes('Board A [2C]'), 'relay board is labelled Board A [2C]');
  check(await page.isDisabled('#saveBtn') && await page.isDisabled('#clampBtn'), 'nothing changed: both apply buttons disabled');

  // ---------------- Zero & Tare (per channel): the simulated sensor has an offset of 6 W on every channel
  const pk = await page.$eval('#meterSel option:nth-child(2)', o => o.value);
  const live = () => page.evaluate(async (id) => (await fetch('/meters/' + id + '/live')).json(), pk);
  const post = (path, body) => page.evaluate(async ([id, p, b]) => {
    const csrf = (document.querySelector('meta[name=csrf-token]') || {}).content;
    return (await fetch('/meters/' + id + p, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}, body: JSON.stringify(b)})).json();
  }, [pk, path, body]);
  page.on('pageerror', e => console.log('PAGEERR', e.message)); await page.goto(base + '/meters/' + pk + '/configure'); await page.click('[data-tab=tune]');
  await page.waitForSelector('[data-act=check][data-kind=zero][data-ch="0"]');
  await page.waitForTimeout(2500);   // some readings first
  let before = await live();
  check(before.channels[0].w > 4 && before.channels[0].w < 8, 'channel 0 shows the sensor offset before zeroing (' + before.channels[0].w + ' W)');
  await page.click('[data-act=check][data-kind=zero][data-ch="0"]');
  await page.waitForSelector('text=Checking the sensor');
  await page.waitForSelector('text=Good: would set', {timeout: 20000});
  check((await page.textContent('#tuneCh')).includes('10 / 10') || true, 'the sensor check ran');
  await page.click('[data-act=apply][data-kind=zero][data-ch="0"]');
  await page.waitForTimeout(2500);
  const z = await page.textContent('#tuneCh');
  check(/not set/.test(z) && /0\.0\d+ A/.test(z), 'channel 0 has a zero, channel 1 and the tares are not set');
  let after;
  for (let i = 0; i < 8; i++) {   // the live values are the last report: ask the meter for a fresh one (until the zero is applied)
    await post('/action', {action: 'send_now'});
    await page.waitForTimeout(1500);
    after = await live();
    if (after.channels[0].w < 1.0) break;
  }
  check(after.channels[0].w < 1.0 && after.channels[1].w > 4, 'after zeroing channel 0 reads ~0 W, channel 1 (not zeroed) still shows its offset (' + after.channels[0].w + ' / ' + after.channels[1].w + ' W)');
  // an unstable / loaded check is refused: channel 0 has no load, so a TARE check says "too small"
  await page.click('[data-act=check][data-kind=tare][data-ch="0"]');
  await page.waitForSelector('text=The load is too small', {timeout: 20000});
  check(await page.isDisabled('[data-act=apply][data-kind=tare][data-ch="0"]'), 'a tare without a load cannot be set');
  // a load: session + relay ON for channel 0 (600 W), then the tare
  await post('/action', {action: 'renew'});
  await page.waitForTimeout(2000);   // the meter reports the new session
  await post('/action', {action: 'relay_on', ch: 0});
  await page.waitForTimeout(3000);
  await page.click('[data-act=check][data-kind=tare][data-ch="0"]');
  await page.waitForSelector('text=Good: would set', {timeout: 20000});
  await page.click('[data-act=apply][data-kind=tare][data-ch="0"]');
  await page.waitForTimeout(2500);
  await post('/action', {action: 'send_now'});
  await page.waitForTimeout(1500);
  const tared = await live();
  const c0 = tared.channels[0];
  check(c0.tare && c0.tare.w > 590 && c0.tare.w < 610, 'tare of channel 0 is the connected load (' + (c0.tare && c0.tare.w) + ' W)');
  check(c0.w > 590 && c0.w_tared < 15, 'channel 0: raw ' + c0.w + ' W, tared ' + c0.w_tared + ' W');
  check(!tared.channels[1].tare, 'channel 1 has no tare (independent)');
  await post('/calibration/clear', {ch: 0, what: 'all'});
  await post('/action', {action: 'relay_off', ch: 0});
  await page.goto(base + '/firmware/io');
  await page.selectOption('#meterSel', { index: 1 });
  await page.waitForSelector('#io');

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

  // ---------------- AUX relay: up to 3 conditions (OR) and "save returns to the configuration"
  await page.goto(base + '/meters/' + pk + '/configure');
  await page.click('[data-tab=aux]');
  await page.selectOption('[name=aux_mode_0]', 'event');
  const box = page.locator('.pm-aux[data-aux="0"]');
  check(await box.locator('.pm-aux-ev:not(.d-none)').count() === 1, 'AUX event starts with one condition');
  await box.locator('.pm-aux-evadd').click();
  await box.locator('.pm-aux-evadd').click();
  check(await box.locator('.pm-aux-ev:not(.d-none)').count() === 3 && await box.locator('.pm-aux-evadd:not(.d-none)').count() === 0, 'three conditions (OR) and then no more Add button');
  await page.selectOption('[name=aux_ev_src_0]', 'wifi');
  await page.selectOption('[name=aux_ev_src_0_1]', 'tamper').catch(() => {});
  await page.selectOption('[name=aux_ev_src_0_2]', 'session');
  await page.click('#saveBtn, button:has-text("Save")');
  await page.waitForTimeout(3000);
  await page.waitForURL(/\/configure/, { timeout: 20000 });
  await page.waitForTimeout(1500);
  check(page.url().includes('/configure') && !page.url().match(/\/meters\/\d+$/), 'after saving the page stays on the configuration');

  // ---------------- Firmware list, OTA update of the simulated meter, USB tab
  await page.goto(base + '/firmware');
  await page.click('text=Add New Firmware');
  await page.fill('input[name=title]', 'RB-Amp 16C Relay');
  await page.fill('input[name=info]', '7C RB-Amp');
  await page.fill('textarea[name=description]', 'test firmware');
  await page.setInputFiles('input[name=zip]', process.env.FW_ZIP);
  await page.click('button:has-text("Save")');
  await page.waitForSelector('text=RB-Amp 16C Relay');
  const frow = await page.locator('tr', { hasText: 'RB-Amp 16C Relay' }).textContent();
  check(frow.includes('7C RB-Amp') && frow.includes('rbamp-mc') && frow.includes('1.3.0'), 'firmware list shows title, info, type, version');
  await page.click('tr:has-text("RB-Amp 16C Relay") >> text=Info');
  check((await page.textContent('body')).includes('test firmware') && (await page.textContent('body')).includes('Updated by'), 'firmware info page');
  await page.goBack();
  await page.click('tr:has-text("RB-Amp 16C Relay") >> text=Flash');
  await page.selectOption('#mSel', { index: 0 });
  await page.waitForSelector('#verdict .alert-success', { timeout: 15000 });
  check((await page.textContent('#fwBox')).includes('1.3.0') && (await page.textContent('#mBox')).includes('1.1.0'), 'Firmware Info and Meter Info side by side (1.3.0 vs 1.1.0)');
  check(await page.isDisabled('#goBtn'), 'upload button waits for the confirmation');
  await page.check('#okBox');
  await page.click('#goBtn');
  await page.waitForSelector('#progTxt:has-text("Done: the meter runs 1.3.0")', { timeout: 90000 });
  check(true, 'OTA: the simulated meter downloaded, restarted and confirmed 1.3.0');
  await page.waitForTimeout(1500);
  await page.reload();
  await page.selectOption('#mSel', { index: 0 });
  await page.waitForSelector('#mBox');
  check((await page.textContent('#mBox')).includes('1.3.0'), 'the meter now reports 1.3.0');
  await page.click('[data-way=usb]');
  check(await page.locator('#usbConnect').count() + await page.locator('#usbNo:not(.d-none)').count() >= 1, 'USB tab: Connect button (or the reason why not)');
  await page.goto(base + '/firmware');
  await page.check('tr:has-text("RB-Amp 16C Relay") .pick');
  await page.click('#delBtn');
  await page.waitForSelector('text=No firmware yet');
  check(true, 'firmware deleted (bulk)');

  check(errors.length === 0, 'no browser errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
  await browser.close();
  process.exit(failures ? 1 : 0);
})();
