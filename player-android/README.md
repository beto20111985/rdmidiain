# RD Player — Android (APK + modo kiosk/launcher)

Player nativo **Kotlin + Media3/ExoPlayer** da RD Mídia Indoor, implementando o contrato
**RD Player Protocol v1** (`protocol/RD_PLAYER_PROTOCOL_v1.md`, ADR_007) e as decisões do ADR_008 item 3:
um único APK com dois modos — **kiosk/launcher** quando é *Device Owner* e **APK comum** (sem bloqueios) caso contrário.

> Situação (AÇÃO 002): código, testes JVM e build reais em Docker. **Nada foi executado em aparelho físico**:
> kiosk/Lock Task, HDMI, boot, OTA silenciosa e reprodução em TV Box estão **BLOQUEADOS** até o proprietário
> fornecer o hardware (ver "O que exige hardware real").

---

## 1. Arquitetura

Pacote `br.com.rdmidiaindoor.player` (um módulo Gradle `:app`, pacotes separados; a lógica de protocolo/ciclo/PoP/sync
é Kotlin puro, sem `android.*`, e roda nos testes JVM):

| Pacote | Responsabilidade |
|---|---|
| `protocol` | JSON estrito + **RDJ1** canônico (ordem por ponto de código, escapes mínimos, só inteiros), base64url, **ES256** (DER↔raw r‖s, SPKI P-256), modelo/validação/assinatura do manifesto, envelopes WS, oferta de OTA assinada, `KeystoreSigner` (Android Keystore, EC P-256 **não exportável**, StrongBox quando houver). |
| `core` | `CycleResolver` = função pura `resolve(manifest, anchor_ms, local_state, t) → Segment` (RDPP §5.1/§5.2/§8.2); `PlaybackPlanner` (regras de runtime: entrada no meio do item ⇒ FILLER + SKIPPED, preempção por emergência, absorção de atraso); `AnchorManager` (âncora write-once, reboot nunca recalcula); `ClockService` (offset pelo `X-Server-Time-Ms`, alerta de relógio); alertas 7/28 dias. |
| `sync` | `SyncEngine` (RECEIVED → VERIFY_SIG → PLAN → DOWNLOADING → VERIFY_SHA → STAGED; COMMIT na fronteira de ciclo), `MediaDownloader` (Range/If-Range `"<sha256>"`, SHA-256 incremental em stream, retomada), `GenerationStore` (gerações em diretórios, ponteiro trocado por *rename* atômico, rollback, GC), `SyncWorker` (WorkManager: só re-tentativa com rede, sem polling). |
| `pop` | Eventos `rd.pop/1` com relógio monotônico, `seq` + `prev_hash` + `nonce`, assinatura ES256 sobre RDJ1, **outbox Room** (seq e evento na mesma transação), envio em lotes ≤ 200 com remoção só dos confirmados. |
| `net` | `ApiClient`/`PlayerApi` (OkHttp; desafio→JWT 15 min; `If-None-Match "<versão>"`), `PlayerSocket` (ticket de uso único, heartbeat 30 s, telemetria 5 min, comandos com ack, reconexão backoff+jitter 1→60 s), `TelemetryCollector` (sem IP), `UrlPolicy`. |
| `player` | `PlayerActivity` (tela cheia; HOME no kiosk), `Stage` (double buffer: duas camadas, pré-carga, fade sobre a camada anterior — sem quadro preto), `PlayerController` (troca nas fronteiras absolutas, PoP contíguo e sem sobreposição), rodapé `"<label> — <code>"` com área útil reduzida (letterbox), pareamento com QR local (ZXing), alertas, relógio. |
| `kiosk` | `KioskManager` (Device Owner: Lock Task, restrições, HOME persistente), `RdDeviceAdminReceiver`, `ProvisioningActivity` (QR Android 10+), `WifiActivity` (Wi-Fi sem PIN), `TechMenuActivity` (PIN). |
| `boot` | `BootReceiver` (`BOOT_COMPLETED`, `MY_PACKAGE_REPLACED`, `QUICKBOOT_POWERON`) e `LockedBootReceiver` (`LOCKED_BOOT_COMPLETED`, Direct Boot, processo `:watchdog`). |
| `watchdog` | `CrashHandler` (exceção não capturada ⇒ registra, agenda reinício), `PlaybackWatchdog` (thread principal bloqueada, troca atrasada, vídeo com posição congelada), `WatchdogReceiver` em processo separado (`:watchdog`, alarme a cada ~60 s). |
| `ota` | `OtaManager` (oferta assinada com chave de release fixada, SHA-256, mesmo pacote/versão maior/mesmo certificado, `PackageInstaller`; silencioso só com DO). |
| `config` | `ConfigStore` (URL base da API configurável — sem domínio hardcoded; PIN em hash PBKDF2), `DeviceStateStore` (estado em JSON com escrita atômica), `SetupActivity`. |

Regras de runtime que vão além da função pura (documentadas no código):
- **100 % sem tolerância × atrasos de troca**: itens EXTERNAL/INSTITUTIONAL e de emergência sempre ficam visíveis
  pelo `d` completo; o atraso acumulado (milissegundos por troca) é absorvido por itens INTERNAL/FALLBACK quando passa de
  250 ms e pelos FILLERs; atraso > 1,5 s = entrada no meio do item (FILLER até o fim + SKIPPED).
- Intervalos de PoP contíguos: fim do item = instante em que o seguinte fica visível (o crossfade conta para o seguinte).
- SKIPPED: entrada no meio de qualquer ITEM (inclusive fallback); na preempção por emergência só itens não-fallback (§5.2); FILLER nunca gera PoP.
- Kill/`revoked` valem na hora (inclusive para o item em exibição ⇒ INTERRUPTED) e ficam persistidos.
- Kill local persistido com o instante; liberado quando é ATIVADO um manifesto gerado > kill + 2 s sem o criativo em
  `revoked` (lift no painel). Correção do relógio para trás: o item atual continua até a fronteira (≤ 5 s) e o PoP
  desconta só o excesso (`PopChain`) — sem sobreposição; jitter < 150 ms do `X-Server-Time-Ms` é ignorado.
- `offline` do PoP = sem WS e sem contato HTTP nos últimos 90 s (capturado no início do item). Um único motor de
  reprodução por processo (`SingleOwner`).

## 2. Build (Docker — nenhuma toolchain no host)

Imagem: `ghcr.io/cirruslabs/android-sdk:35` (JDK 21, SDK 35, build-tools 35.0.0). **Atenção do proprietário:** os
arquivos de aceite de licença do Android SDK (`$ANDROID_HOME/licenses/*`) **já vêm incluídos na imagem pelos
mantenedores**; o agente não executou `sdkmanager --licenses`. Cache do Gradle no volume nomeado `rd-gradle-cache`.

```bash
# a partir da raiz do monorepo (Git Bash/Linux/macOS)
player-android/scripts/gen-dev-keystore.sh                 # 1x: keystore de DESENVOLVIMENTO (ignorada pelo git)
player-android/scripts/docker-gradle.sh testDebugUnitTest assembleDebug
player-android/scripts/docker-gradle.sh assembleRelease    # assinado com keystore/dev.properties
player-android/scripts/docker-gradle.sh lintDebug
```

Saídas: `app/build/outputs/apk/{debug,release}/`; cópias em `dist/` (ignorado). Relatórios: `app/build/reports/`.

Versões: AGP 8.7.3, Gradle 8.11.1, Kotlin 2.0.21 (KSP 2.0.21-1.0.28), compileSdk/targetSdk 35, **minSdk 24**,
Media3 1.4.1, WorkManager 2.9.1, Room 2.6.1, OkHttp 4.12.0, ZXing core 3.5.3. Atualizar Media3/Room/core-ktx para as
versões mais novas exige compileSdk 36 (imagem com a plataforma 36) — pendência.

Parâmetros de build (`gradle.properties`, sobrescrevíveis com `-P`):

| Propriedade | Uso |
|---|---|
| `rd.dev.cleartextHosts` | hosts onde HTTP é permitido **só no debug** (padrão `localhost,127.0.0.1,10.0.2.2`). Gera `network_security_config` por variante. |
| `rd.release.cleartextHosts` | idem para release (padrão vazio ⇒ **HTTPS obrigatório**). |
| `rd.apiBaseUrl` | URL base padrão da API (vazio ⇒ perguntada no primeiro boot). |
| `rd.update.publicKeys` | chaves de release do app-update: `keyId=SPKI_b64u;…` (sem chave ⇒ OTA recusada). |

**Assinatura:** `keystore/dev.jks` é só para desenvolvimento. Release real = keystore do **proprietário**, fora do repositório
(mesmas chaves `storeFile/storePassword/keyAlias/keyPassword` num `dev.properties` equivalente ou em variáveis de CI).
Trocar a chave de assinatura impede atualizar aparelhos já instalados (Android exige mesmo certificado).

## 3. Instalação

```bash
adb install -r dist/rd-player-<versão>-release.apk      # ou debug
adb shell am start -n br.com.rdmidiaindoor.player/.player.PlayerActivity
```
Primeiro boot: conexão à internet (Wi-Fi ou cabo), URL da API (HTTPS; HTTP só para hosts de desenvolvimento do build),
PIN técnico (obrigatório no kiosk) e — no modo APK comum, Android 10+ — permissão "sobrepor a outros apps" para abrir
sozinho ao ligar. Depois aparece a tela de pareamento (código `XXXX-XXXX` + QR do `panel_pair_url`).

## 4. Modo kiosk (Device Owner) × APK comum

| | Kiosk (Device Owner) | APK comum |
|---|---|---|
| HOME/launcher | sim (activity-alias `HomeAlias` + `addPersistentPreferredActivity`) | não (técnico pode definir como tela inicial) |
| Lock Task | sim, `LOCK_TASK_FEATURE_NONE` (sem home/recentes/notificações/barra) | não |
| Bloqueios | instalar/desinstalar apps, fontes desconhecidas, USB (transferência e mídia física), Bluetooth, modo seguro, reset de fábrica, contas, novos usuários, credenciais, janelas de outros apps, diálogos de erro do sistema, modo avião; **depuração/adb bloqueada só no build release**; Play Store/YouTube/navegadores/launchers conhecidos ocultados (reversível) | nenhum (ETAPA2 #246) |
| Wi-Fi | tela própria, sem PIN (conecta direto: DO pode usar `addNetwork`) | tela própria; Android 10+ abre o painel do sistema |
| Sair | só com PIN (menu técnico ⇒ "Sair do modo kiosk", reativado no próximo boot) | botão Voltar/menu |
| Boot | sistema abre o HOME + `BootReceiver` (DO pode abrir Activity em background) | `BootReceiver` (Android 10+: precisa de sobreposição ou ser HOME) |
| OTA | silenciosa (`PackageInstaller`) | sistema pede confirmação (permitir "instalar apps desconhecidos") |
| Reboot remoto | `DevicePolicyManager.reboot` | `UNSUPPORTED` |

Controle remoto: botão **Configurações** ⇒ Wi-Fi direto (sem PIN). **OK/Menu/Info** ⇒ opções: Wi-Fi (sem PIN),
Menu técnico (PIN de 6 a 12 dígitos; 5 erros ⇒ bloqueio de 5 min), Parear com login de técnico (§2.3b). **Voltar** é
ignorado no kiosk. O PIN é guardado só como hash PBKDF2-HMAC-SHA256 (210 000 iterações, sal 16 B, hash 32 B — mesmo
custo do backend; verificação fora da thread principal). Origens: comando WS `set_tech_pin` (RDPP §4, normativo:
`args = {"algo":"pbkdf2-sha256","iterations":210000,"salt_b64u","hash_b64u"}` — o servidor nunca envia o PIN em claro),
primeiro boot, ou provisionamento (`rd_tech_pin_hash` no formato interno `pbkdf2-sha256$<iter>$<salt b64u>$<hash b64u>`).

### 4.1 Provisionar Device Owner por adb (aparelho sem contas)
```bash
# aparelho recém-resetado, sem contas Google/usuários extras
adb install -r rd-player.apk
adb shell dpm set-device-owner br.com.rdmidiaindoor.player/.kiosk.RdDeviceAdminReceiver
adb shell am start -n br.com.rdmidiaindoor.player/.player.PlayerActivity
```
Falhas comuns: "already several accounts on the device" (remova contas/resete), "not allowed to set the device owner
because there are already some accounts", ou ROM de TV Box sem o comando `dpm` funcional (limitação do fabricante).
Descomissionar: menu técnico ⇒ "Remover Device Owner" (com PIN).

### 4.2 Provisionamento por QR (quando o assistente de configuração do aparelho suporta — muitas TV Box genéricas não)
Conteúdo do QR (JSON):
```json
{
  "android.app.extra.PROVISIONING_DEVICE_ADMIN_COMPONENT_NAME": "br.com.rdmidiaindoor.player/.kiosk.RdDeviceAdminReceiver",
  "android.app.extra.PROVISIONING_DEVICE_ADMIN_PACKAGE_DOWNLOAD_LOCATION": "https://<host do proprietário>/rd-player.apk",
  "android.app.extra.PROVISIONING_DEVICE_ADMIN_SIGNATURE_CHECKSUM": "<SHA-256 do certificado de assinatura, base64url>",
  "android.app.extra.PROVISIONING_ADMIN_EXTRAS_BUNDLE": {
    "rd_api_base_url": "https://api.<domínio>",
    "rd_panel_base_url": "https://painel.<domínio>",
    "rd_tech_pin_hash": "pbkdf2-sha256$60000$<salt>$<hash>",
    "rd_update_public_keys": "rel-1=<SPKI b64u>"
  }
}
```
O checksum vem de `apksigner verify --print-certs <apk>` (SHA-256 hex ⇒ bytes ⇒ base64url). O app trata
`GET_PROVISIONING_MODE`/`ADMIN_POLICY_COMPLIANCE` (exigidos no Android 10+).

## 5. Limitações por versão de Android / fabricante (conhecidas, documentadas)
- **Android 10+ (API 29)**: app em background não abre Activity no boot, salvo Device Owner, HOME padrão ou "sobrepor a outros
  apps"; `WifiManager.addNetwork/enableNetwork/setWifiEnabled` só para DO/sistema (modo APK comum usa o painel do sistema).
- **Android 8.1+**: SSID exige permissão de localização (DO concede automaticamente) e, em vários aparelhos, localização ligada;
  `startScan` tem limitação de frequência (API 28+). Android 11+: DO liga a localização (`setLocationEnabled`).
- **Android 8+**: `/proc/stat` bloqueado ⇒ `cpu_pct` é o uso de CPU **do próprio processo**.
- **HDMI**: `HDMI_PLUGGED`/`ACTION_HDMI_AUDIO_PLUG` variam por fabricante ⇒ `hdmi_connected` pode ser `null`.
- **KEYCODE_SETTINGS** pode ser interceptado pelo sistema em algumas TV Box; OK/Menu sempre funcionam como alternativa.
- **Decodificadores**: o double buffer de vídeo→vídeo usa dois ExoPlayer; SoCs baratos podem ter só 1 decodificador de
  hardware simultâneo (o ExoPlayer cai para software ou falha ⇒ o item vira tela institucional sem PoP). Validar no aparelho.
- **TextureView** (necessária para o fade por alpha) custa mais GPU que SurfaceView em TV Box fraca.
- **LOCKED_BOOT_COMPLETED**: só agenda o watchdog; o player abre após o desbloqueio (TV Box sem bloqueio de tela desbloqueia logo).
- **Fire TV / Google TV / Chromecast**: Device Owner normalmente indisponível ⇒ modo APK comum; Fire TV não tem Play Services
  (o app não depende deles).
- **OTA em API < 28**: o PackageManager pode não expor o certificado do APK baixado; a proteção fica com o instalador do sistema
  (recusa certificado diferente).

## 6. O que exige hardware real — BLOQUEADO até o proprietário fornecer o TV Box
Testes instrumentados; reprodução real (codecs, fade, letterbox, rotação vertical por software); Lock Task/Device Owner e
restrições; tela de Wi-Fi; boot automático (incluindo Direct Boot); watchdog/reinício; HDMI; OTA silenciosa e com confirmação;
consumo de RAM/CPU com 2 decodificadores; provisionamento por QR. Emulador em Docker Desktop/Windows não tem KVM (A4 Q26).

## 7. Firmware/ROM futura — arquitetura e interfaces previstas (NÃO implementada, NÃO testada)
Depende do modelo do TV Box (SoC, BSP/device tree, kernel) e das **chaves de plataforma** — BLOQUEADO POR HARDWARE (ADR_008 item 5).
Interfaces já preparadas no APK para a ROM:
- **priv-app com assinatura de plataforma**: o mesmo APK em `/system/priv-app/RdPlayer/` (ou `/product/priv-app`), com
  `privapp-permissions-br.com.rdmidiaindoor.player.xml` concedendo `REBOOT`, `INSTALL_PACKAGES`, `WRITE_SECURE_SETTINGS`,
  `MANAGE_DEVICE_ADMINS`; a ROM pode, alternativamente, pré-provisionar o Device Owner (`device_owner_2.xml`) para reaproveitar
  todo o `KioskManager` sem mudanças.
- **HOME padrão** por overlay (`config_defaultHome`/remoção do launcher de fábrica) ⇒ dispensa `addPersistentPreferredActivity`.
- **Atualização**: o `OtaManager` continua válido para o app; atualização do sistema via A/B (`update_engine`) com pacote
  assinado pela chave de plataforma — a definir com o BSP.
- **Bloqueios na ROM**: remover Play Store/navegador/YouTube da imagem, desabilitar adb em `user` build, `ro.secure=1`,
  Wi-Fi exposto só pela tela do app.
- **Watchdog**: serviço `init` (`.rc`) com `restart` para o app (substitui o alarme do processo `:watchdog`).

## 8. Conformidade com o protocolo
Testes JVM (`app/src/test`): RDJ1 (ordem por ponto de código ≠ UTF-16, escapes, floats proibidos), ES256 (DER↔raw, SPKI P-256,
envelope do manifesto, oferta de OTA), ciclo (âncora 08:07, reboot 08:47 e 08:49:10, 2+2, 3+1, repetição semanal, vencido→fallback,
kill/revogado/mídia não pronta, emergência, meio do item→SKIPPED, defesa de `external_plan`), PoP (COMPLETED só com 100 %, `seq`
persistente, `prev_hash`), sync (retomada por Range, sha divergente nunca ativa, rollback, GC, headers com aspas).
`ConformanceVectorsTest` consome os vetores PUBLICADOS em `protocol/vectors/` (montado em `/work/protocol` pelo script):
canonical_json (19 casos, 5 floats rejeitados), es256_signatures (13 casos: inclui `=`/DER rejeitados e high-S aceito,
round-trip próprio), cycle_resolution (11 casos / 74 consultas da função pura `resolve()`) e pop_validation (forma:
RDJ1 dos 37 eventos, chaves do `rd.pop/1`, assinaturas com a `device_key`). Arquivo ausente = FALHA.
Os valores esperados nunca são ajustados no player: divergência é reportada ao coordenador.
