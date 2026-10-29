# RD Mídia Indoor — Player Windows

Casca nativa **.NET 8 (WinForms) + WebView2** que hospeda o **mesmo bundle do web player** (`player-web/`), conforme
`stack/bk/ADR_008_PLATAFORMAS_DOS_PLAYERS.md` (item 2, revisão do ADR_003/Tauri). O protocolo de negócio (pareamento,
manifesto, ciclo, PoP, WebSocket — `protocol/RD_PLAYER_PROTOCOL_v1.md`) é implementado pelo bundle; a casca fornece a
**plataforma**: tela cheia/quiosque, energia, watchdog, autostart, telemetria de sistema, atualização e ponte JS↔nativo
(`BRIDGE_PROPOSTA.md`, `rd-native/1`, adotada pelo `player-web`).

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `src/RdPlayer.Core` (net8.0) | Lógica pura, testada em Linux: config, política do watchdog, protocolo do pipe, ponte, RDJ1 + ES256, verificação/aplicação de atualização (slots, zip seguro, rollback), parser de telemetria (`netsh`), atalhos bloqueados, PIN (PBKDF2), log rotacionado com redação, autostart (XML da tarefa). |
| `src/RdPlayer.Shell` (net8.0-windows, WinForms) | `RdPlayer.Shell.exe`: janela, `CoreWebView2Controller` direto, ponte, telemetria Win32/WLAN, menu técnico, atualização, teste de fumaça. `shell-ui/` = página de diagnóstico (`https://shell.rd.local/diag.html`). |
| `src/RdPlayer.Watchdog` (net8.0-windows, sem UI, *trimmed*) | `RdPlayer.Watchdog.exe`: lança/adota a casca, heartbeat por named pipe, recarga/reinício, backoff, updater com probação e rollback, auto-atualização, `--install-autostart`/`--remove-autostart`. |
| `tests/RdPlayer.Tests` (xUnit, net8.0) | 181 testes da lógica independente de UI. |
| `packaging/` | `build.sh` (container), `make-package.sh` (host), `install.ps1`, `uninstall.ps1`, `config.example.json`, `LEIAME-INSTALACAO.txt`, `fallback-bundle/` (página provisória se o player-web não estiver buildado). |
| `dist/` (ignorado) | Artefatos: `build/`, `web-bundle/`, `package/`, `RdPlayer-Windows-<v>-win-x64.zip`, `RdPlayer-Windows-<v>-update.zip`. |

## Requisitos do PC / TV Box

- Windows 10 **1803 (build 17134)+** ou Windows 11, **x64**.
- **Microsoft Edge WebView2 Runtime (Evergreen)** — já presente no Windows 11 e no Windows 10 atualizado. Se faltar, instalar o
  *Evergreen Bootstrapper* da Microsoft (`https://go.microsoft.com/fwlink/p/?LinkId=2124703`). O `install.ps1` só verifica e orienta
  (não aceita licença nem baixa nada em nome do proprietário).
- Não precisa de .NET instalado (pacote *self-contained*) nem de administrador.

## Build (sem toolchain no host)

```bash
# testes + publish (container oficial do SDK; cache NuGet em volume nomeado)
MSYS_NO_PATHCONV=1 docker run --rm -v "D:/oficial/midiaindoor/player-windows:/src" \
  -v rd-nuget-cache:/root/.nuget/packages mcr.microsoft.com/dotnet/sdk:8.0 bash /src/packaging/build.sh
# bundle do web player (copia o player-web para dentro do container; não cria nada no repositório)
MSYS_NO_PATHCONV=1 docker run --rm -v "D:/oficial/midiaindoor/player-web:/app:ro" \
  -v "D:/oficial/midiaindoor/player-windows/dist:/out" node:20-alpine sh -c \
  'mkdir /b && cd /app && for f in $(ls -A | grep -v -e "^node_modules$" -e "^dist$"); do cp -r "$f" /b/; done && cd /b && npm ci && RD_OUT_DIR=/b/out npx vite build && rm -rf /out/web-bundle && cp -r /b/out /out/web-bundle'
# pacote (host, Git Bash; usa o tar.exe do Windows para gerar .zip)
bash packaging/make-package.sh
```

`build.sh` publica `win-x64` com `-p:EnableWindowsTargeting=true`: *self-contained single-file* comprimido (pacote padrão) e
*framework-dependent* (só para comparação de tamanho). Tamanhos e sha256 ficam em `dist/build/BUILD_INFO.txt` e `dist/PACKAGE_INFO.txt`.

| Variante | RdPlayer.Shell.exe | RdPlayer.Watchdog.exe | Exige no PC |
|---|---|---|---|
| self-contained (padrão) | ≈ 44,8 MB (WPF/designer removidos do runtime pack) | ≈ 11,0 MB (trimmed) | só WebView2 |
| framework-dependent | ≈ 1,2 MB + WebView2Loader.dll | ≈ 0,4 MB | .NET 8 Desktop Runtime + WebView2 |

## Instalação

Extrair `RdPlayer-Windows-<versão>-win-x64.zip` e, no PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -ApiBaseUrl https://api.SEU-DOMINIO -PanelUrl https://painel.SEU-DOMINIO `
  [-Autostart] [-AutostartMethod task|run] [-Monitor primary|0|1|\\.\DISPLAY2] [-UpdateKeyId release-1 -UpdatePublicKey <SPKI b64url>] [-Pin <pin>]
```

Instala em `%LOCALAPPDATA%\Programs\RdMidiaIndoor\Player` (layout com slots: `RdPlayer.Watchdog.exe` + `app\current\…`),
grava `%PROGRAMDATA%\RdMidiaIndoor\Player\config.json`, pede o PIN técnico (salvo só como hash PBKDF2-SHA256), cria atalho no
menu Iniciar e inicia o player. **Ponto de entrada sempre = `RdPlayer.Watchdog.exe`** (ele inicia a casca).
Desinstalar: `uninstall.ps1` (`-RemoveData` apaga cache/chave do dispositivo → exige novo pareamento; `-RemoveConfig`).

## Configuração (`config.json`)

Sem domínio no código: `api_base_url` (obrigatório, https), `ws_base_url` (opcional; derivado), `panel_url`. Demais seções
(ver `packaging/config.example.json`): `web` (host virtual `player.rd.local`, `source` local|remote), `display` (monitor,
tela cheia, sempre visível, cursor), `kiosk` (bloqueios, atalho técnico, PIN, devtools), `power`, `watchdog`, `autostart`,
`update` (chaves de release fixadas), `telemetry`, `logging`, `dev.allow_insecure_api` (só desenvolvimento).
Configuração ausente/inválida → a casca abre a página de diagnóstico listando os erros (não fica tela preta).

Dados locais: `%LOCALAPPDATA%\RdMidiaIndoor\Player\` → `WebView2\` (OPFS/IndexedDB/chave ES256 do bundle; persistente,
com `navigator.storage.persist()` concedido pela casca às origens do player), `logs\`, `state\`, `updates\`.

## Comportamento de quiosque

- Janela sem bordas cobrindo o monitor configurado; *always-on-top* configurável (reafirmado a cada 30 s); readaptação em mudança de resolução/HDMI.
- Bloqueados: Alt+F4, Ctrl+W/F4, F5/Ctrl+R/Ctrl+Shift+R, F11, F12/Ctrl+Shift+I/J/C, Ctrl+P/S/O/N/T/F/L/U, zoom, Alt+←/→, teclas de
  navegador/aplicativos, menu de contexto, novas janelas, downloads, diálogos JS, navegação para outras origens. Digitação comum
  (inclusive AltGr do ABNT2 e Ctrl+C/V para o login técnico) passa. `kiosk.block_system_keys=true` também bloqueia tecla Windows,
  Alt+Tab, Alt+Esc e Ctrl+Esc (hook de baixo nível). **Ctrl+Alt+Del não é bloqueável** (saída de emergência do Windows).
- Devtools desligado em Release (só pelo menu técnico se `kiosk.devtools_in_tech_menu=true`).
- Cursor oculto (`ShowCursor` + CSS injetado) e estacionado no canto.
- Energia: `SetThreadExecutionState(ES_CONTINUOUS|ES_DISPLAY_REQUIRED|ES_SYSTEM_REQUIRED)` enquanto roda (renovado a cada 30 s e na volta
  de suspensão) + bloqueio de `SC_SCREENSAVE`/`SC_MONITORPOWER`. Não altera planos de energia do Windows.
- Autoplay liberado: `--autoplay-policy=no-user-gesture-required`.

## Menu técnico / PIN

`Ctrl+Alt+Shift+F12` (configurável em `kiosk.tech_shortcut`) → PIN (5 erros = bloqueio de 5 min) → recarregar, reiniciar o app,
diagnóstico, ligar/desligar "iniciar com o Windows", sempre visível (sessão), devtools (se permitido), **fechar o player** (encerra
também o watchdog). Sem PIN configurado o menu fica desativado.
**Fonte única do PIN = servidor**: o PIN definido no painel (`set_tech_pin`, só sal+hash) chega pela notificação `tech_pin` da
ponte e passa a valer no menu nativo, com precedência sobre o PIN do `install.ps1` (que é só semente até o primeiro PIN do painel).
O menu nativo verifica localmente (funciona mesmo com o bundle travado); o PIN nunca é gravado em claro.

## Watchdog

- Casca → watchdog: `hello` + `hb` a cada 5 s pelo pipe `\\.\pipe\RdPlayer.Watchdog.S<sessão>` (`PipeOptions.CurrentUserOnly`; o
  watchdog confere que o cliente é o processo `RdPlayer.Shell`). O `hb` traz a idade da thread de UI e do último `progress` do bundle.
- Sem heartbeat por 20 s, ou UI parada > 20 s, ou sem heartbeat em 60 s na partida → mata a árvore de processos e relança
  (backoff 1–60 s; laço de falhas ≥ 6 em 10 min → 5 min).
- Bundle sem `progress` por 30 s (após 90 s de carência) ou estado `FROZEN` → recarrega o WebView; 3 recargas em 10 min → reinicia o processo.
- Saída pelo técnico → watchdog também sai. Supervisão mútua: se o watchdog sumir > 30 s, a casca o relança (`--adopt`).
  Sem watchdog, a própria casca aplica a política de progresso.
- Log local rotacionado (`logs\watchdog.log`, `logs\shell.log`; 5 MB × 10), com redação de tokens/JWT/senhas/tickets.

## Autostart (desligado por padrão)

`install.ps1 -Autostart` ou menu técnico. Método `task` (preferido): tarefa agendada **"RdMidiaIndoor Player"** do usuário atual,
gatilho *ao fazer logon* (+10 s), sem elevação, sem limite de tempo, ignorando bateria, com **reinício em falha** (1 min × 999).
Método `run`: valor `RdMidiaIndoorPlayer` em `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`. Ambos apontam para
`RdPlayer.Watchdog.exe --autostart`.

## Atualização (RDPP §8)

1. O bundle (que tem o token do dispositivo) chama `update_app` pela ponte (`mode` check|apply; o comando remoto usa `apply_now`),
   ou a casca consulta `GET /api/v1/player/app-update?platform=windows&version=<atual>` com o token recebido (só em memória).
2. **Assinatura**: ES256 (r‖s, base64url) da **chave de release** sobre RDJ1
   `{"platform":"windows","purpose":"rd-app-update-v1","sha256","size","version"}`; a chave pública (SPKI) é **fixada** em
   `config.json → update.public_keys` (a casca não confia no bundle nem no servidor para a chave). Sem Authenticode disponível,
   esta assinatura + SHA-256 são a garantia de integridade/autenticidade. Versão ≤ atual ou em lista negra → recusada.
3. Download em segundo plano (Range/retomada, SHA-256 incremental, limite de tamanho, backoff com jitter) → extração com proteção
   zip-slip para `app\staging\<versão>` → `version.json` conferido → `update_state.json` (pendente) → a casca sai (código 5).
4. O watchdog (updater) troca os slots por rename atômico (`current`→`previous`, `staging`→`current`) e entra em **probação**
   (padrão 120 s): precisa de `ready`/`progress` do bundle por 15 s. Se cair 2× ou não ficar saudável → **rollback** para `previous`,
   versão na lista negra, evento `update_status ROLLED_BACK` ao bundle. Se o pacote trouxer um `RdPlayer.Watchdog.exe` novo, o
   watchdog se auto-substitui após a aprovação (com desfazer se o novo não sinalizar).
5. Pacote de atualização = `dist/RdPlayer-Windows-<v>-update.zip` (conteúdo de `app\current` + watchdog). O servidor/pipeline de
   release assina `{platform,purpose,sha256,size,version}` com a chave de release **fora do repositório** (ondas/grupos no servidor).

## Teste de fumaça (seguro)

```powershell
.\RdPlayer.Watchdog.exe --smoke-test --config <config.json> --data-root $env:TEMP\rd-smoke --smoke-log $env:TEMP\rd-smoke\smoke.json [--smoke-simulate-hang]
.\app\current\RdPlayer.Shell.exe --smoke-test --config <config.json> --data-root $env:TEMP\rd-smoke --smoke-log $env:TEMP\rd-smoke\smoke.json
```

Janela comum, sem tela cheia/topmost/cursor/energia/hook/autostart/reboot/atualização; cria o WebView2, carrega o bundle local,
sonda a página, abre a página de diagnóstico (ponte, WebCrypto ES256 não exportável, IndexedDB, OPFS, armazenamento persistente,
autoplay) e encerra sozinho (código 0 = aprovado). `--smoke-simulate-hang` trava a UI da 1ª execução para provar que o watchdog detecta
e relança.

## Limitações conhecidas

- Sem assinatura Authenticode: o SmartScreen pode avisar na primeira execução (assinar com certificado de code signing é decisão do proprietário).
- O pacote self-contained remove WPF/designer do runtime (WinForms não os usa); validado pelo smoke test, mas caminhos raros de UI não exercitados.
- `hdmi_connected` é `null` no Windows (sem API confiável genérica); SSID/sinal exigem Wi-Fi e o serviço WLAN AutoConfig (no Windows 11 24H2+
  a leitura do SSID pode exigir permissão de localização).
- Reboot remoto usa `shutdown /r /t 30` (cancelável com `shutdown /a`); requer que o usuário tenha o direito de desligar o sistema.
- Rollback automático cobre a casca e o bundle; a auto-atualização do watchdog só é desfeita se o novo não subir na hora.
- O backend precisa aceitar a origem `https://player.rd.local` em CORS e no `Origin` do WebSocket (registrado no RDPP).

## Exige teste manual do proprietário (PC/TV Box Windows real)

Tela cheia na TV via HDMI (inclusive desligar/ligar a TV e trocar resolução), monitor secundário, orientação vertical; bloqueio de
Alt+F4/atalhos e menu técnico com PIN; cursor oculto; protetor de tela/suspensão/desligamento de vídeo após horas ociosas;
autostart (tarefa e Run) após reinício; **retorno após queda de energia** (configurar BIOS "restore on AC power loss" e logon
automático do Windows — fora do escopo do app); watchdog matando a casca pelo Gerenciador de Tarefas; atualização real com
pacote assinado pelo servidor + rollback; reboot remoto; Wi-Fi (SSID/RSSI) num aparelho com Wi-Fi; consumo de memória em TV Box de 2 GB
(soak de 72 h).
