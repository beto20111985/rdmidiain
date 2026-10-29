using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using Microsoft.Web.WebView2.Core;
using RdPlayer.Core;
using RdPlayer.Core.IO;
using RdPlayer.Core.Security;
using RdPlayer.Core.Update;
using WinFormsTimer = System.Windows.Forms.Timer;

namespace RdPlayer.Shell;

/// <summary>
/// Teste de fumaça (--smoke-test): janela comum (sem tela cheia/topmost/cursor/energia/hook/autostart), cria o WebView2,
/// carrega o bundle local pelo host virtual, sonda a página, carrega a página de diagnóstico da casca (ponte, WebCrypto,
/// IndexedDB, OPFS, autoplay), grava um relatório JSON e encerra sozinho.
/// </summary>
internal sealed class SmokeRunner
{
    private readonly ShellContext _ctx;
    private readonly MainForm _form;
    private readonly JsonObject _report = new();
    private readonly Dictionary<string, int> _traffic = new();
    private readonly TaskCompletionSource<JsonObject> _diag = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource<bool> _wdAck = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private TaskCompletionSource<(bool Ok, string Status, string Uri)>? _nav;
    private readonly long _start = Environment.TickCount64;
    private WinFormsTimer? _timeout;
    private bool _done;

    public SmokeRunner(ShellContext ctx, MainForm form)
    {
        _ctx = ctx;
        _form = form;
        _report["schema"] = "rd.player.windows.smoke/1";
        _report["started_at"] = DateTimeOffset.UtcNow.ToString("o");
        _report["shell_version"] = AppInfo.Version;
        _report["process_id"] = Environment.ProcessId;
        _report["supervised"] = ctx.Args.Supervised;
        _report["exe_dir"] = ctx.ExeDir;
        _report["config_path"] = ctx.Paths.ConfigPath;
        _report["config_ok"] = ctx.ConfigResult.Ok;
        _report["config_errors"] = new JsonArray(ctx.ConfigResult.Errors.Select(e => (JsonNode?)JsonValue.Create(e)).ToArray());
        _report["data_root"] = ctx.Paths.DataRoot;
        _report["bundle_dir"] = ctx.BundleDir;
        _report["bundle_index_present"] = File.Exists(Path.Combine(ctx.BundleDir, ctx.Config.Web.Entry));
        _report["shell_ui_present"] = File.Exists(Path.Combine(ctx.ShellUiDir, "diag.html"));
        _report["kiosk_features_disabled"] = new JsonObject
        {
            ["fullscreen"] = false, ["topmost"] = false, ["hide_cursor"] = false,
            ["set_thread_execution_state"] = false, ["keyboard_hook"] = false, ["autostart"] = false, ["reboot"] = false,
        };
    }

    public void StartGlobalTimeout()
    {
        _timeout = new WinFormsTimer { Interval = _ctx.Args.SmokeTimeoutSeconds * 1000 };
        _timeout.Tick += (_, _) =>
        {
            _timeout.Stop();
            Finish(false, "TIMEOUT", ShellExitCodes.SmokeTimeout);
        };
        _timeout.Start();
    }

    public void Fail(string reason) => Finish(false, reason, ShellExitCodes.SmokeFailed);

    public void OnBridgeTraffic(string method)
    {
        _traffic.TryGetValue(method, out var n);
        _traffic[method] = n + 1;
        if (method == "ready" && _readyWaiter is not null && n + 1 >= _readyTarget) _readyWaiter.TrySetResult(true);
    }

    public void OnNavigationCompleted(bool ok, string status, string uri) => _nav?.TrySetResult((ok, status, uri));

    public void OnDiagResult(JsonObject result) => _diag.TrySetResult((JsonObject)result.DeepClone());

    public void OnWatchdogAck() => _wdAck.TrySetResult(true);

    public void OnTechPinStored() => _techPin.TrySetResult(true);

    private readonly TaskCompletionSource<bool> _techPin = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private TaskCompletionSource<bool>? _readyWaiter;
    private int _readyTarget;

    private int Count(string method) => _traffic.TryGetValue(method, out var n) ? n : 0;

    private async Task<bool> WaitReadyAsync(int target, int timeoutMs)
    {
        if (Count("ready") >= target) return true;
        _readyTarget = target;
        _readyWaiter = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var done = await Task.WhenAny(_readyWaiter.Task, Task.Delay(timeoutMs));
        return done == _readyWaiter.Task || Count("ready") >= target;
    }

    /// <summary>navigator.locks.query() no documento atual (null se indisponível).</summary>
    private async Task<JsonObject?> QueryLocksAsync(CoreWebView2 core)
    {
        try
        {
            await core.ExecuteScriptAsync("window.__rdLocks=null;navigator.locks?navigator.locks.query().then(s=>{window.__rdLocks=JSON.stringify({held:s.held.map(h=>h.name),pending:s.pending.map(p=>p.name)})}):(window.__rdLocks='{\"unsupported\":true}');");
            for (int i = 0; i < 10; i++)
            {
                await Task.Delay(100);
                var raw = await core.ExecuteScriptAsync("window.__rdLocks");
                var s = JsonNode.Parse(raw) is JsonValue v && v.TryGetValue<string>(out var str) ? str : null;
                if (s is not null) return JsonNode.Parse(s) as JsonObject;
            }
        }
        catch (Exception ex)
        {
            _ctx.Log.Warn("smoke", "consulta de Web Locks falhou: " + ex.Message);
        }
        return null;
    }

    private static bool LockPending(JsonObject? locks) =>
        locks?["pending"] is JsonArray a && a.Any(n => n?.GetValue<string>() == "rd-player-instance");

    /// <summary>
    /// Geometria/rotação: o controlador WebView2 ocupa exatamente a área cliente (sem barras do host), a viewport CSS
    /// corresponde aos pixels físicos ÷ escala de DPI, e a rotação por software do bundle (mesma fórmula de
    /// ui/orientation.ts aplicada ao contêiner `.screen`) cobre a viewport sem gerar rolagem — em saída paisagem
    /// (gira +90°) e numa área cliente retrato (gira −90°). A tela cheia em si não é exercida no smoke (regra).
    /// </summary>
    private async Task<bool> GeometryCheckAsync(CoreWebView2 core)
    {
        const string Probe =
            "(()=>{const el=document.querySelector('.screen');const W=innerWidth,H=innerHeight;const de=document.documentElement;" +
            "const base={W,H,dpr:devicePixelRatio,screen:screen.width+'x'+screen.height};if(!el)return JSON.stringify(Object.assign(base,{error:'sem .screen'}));" +
            "const phys=W>=H?'LANDSCAPE':'PORTRAIT';const conf=phys==='LANDSCAPE'?'PORTRAIT':'LANDSCAPE';const rot=conf==='PORTRAIT'?90:-90;" +
            "const s=el.style;const saved=[s.width,s.height,s.left,s.top,s.transform];" +
            "s.width=H+'px';s.height=W+'px';s.left=((W-H)/2)+'px';s.top=((H-W)/2)+'px';s.transform='rotate('+rot+'deg)';" +
            "const r=el.getBoundingClientRect();" +
            "const res=Object.assign(base,{physical:phys,configured:conf,rotate:rot,rect:[r.left,r.top,r.width,r.height].map(x=>Math.round(x*10)/10)," +
            "covers:Math.abs(r.left)<1&&Math.abs(r.top)<1&&Math.abs(r.width-W)<1&&Math.abs(r.height-H)<1," +
            "no_scroll:de.scrollWidth<=W&&de.scrollHeight<=H&&document.body.scrollWidth<=W&&document.body.scrollHeight<=H&&scrollX===0&&scrollY===0," +
            "scroll:[de.scrollWidth,de.scrollHeight]});" +
            "[s.width,s.height,s.left,s.top,s.transform]=saved;return JSON.stringify(res);})()";

        var results = new JsonArray();
        bool ok = true;
        var original = _form.ClientSize;
        foreach (var size in new[] { (Width: 0, Height: 0), (Width: 600, Height: 960) })
        {
            if (size.Width > 0)
            {
                _form.SmokeSetClientSize(size.Width, size.Height);
                await Task.Delay(800);
            }
            var host = _form.SmokeGeometry();
            var page = await ProbeAsync(core, Probe);
            double scale = host["rasterization_scale"]?.GetValue<double>() ?? 1.0;
            bool cssMatches = false;
            if (page?["W"] is JsonNode wN && page["H"] is JsonNode hN && host["client_px"]?.GetValue<string>() is string cp)
            {
                var parts = cp.Split('x');
                double expW = int.Parse(parts[0]) / scale, expH = int.Parse(parts[1]) / scale;
                cssMatches = Math.Abs(wN.GetValue<double>() - expW) <= 1.5 && Math.Abs(hN.GetValue<double>() - expH) <= 1.5;
            }
            bool step = host["controller_fills_client"]?.GetValue<bool>() == true && host["controller_at_origin"]?.GetValue<bool>() == true
                        && cssMatches && Bool(page ?? new JsonObject(), "covers") && Bool(page ?? new JsonObject(), "no_scroll");
            ok &= step;
            results.Add(new JsonObject { ["host"] = host, ["page"] = page?.DeepClone(), ["css_viewport_matches_physical_div_scale"] = cssMatches, ["ok"] = step });
        }
        _form.SmokeSetClientSize(original.Width, original.Height);
        await Task.Delay(300);
        _report["geometry_rotation"] = results;
        _ctx.Log.Info("smoke", "geometria/rotação: " + results.ToJsonString());
        return ok;
    }

    private const string VectorSalt = "EBESExQVFhcYGRobHB0eHw";                       // bytes 16..31
    private const string VectorHash = "eT3QxwsoWhZv8ZNt9y3OoaR_D6quMwV8XJSPsQyiXWI";  // PBKDF2(739154), algoritmo do backend

    /// <summary>Grava no IndexedDB do bundle (db rd-player, store kv, chave tech.pin) o registro do set_tech_pin.</summary>
    private async Task SeedBundleTechPinAsync(CoreWebView2 core)
    {
        await core.ExecuteScriptAsync(
            "window.__rdSeed=null;(()=>{const r=indexedDB.open('rd-player');r.onsuccess=()=>{const db=r.result;" +
            "if(!db.objectStoreNames.contains('kv')){window.__rdSeed='no-kv';db.close();return;}" +
            "const tx=db.transaction('kv','readwrite');tx.objectStore('kv').put({algo:'pbkdf2-sha256',iterations:210000," +
            $"salt_b64u:'{VectorSalt}',hash_b64u:'{VectorHash}',set_at_ms:Date.now()}},'tech.pin');" +
            "tx.oncomplete=()=>{window.__rdSeed='ok';db.close();};tx.onerror=()=>{window.__rdSeed='tx-error';};};" +
            "r.onerror=()=>{window.__rdSeed='open-error';};})();");
        string? seed = null;
        for (int i = 0; i < 30 && seed is null; i++)
        {
            await Task.Delay(100);
            var raw = await core.ExecuteScriptAsync("window.__rdSeed");
            seed = JsonNode.Parse(raw) is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
        }
        _report["tech_pin_seed_in_bundle_idb"] = seed ?? "timeout";
        _ctx.Log.Info("smoke", "registro tech.pin gravado no IndexedDB do bundle: " + (seed ?? "timeout"));
    }

    /// <summary>
    /// Caminho completo: bundle (após ready) → notify tech_pin → casca grava só o hash em shell_state.json →
    /// formulário de PIN do menu técnico nativo aceita 739154 e recusa 739155.
    /// </summary>
    private async Task<bool> TechPinFromBundleAsync()
    {
        var done = await Task.WhenAny(_techPin.Task, Task.Delay(8_000));
        bool fromBundle = Count("tech_pin") > 0;
        var ss = StateStore.LoadShellState(_ctx.Paths.ShellStatePath);
        bool stored = done == _techPin.Task && ss.ServerTechPinHash is not null;
        var (eff, from) = TechPinResolver.Effective(ss.ServerTechPinHash, _ctx.Config.Kiosk.TechPinHash);
        bool menuAccepts = false, menuRejects = false;
        if (eff is not null)
        {
            using (var f = new PinPromptForm(new PinGuard(5, 300_000), eff)) menuAccepts = f.SubmitForSmokeTest("739154");
            using (var f = new PinPromptForm(new PinGuard(5, 300_000), eff)) menuRejects = !f.SubmitForSmokeTest("739155");
        }
        bool noPlain = File.Exists(_ctx.Paths.ShellStatePath) && !File.ReadAllText(_ctx.Paths.ShellStatePath).Contains("739154");
        _report["tech_pin_bridge"] = new JsonObject
        {
            ["notify_from_bundle"] = Count("tech_pin"),
            ["stored_in_shell_state"] = stored,
            ["source"] = from.ToString(),
            ["native_menu_accepts_correct_pin"] = menuAccepts,
            ["native_menu_rejects_wrong_pin"] = menuRejects,
            ["no_plaintext_on_disk"] = noPlain,
        };
        _ctx.Log.Info("smoke", $"tech_pin do bundle: notificações={Count("tech_pin")}, gravado={stored}, origem={from}, menu aceita={menuAccepts}, recusa outro={menuRejects}");
        return fromBundle && stored && from == TechPinResolver.Source.Server && menuAccepts && menuRejects && noPlain;
    }

    private async Task<(bool Ok, string Status, string Uri)> NavigateAndWait(CoreWebView2 core, string url, int timeoutMs)
    {
        _nav = new TaskCompletionSource<(bool, string, string)>(TaskCreationOptions.RunContinuationsAsynchronously);
        core.Navigate(url);
        var done = await Task.WhenAny(_nav.Task, Task.Delay(timeoutMs));
        return done == _nav.Task ? _nav.Task.Result : (false, "TIMEOUT", url);
    }

    public async Task RunAsync(CoreWebView2 core, CoreWebView2Environment env)
    {
        try
        {
            _report["webview2_created"] = true;
            _report["webview2_runtime"] = env.BrowserVersionString;
            _report["webview2_user_data_folder"] = env.UserDataFolder;
            _report["webview2_browser_process_id"] = (long)core.BrowserProcessId;
            _ctx.Log.Info("smoke", $"WebView2 criado (runtime {env.BrowserVersionString}, dados {env.UserDataFolder})");

            // 1) bundle do web player servido localmente pelo host virtual
            bool bundlePresent = File.Exists(Path.Combine(_ctx.BundleDir, _ctx.Config.Web.Entry));
            bool bundleOk = !bundlePresent;
            bool reloadOk = !bundlePresent;
            bool lockOk = true;
            bool geometryOk = true;
            bool techPinOk;
            if (bundlePresent)
            {
                var url = $"{_ctx.PlayerOrigin}/{_ctx.Config.Web.Entry}";
                var nav = await NavigateAndWait(core, url, 15_000);
                _report["bundle_navigation"] = new JsonObject { ["url"] = url, ["is_success"] = nav.Ok, ["web_error_status"] = nav.Status };
                _ctx.Log.Info("smoke", $"bundle: {url} → sucesso={nav.Ok} ({nav.Status})");
                await WaitReadyAsync(1, 10_000); // deixa o bundle inicializar e falar com a ponte
                await Task.Delay(500);
                var probe = await ProbeAsync(core,
                    "(()=>{const n=window.RD_NATIVE;return JSON.stringify({title:document.title,href:location.href,origin:location.origin," +
                    "secure_context:window.isSecureContext,native_present:!!n,native_bridge:n?n.bridge:null,native_platform:n?n.platform:null," +
                    "native_frozen:n?Object.isFrozen(n):null,ready_state:document.readyState,scripts:document.scripts.length," +
                    "body_children:document.body?document.body.children.length:0,has_webview:!!(window.chrome&&window.chrome.webview)," +
                    "web_locks:!!navigator.locks});})()");
                _report["bundle_probe"] = probe;
                _report["bridge_traffic_from_bundle"] = TrafficJson();
                bundleOk = nav.Ok && probe?["secure_context"]?.GetValue<bool>() == true && probe?["native_present"]?.GetValue<bool>() == true;
                var locks1 = await QueryLocksAsync(core);
                _report["web_locks_after_load"] = locks1?.DeepClone();
                geometryOk = await GeometryCheckAsync(core);

                if (_ctx.Args.SmokeSimulateHang && _ctx.Args.Supervised)
                {
                    // prova de ponta a ponta do watchdog COM o bundle segurando o Web Lock de instância única:
                    // trava a thread de UI; o watchdog deve matar a árvore (casca + WebView2) e relançar; a 2ª execução,
                    // no MESMO perfil do WebView2, precisa reassumir o lock (sem lock preso após crash).
                    _ctx.Log.Warn("smoke", "SIMULANDO TRAVAMENTO da thread de UI por até 90 s com o bundle carregado (o watchdog deve reiniciar a casca)");
                    System.Threading.Thread.Sleep(90_000);
                    Fail("HANG_NOT_DETECTED_BY_WATCHDOG");
                    return;
                }

                // PIN técnico vindo DO BUNDLE: grava no IndexedDB do bundle o mesmo registro que o TechPinStore grava ao
                // receber set_tech_pin pelo WS (vetor do backend, PIN 739154) e recarrega; ao ficar "ready" o bundle lê o
                // registro e o repassa à casca (notify tech_pin). A recarga também é o caminho "reload" do watchdog.
                await SeedBundleTechPinAsync(core);

                // recarga: o documento antigo libera o Web Lock e o novo o reassume
                int readyBefore = Count("ready");
                core.Reload();
                reloadOk = await WaitReadyAsync(readyBefore + 1, 15_000);
                await Task.Delay(500);
                var locks2 = await QueryLocksAsync(core);
                _report["bundle_reload"] = new JsonObject { ["ready_again"] = reloadOk, ["ready_count"] = Count("ready") };
                _report["web_locks_after_reload"] = locks2?.DeepClone();
                lockOk = !LockPending(locks1) && !LockPending(locks2);
                _ctx.Log.Info("smoke", $"reload: ready de novo={reloadOk}; locks={locks2?.ToJsonString() ?? "?"}");

                techPinOk = await TechPinFromBundleAsync();
            }
            else
            {
                techPinOk = true;
            }

            // 2) página de diagnóstico da casca: ponte rd-native/1 ida-e-volta, WebCrypto ES256, IndexedDB, OPFS, autoplay
            var diagUrl = $"{_ctx.ShellUiOrigin}/diag.html?reason=smoke";
            var dnav = await NavigateAndWait(core, diagUrl, 15_000);
            _report["diag_navigation"] = new JsonObject { ["url"] = diagUrl, ["is_success"] = dnav.Ok, ["web_error_status"] = dnav.Status };
            var diagDone = await Task.WhenAny(_diag.Task, Task.Delay(20_000));
            JsonObject? diag = diagDone == _diag.Task ? _diag.Task.Result : null;
            _report["diag"] = diag?.DeepClone();
            _ctx.Log.Info("smoke", "diagnóstico: " + (diag?.ToJsonString() ?? "SEM RESPOSTA"));

            bool diagOk = diag is not null
                && Bool(diag, "secure_context") && Bool(diag, "native_present") && Bool(diag, "bridge_roundtrip");

            // 3) canal com o watchdog (somente quando o smoke é lançado pelo watchdog)
            bool wdOk = true;
            if (_ctx.Args.Supervised)
            {
                var ack = await Task.WhenAny(_wdAck.Task, Task.Delay(8_000));
                wdOk = ack == _wdAck.Task;
                _report["watchdog_ack"] = wdOk;
            }

            _report["bridge_traffic_total"] = TrafficJson();
            bool pass = bundleOk && geometryOk && reloadOk && lockOk && techPinOk && diagOk && wdOk && dnav.Ok;
            var reason = pass ? "OK" : $"bundle_ok={bundleOk}; geometry_ok={geometryOk}; reload_ok={reloadOk}; lock_ok={lockOk}; tech_pin_ok={techPinOk}; diag_ok={diagOk}; watchdog_ok={wdOk}; diag_nav={dnav.Ok}";
            Finish(pass, reason, ShellExitCodes.SmokeFailed);
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("smoke", "falha no teste de fumaça", ex);
            Finish(false, "EXCEPTION: " + ex.GetType().Name + ": " + ex.Message, ShellExitCodes.SmokeFailed);
        }
    }

    private static bool Bool(JsonObject o, string name)
    {
        try { return o[name]?.GetValue<bool>() == true; }
        catch (Exception) { return false; }
    }

    private async Task<JsonObject?> ProbeAsync(CoreWebView2 core, string script)
    {
        try
        {
            var raw = await core.ExecuteScriptAsync(script);
            var s = JsonNode.Parse(raw)?.GetValue<string>();
            return s is null ? null : JsonNode.Parse(s) as JsonObject;
        }
        catch (Exception ex)
        {
            _ctx.Log.Warn("smoke", "sonda falhou: " + ex.Message);
            return new JsonObject { ["error"] = ex.GetType().Name + ": " + ex.Message };
        }
    }

    private JsonObject TrafficJson()
    {
        var o = new JsonObject();
        foreach (var kv in _traffic) o[kv.Key] = kv.Value;
        return o;
    }

    private void Finish(bool pass, string reason, int failCode)
    {
        if (_done) return;
        _done = true;
        _timeout?.Stop();
        _report["passed"] = pass;
        _report["result"] = reason;
        _report["duration_ms"] = Environment.TickCount64 - _start;
        _report["finished_at"] = DateTimeOffset.UtcNow.ToString("o");
        var path = _ctx.Args.SmokeLog ?? Path.Combine(_ctx.Paths.LogsDir, "smoke-result.json");
        try
        {
            FileUtil.AtomicWriteAllText(path, _report.ToJsonString(new JsonSerializerOptions { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping }));
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("smoke", "não foi possível gravar o relatório em " + path, ex);
        }
        _ctx.Log.Info("smoke", $"RESULTADO: {(pass ? "APROVADO" : "REPROVADO")} — {reason}; relatório em {path}");
        _form.ExitWith(pass ? 0 : failCode, "teste de fumaça concluído");
    }
}
