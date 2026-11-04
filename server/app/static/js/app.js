/* PowerManager front-end helpers (no build step). */
var PM = (function () {
  var csrf = (document.querySelector('meta[name=csrf-token]') || {}).content;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().catch(function () { return {ok: false, error: 'HTTP ' + r.status}; }); });
  }

  function poll(url, ms, cb) {
    var stopped = false, timer;
    function run() {
      if (stopped) return;
      fetch(url, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d && !stopped) cb(d); })
        .catch(function () {})
        .finally(function () { if (!stopped) timer = setTimeout(run, ms); });
    }
    run();
    return function () { stopped = true; clearTimeout(timer); };
  }

  function toast(msg, kind) {
    var box = document.getElementById('toasts');
    var el = document.createElement('div');
    el.className = 'toast align-items-center text-bg-' + (kind || 'secondary') + ' border-0';
    el.innerHTML = '<div class="d-flex"><div class="toast-body">' + esc(msg) + '</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>';
    box.appendChild(el);
    var t = new bootstrap.Toast(el, {delay: 4000}); t.show();
    el.addEventListener('hidden.bs.toast', function () { el.remove(); });
  }

  function bindActions(url) {
    document.querySelectorAll('[data-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
        btn.disabled = true;
        post(url, {action: btn.dataset.action}).then(function (r) {
          if (r.ok && r.redirect) { location.href = r.redirect; return; }
          toast(r.message || (r.ok ? 'Done' : r.error), r.ok ? (r.queued ? 'info' : 'success') : 'danger');
        }).finally(function () { btn.disabled = false; });
      });
    });
  }

  var STATUS = {active: ['success', 'Active'], quota: ['warning', 'kWh limit reached'], expired: ['warning', 'Session ended'],
                tripped: ['danger', 'Over-current trip'], unknown: ['secondary', 'No data yet'],
                over: ['warning', 'Over kWh limit (power on)'], ended_voltage: ['danger', 'Session ended (voltage)'],
                ended_tamper: ['danger', 'Session ended (tamper)'], ended_: ['warning', 'Session ended'], ended_disabled: ['secondary', 'Session ended (disabled)'],
                disabled: ['dark', 'Disabled']};
  var INCIDENT = {power: '<span class="badge text-bg-warning"><i class="bi bi-lightning"></i> No power</span>',
                  trip: '<span class="badge text-bg-danger"><i class="bi bi-exclamation-octagon"></i> Trip</span>',
                  tamper: '<span class="badge pm-bg-purple"><i class="bi bi-unlock"></i> Tamper</span>',
                  voltage: '<span class="badge pm-bg-orange"><i class="bi bi-activity"></i> Voltage</span>'};

  function set(root, field, fn) {
    root.querySelectorAll('[data-field="' + field + '"]').forEach(fn);
  }

  function updateMeterCard(root, m) {
    set(root, 'online', function (el) { el.className = 'pm-dot ' + (m.online ? 'on' : 'off'); el.title = m.online ? 'Online' : 'Offline'; });
    set(root, 'w', function (el) { el.textContent = m.w == null ? '–' : m.w; });
    var WHY = {disabled: 'disabled', tripped: 'trip', quota: 'kWh limit', expired: 'session ended', ended: 'session ended', offline: 'offline',
               voltage: 'voltage', tamper: 'tamper'};
    set(root, 'relay', function (el) {
      el.className = 'badge rounded-pill ' + (m.relay_on ? 'text-bg-success' : 'text-bg-secondary');
      el.textContent = 'Relay ' + (m.relay_on ? 'ON' : 'OFF' + (m.relay_block && WHY[m.relay_block] ? ' · ' + WHY[m.relay_block] : ''));
    });
    set(root, 'kwh', function (el) { el.textContent = (m.kwh == null ? '–' : Number(m.kwh).toFixed(3)) + ' / ' + (m.kwh_max ? m.kwh_max : '∞') + ' kWh'; });
    set(root, 'kwh-bar', function (el) {
      var pct = m.kwh_max ? Math.min(100, (m.kwh || 0) / m.kwh_max * 100) : 0;
      el.style.width = pct + '%'; el.classList.toggle('bg-warning', pct >= 100);
    });
    set(root, 'status', function (el) { var s = STATUS[m.status] || ['secondary', m.status]; el.innerHTML = '<span class="badge text-bg-' + s[0] + '">' + esc(s[1]) + '</span>'; });
    set(root, 'ongoing', function (el) { el.innerHTML = m.ongoing.map(function (t) { return INCIDENT[t] || ''; }).join(' '); });
    set(root, 'last_seen', function (el) { el.textContent = m.last_seen; });
    set(root, 'cost', function (el) { el.textContent = m.cost || '–'; });
    set(root, 'cost-row', function (el) { el.classList.toggle('d-none', !m.cost); });
    set(root, 'reading_at', function (el) { el.textContent = m.reading_at; });
    set(root, 'next_reset', function (el) { el.textContent = m.next_reset || ''; });
  }

  // ------------------------------------------------------------ session time helpers
  function dur(sec) {
    sec = Math.max(0, Math.floor(sec));
    var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return d + ' d ' + h + ' h ' + m + ' min';
    if (h) return h + ' h ' + m + ' min ' + s + ' sec';
    return m + ' min ' + s + ' sec';
  }
  // {pct, text, cls}: how much of the session time is used, and what is left
  function sessionTime(m, now) {
    if (m.sess_mode === 'none') return null;
    if (m.sess_ended) return {pct: 100, text: m.auto && m.next_reset_ts ? 'Ended, renews in ' + dur(m.next_reset_ts - now) : 'Session ended', cls: 'bg-secondary'};
    if (!m.next_reset_ts || !m.sess_start_ts) return {pct: 0, text: '–', cls: 'bg-info'};
    var total = m.next_reset_ts - m.sess_start_ts, used = now - m.sess_start_ts;
    var pct = total > 0 ? Math.min(100, Math.max(0, used / total * 100)) : 0;
    var label = m.auto ? ' left (auto renew)' : ' left';
    return {pct: pct, text: dur(m.next_reset_ts - now) + label, cls: pct >= 90 ? 'bg-warning' : 'bg-info'};
  }

  // ------------------------------------------------------------ dashboard
  function dashboard(opt) {
    var meters = {}, offset = 0;
    function store(list, serverTime) {
      list.forEach(function (m) { meters[m.id] = m; if (m.server_time) offset = m.server_time - Date.now() / 1000; });
    }
    function tick() {
      var now = Date.now() / 1000 + offset;
      Object.keys(meters).forEach(function (id) {
        var card = document.querySelector('[data-meter="' + id + '"]');
        if (!card) return;
        var t = sessionTime(meters[id], now);
        set(card, 'time-row', function (el) { el.classList.toggle('d-none', !t); });
        if (!t) return;
        set(card, 'time-left', function (el) { el.textContent = t.text; });
        set(card, 'time-bar', function (el) { el.style.width = t.pct + '%'; el.className = 'progress-bar ' + t.cls; });
      });
    }
    store(opt.data);
    tick();
    setInterval(tick, 1000);
    poll(opt.poll, 5000, function (data) {
      store(data.meters);
      data.meters.forEach(function (m) {
        var card = document.querySelector('[data-meter="' + m.id + '"]');
        if (card) updateMeterCard(card, m);
      });
      tick();
    });

    // selection (admin)
    var bar = document.getElementById('selBar');
    if (!bar) return;
    function selected() { return Array.prototype.map.call(document.querySelectorAll('.pm-sel:checked'), function (c) { return +c.value; }); }
    function refresh() {
      var ids = selected();
      bar.classList.toggle('d-none', ids.length === 0);
      document.getElementById('selCount').textContent = ids.length;
      document.querySelectorAll('.pm-sel').forEach(function (c) { c.closest('.meter-card').classList.toggle('pm-selected', c.checked); });
    }
    document.querySelectorAll('.pm-sel').forEach(function (c) { c.addEventListener('change', refresh); });
    document.getElementById('selAll').onclick = function () { document.querySelectorAll('.pm-sel').forEach(function (c) { c.checked = true; }); refresh(); };
    document.getElementById('selNone').onclick = function () { document.querySelectorAll('.pm-sel').forEach(function (c) { c.checked = false; }); refresh(); };
    document.getElementById('bulkRenew').onclick = function () {
      var ids = selected();
      if (!ids.length || !confirm('Start a new session now on ' + ids.length + ' meter(s)?')) return;
      post(opt.renew, {ids: ids}).then(function (r) {
        toast(r.message || r.error, r.ok ? 'success' : 'danger');
      });
    };
    document.getElementById('bulkCfg').onclick = function () {
      var ids = selected();
      if (ids.length) location.href = opt.configure + '?ids=' + ids.join(',');
    };
  }

  // ------------------------------------------------------------ meter info page
  function fmt(v, d) { return v == null ? '–' : Number(v).toFixed(d); }

  function renderLatest(m) {
    var box = document.getElementById('latest');
    if (!m.reading || !m.reading.length) { box.innerHTML = '<div class="text-body-secondary">No readings yet.</div>'; return; }
    var html = '';
    m.reading.forEach(function (c) {
      if (m.reading.length > 1) html += '<div class="fw-semibold small mb-1">Channel ' + (c.i + 1) + '</div>';
      html += '<div class="row g-2 mb-2">' +
        [['Voltage', fmt(c.v, 1), 'V'], ['Current', fmt(c.a, 3), 'A'], ['Power', fmt(c.w, 1), 'W'],
         ['Power factor', fmt(c.pf, 2), ''], ['Frequency', fmt(c.hz, 1), 'Hz'], ['Energy total', fmt(c.e, 3), 'kWh']]
        .map(function (x) { return '<div class="col-6 col-md-4"><div class="pm-stat"><div class="small text-body-secondary">' + x[0] + '</div><div class="pm-big">' + x[1] + ' <small class="fs-6 text-body-secondary">' + x[2] + '</small></div></div></div>'; }).join('') +
        '</div>';
    });
    var remain = m.kwh_max ? Math.max(0, m.kwh_max - (m.kwh || 0)) : null;
    var col = m.cost ? 'col-6 col-md-4' : 'col-6';
    html += '<div class="row g-2"><div class="' + col + '"><div class="pm-stat"><div class="small text-body-secondary">Session used</div><div class="pm-big">' + fmt(m.kwh, 3) + ' <small class="fs-6 text-body-secondary">kWh</small></div></div></div>' +
      '<div class="' + col + '"><div class="pm-stat"><div class="small text-body-secondary">Balance</div><div class="pm-big">' + (remain == null ? '∞' : remain.toFixed(3)) + ' <small class="fs-6 text-body-secondary">kWh</small></div></div></div>' +
      (m.cost ? '<div class="col-12 col-md-4"><div class="pm-stat"><div class="small text-body-secondary">Session cost</div><div class="pm-big">' + esc(m.cost) + '</div></div></div>' : '') + '</div>';
    box.innerHTML = html;
  }

  function meterInfo(opt) {
    var head = document.getElementById('meterHead');
    function onLive(m) {
      updateMeterCard(document.body, m);
      renderLatest(m);
      var on = document.getElementById('btnRelayOn');
      if (on) {
        var blocked = m.status === 'quota' || m.status === 'expired' || m.status === 'tripped' || m.status.indexOf('ended') === 0;
        on.disabled = blocked;
        on.title = blocked ? 'Blocked until the session is renewed / trip is reset' : '';
      }
      var rt = document.getElementById('btnResetTrip');
      if (rt) rt.classList.toggle('d-none', m.status !== 'tripped');
    }
    // ---- session timer (ticks every second between live updates)
    var lastLive = opt.initial, clockOffset = 0;
    function dur(sec) {
      sec = Math.max(0, Math.floor(sec));
      var d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
      if (d) return d + ' d ' + h + ' h ' + m + ' min';
      if (h) return h + ' h ' + m + ' min ' + s + ' sec';
      return m + ' min ' + s + ' sec';
    }
    function tick() {
      var m = lastLive, now = Date.now() / 1000 + clockOffset;
      var ended = !!m.sess_ended;  // with "power off, end at time limit" a quota session keeps running
      set(document.body, 'sess_started', function (el) { el.textContent = m.sess_start || '–'; });
      set(document.body, 'sess_elapsed', function (el) {
        el.textContent = m.sess_start_ts ? dur((m.sess_end_ts || now) - m.sess_start_ts) + (ended ? ' (ended)' : '') : '–';
      });
      set(document.body, 'sess_left', function (el) {
        if (ended) el.textContent = 'Session ended';
        else if (m.status === 'quota' && !m.next_reset_ts) el.textContent = 'Power off (kWh limit): ends on Renew';
        else if (m.sess_mode === 'none') el.textContent = 'No time limit';
        else if (m.next_reset_ts) el.textContent = dur(m.next_reset_ts - now);
        else el.textContent = '– (meter clock not set)';
      });
    }
    var origOnLive = onLive;
    onLive = function (m) { lastLive = m; if (m.server_time) clockOffset = m.server_time - Date.now() / 1000; origOnLive(m); tick(); };
    setInterval(tick, 1000);

    onLive(opt.initial);
    poll(opt.live, 5000, onLive);
    if (opt.action) bindActions(opt.action);

    // ---- Get meter info (admin): ask the meter, wait for its "info" message, show it
    var mi = document.getElementById('btnMeterInfo');
    function infoTable(d) {
      var i = d.info || {}, rows = [
        ['IP address', i.ip], ['Wi-Fi', i.ssid ? i.ssid + (i.rssi ? ' (' + i.rssi + ' dBm)' : '') : null], ['MAC', i.mac],
        ['Battery', i.bat_v != null ? Number(i.bat_v).toFixed(2) + ' V' + (i.bat_pct != null ? ' · ~' + i.bat_pct + ' %' : '') : 'not enabled (config.h)'],
        ['Free heap', i.heap != null ? Math.round(i.heap / 1024) + ' KB' + (i.heap_min != null ? ' (lowest ' + Math.round(i.heap_min / 1024) + ' KB)' : '') : null],
        ['PSRAM', i.psram ? Math.round(i.psram / 1024) + ' KB' + (i.psram_free != null ? ' (' + Math.round(i.psram_free / 1024) + ' KB free)' : '') : 'none'],
        ['Firmware', i.fw], ['Hardware', i.hw], ['Hardware info', i.hw_info], ['Chip', i.chip ? i.chip + (i.flash ? ' · ' + Math.round(i.flash / 1048576) + ' MB flash' : '') : null],
        ['Core', i.core], ['Uptime', i.uptime != null ? dur(i.uptime) : null], ['Boot #', i.boot], ['Clock from', i.time_src], ['Waiting to send', i.outbox != null ? i.outbox + ' item(s)' : null]];
      return '<table class="table table-sm mb-2">' + rows.filter(function (r) { return r[1] != null && r[1] !== ''; })
        .map(function (r) { return '<tr><th class="text-body-secondary fw-normal">' + r[0] + '</th><td>' + esc(r[1]) + '</td></tr>'; }).join('') +
        '</table><div class="small text-body-secondary">Received ' + esc(d.at_local) + '</div>';
    }
    if (mi) mi.addEventListener('click', function () {
      var body = document.getElementById('infoBody'), modal = bootstrap.Modal.getOrCreateInstance('#infoModal');
      body.innerHTML = '<div class="text-center py-4"><span class="spinner-border"></span><div class="mt-2">Asking the meter…</div></div>';
      modal.show();
      fetch(opt.deviceInfo, {credentials: 'same-origin'}).then(function (x) { return x.json(); }).then(function (prev) {
        post(opt.action, {action: 'get_info'}).then(function (r) {
          if (!r.ok) {
            body.innerHTML = '<div class="alert alert-warning">' + esc(r.message || r.error) + '</div>' + (prev.at ? '<div class="small mb-2">Last known info:</div>' + infoTable(prev) : '');
            return;
          }
          var tries = 0;
          (function check() {
            fetch(opt.deviceInfo, {credentials: 'same-origin'}).then(function (x) { return x.json(); }).then(function (d) {
              if (d.at && d.at !== prev.at) return (body.innerHTML = infoTable(d));
              if (++tries >= 15) return (body.innerHTML = '<div class="alert alert-warning">The meter did not answer (firmware older than 0.5?).</div>' + (prev.at ? infoTable(prev) : ''));
              setTimeout(check, 1000);
            });
          })();
        });
      });
    });

    // ---- select + delete historical events (admin)
    var incBar = document.getElementById('incBar');
    if (incBar) {
      var boxes = function () { return Array.prototype.slice.call(document.querySelectorAll('.pm-inc')); };
      var refreshInc = function () {
        var n = boxes().filter(function (c) { return c.checked; }).length;
        incBar.classList.toggle('d-none', !n); document.getElementById('incCount').textContent = n;
      };
      boxes().forEach(function (c) { c.addEventListener('change', refreshInc); });
      document.getElementById('incAll').addEventListener('change', function (e) { boxes().forEach(function (c) { c.checked = e.target.checked; }); refreshInc(); });
      document.getElementById('incDelete').addEventListener('click', function () {
        var ids = boxes().filter(function (c) { return c.checked; }).map(function (c) { return +c.value; });
        if (!ids.length || !confirm('Delete ' + ids.length + ' historical event(s)? This cannot be undone.')) return;
        post(opt.incDelete, {ids: ids}).then(function (r) { toast(r.message || r.error, r.ok ? 'success' : 'danger'); if (r.ok) setTimeout(function () { location.reload(); }, 600); });
      });
    }

    // ---- Get Data Now: ask the meter for a reading and wait for it to arrive
    var gd = document.getElementById('btnGetData');
    if (gd) gd.addEventListener('click', function () {
      var before = lastLive.reading_ts || 0, html = gd.innerHTML;
      gd.disabled = true; gd.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Waiting…';
      function done(msg, kind) { gd.disabled = false; gd.innerHTML = html; if (msg) toast(msg, kind); }
      post(opt.refresh, {}).then(function (r) {
        if (!r.ok) return done(r.message || r.error, 'danger');
        var tries = 0;
        (function check() {
          fetch(opt.live, {credentials: 'same-origin'}).then(function (x) { return x.json(); }).then(function (m) {
            if ((m.reading_ts || 0) > before) { onLive(m); return done('Latest data received', 'success'); }
            if (++tries >= 15) return done('The meter did not answer yet; it will show up when it does.', 'warning');
            setTimeout(check, 1000);
          }).catch(function () { setTimeout(check, 1000); });
        })();
      });
    });

    // history
    var state = {range: '1d', series: 'w', from: '', to: '', ch: 0};
    var chart = null, last = null;
    var colors = {w: '#0d6efd', a: '#fd7e14', v: '#198754', kwh: '#6f42c1'};
    var units = {w: 'W', a: 'A', v: 'V', kwh: 'kWh'};

    function query() {
      var q = 'range=' + state.range + '&ch=' + state.ch;
      if (state.range === 'custom') {
        q += '&from=' + encodeURIComponent(state.from) + '&to=' + encodeURIComponent(state.to);
        if (state.startTs && state.endTs) q += '&start_ts=' + state.startTs + '&end_ts=' + state.endTs;
      }
      return q;
    }
    function localInput(ts) {  // epoch -> "YYYY-MM-DDTHH:MM" in the server's display time zone
      return new Date((ts + (last ? last.tz_offset_s : 0)) * 1000).toISOString().slice(0, 16);
    }
    function showCustom(on) {
      var cr = document.getElementById('customRange');
      cr.classList.toggle('d-none', !on); cr.classList.toggle('d-flex', on);
      document.querySelectorAll('#rangeBtns .active').forEach(function (x) { x.classList.remove('active'); });
      document.querySelector('#rangeBtns [data-range="' + (on ? 'custom' : state.range) + '"]').classList.add('active');
    }
    // "Historical" on a session row: show exactly that session's time range.
    document.querySelectorAll('[data-hist-from]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.range = 'custom';
        state.startTs = +btn.dataset.histFrom; state.endTs = +btn.dataset.histTo;
        state.from = localInput(state.startTs); state.to = localInput(state.endTs + 59);
        document.getElementById('fromIn').value = state.from;
        document.getElementById('toIn').value = state.to;
        showCustom(true);
        load();
        document.getElementById('historyCard').scrollIntoView({behavior: 'smooth', block: 'start'});
        toast(btn.dataset.histLabel + ' shown in Historical data', 'info');
      });
    });
    function label(t, span, off) {
      var d = new Date((t + off) * 1000), p = function (n) { return String(n).padStart(2, '0'); };
      var hm = p(d.getUTCHours()) + ':' + p(d.getUTCMinutes());
      var md = p(d.getUTCDate()) + '/' + p(d.getUTCMonth() + 1);
      return span <= 86400 ? hm : (span <= 7 * 86400 ? md + ' ' + hm : md + '/' + d.getUTCFullYear());
    }
    function draw() {
      if (!last) return;
      var s = state.series, span = last.end - last.start, off = last.tz_offset_s;
      var pts = last.points, labels = pts.map(function (p) { return label(p.t, span, off); });
      var datasets;
      if (s === 'kwh') {
        if (last.source === 'raw') {
          datasets = [{label: 'Energy used since range start (kWh)', data: pts.map(function (p) { return p.kwh_cum; }), borderColor: colors.kwh, backgroundColor: colors.kwh + '22', fill: true, pointRadius: 0, borderWidth: 2}];
        } else {
          datasets = [{type: 'bar', label: 'kWh per ' + (last.source === 'h' ? 'hour' : 'day'), data: pts.map(function (p) { return p.kwh; }), backgroundColor: colors.kwh}];
        }
      } else {
        datasets = [
          {label: 'Avg ' + units[s], data: pts.map(function (p) { return p[s]; }), borderColor: colors[s], backgroundColor: colors[s], pointRadius: 0, borderWidth: 2, tension: .2},
          {label: 'Max', data: pts.map(function (p) { return p[s + '_max']; }), borderColor: colors[s] + '55', pointRadius: 0, borderWidth: 1, fill: false},
          {label: 'Min', data: pts.map(function (p) { return p[s + '_min']; }), borderColor: colors[s] + '55', backgroundColor: colors[s] + '18', pointRadius: 0, borderWidth: 1, fill: '-1'}
        ];
      }
      if (chart) chart.destroy();
      chart = new Chart(document.getElementById('chart'), {
        type: 'line', data: {labels: labels, datasets: datasets},
        options: {responsive: true, maintainAspectRatio: false, animation: false, interaction: {mode: 'index', intersect: false},
                  scales: {x: {ticks: {maxTicksLimit: 10, autoSkip: true}}, y: {beginAtZero: s !== 'v'}},
                  plugins: {legend: {display: true, labels: {boxWidth: 12}}}}
      });
      var st = last.stats, html = '';
      [['v', 'Voltage', 'V', 1], ['a', 'Current', 'A', 3], ['w', 'Power', 'W', 1]].forEach(function (x) {
        var k = st[x[0]] || {};
        html += '<div class="col-6 col-md-3"><div class="pm-stat"><div class="text-body-secondary">' + x[1] + ' (' + x[2] + ')</div>' +
          '<div>min <b>' + fmt(k.min, x[3]) + '</b> · max <b>' + fmt(k.max, x[3]) + '</b> · avg <b>' + fmt(k.avg, x[3]) + '</b></div></div></div>';
      });
      html += '<div class="col-6 col-md-3"><div class="pm-stat"><div class="text-body-secondary">Energy used</div><div><b>' + fmt(st.kwh, 3) + '</b> kWh' + (last.cost ? ' · <b>' + esc(last.cost) + '</b>' : '') + ' · ' + (st.n || 0) + ' samples</div></div></div>';
      document.getElementById('stats').innerHTML = html;
      document.getElementById('rangeLabel').textContent = last.start_local + ' → ' + last.end_local + ' (' + {raw: 'raw', h: 'hourly', d: 'daily'}[last.source] + ')';
    }
    function load() {
      var csvBtn = document.getElementById('csvBtn');
      if (csvBtn) csvBtn.href = opt.csv + '?' + query();
      fetch(opt.history + '?' + query(), {credentials: 'same-origin'}).then(function (r) { return r.json(); })
        .then(function (d) { last = d; draw(); });
    }
    document.querySelectorAll('#rangeBtns [data-range]').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('#rangeBtns .active').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        var custom = b.dataset.range === 'custom';
        var cr = document.getElementById('customRange');
        cr.classList.toggle('d-none', !custom); cr.classList.toggle('d-flex', custom);
        if (!custom) { state.range = b.dataset.range; state.startTs = state.endTs = null; load(); }
      });
    });
    document.getElementById('applyCustom').addEventListener('click', function () {
      state.range = 'custom'; state.startTs = state.endTs = null;
      state.from = document.getElementById('fromIn').value; state.to = document.getElementById('toIn').value; load();
    });
    document.querySelectorAll('#seriesBtns [data-series]').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('#seriesBtns .active').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active'); state.series = b.dataset.series; draw();
      });
    });
    var chSel = document.getElementById('chSel');
    if (chSel) chSel.addEventListener('change', function () { state.ch = +chSel.value; load(); });
    load();
    setInterval(function () { if (state.range === '1h' || state.range === '1d') load(); }, 60000);
  }

  // ------------------------------------------------------------ global UI
  document.addEventListener('DOMContentLoaded', function () {
    var tt = document.getElementById('themeToggle');
    if (tt) tt.addEventListener('click', function () {
      var t = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-bs-theme', t); localStorage.setItem('pm-theme', t);
    });
    var st = document.getElementById('sidebarToggle');
    if (st) st.addEventListener('click', function (e) { e.stopPropagation(); document.getElementById('pmSidebar').classList.toggle('show'); });
    document.addEventListener('click', function (e) {
      var sb = document.getElementById('pmSidebar');
      if (sb && sb.classList.contains('show') && !sb.contains(e.target)) sb.classList.remove('show');
    });
    document.querySelectorAll('[data-copy]').forEach(function (el) {
      el.title = 'Click to copy';
      el.addEventListener('click', function () { navigator.clipboard && navigator.clipboard.writeText(el.textContent.trim()).then(function () { toast('Copied', 'success'); }); });
    });
    document.querySelectorAll('[data-strength]').forEach(function (inp) {
      var bar = inp.parentElement.querySelector('[data-strength-bar]');
      inp.addEventListener('input', function () {
        var v = inp.value, s = 0;
        if (v.length >= 10) s++; if (/[a-z]/.test(v) && /[A-Z]/.test(v)) s++; if (/\d/.test(v)) s++; if (/[^A-Za-z0-9]/.test(v)) s++; if (v.length >= 14) s++;
        bar.style.width = (s * 20) + '%';
        bar.className = 'progress-bar ' + (s < 3 ? 'bg-danger' : s < 4 ? 'bg-warning' : 'bg-success');
      });
    });
  });

  return {post: post, poll: poll, toast: toast, esc: esc, bindActions: bindActions, updateMeterCard: updateMeterCard, meterInfo: meterInfo,
          dashboard: dashboard, sessionTime: sessionTime, dur: dur};
})();
