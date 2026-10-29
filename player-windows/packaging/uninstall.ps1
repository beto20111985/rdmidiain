<#
  RD Midia Indoor - Player Windows - desinstalador (PowerShell 5.1, sem elevacao). Arquivo em ASCII de proposito.
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 [-InstallDir <pasta>] [-RemoveData] [-RemoveConfig]
  -RemoveData   apaga %LOCALAPPDATA%\RdMidiaIndoor\Player (cache de midia, IndexedDB, CHAVE DO DISPOSITIVO, logs).
                Depois disso a tela precisa ser PAREADA novamente.
  -RemoveConfig apaga %PROGRAMDATA%\RdMidiaIndoor\Player\config.json
#>
[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\RdMidiaIndoor\Player'),
  [switch]$RemoveData,
  [switch]$RemoveConfig
)
$ErrorActionPreference = 'Stop'
function Info($m) { Write-Host "[RD] $m" }

$wd = Join-Path $InstallDir 'RdPlayer.Watchdog.exe'
if (Test-Path $wd) {
  $p = Start-Process -FilePath $wd -ArgumentList '--remove-autostart' -Wait -PassThru
  Info "autostart removido (codigo $($p.ExitCode))"
}
foreach ($n in @('RdPlayer.Watchdog','RdPlayer.Shell')) {
  Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object { Info "encerrando $n (pid $($_.Id))"; $_ | Stop-Process -Force }
}
Start-Sleep -Seconds 2
$lnkDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\RD Midia Indoor'
if (Test-Path $lnkDir) { Remove-Item -Recurse -Force $lnkDir; Info 'atalho removido' }
if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir; Info "removido $InstallDir" }
if ($RemoveData) {
  $data = Join-Path $env:LOCALAPPDATA 'RdMidiaIndoor\Player'
  if (Test-Path $data) { Remove-Item -Recurse -Force $data; Info "dados locais removidos ($data) - a tela precisara ser pareada de novo" }
}
if ($RemoveConfig) {
  $cfg = Join-Path $env:ProgramData 'RdMidiaIndoor\Player\config.json'
  if (Test-Path $cfg) { Remove-Item -Force $cfg; Info "configuracao removida ($cfg)" }
}
Info 'desinstalacao concluida.'
