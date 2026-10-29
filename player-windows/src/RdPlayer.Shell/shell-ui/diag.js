// Página de diagnóstico da casca Windows (host virtual https://shell.rd.local/).
// Testa a ponte rd-native/1 (ida e volta), WebCrypto ES256 não exportável, IndexedDB, OPFS, armazenamento e autoplay.
// Usada pelo teste de fumaça (--smoke-test), pelo menu técnico e quando a configuração/bundle estão ausentes.
(function () {
  'use strict';
  var wv = window.chrome && window.chrome.webview;
  var pending = new Map();
  var REASONS = {
    smoke: 'Teste de fumaça em execução.',
    config: 'Configuração ausente ou inválida — o player não pode iniciar.',
    bundle_missing: 'Bundle do web player não encontrado na instalação.',
    tech_menu: 'Diagnóstico aberto pelo menu técnico.'
  };

  if (wv) {
    wv.addEventListener('message', function (ev) {
      var m = ev.data;
      if (m && m.rd === 1 && m.kind === 'response' && pending.has(m.id)) {
        var cb = pending.get(m.id);
        pending.delete(m.id);
        cb(m);
      }
    });
  }

  function call(method, params) {
    return new Promise(function (resolve, reject) {
      if (!wv) { reject(new Error('window.chrome.webview indisponível')); return; }
      var id = 'd' + Math.random().toString(36).slice(2, 12);
      var t = setTimeout(function () { pending.delete(id); reject(new Error('timeout')); }, 8000);
      pending.set(id, function (m) { clearTimeout(t); resolve(m); });
      wv.postMessage({ rd: 1, kind: 'request', id: id, method: method, params: params || {} });
    });
  }

  function notify(method, params) {
    if (wv) wv.postMessage({ rd: 1, kind: 'notify', method: method, params: params || {} });
  }

  function row(tbody, label, value, ok) {
    var tr = document.createElement('tr');
    var a = document.createElement('td');
    var b = document.createElement('td');
    a.textContent = label;
    b.textContent = value;
    if (ok === true) b.className = 'ok';
    if (ok === false) b.className = 'bad';
    tr.appendChild(a);
    tr.appendChild(b);
    tbody.appendChild(tr);
  }

  async function run() {
    var reason = new URLSearchParams(location.search).get('reason') || '';
    document.getElementById('reason').textContent = REASONS[reason] || '';
    var out = {
      href: location.href,
      secure_context: window.isSecureContext,
      native_present: !!window.RD_NATIVE,
      native_bridge: window.RD_NATIVE ? window.RD_NATIVE.bridge : null,
      checks: {},
      errors: []
    };

    try { var r1 = await call('get_info'); out.checks.bridge_get_info = !!r1.ok; out.info = r1.result; }
    catch (e) { out.checks.bridge_get_info = false; out.errors.push('get_info: ' + e.message); }
    try { var r2 = await call('get_telemetry'); out.checks.bridge_get_telemetry = !!r2.ok; out.telemetry = r2.result; }
    catch (e) { out.checks.bridge_get_telemetry = false; out.errors.push('get_telemetry: ' + e.message); }
    try { var r3 = await call('command', { command_id: 'diag', command: 'restart_app', args: {} }); out.checks.bridge_policy_blocks_commands = !r3.ok && r3.error && r3.error.code === 'POLICY_DENIED'; }
    catch (e) { out.checks.bridge_policy_blocks_commands = false; out.errors.push('command: ' + e.message); }
    out.bridge_roundtrip = out.checks.bridge_get_info && out.checks.bridge_get_telemetry;

    try {
      var kp = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
      var sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, kp.privateKey, new TextEncoder().encode('rd'));
      var exportable = true;
      try { await crypto.subtle.exportKey('pkcs8', kp.privateKey); } catch (e) { exportable = false; }
      out.checks.webcrypto_es256 = sig.byteLength === 64 && !exportable;
    } catch (e) { out.checks.webcrypto_es256 = false; out.errors.push('webcrypto: ' + e.message); }

    try {
      await new Promise(function (resolve, reject) {
        var req = indexedDB.open('rd-shell-diag', 1);
        req.onupgradeneeded = function () { req.result.createObjectStore('kv'); };
        req.onsuccess = function () {
          var db = req.result;
          var tx = db.transaction('kv', 'readwrite');
          tx.objectStore('kv').put(Date.now(), 'last');
          tx.oncomplete = function () { db.close(); resolve(); };
          tx.onerror = function () { reject(tx.error); };
        };
        req.onerror = function () { reject(req.error); };
      });
      out.checks.indexeddb = true;
    } catch (e) { out.checks.indexeddb = false; out.errors.push('indexeddb: ' + e); }

    try {
      var root = await navigator.storage.getDirectory();
      var fh = await root.getFileHandle('rd-shell-diag.txt', { create: true });
      var w = await fh.createWritable();
      await w.write('ok');
      await w.close();
      out.checks.opfs = (await (await fh.getFile()).text()) === 'ok';
    } catch (e) { out.checks.opfs = false; out.errors.push('opfs: ' + e); }

    try { out.checks.persistent_storage = await navigator.storage.persist(); }
    catch (e) { out.checks.persistent_storage = false; out.errors.push('persist: ' + e); }

    try {
      var est = await navigator.storage.estimate();
      out.storage = { persisted: await navigator.storage.persisted(), quota_mb: Math.round((est.quota || 0) / 1048576), usage_mb: Math.round((est.usage || 0) / 1048576) };
    } catch (e) { out.errors.push('storage: ' + e); }

    try {
      var ac = new AudioContext();
      await new Promise(function (r) { setTimeout(r, 300); });
      out.checks.autoplay_no_gesture = ac.state === 'running';
      await ac.close();
    } catch (e) { out.checks.autoplay_no_gesture = false; out.errors.push('autoplay: ' + e); }

    out.user_agent = navigator.userAgent;

    var tb = document.querySelector('#checks tbody');
    row(tb, 'Contexto seguro (https)', String(out.secure_context), out.secure_context);
    row(tb, 'window.RD_NATIVE', out.native_present ? out.native_bridge : 'ausente', out.native_present);
    Object.keys(out.checks).forEach(function (k) { row(tb, k, out.checks[k] ? 'ok' : 'falhou', !!out.checks[k]); });
    if (out.storage) row(tb, 'Armazenamento', 'quota ' + out.storage.quota_mb + ' MB, uso ' + out.storage.usage_mb + ' MB, persistente: ' + out.storage.persisted);
    out.errors.forEach(function (e) { row(tb, 'erro', e, false); });

    var sys = document.querySelector('#system tbody');
    var t = out.telemetry || {};
    var info = out.info || {};
    row(sys, 'Versão do player', info.shell_version || '-');
    row(sys, 'Windows', t.os || info.os || '-');
    row(sys, 'Resolução / orientação', (t.resolution || '-') + ' / ' + (t.orientation || '-'));
    row(sys, 'CPU', t.cpu_pct == null ? '-' : t.cpu_pct + ' %');
    row(sys, 'RAM', t.ram_total_mb == null ? '-' : t.ram_used_mb + ' / ' + t.ram_total_mb + ' MB');
    row(sys, 'Disco livre', t.disk_free_mb == null ? '-' : t.disk_free_mb + ' / ' + t.disk_total_mb + ' MB');
    row(sys, 'Uptime', t.uptime_s == null ? '-' : Math.round(t.uptime_s / 60) + ' min');
    row(sys, 'Wi-Fi', t.wifi ? (t.wifi.ssid + ' (' + t.wifi.rssi_dbm + ' dBm)') : 'sem Wi-Fi / cabo');
    row(sys, 'API', (info.config && info.config.api_base_url) || '(não configurada)');

    var cfg = info.config || (window.RD_NATIVE && window.RD_NATIVE.config) || {};
    if (cfg.config_errors && cfg.config_errors.length) {
      document.getElementById('config-section').hidden = false;
      var ul = document.getElementById('config-errors');
      cfg.config_errors.forEach(function (e) { var li = document.createElement('li'); li.textContent = e; ul.appendChild(li); });
    }

    notify('diag_result', out);
  }

  run();
  // sinal de vida para o watchdog não recarregar a página de diagnóstico em laço
  notify('progress', { state: 'ERROR' });
  setInterval(function () { notify('progress', { state: 'ERROR' }); }, 5000);
})();
