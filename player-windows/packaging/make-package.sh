#!/usr/bin/env bash
# Empacota o Player Windows (rodar no host, Git Bash, depois de packaging/build.sh no container).
#   bash packaging/make-package.sh [pasta-do-bundle-web]
# Bundle web: argumento > dist/web-bundle (build do player-web) > packaging/fallback-bundle (provisório).
# Saída (dist/ é ignorado pelo .gitignore):
#   dist/RdPlayer-Windows-<versão>-win-x64.zip   pacote de instalação (install.ps1 + app\current + watchdog)
#   dist/RdPlayer-Windows-<versão>-update.zip    pacote de atualização OTA (conteúdo de app\current + watchdog)
#   dist/PACKAGE_INFO.txt                        tamanhos e sha256
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VER="$(sed -n 's:.*<RdPlayerVersion>\(.*\)</RdPlayerVersion>.*:\1:p' "$ROOT/Directory.Build.props")"
BUILD="$ROOT/dist/build/sc"
TAR_EXE="/c/Windows/System32/tar.exe"   # bsdtar do Windows 10+ (gera .zip com separador "/")

WEB="${1:-}"
if [ -z "$WEB" ]; then
  if [ -f "$ROOT/dist/web-bundle/index.html" ]; then WEB="$ROOT/dist/web-bundle"; else WEB="$ROOT/packaging/fallback-bundle"; fi
fi
case "$WEB" in *fallback-bundle*) BUNDLE_KIND="fallback";; *) BUNDLE_KIND="player-web";; esac
[ -f "$WEB/index.html" ] || { echo "bundle sem index.html: $WEB" >&2; exit 1; }
[ -f "$BUILD/shell/RdPlayer.Shell.exe" ] || { echo "rode packaging/build.sh antes" >&2; exit 1; }

PKG="RdPlayer-Windows-$VER"
STAGE="$ROOT/dist/package/$PKG"
rm -rf "$ROOT/dist/package" && mkdir -p "$STAGE/app/current/wwwroot"
cp "$BUILD/watchdog/RdPlayer.Watchdog.exe" "$STAGE/"
cp "$BUILD/shell/RdPlayer.Shell.exe" "$STAGE/app/current/"
cp -r "$BUILD/shell/shell-ui" "$STAGE/app/current/"
cp -r "$WEB"/. "$STAGE/app/current/wwwroot/"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '{"version":"%s","platform":"windows","bundle":"%s","built_at":"%s"}\n' "$VER" "$BUNDLE_KIND" "$BUILT_AT" > "$STAGE/app/current/version.json"
cp "$ROOT/packaging/install.ps1" "$ROOT/packaging/uninstall.ps1" "$ROOT/packaging/config.example.json" "$ROOT/packaging/LEIAME-INSTALACAO.txt" "$STAGE/"

rm -f "$ROOT/dist/$PKG-win-x64.zip" "$ROOT/dist/$PKG-update.zip"
( cd "$ROOT/dist/package" && "$TAR_EXE" -a -c -f "../$PKG-win-x64.zip" "$PKG" )

# pacote de atualização: o que vira app\current (+ watchdog para auto-atualização)
UPD="$ROOT/dist/package/update-$VER"
cp -r "$STAGE/app/current" "$UPD"
cp "$BUILD/watchdog/RdPlayer.Watchdog.exe" "$UPD/"
( cd "$UPD" && "$TAR_EXE" -a -c -f "../../$PKG-update.zip" * )

{
  echo "pacote: $PKG (bundle web: $BUNDLE_KIND, origem: $WEB)"
  echo "gerado em (UTC): $BUILT_AT"
  echo
  for f in "$ROOT/dist/$PKG-win-x64.zip" "$ROOT/dist/$PKG-update.zip" "$STAGE/RdPlayer.Watchdog.exe" "$STAGE/app/current/RdPlayer.Shell.exe"; do
    printf '%12d  %s  %s\n' "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)" "${f#$ROOT/}"
  done
} | tee "$ROOT/dist/PACKAGE_INFO.txt"
