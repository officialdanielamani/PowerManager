// The meter's local web page: one self-contained HTML file (no CDN, no external files).
// Edit freely; it is served as-is from flash.
#pragma once
#include <Arduino.h>

static const char INDEX_HTML[] PROGMEM = R"HTML(<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PowerMeter</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#6b7280;--line:#e3e6ea;--pri:#0d6efd;--ok:#198754;--warn:#d39e00;--bad:#dc3545}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e8eaed;--mut:#9aa1ab;--line:#2c323b}}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;gap:8px;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--line)}
header b{font-size:17px;white-space:nowrap}#hid{font-size:12px}@media(max-width:480px){#hid{display:none}}header .sp{flex:1}main{max-width:720px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
h2{font-size:16px;margin:0 0 12px}label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
input,select,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:60px;font-family:monospace;font-size:12px}
button{padding:9px 14px;border:0;border-radius:7px;background:var(--pri);color:#fff;font:inherit;cursor:pointer}
button.sec{background:transparent;color:var(--fg);border:1px solid var(--line)}button.bad{background:var(--bad)}button.ok{background:var(--ok)}
button:disabled{opacity:.45;cursor:not-allowed}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.row>*{flex:1}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px}
.stat{background:var(--bg);border-radius:8px;padding:8px 10px}.stat small{color:var(--mut);display:block}.stat b{font-size:20px}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;color:#fff;background:var(--mut)}
.pill.ok{background:var(--ok)}.pill.warn{background:var(--warn)}.pill.bad{background:var(--bad)}
.msg{padding:9px 12px;border-radius:7px;margin:10px 0;font-size:14px}.msg.err{background:#f8d7da;color:#842029}.msg.inf{background:#cff4fc;color:#055160}.msg.okm{background:#d1e7dd;color:#0f5132}
.mut{color:var(--mut);font-size:13px}.hide{display:none!important}ul.nets{list-style:none;padding:0;margin:8px 0;max-height:200px;overflow:auto}
ul.nets li{padding:8px;border-bottom:1px solid var(--line);cursor:pointer;display:flex}ul.nets li span{flex:1}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font-size:14px}dt{color:var(--mut)}dd{margin:0}
.pw{position:relative}.pw input{padding-right:42px}.pw button{position:absolute;right:3px;top:50%;transform:translateY(-50%);padding:5px 8px;background:transparent;color:var(--mut);border:0;line-height:0}
.chk{display:flex;gap:8px;align-items:center;margin-top:10px}.chk input{width:auto}
</style></head><body>
<header><b>&#9889; PowerMeter</b><span class="mut" id="hid"></span><span class="sp"></span>
<button class="sec hide" id="btnSet" onclick="show('settings')">Settings</button>
<button class="sec hide" id="btnOut" onclick="logout()">Logout</button></header>
<main>
<div id="msg"></div>

<section id="v-setup" class="hide"><div class="card"><h2>1. Create admin account</h2>
<p class="mut">This account protects the meter's page. It is stored only on the meter.</p>
<label>Username</label><input id="su" maxlength="32" autocomplete="username">
<label>Password (10+ characters with upper, lower, digit and symbol)</label><input id="sp" type="password" maxlength="64" autocomplete="new-password">
<label>Confirm password</label><input id="sp2" type="password" maxlength="64" autocomplete="new-password">
<p><button onclick="setupAdmin()">Create &amp; continue</button></p></div></section>

<section id="v-wifi" class="hide"><div class="card"><h2 id="wifiTitle">2. Connect to Wi-Fi</h2>
<div class="row"><span class="mut">Tap a network or type it below.</span><button class="sec" style="flex:0" onclick="scan(1)">Scan</button></div>
<ul class="nets" id="nets"><li class="mut">Scanning…</li></ul>
<label>Network name (SSID)</label><input id="ws" maxlength="32">
<label>Password</label><input id="wp" type="password" maxlength="63">
<p><button id="wbtn" onclick="saveWifi()">Connect</button> <button class="sec hide" id="wcancel" onclick="show('dash')">Cancel</button></p>
<p class="mut" id="meshOnlyBox">No Wi-Fi where this meter is? <button class="sec" onclick="if(confirm('Use mesh only? This meter will reach the server through a nearby meter that is on Wi-Fi (gateway). Its own page stays on the Power_… hotspot.'))post('/api/wifi/meshonly').then(r=>note(r.message||r.error,r.ok?'inf':'err'))">Use mesh only (no Wi-Fi here)</button></p>
<div id="wres"></div></div></section>

<section id="v-login" class="hide"><div class="card"><h2>Login</h2>
<label>Username</label><input id="lu" autocomplete="username">
<label>Password</label><input id="lp" type="password" autocomplete="current-password" onkeydown="if(event.key=='Enter')login()">
<p><button onclick="login()">Login</button></p></div></section>

<section id="v-dash" class="hide">
<div class="card" id="pairCard"><h2>Pair with server</h2>
<p class="mut">On the server: <b>Add New Meter</b>. Then use one of these:</p>
<div class="row"><button class="sec" onclick="document.getElementById('qrf').click()">&#128247; Take photo of QR</button></div>
<input type="file" id="qrf" accept="image/*" capture="environment" class="hide" onchange="readQr(this)">
<label>…or paste the pairing text from the server page</label><textarea id="ptxt" placeholder='{"pm":1,"h":"power.example.com","p":443,"s":1,"c":"K7P29QXM"}' oninput="fromText(this.value)"></textarea>
<label>…or type it</label>
<div class="row"><div style="flex:3"><label>Server address</label><input id="ph" placeholder="power.example.com"></div>
<div><label>Port</label><input id="pp" type="number" placeholder="443"></div></div>
<div class="chk"><input type="checkbox" id="pt" checked><span>Secure (https / wss)</span></div>
<div class="chk"><input type="checkbox" id="pi"><span>Skip certificate check (self-signed server only)</span></div>
<label>Pairing code</label><input id="pc" maxlength="9" placeholder="XXXX-XXXX" style="text-transform:uppercase;letter-spacing:.15em">
<p><button id="pbtn" onclick="pair()">Pair</button></p><div id="pres"></div></div>

<div class="card"><div class="row" style="margin-bottom:10px"><h2 style="margin:0" id="dName">Meter</h2>
<span style="flex:0;white-space:nowrap" id="dNet"></span></div>
<div class="row" style="margin-bottom:12px"><span class="mut" id="dTime"></span></div>
<div class="grid" id="dTop"></div>
<div id="dCh"></div>
<div id="dAux"></div>
<div class="row" style="margin-top:8px"><button class="sec" onclick="post('/api/send_now').then(r=>note(r.message||'Sent','inf'))">Send Data Now</button></div></div>

<div class="card"><h2>Session &amp; limits</h2><dl id="dSess"></dl></div>
<div class="card"><h2>Device</h2><dl id="dDev"></dl></div>
</section>

<section id="v-settings" class="hide">
<div class="card"><h2>Change password</h2>
<label>Current password</label><input id="op" type="password">
<label>New password</label><input id="np" type="password" maxlength="64">
<label>Confirm</label><input id="np2" type="password" maxlength="64">
<p><button onclick="chpw()">Change password</button></p></div>
<div class="card"><h2>Wi-Fi</h2><p class="mut">Connected to <b id="curSsid"></b>. Changing it restarts the meter.</p>
<button class="sec" onclick="show('wifi');scan(1)">Change Wi-Fi</button></div>
<div class="card"><h2>Mesh</h2><p class="mut">Auto: Wi-Fi first. Without Wi-Fi for 90 s the meter talks through a nearby meter that is online (gateway), and an online meter relays for others. Wi-Fi only: never uses or offers the mesh.</p>
<div class="row"><select id="mrole"><option value="auto">Auto (recommended)</option><option value="wifi">Wi-Fi only</option></select>
<button class="sec" style="flex:0" onclick="post('/api/mesh',{role:$('mrole').value}).then(r=>note(r.message||r.error,r.ok?'inf':'err'))">Save</button></div></div>
<div class="card"><h2>Firmware</h2><p class="mut">Installed: <b id="fwVer"></b></p>
<button class="sec" disabled title="Coming in a later version">Check OTA Update</button>
<p class="mut">Over-the-air updates from the server are coming in a later version.</p></div>
<div class="card"><h2>Maintenance</h2><div class="row">
<button class="sec" onclick="if(confirm('Restart the meter?'))post('/api/reboot').then(r=>note(r.message,'inf'))">Restart</button>
<button class="bad" onclick="if(confirm('Erase admin, Wi-Fi, pairing and buffered data? The meter returns to first setup.'))post('/api/factory_reset').then(r=>note(r.message,'inf'))">Factory reset</button></div>
<p class="mut">Or hold the BOOT button for 10 seconds.</p></div>
<p><button class="sec" onclick="show('dash')">&larr; Back</button></p>
</section>
</main>
<script>
var $=function(id){return document.getElementById(id)},S={},timer=null;
function hdr(){return{'Content-Type':'application/json','X-Time':String(Math.floor(Date.now()/1000))}}
function api(u,o){o=o||{};o.headers=hdr();o.credentials='same-origin';return fetch(u,o).then(function(r){return r.json().catch(function(){return{ok:false,error:'HTTP '+r.status}}).then(function(j){j._s=r.status;return j})}).catch(function(){return{ok:false,error:'Meter not reachable'}})}
function post(u,b){return api(u,{method:'POST',body:JSON.stringify(b||{})})}
function note(t,k){$('msg').innerHTML=t?'<div class="msg '+(k||'err')+'">'+esc(t)+'</div>':''}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function show(v){['setup','wifi','login','dash','settings'].forEach(function(n){$('v-'+n).classList.toggle('hide',n!=v)});
 var inside=v=='dash'||v=='settings';$('fwVer').textContent=S.fw||'';$('btnSet').classList.toggle('hide',!inside);$('btnOut').classList.toggle('hide',!inside);
 $('wcancel').classList.toggle('hide',!S.wifi);$('wifiTitle').textContent=S.wifi?'Change Wi-Fi':'2. Connect to Wi-Fi';
 if(v=='wifi')scan(0);note('')}
function route(){api('/api/status').then(function(s){S=s;$('hid').textContent=s.device_id?s.device_id+' · '+(s.sensor||'')+' · v'+s.fw:'';
 if(!s.admin)return show('setup');if(!s.auth)return show('login');if(!s.wifi)return show('wifi');
 show('dash');load();clearInterval(timer);timer=setInterval(load,3000)})}
function setupAdmin(){post('/api/setup/admin',{user:$('su').value,pass:$('sp').value,confirm:$('sp2').value}).then(function(r){if(!r.ok)return note(r.error);route()})}
function login(){post('/api/login',{user:$('lu').value,pass:$('lp').value}).then(function(r){if(!r.ok)return note(r.error);route()})}
function logout(){post('/api/logout').then(function(){clearInterval(timer);route()})}
function scan(force){api('/api/wifi/scan'+(force?'?refresh=1':'')).then(function(r){var ul=$('nets');
 if(!r.networks||!r.networks.length){ul.innerHTML='<li class="mut">'+(r.running?'Scanning…':'No networks found')+'</li>'}
 else{ul.innerHTML=r.networks.sort(function(a,b){return b.rssi-a.rssi}).map(function(n){return'<li data-s="'+esc(n.ssid)+'"><span>'+esc(n.ssid)+'</span><small class="mut">'+(n.open?'open ':'&#128274; ')+n.rssi+' dBm</small></li>'}).join('');
  ul.querySelectorAll('li').forEach(function(li){li.onclick=function(){$('ws').value=li.dataset.s;$('wp').focus()}})}
 if(r.running)setTimeout(function(){scan(0)},1500)})}
function saveWifi(){$('wbtn').disabled=true;post('/api/wifi',{ssid:$('ws').value,pass:$('wp').value}).then(function(r){
 if(!r.ok){$('wbtn').disabled=false;return note(r.error)}
 if(S.wifi){$('wres').innerHTML='<div class="msg inf">Saved. The meter is restarting and will join the new network.</div>';return}
 $('wres').innerHTML='<div class="msg inf">Connecting… (up to 20 s)</div>';pollWifi()})}
function pollWifi(){api('/api/wifi/status').then(function(r){
 if(r.status=='connecting')return setTimeout(pollWifi,1500);$('wbtn').disabled=false;
 if(r.status=='ok'){$('wres').innerHTML='<div class="msg okm"><b>Connected!</b><br>Meter address on your network: <b>http://'+esc(r.ip)+'/</b> (or http://'+esc(r.mdns)+'/).<br>Write it down, switch your phone back to your Wi-Fi and open that address. This setup hotspot turns off in 5 minutes.</div><button onclick="route()">Continue here for now</button>'}
 else $('wres').innerHTML='<div class="msg err">Could not connect. Check the name and password.</div>'})}
function dur(s){s=Math.max(0,Math.floor(s));var d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?d+' d '+h+' h '+m+' min':h?h+' h '+m+' min '+(s%60)+' sec':m+' min '+(s%60)+' sec'}
function fmt(e){if(!e)return'–';return new Date(e*1000).toLocaleString()}
function n(v,d){return v==null?'–':Number(v).toFixed(d)}
function pill(t,k){return'<span class="pill '+k+'">'+esc(t)+'</span>'}
function stat(x){return'<div class="stat"><small>'+x[0]+'</small><b>'+x[1]+'</b></div>'}
function load(){api('/api/state').then(function(d){if(d._s==401)return route();if(!d.state)return;var st=d.state,r=d.reading||{};
 var netMap={online:[d.link=='mesh'?'Online (mesh)':'Online','ok'],connecting:['Server offline','bad'],not_paired:['Not paired','warn'],wifi_connecting:['Wi-Fi connecting','warn'],setup:['Setup','warn']};
 var nm=netMap[d.net]||[d.net,''];$('dNet').innerHTML=pill(nm[0],nm[1]);$('curSsid').textContent=d.mesh_only?'(mesh only)':d.ssid;$('mrole').value=d.role||'auto';
 $('pairCard').classList.toggle('hide',d.paired);if(d.unauthorized)note('The server no longer accepts this meter. Pair it again.');
 $('dTime').textContent=(d.time?fmt(d.time):'Clock not set')+' · time from '+d.time_src+(d.rtc!=null?' · RTC (DS3231): '+(d.rtc?fmt(d.rtc):'not readable'):'');
 var c=d.cfg||{},cs=c.ch||[],ind=c.mode=='independent',sl=ind?(st.sessions||[]):[st.sess||{}],act={run:'run as usual',hold:'relay off',end:'end session'};
 function sstat(x){return st.disabled?pill('Disabled by server','bad'):x.status=='quota'?pill('kWh limit reached','warn'):x.status=='expired'?pill('Session ended','warn'):x.status=='ended'?pill('Session ended ('+(x.end_reason||'')+')','bad'):x.over?pill('Over kWh limit (power on)','warn'):x.sid?pill('Active','ok'):pill('No session','warn')}
 $('dTop').innerHTML=[['Voltage',r.voltage===false?'no input':r.ok?n(r.v,1)+' V':(r.err?'Sensor error':(st.ac?'–':'No AC'))],['Frequency',r.ok&&r.hz?r.hz+' Hz':'–'],['Sensor','rbAmp '+esc(r.variant||'?')]].concat((r.t||[]).map(function(t,i){return['Temperature T'+(i+1),t==null?'–':t+' °C'+(r.temp_high?' ⚠':'')]})).map(stat).join('');
 $('dCh').innerHTML=(r.ch||[]).map(function(x,i){var rl=(st.relay||[])[i]||{},s=ind?(sl[i]||{}):(sl[0]||{}),blk=st.disabled||rl.tripped||!s.sid||s.status=='quota'||s.status=='expired'||s.status=='ended';
  return'<div class="card" style="padding:12px;margin:10px 0 0"><div class="row"><b>CH'+(i+1)+(x.name?' · '+esc(x.name):'')+'</b><span style="flex:0;white-space:nowrap">'+(rl.on?pill('ON','ok'):pill('OFF'+(rl.block?' ('+rl.block+')':''),rl.tripped?'bad':''))+'</span></div>'+
  '<div class="grid" style="margin:8px 0">'+[['Current',r.ok?n(x.a,3)+' A':'–'],['Power',r.ok&&r.voltage!==false?n(x.w,1)+' W':'–'],['Power factor',r.ok&&r.voltage!==false?n(x.pf,2):'–'],['Energy',esc(x.e)+' kWh']].map(stat).join('')+'</div>'+
  (ind?'<p class="mut" style="margin:0 0 8px">Session #'+(s.sid||'–')+' '+sstat(s)+' · '+n(s.kwh,3)+(s.max?' / '+s.max:'')+' kWh</p>':'')+
  '<div class="row"><button class="ok" '+(blk?'disabled title="Blocked until the session is renewed / trip is reset"':'')+' onclick="relay('+i+',true)">ON</button><button class="sec" onclick="relay('+i+',false)">OFF</button>'+(rl.tripped?'<button class="bad" onclick="trip('+i+')">Reset Trip</button>':'')+'</div></div>'}).join('')||'<p class="mut">'+(st.no_sensor?'Power sensor (rbAmp) not found: check the I2C wiring.':'No channels.')+'</p>';
 $('dAux').innerHTML=(st.aux||[]).length?'<div class="card" style="padding:12px;margin:10px 0 0"><b>AUX relays</b> <span class="mut">(not metered, manual only)</span>'+st.aux.map(function(a){var nm=(c.aux||[])[a.i]||'';return'<div class="row" style="margin-top:8px"><span>AUX'+(a.i+1)+(nm?' · '+esc(nm):'')+' <small class="mut">relay '+a.relay+'</small></span><span style="flex:0;white-space:nowrap">'+(a.on?pill('ON','ok'):pill('OFF',''))+'</span><button class="ok" style="flex:0" onclick="aux('+a.i+',true)">ON</button><button class="sec" style="flex:0" onclick="aux('+a.i+',false)">OFF</button></div>'}).join('')+'</div>':'';
 var s0=sl[0]||{};
 $('dSess').innerHTML='<dt>Mode</dt><dd>'+(ind?'Independent (one session per channel)':'Shared (one session for all channels)')+'</dd>'+(ind?'':'<dt>Status</dt><dd>'+sstat(s0)+'</dd><dt>Session used</dt><dd>'+n(s0.kwh,3)+' kWh'+(s0.max?' of '+s0.max+' kWh (all channels)':'')+'</dd><dt>Session restarts</dt><dd>'+(c.sess_mode=='none'?'No time limit':fmt(s0.next_reset))+'</dd><dt>Auto renew</dt><dd>'+(c.auto?'Yes':'No')+'</dd>')+
  (st.tamper?'<dt>Panel</dt><dd>'+pill('Open','bad')+'</dd>':'')+'<dt>Max current</dt><dd>'+(cs.map(function(x,i){return'CH'+(i+1)+' '+(x.amp_max?x.amp_max+' A':'off')}).join(' · ')||'–')+'</dd><dt>Sensor timeout</dt><dd>'+(c.sensor_to_s?c.sensor_to_s+' s ('+act[c.sensor_action]+')':'Off')+'</dd><dt>Relay off when offline</dt><dd>'+(c.offline_off_min?'after '+c.offline_off_min+' min':'Off')+'</dd><dt>Read / send data</dt><dd>'+(c.drdy?'DRDY: every measurement window (~100 ms), raw':'every '+c.read_s+' s')+' / sent every '+c.report_s+' s</dd>';
 $('dDev').innerHTML='<dt>Device ID</dt><dd>'+esc(d.device_id)+'</dd><dt>Firmware</dt><dd>'+esc(d.fw)+'</dd><dt>Hardware</dt><dd>'+esc(d.hw_info||'')+'</dd>'+(d.bat_v!=null?'<dt>Battery</dt><dd>'+d.bat_v+' V (~'+d.bat_pct+' %)</dd>':'')+'<dt>Free memory</dt><dd>'+Math.round((d.heap||0)/1024)+' KB</dd><dt>Server</dt><dd>'+esc(d.server||'–')+'</dd><dt>Last sync</dt><dd>'+fmt(d.last_sync)+'</dd><dt>Waiting to send</dt><dd>'+d.outbox+' item(s)</dd><dt>Connection</dt><dd>'+(d.link=='mesh'?'Mesh via '+esc(d.gateway):d.link=='searching'?'Mesh: looking for a gateway…':d.link=='wifi'?'Wi-Fi '+esc(d.ssid)+' ('+d.rssi+' dBm) · '+esc(d.ip)+(d.leaves?' · relaying '+d.leaves+' meter(s)':''):'Not connected')+'</dd><dt>Uptime</dt><dd>'+Math.floor(d.uptime/3600)+' h '+Math.floor(d.uptime%3600/60)+' min</dd>'})}
function relay(ch,on){post('/api/relay',{ch:ch,on:on}).then(function(r){if(!r.ok)note(r.error);load()})}
function trip(ch){post('/api/reset_trip',{ch:ch}).then(load)}
function aux(i,on){post('/api/aux',{i:i,on:on}).then(function(r){if(!r.ok)note(r.error);load()})}
function fromText(t){try{var q=JSON.parse(t.trim());if(!q.h||!q.c)return;$('ph').value=q.h;$('pp').value=q.p||'';$('pt').checked=!!q.s;$('pc').value=q.c.slice(0,4)+'-'+q.c.slice(4);note('Pairing details filled in. Press Pair.','okm')}catch(e){}}
function readQr(inp){var f=inp.files[0];inp.value='';if(!f)return;
 if(!('BarcodeDetector' in window)){note('This browser cannot read QR codes from a photo. Paste the pairing text or type the details instead.');return}
 createImageBitmap(f).then(function(img){return new BarcodeDetector({formats:['qr_code']}).detect(img)}).then(function(c){
  if(!c.length)return note('No QR code found in the photo. Try again closer, or paste/type the details.');$('ptxt').value=c[0].rawValue;fromText(c[0].rawValue)})
  .catch(function(){note('Could not read the photo. Paste or type the details instead.')})}
function pair(){var b={host:$('ph').value.trim().replace(/^https?:\/\//,'').replace(/\/.*$/,''),port:+$('pp').value||0,tls:$('pt').checked,insecure:$('pi').checked,code:$('pc').value};
 $('pbtn').disabled=true;$('pres').innerHTML='<div class="msg inf">Pairing…</div>';
 post('/api/pair',b).then(function(r){if(!r.ok){$('pbtn').disabled=false;$('pres').innerHTML='';return note(r.error)}pollPair()})}
function pollPair(){api('/api/pair/status').then(function(r){if(r.status=='working')return setTimeout(pollPair,1000);$('pbtn').disabled=false;
 if(r.status=='ok'){$('pres').innerHTML='';note('Paired! Finish the setup on the server page.','okm');load()}else{$('pres').innerHTML='<div class="msg err">'+esc(r.status)+'</div>'}})}
function chpw(){post('/api/password',{old:$('op').value,pass:$('np').value,confirm:$('np2').value}).then(function(r){note(r.ok?r.message:r.error,r.ok?'okm':'err')})}
// Eye button on every password field: show / hide what was typed.
(function(){var E='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>',
 H='<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/><path d="M3 3l18 18"/></svg>';
 document.querySelectorAll('input[type=password]').forEach(function(i){var w=document.createElement('div');w.className='pw';i.parentNode.insertBefore(w,i);w.appendChild(i);
  var b=document.createElement('button');b.type='button';b.tabIndex=-1;b.title='Show password';b.innerHTML=E;
  b.onclick=function(){var sh=i.type=='password';i.type=sh?'text':'password';b.innerHTML=sh?H:E;b.title=sh?'Hide password':'Show password'};w.appendChild(b)})})();
route();
</script></body></html>)HTML";
