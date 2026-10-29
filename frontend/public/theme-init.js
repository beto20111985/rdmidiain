/*
 * RD Mídia Indoor — aplica o tema ANTES da primeira pintura (sem flash).
 * Arquivo externo (não inline) para continuar compatível com CSP `script-src 'self'`.
 * localStorage guarda SOMENTE a preferência de tema ("dark" | "light" | "system").
 */
;(function () {
  var pref = 'dark'
  try {
    var stored = window.localStorage.getItem('rd-theme')
    if (stored === 'dark' || stored === 'light' || stored === 'system') pref = stored
  } catch (e) {
    /* armazenamento indisponível: mantém o padrão escuro */
  }
  var resolved = pref
  if (pref === 'system') {
    resolved =
      window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  }
  document.documentElement.setAttribute('data-theme', resolved)
  var meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', resolved === 'light' ? '#f2f5f9' : '#05070a')
})()
