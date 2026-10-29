<#
  RD Midia Indoor - Player Windows - instalador (PowerShell 5.1, sem elevacao).
  (arquivo mantido em ASCII de proposito: o PowerShell 5.1 le .ps1 sem BOM como ANSI)

  Uso tipico (na pasta extraida do zip):
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -ApiBaseUrl https://api.SEU-DOMINIO -PanelUrl https://painel.SEU-DOMINIO
  Opcoes:
    -InstallDir <pasta>        padrao: %LOCALAPPDATA%\Programs\RdMidiaIndoor\Player
    -WsBaseUrl <wss://...>     opcional (derivado de -ApiBaseUrl)
    -Monitor primary|0|1|\\.\DISPLAY2
    -Autostart                 inicia com o Windows (tarefa agendada "ao fazer logon"); padrao: desligado
    -AutostartMethod task|run  task = Agendador de Tarefas (preferido); run = HKCU\...\Run
    -UpdateKeyId <id> -UpdatePublicKey <SPKI base64url>   chave de release p/ verificar atualizacoes (RDPP 8)
    -Pin <pin>                 PIN tecnico (se omitido, sera pedido de forma oculta)
    -KeepConfig                nao altera um config.json existente
    -NoStart                   nao inicia o player ao final
  O que NAO faz: nao instala o WebView2 Runtime (so verifica e orienta), nao altera energia/registro do sistema,
  nao cria servico. Tudo roda no contexto do usuario logado.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\RdMidiaIndoor\Player'),
  [string]$ApiBaseUrl,
  [string]$WsBaseUrl,
  [string]$PanelUrl,
  [string]$Monitor = 'primary',
  [switch]$Autostart,
  [ValidateSet('task','run')][string]$AutostartMethod = 'task',
  [string]$UpdateKeyId,
  [string]$UpdatePublicKey,
  [string]$Pin,
  [switch]$KeepConfig,
  [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$configDir = Join-Path $env:ProgramData 'RdMidiaIndoor\Player'
$configPath = Join-Path $configDir 'config.json'

function Info($m) { Write-Host "[RD] $m" }
function Warn($m) { Write-Host "[RD] AVISO: $m" -ForegroundColor Yellow }

# 1) requisitos
$build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuild
if ($build -lt 17134) { throw "Windows 10 1803 (build 17134) ou superior e obrigatorio (build atual: $build)." }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'O player exige Windows 64 bits (x64).' }
$wv2 = $null
foreach ($k in @('HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                 'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                 'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')) {
  try { $v = (Get-ItemProperty $k -ErrorAction Stop).pv; if ($v -and $v -ne '0.0.0.0') { $wv2 = $v; break } } catch {}
}
if ($wv2) { Info "WebView2 Runtime encontrado: $wv2" }
else { Warn 'WebView2 Runtime NAO encontrado. Instale o "Evergreen Bootstrapper" da Microsoft: https://go.microsoft.com/fwlink/p/?LinkId=2124703 (o player nao abre sem ele).' }

# 2) para uma instancia em execucao
foreach ($n in @('RdPlayer.Watchdog','RdPlayer.Shell')) {
  Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object { Info "encerrando $n (pid $($_.Id))"; $_ | Stop-Process -Force }
}
Start-Sleep -Seconds 2

# 3) copia os arquivos (layout com slots: app\current; a versao anterior, se houver, vai para app\previous)
if (-not (Test-Path (Join-Path $src 'app\current\RdPlayer.Shell.exe'))) { throw "Pacote incompleto: app\current\RdPlayer.Shell.exe nao encontrado em $src" }
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'app') | Out-Null
$cur = Join-Path $InstallDir 'app\current'
$prev = Join-Path $InstallDir 'app\previous'
if (Test-Path $cur) {
  if (Test-Path $prev) { Remove-Item -Recurse -Force $prev }
  Move-Item $cur $prev
  Info "versao anterior preservada em $prev"
}
Copy-Item -Recurse -Force (Join-Path $src 'app\current') $cur
Copy-Item -Force (Join-Path $src 'RdPlayer.Watchdog.exe') (Join-Path $InstallDir 'RdPlayer.Watchdog.exe')
Info "arquivos instalados em $InstallDir"

# 4) configuracao
function New-PinHash([string]$plain) {
  $salt = New-Object byte[] 16
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($salt)
  $iter = 210000
  $kdf = New-Object System.Security.Cryptography.Rfc2898DeriveBytes($plain, $salt, $iter, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
  $hash = $kdf.GetBytes(32)
  return ('pbkdf2-sha256${0}${1}${2}' -f $iter, [Convert]::ToBase64String($salt), [Convert]::ToBase64String($hash))
}

if ((Test-Path $configPath) -and $KeepConfig) {
  Info "mantendo configuracao existente: $configPath"
} else {
  if (Test-Path $configPath) {
    $cfg = Get-Content -Raw -Path $configPath | ConvertFrom-Json
    Copy-Item $configPath "$configPath.bak" -Force
    Info "configuracao existente atualizada (backup em $configPath.bak)"
  } else {
    $cfg = Get-Content -Raw -Path (Join-Path $src 'config.example.json') | ConvertFrom-Json
  }
  if ($ApiBaseUrl) { $cfg.api_base_url = $ApiBaseUrl.TrimEnd('/') }
  if ($WsBaseUrl) { $cfg.ws_base_url = $WsBaseUrl.TrimEnd('/') }
  if ($PanelUrl) { $cfg.panel_url = $PanelUrl.TrimEnd('/') }
  if ($cfg.api_base_url -like '*SEU-DOMINIO*') { Warn 'api_base_url ainda e o exemplo; informe -ApiBaseUrl (o player mostrara a tela de diagnostico ate corrigir).' }
  $cfg.display.monitor = $Monitor
  $cfg.autostart.enabled = [bool]$Autostart
  $cfg.autostart.method = $AutostartMethod
  if ($UpdatePublicKey) {
    if (-not $UpdateKeyId) { $UpdateKeyId = 'release-1' }
    $cfg.update.public_keys = @([pscustomobject]@{ key_id = $UpdateKeyId; public_key = $UpdatePublicKey })
  }
  if (-not $Pin -and -not $cfg.kiosk.tech_pin_hash) {
    $sec = Read-Host -AsSecureString 'Defina o PIN tecnico (4 a 32 caracteres) para o menu tecnico (Ctrl+Alt+Shift+F12)'
    $Pin = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  }
  if ($Pin) {
    if ($Pin.Length -lt 4 -or $Pin.Length -gt 32 -or $Pin -match '\s') { throw 'PIN invalido: use de 4 a 32 caracteres, sem espacos.' }
    $cfg.kiosk.tech_pin_hash = New-PinHash $Pin
    $Pin = $null
    Info 'PIN tecnico definido (armazenado apenas como hash PBKDF2-SHA256).'
  }
  New-Item -ItemType Directory -Force -Path $configDir | Out-Null
  $json = $cfg | ConvertTo-Json -Depth 8
  [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
  Info "configuracao gravada em $configPath"
}

# 5) autostart (opcional; desligado por padrao)
$wd = Join-Path $InstallDir 'RdPlayer.Watchdog.exe'
if ($Autostart) {
  $p = Start-Process -FilePath $wd -ArgumentList '--install-autostart' -Wait -PassThru
  if ($p.ExitCode -eq 0) { Info "autostart ligado ($AutostartMethod)" } else { Warn "falha ao ligar o autostart (codigo $($p.ExitCode)); veja o log em %LOCALAPPDATA%\RdMidiaIndoor\Player\logs" }
} else {
  $p = Start-Process -FilePath $wd -ArgumentList '--remove-autostart' -Wait -PassThru
  Info 'autostart desligado (padrao). Para ligar: reinstale com -Autostart ou use o menu tecnico.'
}

# 6) atalho no menu Iniciar
$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\RD Midia Indoor'
New-Item -ItemType Directory -Force -Path $lnkDir | Out-Null
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $lnkDir 'RD Midia Indoor Player.lnk'))
$lnk.TargetPath = $wd
$lnk.WorkingDirectory = $InstallDir
$lnk.Description = 'RD Midia Indoor Player (watchdog + player)'
$lnk.Save()
Info 'atalho criado no menu Iniciar (RD Midia Indoor Player)'

# 7) inicia
if (-not $NoStart) {
  Start-Process -FilePath $wd -WorkingDirectory $InstallDir
  Info 'player iniciado. Saida tecnica: Ctrl+Alt+Shift+F12 + PIN.'
}
