#!/usr/bin/env bash
# Build do Player Windows dentro do container mcr.microsoft.com/dotnet/sdk:8.0 (sem toolchain no host).
# Uso (Git Bash no host):
#   MSYS_NO_PATHCONV=1 docker run --rm \
#     -v "D:/oficial/midiaindoor/player-windows:/src" -v rd-nuget-cache:/root/.nuget/packages \
#     mcr.microsoft.com/dotnet/sdk:8.0 bash /src/packaging/build.sh
# Saída: /src/dist/build/{sc,fd}/... + /src/dist/build/BUILD_INFO.txt  (dist/ é ignorado pelo .gitignore)
set -euo pipefail
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1

SRC=/src
WORK=/work
OUT=$SRC/dist/build
rm -rf "$WORK" && mkdir -p "$WORK"
# copia só o código-fonte (sem dist/bin/obj) para não sujar o repositório com artefatos
tar -C "$SRC" --exclude=./dist --exclude='*/bin' --exclude='*/obj' -cf - . | tar -C "$WORK" -xf -
cd "$WORK"
rm -rf "$OUT" && mkdir -p "$OUT"

echo "== dotnet --info (resumo)"
dotnet --version | tee "$OUT/BUILD_INFO.txt"

echo "== testes (RdPlayer.Tests, net8.0 em Linux)"
dotnet test tests/RdPlayer.Tests/RdPlayer.Tests.csproj -c Release --logger "trx;LogFileName=tests.trx" --results-directory "$OUT/test-results" 2>&1 | tee "$OUT/test.log"

COMMON="-c Release -r win-x64 -p:EnableWindowsTargeting=true -p:DebugType=none -p:DebugSymbols=false"

echo "== publish self-contained single-file (pacote padrão)"
dotnet publish src/RdPlayer.Shell/RdPlayer.Shell.csproj $COMMON --self-contained true \
  -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
  -o "$OUT/sc/shell" 2>&1 | tee "$OUT/publish-shell-sc.log"
dotnet publish src/RdPlayer.Watchdog/RdPlayer.Watchdog.csproj $COMMON --self-contained true \
  -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true -p:PublishTrimmed=true -p:TrimMode=partial \
  -o "$OUT/sc/watchdog" 2>&1 | tee "$OUT/publish-watchdog-sc.log"

echo "== publish framework-dependent (comparação de tamanho; exige .NET 8 Desktop Runtime no PC)"
dotnet publish src/RdPlayer.Shell/RdPlayer.Shell.csproj $COMMON --self-contained false \
  -p:PublishSingleFile=true -o "$OUT/fd/shell" 2>&1 | tee "$OUT/publish-shell-fd.log"
dotnet publish src/RdPlayer.Watchdog/RdPlayer.Watchdog.csproj $COMMON --self-contained false \
  -p:PublishSingleFile=true -o "$OUT/fd/watchdog" 2>&1 | tee "$OUT/publish-watchdog-fd.log"

echo "== artefatos"
{
  echo "dotnet SDK: $(dotnet --version)"
  echo "data (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "# tamanhos (bytes) e sha256"
  cd "$OUT"
  find sc fd -type f | sort | while read -r f; do
    printf '%12d  %s  %s\n' "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d' ' -f1)" "$f"
  done
} | tee -a "$OUT/BUILD_INFO.txt"
