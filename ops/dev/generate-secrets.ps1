# =============================================================================
# RD Mídia Indoor — gera os Docker secrets locais (Windows / PowerShell 5.1+).
#
#   powershell -ExecutionPolicy Bypass -File ops\dev\generate-secrets.ps1
#   powershell -ExecutionPolicy Bypass -File ops\dev\generate-secrets.ps1 -Rotate db_app_password
#
# - Destino: .\secrets\ (ou $env:RD_SECRETS_DIR). Ignorado por .gitignore/.dockerignore.
# - NUNCA imprime valores; só o nome e a ação (gerado/mantido).
# - Valores alfanuméricos (seguros em URLs), UTF-8 sem BOM e sem quebra de linha.
# - Mesma lista e mesmos tamanhos de ops/dev/generate-secrets.sh.
# =============================================================================
param([string]$Rotate = "")

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dir  = if ($env:RD_SECRETS_DIR) { $env:RD_SECRETS_DIR } else { Join-Path $root "secrets" }

$secrets = [ordered]@{
  "postgres_superuser_password" = 48
  "db_owner_password"           = 48
  "db_app_password"             = 48
  "db_backup_password"          = 48
  "redis_password"              = 48
  "app_secret_key"              = 64
  "jwt_secret_key"              = 64
  "data_encryption_key"         = 64
  "backup_passphrase"           = 64
  "grafana_admin_password"      = 32
  # AÇÃO 003: senha INICIAL do Master primário (só o serviço bootstrap a monta)
  "bootstrap_master_password"   = 32
}

$alphabet = [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()

function New-RdSecret([int]$length) {
  $sb  = New-Object System.Text.StringBuilder
  $buf = New-Object byte[] 64
  while ($sb.Length -lt $length) {
    $rng.GetBytes($buf)
    foreach ($b in $buf) {
      # 248 = 62*4 → sem viés de módulo
      if ($b -lt 248 -and $sb.Length -lt $length) { [void]$sb.Append($alphabet[$b % 62]) }
    }
  }
  return $sb.ToString()
}

# Senhas de LOGIN precisam passar na política da aplicação (letras E dígitos).
function New-RdSecretFor([string]$name, [int]$length) {
  while ($true) {
    $v = New-RdSecret $length
    if ($name -eq "bootstrap_master_password" -and -not ($v -cmatch '[A-Za-z]' -and $v -match '[0-9]')) { continue }
    return $v
  }
}

if ($Rotate -and -not $secrets.Contains($Rotate) -and $Rotate -ne "manifest_signing_key") {
  Write-Error "segredo desconhecido: $Rotate"
  exit 2
}

New-Item -ItemType Directory -Force -Path $dir | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

foreach ($name in $secrets.Keys) {
  if ($Rotate -and $Rotate -ne $name) { continue }
  $file = Join-Path $dir $name
  if ((Test-Path $file) -and ((Get-Item $file).Length -gt 0) -and -not $Rotate) {
    Write-Output "mantido: $name"
    continue
  }
  $tmp = "$file.tmp"
  [System.IO.File]::WriteAllText($tmp, (New-RdSecretFor $name $secrets[$name]), $utf8NoBom)
  Move-Item -Force -Path $tmp -Destination $file
  Write-Output "gerado:  $name"
}
# ─── Chave ES256 de assinatura de MANIFESTO (PEM PKCS8, P-256) ────────────────
# Produção: MANIFEST_SIGNING_KEY_FILE. Dev local: o backend usa a chave própria do volume
# media-storage. ROTACIONAR = re-parear TODAS as telas (ops/README.md §4.1).
if (-not $Rotate -or $Rotate -eq "manifest_signing_key") {
  $mk = Join-Path $dir "manifest_signing_key"
  if ((Test-Path $mk) -and ((Get-Item $mk).Length -gt 0) -and -not $Rotate) {
    Write-Output "mantido: manifest_signing_key"
  } elseif ($Rotate -eq "manifest_signing_key" -and $env:RD_CONFIRM_MANIFEST_KEY_ROTATION -ne "sim") {
    Write-Error "recusado: rotacionar manifest_signing_key exige re-parear todas as telas; defina RD_CONFIRM_MANIFEST_KEY_ROTATION=sim"
    exit 3
  } else {
    $p = New-Object System.Security.Cryptography.CngKeyCreationParameters
    $p.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::AllowPlaintextExport
    $p.KeyCreationOptions = [System.Security.Cryptography.CngKeyCreationOptions]::None
    # [NullString]::Value: chave EFÊMERA (o PowerShell converteria $null em "" e a chave seria
    # PERSISTIDA no repositório de chaves do usuário com nome vazio)
    $k = [System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::ECDsaP256, [NullString]::Value, $p)
    if ($k.IsEphemeral -ne $true) { $k.Delete(); throw "chave CNG não efêmera — abortado" }
    try {
      $der = $k.Export([System.Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
    } finally { $k.Dispose() }
    $b64 = [Convert]::ToBase64String($der)
    $lines = for ($i = 0; $i -lt $b64.Length; $i += 64) { $b64.Substring($i, [Math]::Min(64, $b64.Length - $i)) }
    $pem = "-----BEGIN PRIVATE KEY-----`n" + ($lines -join "`n") + "`n-----END PRIVATE KEY-----`n"
    [System.IO.File]::WriteAllText("$mk.tmp", $pem, $utf8NoBom)
    Move-Item -Force -Path "$mk.tmp" -Destination $mk
    [Array]::Clear($der, 0, $der.Length)
    Write-Output "gerado:  manifest_signing_key (PEM PKCS8 P-256)"
  }
}
Write-Output "secrets em: $dir (valores não exibidos)"
