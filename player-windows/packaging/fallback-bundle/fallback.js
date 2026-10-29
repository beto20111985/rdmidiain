// Bundle provisório (usado só quando o player-web não está buildado): informa "ready"/"progress" à casca
// pela ponte rd-native/1 para que o watchdog não entre em laço de recarga.
(function () {
  'use strict';
  var wv = window.chrome && window.chrome.webview;
  var n = window.RD_NATIVE;
  var el = document.getElementById('native');
  if (el) el.textContent = n ? ('Casca ' + n.platform + ' ' + n.shell_version + ' — ponte ' + n.bridge) : 'Fora da casca Windows (navegador comum).';
  function notify(method, params) {
    if (wv) wv.postMessage({ rd: 1, kind: 'notify', method: method, params: params || {} });
  }
  notify('ready', { bundle_version: 'fallback-0' });
  notify('progress', { state: 'IDLE' });
  setInterval(function () { notify('progress', { state: 'IDLE' }); }, 5000);
})();
