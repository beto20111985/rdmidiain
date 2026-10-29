using System;
using System.Diagnostics;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using RdPlayer.Core;
using RdPlayer.Core.Bridge;
using RdPlayer.Core.Config;
using RdPlayer.Core.Platform;
using RdPlayer.Core.Update;

namespace RdPlayer.Shell;

/// <summary>Operações que a ponte pede à janela principal (sempre chamadas na thread de UI).</summary>
internal interface IShellHost
{
    void PostJson(string json);
    void ReloadPlayer(string reason);
    void ScheduleRestart(string reason, int delayMs);
    (int Width, int Height) ScreenSize { get; }
    void OnProgress(string? state);
    void OnBundleReady(string? bundleVersion);
    void OnDiagResult(JsonObject result);
    void OnServerTechPin(string encodedHash, long setAtMs);
    void OnBridgeTraffic(string method);
}

/// <summary>Implementação nativa da ponte <c>rd-native/1</c> (BRIDGE_PROPOSTA.md).</summary>
internal sealed class BridgeHost
{
    private static readonly string[] Capabilities = { "telemetry", "restart_app", "reboot", "update_app", "watchdog", "kiosk", "power" };

    private readonly ShellContext _ctx;
    private readonly IShellHost _host;
    private readonly SystemTelemetry _telemetry;
    private readonly UpdateManager _updates;

    public BridgeHost(ShellContext ctx, IShellHost host, SystemTelemetry telemetry, UpdateManager updates)
    {
        _ctx = ctx;
        _host = host;
        _telemetry = telemetry;
        _updates = updates;
    }

    public JsonObject NativeInfo()
    {
        var c = _ctx.Config;
        var cfg = new JsonObject
        {
            ["api_base_url"] = c.ApiBaseUrl?.TrimEnd('/'),
            ["ws_base_url"] = ConfigLoader.EffectiveWsBaseUrl(c),
            ["panel_url"] = c.PanelUrl?.TrimEnd('/'),
            ["update_check_interval_hours"] = c.Update.CheckIntervalHours,
            ["config_ok"] = _ctx.ConfigResult.Ok,
            ["config_errors"] = new JsonArray(_ctx.ConfigResult.Errors.ConvertAll(e => (JsonNode?)JsonValue.Create(e)).ToArray()),
        };
        var caps = new JsonArray();
        foreach (var cap in Capabilities) caps.Add(cap);
        return new JsonObject
        {
            ["bridge"] = BridgeProtocol.Name,
            ["platform"] = AppInfo.Platform,
            ["shell_version"] = AppInfo.Version,
            ["launched_by_watchdog"] = _ctx.Args.Supervised,
            ["smoke_test"] = _ctx.Smoke,
            ["config"] = cfg,
            ["capabilities"] = caps,
        };
    }

    private bool IsAllowedSource(string source, out bool fromShellUi)
    {
        fromShellUi = false;
        if (!Uri.TryCreate(source, UriKind.Absolute, out var u)) return false;
        var origin = u.GetLeftPart(UriPartial.Authority);
        if (string.Equals(origin, _ctx.ShellUiOrigin, StringComparison.OrdinalIgnoreCase)) { fromShellUi = true; return true; }
        if (string.Equals(origin, _ctx.PlayerOrigin, StringComparison.OrdinalIgnoreCase)) return true;
        if (_ctx.Config.Web.Source == "remote" && Uri.TryCreate(_ctx.Config.Web.RemoteUrl, UriKind.Absolute, out var r) &&
            string.Equals(origin, r.GetLeftPart(UriPartial.Authority), StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    public async void Handle(string source, string json)
    {
        if (!IsAllowedSource(source, out bool fromShellUi))
        {
            _ctx.Log.Warn("bridge", "mensagem descartada de origem não autorizada: " + source);
            return;
        }
        if (!BridgeProtocol.TryParse(json, out var msg, out var err))
        {
            _ctx.Log.Warn("bridge", "mensagem inválida descartada: " + err);
            return;
        }
        _host.OnBridgeTraffic(msg!.Method);
        try
        {
            if (msg.Kind == BridgeKind.Notify)
            {
                HandleNotify(msg, fromShellUi);
                return;
            }
            var result = await HandleRequestAsync(msg, fromShellUi).ConfigureAwait(true);
            _host.PostJson(result);
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("bridge", "falha ao tratar " + msg.Method, ex);
            if (msg.Kind == BridgeKind.Request) _host.PostJson(BridgeProtocol.Error(msg.Id!, "INTERNAL", "erro interno da casca"));
        }
    }

    private void HandleNotify(BridgeInbound msg, bool fromShellUi)
    {
        switch (msg.Method)
        {
            case "progress":
                _host.OnProgress(BridgeProtocol.Str(msg.Params, "state"));
                break;
            case "ready":
                if (!fromShellUi) _host.OnBundleReady(BridgeProtocol.Str(msg.Params, "bundle_version"));
                break;
            case "log":
                var level = BridgeProtocol.Str(msg.Params, "level") ?? "info";
                var text = BridgeProtocol.Str(msg.Params, "message") ?? "";
                if (text.Length > 2048) text = text[..2048] + "…";
                var lv = Core.Logging.RotatingFileLog.ParseLevel(level);
                _ctx.Log.Write(lv, fromShellUi ? "shell-ui" : "bundle", text);
                break;
            case "tech_pin":
                // PIN técnico do servidor (set_tech_pin) repassado pelo bundle: só sal+hash; nunca o PIN em claro
                if (fromShellUi) break;
                if (Core.Security.ServerTechPin.TryConvert(msg.Params, out var encoded, out var setAt, out var pinErr))
                    _host.OnServerTechPin(encoded!, setAt);
                else
                    _ctx.Log.Warn("bridge", "tech_pin recusado: " + pinErr);
                break;
            case "diag_result":
                if (fromShellUi) _host.OnDiagResult(msg.Params);
                break;
            default:
                _ctx.Log.Debug("bridge", "notificação desconhecida ignorada: " + msg.Method);
                break;
        }
    }

    private async Task<string> HandleRequestAsync(BridgeInbound msg, bool fromShellUi)
    {
        var id = msg.Id!;
        switch (msg.Method)
        {
            case "get_info":
                {
                    var info = NativeInfo();
                    info["os"] = _telemetry.Os();
                    return BridgeProtocol.Response(id, info);
                }
            case "get_telemetry":
                {
                    var (w, h) = _host.ScreenSize;
                    var dataRoot = _ctx.Paths.DataRoot;
                    bool wifi = _ctx.Config.Telemetry.Wifi;
                    var snap = await Task.Run(() => _telemetry.Collect(w, h, dataRoot, wifi)).ConfigureAwait(true);
                    return BridgeProtocol.Response(id, snap.ToJson());
                }
            case "reload":
                if (fromShellUi) return BridgeProtocol.Error(id, "POLICY_DENIED", "não permitido a partir da página de diagnóstico");
                _host.ReloadPlayer("BUNDLE_REQUEST:" + (BridgeProtocol.Str(msg.Params, "reason") ?? ""));
                return BridgeProtocol.Response(id, new JsonObject { ["status"] = "ACCEPTED" });
            case "command":
                if (fromShellUi) return BridgeProtocol.Error(id, "POLICY_DENIED", "não permitido a partir da página de diagnóstico");
                return BridgeProtocol.Response(id, await CommandAsync(msg.Params).ConfigureAwait(true));
            case "update_app":
                if (fromShellUi) return BridgeProtocol.Error(id, "POLICY_DENIED", "não permitido a partir da página de diagnóstico");
                return BridgeProtocol.Response(id, await _updates.RunAsync(msg.Params, CancellationToken.None).ConfigureAwait(true));
            default:
                return BridgeProtocol.Error(id, "UNKNOWN_METHOD", "método desconhecido: " + msg.Method);
        }
    }

    private async Task<JsonObject> CommandAsync(JsonObject p)
    {
        var command = BridgeProtocol.Str(p, "command") ?? "";
        var commandId = BridgeProtocol.Str(p, "command_id") ?? "";
        _ctx.Log.Info("command", $"comando recebido: {command} (id {commandId})");
        switch (command)
        {
            case "restart_app":
                if (_ctx.Smoke) return Status("UNSUPPORTED", "SMOKE_TEST");
                _host.ScheduleRestart("REMOTE_COMMAND", 2_000);
                return Status("ACCEPTED", "reiniciando o app", 2_000);
            case "reboot":
                return Reboot();
            case "update_app":
                {
                    var prm = p["args"] is JsonObject a ? (JsonObject)a.DeepClone() : new JsonObject();
                    if (p["access_token"] is JsonValue tok) prm["access_token"] = tok.DeepClone();
                    prm["mode"] = "apply_now";
                    var r = await _updates.RunAsync(prm, CancellationToken.None).ConfigureAwait(true);
                    var st = BridgeProtocol.Str(r, "status");
                    return st switch
                    {
                        "ACCEPTED" or "APPLYING" or "STAGED" => Status("ACCEPTED", BridgeProtocol.Str(r, "detail") ?? ""),
                        "NO_UPDATE" => Status("DONE", "NO_UPDATE"),
                        _ => Status("FAILED", BridgeProtocol.Str(r, "detail") ?? st ?? "FAILED"),
                    };
                }
            default:
                // resync, clear_cache, redownload, reload_manifest: comandos de negócio tratados pelo bundle
                return Status("UNSUPPORTED", "comando não é de plataforma: " + command);
        }
    }

    private JsonObject Reboot()
    {
        var p = _ctx.Config.Power;
        var statePath = _ctx.Paths.ShellStatePath;
        var state = StateStore.LoadShellState(statePath);
        long now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var d = RebootPolicy.Decide(now, state.LastRemoteRebootMs, p.AllowRemoteReboot, p.RebootMinIntervalMinutes, _ctx.Smoke);
        if (!d.Allowed)
        {
            _ctx.Log.Warn("command", "reboot recusado: " + d.Reason);
            return Status(d.Reason is "REMOTE_REBOOT_DISABLED" or "SMOKE_TEST" ? "UNSUPPORTED" : "FAILED", d.Reason);
        }
        state.LastRemoteRebootMs = now;
        StateStore.SaveShellState(statePath, state);
        try
        {
            var psi = new ProcessStartInfo("shutdown.exe", RebootPolicy.ShutdownArguments(p.RebootDelaySeconds))
            {
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var proc = Process.Start(psi);
            _ctx.Log.Warn("command", $"reinício do Windows agendado em {p.RebootDelaySeconds} s (cancelável com 'shutdown /a')");
            return Status("ACCEPTED", "reinício do Windows agendado", p.RebootDelaySeconds * 1000);
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("command", "falha ao agendar reinício", ex);
            return Status("FAILED", "SHUTDOWN_FAILED");
        }
    }

    private static JsonObject Status(string status, string detail, int? delayMs = null)
    {
        var o = new JsonObject { ["status"] = status, ["detail"] = detail };
        if (delayMs is int d) o["delay_ms"] = d;
        return o;
    }
}
