namespace RdPlayer.Core.Config;

/// <summary>
/// Configuração do Player Windows — <c>%PROGRAMDATA%\RdMidiaIndoor\Player\config.json</c> (snake_case).
/// Nenhum domínio é fixo no código: URLs vêm daqui.
/// </summary>
public sealed class PlayerConfig
{
    public string? Schema { get; set; } = "rd.player.windows.config/1";
    /// <summary>Base HTTP(S) da API, ex.: https://api.exemplo.com.br (obrigatório).</summary>
    public string? ApiBaseUrl { get; set; }
    /// <summary>Base do WebSocket; se ausente é derivada de api_base_url (https→wss).</summary>
    public string? WsBaseUrl { get; set; }
    /// <summary>URL do painel (QR de pareamento).</summary>
    public string? PanelUrl { get; set; }
    public WebConfig Web { get; set; } = new();
    public DisplayConfig Display { get; set; } = new();
    public KioskConfig Kiosk { get; set; } = new();
    public PowerConfig Power { get; set; } = new();
    public WatchdogConfig Watchdog { get; set; } = new();
    public AutostartConfig Autostart { get; set; } = new();
    public UpdateConfig Update { get; set; } = new();
    public TelemetryConfig Telemetry { get; set; } = new();
    public LoggingConfig Logging { get; set; } = new();
    public DevConfig Dev { get; set; } = new();
}

public sealed class WebConfig
{
    /// <summary>"local" (bundle empacotado, servido por host virtual — funciona offline) ou "remote" (carrega remote_url).</summary>
    public string Source { get; set; } = "local";
    public string VirtualHost { get; set; } = "player.rd.local";
    public string Entry { get; set; } = "index.html";
    public string? RemoteUrl { get; set; }
    /// <summary>Sobrescreve a pasta do bundle (padrão: &lt;pasta do exe&gt;\wwwroot).</summary>
    public string? BundleDir { get; set; }
}

public sealed class DisplayConfig
{
    /// <summary>"primary", índice (0, 1, …) ou nome do dispositivo (\\.\DISPLAY2).</summary>
    public string Monitor { get; set; } = "primary";
    public bool Fullscreen { get; set; } = true;
    public bool AlwaysOnTop { get; set; } = true;
    public bool HideCursor { get; set; } = true;
}

public sealed class KioskConfig
{
    public bool BlockClose { get; set; } = true;
    /// <summary>Bloqueia tecla Windows, Alt+Tab, Alt+Esc e Ctrl+Esc via hook de teclado de baixo nível.</summary>
    public bool BlockSystemKeys { get; set; } = false;
    public string TechShortcut { get; set; } = "Ctrl+Alt+Shift+F12";
    /// <summary>pbkdf2-sha256$iterações$sal_b64$hash_b64 (gerado pelo install.ps1). Sem PIN o menu técnico fica desativado.</summary>
    public string? TechPinHash { get; set; }
    public bool DevtoolsInTechMenu { get; set; } = false;
    public int PinMaxAttempts { get; set; } = 5;
    public int PinLockoutSeconds { get; set; } = 300;
}

public sealed class PowerConfig
{
    /// <summary>SetThreadExecutionState(ES_CONTINUOUS|ES_DISPLAY_REQUIRED|ES_SYSTEM_REQUIRED) enquanto roda.</summary>
    public bool PreventSleep { get; set; } = true;
    public bool AllowRemoteReboot { get; set; } = true;
    public int RebootMinIntervalMinutes { get; set; } = 60;
    public int RebootDelaySeconds { get; set; } = 30;
}

public sealed class WatchdogConfig
{
    public bool Enabled { get; set; } = true;
    public int HeartbeatIntervalMs { get; set; } = 5000;
    public int HangTimeoutMs { get; set; } = 20000;
    public int StartupTimeoutMs { get; set; } = 60000;
    public int ProgressTimeoutMs { get; set; } = 30000;
    public int StartupGraceMs { get; set; } = 90000;
    public int ReloadGraceMs { get; set; } = 60000;
    public int MaxReloadsBeforeRestart { get; set; } = 3;
    public int ReloadWindowMs { get; set; } = 600000;
    /// <summary>A casca relança o watchdog se ele sumir (supervisão mútua).</summary>
    public bool ReviveWatchdog { get; set; } = true;
}

public sealed class AutostartConfig
{
    /// <summary>Desligado por padrão (ETAPA2 #221: opção SIM/NÃO).</summary>
    public bool Enabled { get; set; } = false;
    /// <summary>"task" (Agendador de Tarefas, ao fazer logon, com reinício em falha) ou "run" (HKCU\...\Run).</summary>
    public string Method { get; set; } = "task";
}

public sealed class UpdateConfig
{
    public bool Enabled { get; set; } = true;
    public bool AutoApply { get; set; } = true;
    public int CheckIntervalHours { get; set; } = 6;
    public bool AllowDowngrade { get; set; } = false;
    public int HealthTimeoutSeconds { get; set; } = 120;
    public bool RequireBundleHealth { get; set; } = true;
    public int MaxPackageMb { get; set; } = 512;
    public List<UpdateKeyConfig> PublicKeys { get; set; } = new();
}

public sealed class UpdateKeyConfig
{
    public string KeyId { get; set; } = "";
    /// <summary>SPKI DER P-256 em base64url sem padding.</summary>
    public string PublicKey { get; set; } = "";
}

public sealed class TelemetryConfig
{
    public bool Wifi { get; set; } = true;
}

public sealed class LoggingConfig
{
    public string Level { get; set; } = "info";
    public int MaxFileMb { get; set; } = 5;
    public int MaxFiles { get; set; } = 10;
}

public sealed class DevConfig
{
    /// <summary>SOMENTE desenvolvimento: aceita API em http:// (conteúdo misto). Nunca em produção.</summary>
    public bool AllowInsecureApi { get; set; } = false;
}
