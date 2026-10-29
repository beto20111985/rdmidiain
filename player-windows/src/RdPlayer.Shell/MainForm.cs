using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;
using RdPlayer.Core;
using RdPlayer.Core.Bridge;
using RdPlayer.Core.Input;
using RdPlayer.Core.Ipc;
using RdPlayer.Core.Security;
using RdPlayer.Core.Update;
using RdPlayer.Core.Watchdog;
using WinFormsTimer = System.Windows.Forms.Timer;

namespace RdPlayer.Shell;

/// <summary>
/// Janela principal: sem bordas, tela cheia no monitor configurado, always-on-top configurável, WebView2 hospedando o
/// bundle local (host virtual), bloqueio de fechamento casual, cursor oculto, energia, watchdog, ponte rd-native/1.
/// </summary>
internal sealed class MainForm : Form, IShellHost
{
    private const int WM_SYSCOMMAND = 0x0112;
    private const int SC_CLOSE = 0xF060, SC_SCREENSAVE = 0xF140, SC_MONITORPOWER = 0xF170, SC_KEYMENU = 0xF100;

    private readonly ShellContext _ctx;
    private readonly ShortcutPolicy _shortcuts;
    private readonly PinGuard _pinGuard;
    private readonly SystemTelemetry _telemetry;
    private readonly UpdateManager _updates;
    private readonly BridgeHost _bridge;
    private readonly WatchdogPolicy _localPolicy;
    private readonly WinFormsTimer _uiTick = new() { Interval = 1000 };
    private readonly WinFormsTimer _slowTick = new() { Interval = 30_000 };

    private CoreWebView2Environment? _env;
    private CoreWebView2Controller? _controller;
    private WatchdogLink? _link;
    private KeyboardHook? _hook;
    private SmokeRunner? _smoke;

    private long _lastUiTickMs = Environment.TickCount64;
    private long _lastProgressMs; // 0 = nunca
    private volatile string? _bundleState;
    private volatile bool _bundleReady;
    private bool _exitAuthorized;
    private bool _cursorHidden;
    private bool _techMenuOpen;
    private bool _showingDiagnostics;
    private bool _topMostSession;
    private string? _pendingReloadReason;
    private long _lastReviveAttemptMs;
    private bool _restartScheduled;
    private int _screenW, _screenH;
    private bool _systemEventsHooked;

    public MainForm(ShellContext ctx)
    {
        _ctx = ctx;
        var cfg = ctx.Config;
        KeyChord.TryParse(cfg.Kiosk.TechShortcut, out var tech);
        if (tech.VirtualKey == 0) KeyChord.TryParse("Ctrl+Alt+Shift+F12", out tech);
        _shortcuts = new ShortcutPolicy(tech, kioskEnabled: ctx.Kiosk);
        _pinGuard = new PinGuard(cfg.Kiosk.PinMaxAttempts, cfg.Kiosk.PinLockoutSeconds * 1000L);
        _telemetry = new SystemTelemetry(m => ctx.Log.Debug("telemetry", m));
        _updates = new UpdateManager(ctx, EmitEvent, RequestApplyUpdate);
        _bridge = new BridgeHost(ctx, this, _telemetry, _updates);
        _localPolicy = new WatchdogPolicy(WatchdogOptions.FromConfig(cfg.Watchdog));
        _topMostSession = cfg.Display.AlwaysOnTop;

        Text = "RD Mídia Indoor Player";
        BackColor = Color.Black;
        KeyPreview = true;
        ShowIcon = false;
        AutoScaleMode = AutoScaleMode.None;
        if (ctx.Kiosk && cfg.Display.Fullscreen)
        {
            FormBorderStyle = FormBorderStyle.None;
            StartPosition = FormStartPosition.Manual;
            ShowInTaskbar = false;
        }
        else
        {
            FormBorderStyle = FormBorderStyle.Sizable;
            StartPosition = FormStartPosition.Manual;
            Size = new Size(1024, 640);
        }
    }

    // ===================== ciclo de vida =====================

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        ApplyWindowMode();
        StartPlatformFeatures();
        _uiTick.Tick += (_, _) => OnUiTick();
        _uiTick.Start();
        _slowTick.Tick += (_, _) => OnSlowTick();
        _slowTick.Start();

        if (_ctx.Smoke)
        {
            _smoke = new SmokeRunner(_ctx, this);
            _smoke.StartGlobalTimeout();
        }

        try
        {
            await InitWebViewAsync();
        }
        catch (WebView2RuntimeNotFoundException ex)
        {
            _ctx.Log.Error("webview", "WebView2 Runtime não encontrado — instale o Evergreen Runtime (https://go.microsoft.com/fwlink/p/?LinkId=2124703)", ex);
            if (_smoke is not null) { _smoke.Fail("WEBVIEW2_RUNTIME_MISSING"); return; }
            ShowFatal("WebView2 Runtime não encontrado.\nInstale o Microsoft Edge WebView2 Runtime (Evergreen) e reinicie o player.");
            _ctx.ExitCode = ShellExitCodes.WebView2Missing;
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("webview", "falha ao inicializar o WebView2", ex);
            if (_smoke is not null) { _smoke.Fail("WEBVIEW2_INIT_FAILED: " + ex.GetType().Name + ": " + ex.Message); return; }
            ShowFatal("Falha ao iniciar o WebView2: " + ex.Message);
            ScheduleRestart("WEBVIEW_INIT_FAILED", 10_000);
        }
    }

    private void ShowFatal(string text)
    {
        var lbl = new Label
        {
            Text = text,
            ForeColor = Color.White,
            BackColor = Color.Black,
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI", 20f),
        };
        Controls.Add(lbl);
        if (_cursorHidden) { Cursor.Show(); _cursorHidden = false; }
    }

    private void StartPlatformFeatures()
    {
        var cfg = _ctx.Config;
        if (!_ctx.Smoke)
        {
            ApplyExecutionState(true);
            SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
            SystemEvents.PowerModeChanged += OnPowerModeChanged;
            _systemEventsHooked = true;

            if (cfg.Watchdog.Enabled)
            {
                var pipe = WdProtocol.PipeName(Process.GetCurrentProcess().SessionId);
                _link = new WatchdogLink(pipe, cfg.Watchdog.HeartbeatIntervalMs, _ctx.Args.Supervised, _ctx.Log, Sample, OnWatchdogMessage);
                _link.Start();
            }
        }
        else if (_ctx.Args.Supervised)
        {
            // teste de fumaça supervisionado: só o canal com o watchdog (sem energia/hook/cursor/autostart)
            var pipe = WdProtocol.PipeName(Process.GetCurrentProcess().SessionId);
            _link = new WatchdogLink(pipe, 1_000, true, _ctx.Log, Sample, OnWatchdogMessage);
            _link.Start();
        }

        if (_ctx.Kiosk)
        {
            if (cfg.Display.HideCursor) HideCursor();
            if (cfg.Kiosk.BlockSystemKeys)
            {
                _hook = new KeyboardHook(() => !_techMenuOpen && ContainsFocus);
                if (!_hook.Install()) _ctx.Log.Warn("kiosk", "não foi possível instalar o hook de teclado (erro " + _hook.LastError + ")");
                else _ctx.Log.Info("kiosk", "hook de teclas de sistema ativo (Win, Alt+Tab, Alt+Esc, Ctrl+Esc)");
            }
        }
        _ctx.Log.Info("main", $"modo: {(_ctx.Smoke ? "smoke-test" : _ctx.Kiosk ? "quiosque" : "janela")}; watchdog={(cfg.Watchdog.Enabled ? "on" : "off")}; prevent_sleep={cfg.Power.PreventSleep && !_ctx.Smoke}");
    }

    private void ApplyExecutionState(bool on)
    {
        if (_ctx.Smoke) return; // teste de fumaça nunca mexe em energia
        uint flags = on && _ctx.Config.Power.PreventSleep
            ? Native.ES_CONTINUOUS | Native.ES_DISPLAY_REQUIRED | Native.ES_SYSTEM_REQUIRED
            : Native.ES_CONTINUOUS;
        if (Native.SetThreadExecutionState(flags) == 0) _ctx.Log.Warn("power", "SetThreadExecutionState falhou");
    }

    private void HideCursor()
    {
        if (_cursorHidden) return;
        Cursor.Hide();
        _cursorHidden = true;
        var b = Bounds;
        Native.SetCursorPos(b.Right - 1, b.Bottom - 1);
    }

    private void ShowCursorTemporarily()
    {
        if (!_cursorHidden) return;
        Cursor.Show();
        _cursorHidden = false;
    }

    private Screen ResolveScreen()
    {
        var mon = _ctx.Config.Display.Monitor ?? "primary";
        var all = Screen.AllScreens;
        if (int.TryParse(mon, out var idx))
        {
            if (idx >= 0 && idx < all.Length) return all[idx];
            _ctx.Log.Warn("display", $"monitor {idx} não existe ({all.Length} conectados); usando o principal");
        }
        else if (mon != "primary")
        {
            var s = all.FirstOrDefault(x => string.Equals(x.DeviceName, mon, StringComparison.OrdinalIgnoreCase));
            if (s is not null) return s;
            _ctx.Log.Warn("display", $"monitor {mon} não encontrado; usando o principal");
        }
        return Screen.PrimaryScreen ?? all[0];
    }

    private void ApplyWindowMode()
    {
        var screen = ResolveScreen();
        _screenW = screen.Bounds.Width;
        _screenH = screen.Bounds.Height;
        if (_ctx.Kiosk && _ctx.Config.Display.Fullscreen)
        {
            WindowState = FormWindowState.Normal;
            Bounds = screen.Bounds;
            TopMost = _topMostSession;
        }
        else
        {
            TopMost = false;
            var wa = screen.WorkingArea;
            Location = new Point(wa.Left + Math.Max(0, (wa.Width - Width) / 2), wa.Top + Math.Max(0, (wa.Height - Height) / 2));
        }
        _ctx.Log.Info("display", $"monitor {screen.DeviceName} {_screenW}x{_screenH} ({(screen.Primary ? "principal" : "secundário")}); topmost={TopMost}");
    }

    private void OnDisplaySettingsChanged(object? sender, EventArgs e)
    {
        if (IsDisposed) return;
        BeginInvoke(new Action(() =>
        {
            ApplyWindowMode();
            EmitEvent("display_changed", new JsonObject
            {
                ["resolution"] = $"{_screenW}x{_screenH}",
                ["orientation"] = _screenW >= _screenH ? "LANDSCAPE" : "PORTRAIT",
            });
        }));
    }

    private void OnPowerModeChanged(object? sender, PowerModeChangedEventArgs e)
    {
        if (e.Mode != PowerModes.Resume || IsDisposed) return;
        BeginInvoke(new Action(() =>
        {
            _ctx.Log.Info("power", "retorno de suspensão");
            ApplyExecutionState(true);
            EmitEvent("power_resumed", new JsonObject());
        }));
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        if (_controller is not null) _controller.Bounds = ClientRectangle;
    }

    protected override void OnMove(EventArgs e)
    {
        base.OnMove(e);
        _controller?.NotifyParentWindowPositionChanged();
    }

    protected override void OnActivated(EventArgs e)
    {
        base.OnActivated(e);
        try { _controller?.MoveFocus(CoreWebView2MoveFocusReason.Programmatic); } catch (Exception) { /* controlador fechando */ }
    }

    // ===================== WebView2 =====================

    private async Task InitWebViewAsync()
    {
        var runtime = CoreWebView2Environment.GetAvailableBrowserVersionString();
        _ctx.Log.Info("webview", "WebView2 Runtime " + runtime);
        Directory.CreateDirectory(_ctx.Paths.WebView2Dir);
        var browserArgs = "--autoplay-policy=no-user-gesture-required";
        if (_ctx.Config.Dev.AllowInsecureApi) browserArgs += " --allow-running-insecure-content";
        var options = new CoreWebView2EnvironmentOptions { AdditionalBrowserArguments = browserArgs };
        _env = await CoreWebView2Environment.CreateAsync(null, _ctx.Paths.WebView2Dir, options);
        _controller = await _env.CreateCoreWebView2ControllerAsync(Handle);
        _controller.DefaultBackgroundColor = Color.Black;
        _controller.Bounds = ClientRectangle;
        _controller.IsVisible = true;
        _controller.AcceleratorKeyPressed += OnAcceleratorKeyPressed;

        var core = _controller.CoreWebView2;
        var s = core.Settings;
#if DEBUG
        s.AreDevToolsEnabled = true;
#else
        s.AreDevToolsEnabled = false; // release: devtools só pelo menu técnico, se permitido no config
#endif
        s.AreDefaultContextMenusEnabled = false;
        s.AreDefaultScriptDialogsEnabled = false;
        s.IsStatusBarEnabled = false;
        s.AreBrowserAcceleratorKeysEnabled = false;
        s.IsZoomControlEnabled = false;
        s.IsPinchZoomEnabled = false;
        s.IsSwipeNavigationEnabled = false;
        s.IsGeneralAutofillEnabled = false;
        s.IsPasswordAutosaveEnabled = false;
        s.AreHostObjectsAllowed = false;
        s.IsWebMessageEnabled = true;
        s.IsScriptEnabled = true;

        if (Directory.Exists(_ctx.BundleDir))
            core.SetVirtualHostNameToFolderMapping(_ctx.Config.Web.VirtualHost, _ctx.BundleDir, CoreWebView2HostResourceAccessKind.Deny);
        if (Directory.Exists(_ctx.ShellUiDir))
            core.SetVirtualHostNameToFolderMapping(ShellContext.ShellUiHost, _ctx.ShellUiDir, CoreWebView2HostResourceAccessKind.Deny);

        bool hideCursor = _ctx.Kiosk && _ctx.Config.Display.HideCursor;
        await core.AddScriptToExecuteOnDocumentCreatedAsync(BridgeProtocol.BuildInjectedScript(_bridge.NativeInfo(), hideCursor));

        core.WebMessageReceived += (_, e) => _bridge.Handle(e.Source, e.WebMessageAsJson);
        core.NavigationStarting += OnNavigationStarting;
        core.NavigationCompleted += OnNavigationCompleted;
        core.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            _ctx.Log.Warn("webview", "nova janela bloqueada: " + Trunc(e.Uri));
        };
        core.PermissionRequested += (_, e) =>
        {
            // armazenamento persistente (navigator.storage.persist) é concedido só às origens do player: evita que o
            // cache de mídia/IndexedDB/chave do dispositivo seja despejado sob pressão de disco. Todo o resto é negado.
            if (e.PermissionKind == CoreWebView2PermissionKind.PersistentStorage && IsAllowedNavigation(e.Uri))
            {
                e.State = CoreWebView2PermissionState.Allow;
                _ctx.Log.Info("webview", $"armazenamento persistente concedido ({Trunc(e.Uri)})");
                return;
            }
            e.State = CoreWebView2PermissionState.Deny;
            _ctx.Log.Info("webview", $"permissão negada: {e.PermissionKind} ({Trunc(e.Uri)})");
        };
        core.DownloadStarting += (_, e) =>
        {
            e.Cancel = true;
            _ctx.Log.Warn("webview", "download bloqueado");
        };
        core.ScriptDialogOpening += (_, e) => _ctx.Log.Warn("webview", $"diálogo JS suprimido ({e.Kind}): {Trunc(e.Message)}");
        core.ProcessFailed += OnProcessFailed;

        _ctx.Log.Info("webview", $"WebView2 pronto; dados em {_ctx.Paths.WebView2Dir}; bundle {_ctx.BundleDir} → {_ctx.PlayerOrigin}/");
        _localPolicy.Reset(Environment.TickCount64);

        if (_smoke is not null)
        {
            await _smoke.RunAsync(core, _env);
            return;
        }
        NavigateToPlayer();
    }

    private static string Trunc(string? s) => s is null ? "" : s.Length <= 200 ? s : s[..200] + "…";

    private void NavigateToPlayer()
    {
        var core = _controller?.CoreWebView2;
        if (core is null) return;
        _showingDiagnostics = false;
        var cfg = _ctx.Config;
        if (!_ctx.ConfigResult.Ok)
        {
            NavigateDiagnostics("config");
            return;
        }
        if (cfg.Web.Source == "remote")
        {
            core.Navigate(cfg.Web.RemoteUrl!);
            return;
        }
        var entry = Path.Combine(_ctx.BundleDir, cfg.Web.Entry.Replace('/', Path.DirectorySeparatorChar));
        if (!File.Exists(entry))
        {
            _ctx.Log.Error("webview", "bundle do player ausente: " + entry);
            NavigateDiagnostics("bundle_missing");
            return;
        }
        core.Navigate($"{_ctx.PlayerOrigin}/{cfg.Web.Entry}");
    }

    public void NavigateDiagnostics(string reason)
    {
        var core = _controller?.CoreWebView2;
        if (core is null) return;
        _showingDiagnostics = true;
        core.Navigate($"{_ctx.ShellUiOrigin}/diag.html?reason={Uri.EscapeDataString(reason)}");
    }

    private bool IsAllowedNavigation(string uri)
    {
        if (uri.StartsWith("about:blank", StringComparison.OrdinalIgnoreCase)) return true;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var u)) return false;
        var origin = u.GetLeftPart(UriPartial.Authority);
        if (string.Equals(origin, _ctx.PlayerOrigin, StringComparison.OrdinalIgnoreCase)) return true;
        if (string.Equals(origin, _ctx.ShellUiOrigin, StringComparison.OrdinalIgnoreCase)) return true;
        if (_ctx.Config.Web.Source == "remote" && Uri.TryCreate(_ctx.Config.Web.RemoteUrl, UriKind.Absolute, out var r) &&
            string.Equals(origin, r.GetLeftPart(UriPartial.Authority), StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        if (!IsAllowedNavigation(e.Uri))
        {
            e.Cancel = true;
            _ctx.Log.Warn("webview", "navegação bloqueada para origem não autorizada: " + Trunc(e.Uri));
        }
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        var uri = _controller?.CoreWebView2?.Source ?? "";
        if (e.IsSuccess) _ctx.Log.Info("webview", "página carregada: " + Trunc(uri));
        else _ctx.Log.Warn("webview", $"falha ao carregar {Trunc(uri)}: {e.WebErrorStatus}");
        _smoke?.OnNavigationCompleted(e.IsSuccess, e.WebErrorStatus.ToString(), uri);
    }

    private void OnProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
    {
        _ctx.Log.Error("webview", $"processo do WebView2 falhou: {e.ProcessFailedKind} ({e.Reason}, código {e.ExitCode})");
        if (_smoke is not null) { _smoke.Fail("WEBVIEW_PROCESS_FAILED: " + e.ProcessFailedKind); return; }
        switch (e.ProcessFailedKind)
        {
            case CoreWebView2ProcessFailedKind.BrowserProcessExited:
                // o ambiente inteiro caiu: recriar exige novo controlador → reinicia o app
                ScheduleRestart("WEBVIEW_BROWSER_PROCESS_EXITED", 1_000);
                break;
            case CoreWebView2ProcessFailedKind.RenderProcessExited:
            case CoreWebView2ProcessFailedKind.RenderProcessUnresponsive:
                ReloadPlayer("RENDER_PROCESS_" + e.ProcessFailedKind);
                break;
        }
    }

    // ===================== teclado / fechamento =====================

    private void OnAcceleratorKeyPressed(object? sender, CoreWebView2AcceleratorKeyPressedEventArgs e)
    {
        if (e.KeyEventKind is not (CoreWebView2KeyEventKind.KeyDown or CoreWebView2KeyEventKind.SystemKeyDown)) return;
        var mods = ModifierKeys;
        var chord = new KeyChord((int)e.VirtualKey,
            Ctrl: (mods & Keys.Control) != 0,
            Alt: e.KeyEventKind == CoreWebView2KeyEventKind.SystemKeyDown || (mods & Keys.Alt) != 0,
            Shift: (mods & Keys.Shift) != 0,
            Win: Native.IsWinDown());
        switch (_shortcuts.Classify(chord))
        {
            case KeyDecision.TechMenu:
                e.Handled = true;
                BeginInvoke(new Action(ShowTechMenu));
                break;
            case KeyDecision.Block:
                e.Handled = true;
                _ctx.Log.Debug("kiosk", "atalho bloqueado: " + chord);
                break;
        }
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        var chord = new KeyChord((int)(keyData & Keys.KeyCode), (keyData & Keys.Control) != 0, (keyData & Keys.Alt) != 0, (keyData & Keys.Shift) != 0, Native.IsWinDown());
        switch (_shortcuts.Classify(chord))
        {
            case KeyDecision.TechMenu:
                ShowTechMenu();
                return true;
            case KeyDecision.Block:
                return true;
        }
        return base.ProcessCmdKey(ref msg, keyData);
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_SYSCOMMAND && _ctx.Kiosk)
        {
            int cmd = (int)((long)m.WParam & 0xFFF0);
            if (cmd == SC_SCREENSAVE) return; // impede o protetor de tela enquanto o player está em primeiro plano
            if (cmd == SC_MONITORPOWER && _ctx.Config.Power.PreventSleep && (long)m.LParam != -1) return;
            if (cmd == SC_KEYMENU) return;
            if (cmd == SC_CLOSE && !_exitAuthorized && _ctx.Config.Kiosk.BlockClose)
            {
                _ctx.Log.Info("kiosk", "fechamento casual bloqueado (SC_CLOSE)");
                return;
            }
        }
        base.WndProc(ref m);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (!_exitAuthorized && _ctx.Kiosk && _ctx.Config.Kiosk.BlockClose &&
            e.CloseReason is CloseReason.UserClosing or CloseReason.None)
        {
            e.Cancel = true;
            _ctx.Log.Info("kiosk", "fechamento casual bloqueado");
            return;
        }
        _ctx.Log.Info("main", "janela fechando (" + e.CloseReason + ")");
        if (e.CloseReason == CloseReason.WindowsShutDown && _ctx.ExitCode == ShellExitCodes.Crash)
            _ctx.ExitCode = ShellExitCodes.ExitRequested;
        base.OnFormClosing(e);
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _uiTick.Stop();
        _slowTick.Stop();
        if (_systemEventsHooked)
        {
            SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
            SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        }
        ApplyExecutionState(false);
        _hook?.Dispose();
        if (_cursorHidden) { Cursor.Show(); _cursorHidden = false; }
        try { _controller?.Close(); } catch (Exception) { /* ignora */ }
        _link?.Dispose();
        base.OnFormClosed(e);
    }

    /// <summary>Encerra a casca com o código dado, sem bloqueio de fechamento.</summary>
    public void ExitWith(int code, string reason)
    {
        _ctx.Log.Info("main", $"saída solicitada: {reason} (código {code})");
        _ctx.ExitCode = code;
        _exitAuthorized = true;
        Close();
    }

    // ===================== menu técnico =====================

    private void ShowTechMenu()
    {
        if (_techMenuOpen || _smoke is not null) return;
        var (hash, pinSource) = EffectiveTechPin();
        _techMenuOpen = true;
        ShowCursorTemporarily();
        try
        {
            if (_ctx.Kiosk)
            {
                if (string.IsNullOrEmpty(hash))
                {
                    _ctx.Log.Warn("kiosk", "menu técnico solicitado, mas não há PIN técnico (nem do servidor, nem do instalador)");
                    MessageBox.Show(this, "Menu técnico desativado: defina o PIN técnico pelo painel (set_tech_pin) ou pelo install.ps1.",
                        "RD Mídia Indoor", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
                using var pin = new PinPromptForm(_pinGuard, hash);
                if (pin.ShowDialog(this) != DialogResult.OK)
                {
                    _ctx.Log.Info("kiosk", "acesso técnico cancelado ou PIN incorreto (falhas recentes: " + _pinGuard.Failures + ")");
                    return;
                }
                _ctx.Log.Info("kiosk", $"acesso técnico liberado por PIN (origem do PIN: {pinSource})");
            }

            bool autostartOn = SafeAutostartState();
            using var menu = new TechMenuForm(StatusText(autostartOn), autostartOn, TopMost, _ctx.Config.Kiosk.DevtoolsInTechMenu, _showingDiagnostics);
            menu.ShowDialog(this);
            _ctx.Log.Info("kiosk", "menu técnico: " + menu.Choice);
            switch (menu.Choice)
            {
                case TechAction.ExitPlayer:
                    _link?.Send(WdProtocol.Simple("exit_requested", "TECH_MENU"));
                    ExitWith(ShellExitCodes.ExitRequested, "menu técnico");
                    return;
                case TechAction.Reload:
                    ReloadPlayer("TECH_MENU");
                    break;
                case TechAction.RestartApp:
                    RestartNow("TECH_MENU");
                    return;
                case TechAction.ToggleAutostart:
                    ToggleAutostart(!autostartOn);
                    break;
                case TechAction.ToggleTopMost:
                    _topMostSession = !TopMost;
                    TopMost = _topMostSession;
                    break;
                case TechAction.Diagnostics:
                    NavigateDiagnostics("tech_menu");
                    break;
                case TechAction.BackToPlayer:
                    NavigateToPlayer();
                    break;
                case TechAction.DevTools:
                    if (_ctx.Config.Kiosk.DevtoolsInTechMenu && _controller?.CoreWebView2 is { } core)
                    {
                        core.Settings.AreDevToolsEnabled = true;
                        core.OpenDevToolsWindow();
                    }
                    break;
            }
        }
        finally
        {
            _techMenuOpen = false;
            if (_ctx.Kiosk && _ctx.Config.Display.HideCursor) HideCursor();
            try { _controller?.MoveFocus(CoreWebView2MoveFocusReason.Programmatic); } catch (Exception) { }
        }
    }

    private bool SafeAutostartState()
    {
        try { return RdPlayer.Core.Platform.Autostart.IsEnabled(); }
        catch (Exception ex) { _ctx.Log.Warn("autostart", "não foi possível consultar: " + ex.Message); return false; }
    }

    private void ToggleAutostart(bool enable)
    {
        try
        {
            var exe = _ctx.WatchdogExePath;
            if (enable && !File.Exists(exe))
            {
                MessageBox.Show(this, "RdPlayer.Watchdog.exe não encontrado em " + exe, "Autostart", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
            var (ok, detail) = enable
                ? RdPlayer.Core.Platform.Autostart.Enable(_ctx.Config.Autostart.Method, exe, Path.Combine(_ctx.Paths.DataRoot, "tmp"))
                : RdPlayer.Core.Platform.Autostart.Disable();
            _ctx.Log.Info("autostart", $"{(enable ? "ligar" : "desligar")}: {(ok ? "ok" : "falhou")} — {detail}");
            MessageBox.Show(this, (ok ? "OK: " : "Falhou: ") + detail, "Autostart", MessageBoxButtons.OK, ok ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("autostart", "falha", ex);
        }
    }

    private string StatusText(bool autostartOn)
    {
        long now = Environment.TickCount64;
        long lp = Interlocked.Read(ref _lastProgressMs);
        return UiText.Lines(new[]
        {
            $"RD Mídia Indoor Player {AppInfo.Version} — {_telemetry.Os()}",
            $"Config: {_ctx.Paths.ConfigPath} ({(_ctx.ConfigResult.Ok ? "ok" : "COM ERROS")})",
            $"API: {_ctx.Config.ApiBaseUrl ?? "(não configurada)"}",
            $"Bundle: {_ctx.BundleDir}",
            $"Tela: {_screenW}x{_screenH}; sempre visível: {(TopMost ? "sim" : "não")}",
            $"Watchdog: {(_link?.IsConnected == true ? "conectado" : "não conectado")}; bundle: {_bundleState ?? "-"}{(_bundleReady ? " (pronto)" : "")}; último progresso: {(lp == 0 ? "nunca" : $"há {(now - lp) / 1000} s")}",
            $"Iniciar com o Windows: {(autostartOn ? "ligado" : "desligado")}; PIN técnico: {EffectiveTechPin().From switch { TechPinResolver.Source.Server => "do servidor (painel)", TechPinResolver.Source.Installer => "do instalador (até o painel definir)", _ => "não definido" }}",
            $"Logs: {_ctx.Paths.LogsDir}",
        });
    }

    // ===================== watchdog / progresso =====================

    private HeartbeatSample Sample()
    {
        long now = Environment.TickCount64;
        long lp = Interlocked.Read(ref _lastProgressMs);
        return new HeartbeatSample(
            UiAgeMs: now - Interlocked.Read(ref _lastUiTickMs),
            ProgressAgeMs: lp == 0 ? null : now - lp,
            BundleState: _bundleState,
            BundleReady: _bundleReady);
    }

    private void OnUiTick()
    {
        long now = Environment.TickCount64;
        Interlocked.Exchange(ref _lastUiTickMs, now);

        if (_ctx.Smoke || _controller is null || _restartScheduled) return;
        // sem watchdog conectado, a própria casca aplica a política de progresso (recarga/reinício)
        _localPolicy.OnHeartbeat(now, Sample());
        if (_link?.IsConnected == true) return; // com watchdog conectado, ele decide
        var action = _localPolicy.Evaluate(now);
        if (action.Kind == WatchdogActionKind.Reload) ReloadPlayer("LOCAL_" + action.Reason);
        else if (action.Kind == WatchdogActionKind.Restart && !action.Reason.StartsWith("UI_", StringComparison.Ordinal) && !action.Reason.Contains("HEARTBEAT"))
            RestartNow("LOCAL_" + action.Reason);
    }

    private void OnSlowTick()
    {
        if (_ctx.Smoke) return;
        ApplyExecutionState(true);
        if (_ctx.Kiosk && _ctx.Config.Display.Fullscreen && _topMostSession && !_techMenuOpen)
            Native.SetWindowPos(Handle, Native.HWND_TOPMOST, 0, 0, 0, 0, Native.SWP_NOMOVE | Native.SWP_NOSIZE | Native.SWP_NOACTIVATE);
        MaybeReviveWatchdog();
    }

    /// <summary>Supervisão mútua: se a casca foi lançada pelo watchdog e ele sumiu por &gt; 30 s, relança-o.</summary>
    private void MaybeReviveWatchdog()
    {
        var cfg = _ctx.Config.Watchdog;
        if (!_ctx.Args.Supervised || !cfg.Enabled || !cfg.ReviveWatchdog || _link is null || _link.IsConnected) return;
        long now = Environment.TickCount64;
        if (now - _link.LastConnectedMs < 30_000 || now - _lastReviveAttemptMs < 120_000) return;
        _lastReviveAttemptMs = now;
        var exe = _ctx.WatchdogExePath;
        if (!File.Exists(exe)) return;
        try
        {
            var psi = new ProcessStartInfo(exe) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(exe)! };
            psi.ArgumentList.Add("--adopt");
            foreach (var a in _ctx.Args.ToShellArgs(supervised: false)) psi.ArgumentList.Add(a);
            Process.Start(psi)?.Dispose();
            _ctx.Log.Warn("watchdog-link", "watchdog ausente há mais de 30 s; relançado");
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("watchdog-link", "falha ao relançar o watchdog", ex);
        }
    }

    private void OnWatchdogMessage(WdMessage msg)
    {
        if (IsDisposed) return;
        try
        {
            BeginInvoke(new Action(() =>
            {
                switch (msg.Type)
                {
                    case "reload":
                        ReloadPlayer("WATCHDOG_" + (msg.GetString("reason") ?? ""));
                        break;
                    case "shutdown":
                        ExitWith(ShellExitCodes.ExitRequested, "watchdog pediu encerramento");
                        break;
                    case "smoke_ack":
                        _smoke?.OnWatchdogAck();
                        break;
                }
            }));
        }
        catch (InvalidOperationException)
        {
            // janela fechando
        }
    }

    // ===================== IShellHost =====================

    public (int Width, int Height) ScreenSize => (_screenW, _screenH);

    public void PostJson(string json)
    {
        try { _controller?.CoreWebView2?.PostWebMessageAsJson(json); }
        catch (Exception ex) { _ctx.Log.Warn("bridge", "falha ao enviar mensagem ao bundle: " + ex.GetType().Name); }
    }

    private void EmitEvent(string name, JsonObject data) => PostJson(BridgeProtocol.Event(name, data));

    public void OnProgress(string? state)
    {
        Interlocked.Exchange(ref _lastProgressMs, Environment.TickCount64);
        _bundleState = state;
    }

    public void OnBundleReady(string? bundleVersion)
    {
        bool first = !_bundleReady;
        _bundleReady = true;
        Interlocked.Exchange(ref _lastProgressMs, Environment.TickCount64);
        if (first) _ctx.Log.Info("bridge", "bundle pronto (versão " + (bundleVersion ?? "?") + ")");
        _link?.Send(WdProtocol.Simple("bundle_ready"));
        if (_pendingReloadReason is not null)
        {
            EmitEvent("watchdog_reload", new JsonObject { ["reason"] = _pendingReloadReason });
            _pendingReloadReason = null;
        }
        ReportUpdateResultOnce();
    }

    private void ReportUpdateResultOnce()
    {
        try
        {
            var us = StateStore.LoadUpdateState(_ctx.Paths.UpdateStatePath);
            if (us.LastResult is not { } r) return;
            var ss = StateStore.LoadShellState(_ctx.Paths.ShellStatePath);
            if (ss.LastReportedUpdateMs is long last && last >= r.AtMs) return;
            EmitEvent("update_status", new JsonObject
            {
                ["state"] = r.Result == "COMMITTED" ? "APPLIED" : r.Result,
                ["version"] = r.Version,
                ["detail"] = r.Detail ?? "",
            });
            ss.LastReportedUpdateMs = r.AtMs;
            StateStore.SaveShellState(_ctx.Paths.ShellStatePath, ss);
        }
        catch (Exception ex)
        {
            _ctx.Log.Warn("update", "não foi possível informar o resultado da atualização: " + ex.Message);
        }
    }

    public void OnDiagResult(JsonObject result) => _smoke?.OnDiagResult(result);

    /// <summary>Somente teste de fumaça: geometria da janela × controlador WebView2 (pixels físicos) e escala de DPI.</summary>
    internal JsonObject SmokeGeometry() => new()
    {
        ["client_px"] = $"{ClientSize.Width}x{ClientSize.Height}",
        ["controller_bounds_px"] = _controller is null ? null : $"{_controller.Bounds.Width}x{_controller.Bounds.Height}",
        ["controller_at_origin"] = _controller is not null && _controller.Bounds.X == 0 && _controller.Bounds.Y == 0,
        ["controller_fills_client"] = _controller is not null && _controller.Bounds.Size == ClientSize,
        ["rasterization_scale"] = _controller is null ? null : Math.Round(_controller.RasterizationScale, 3),
        ["window_dpi"] = DeviceDpi,
    };

    /// <summary>Somente teste de fumaça: redimensiona a área cliente (ex.: proporção retrato) sem tela cheia.</summary>
    internal void SmokeSetClientSize(int width, int height) => ClientSize = new Size(width, height);

    /// <summary>PIN efetivo: o do servidor (set_tech_pin via bundle) prevalece; o do instalador é só semente.</summary>
    private (string? Hash, TechPinResolver.Source From) EffectiveTechPin()
    {
        var ss = StateStore.LoadShellState(_ctx.Paths.ShellStatePath);
        return TechPinResolver.Effective(ss.ServerTechPinHash, _ctx.Config.Kiosk.TechPinHash);
    }

    public void OnServerTechPin(string encodedHash, long setAtMs)
    {
        try
        {
            var ss = StateStore.LoadShellState(_ctx.Paths.ShellStatePath);
            if (!TechPinResolver.ShouldAccept(ss.ServerTechPinSetAtMs, setAtMs, ss.ServerTechPinHash, encodedHash))
            {
                _ctx.Log.Debug("kiosk", "tech_pin repetido ou mais antigo que o atual: ignorado");
                return;
            }
            ss.ServerTechPinHash = encodedHash;
            ss.ServerTechPinSetAtMs = setAtMs > 0 ? setAtMs : DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            StateStore.SaveShellState(_ctx.Paths.ShellStatePath, ss);
            _ctx.Log.Info("kiosk", "PIN técnico atualizado pelo servidor (set_tech_pin repassado pelo bundle); passa a valer no menu técnico nativo");
            _smoke?.OnTechPinStored();
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("kiosk", "falha ao gravar o PIN técnico do servidor", ex);
        }
    }

    public void OnBridgeTraffic(string method) => _smoke?.OnBridgeTraffic(method);

    public void ReloadPlayer(string reason)
    {
        _ctx.Log.Warn("watchdog", "recarregando o WebView: " + reason);
        _pendingReloadReason = reason;
        _localPolicy.OnReloadIssued(Environment.TickCount64);
        _bundleReady = false;
        NavigateToPlayer();
    }

    public void ScheduleRestart(string reason, int delayMs)
    {
        if (_restartScheduled) return;
        _restartScheduled = true;
        _ctx.Log.Warn("main", $"reinício do app agendado em {delayMs} ms: {reason}");
        var t = new WinFormsTimer { Interval = Math.Max(1, delayMs) };
        t.Tick += (_, _) =>
        {
            t.Stop();
            t.Dispose();
            RestartNow(reason);
        };
        t.Start();
    }

    /// <summary>
    /// Reinicia o app: com watchdog conectado, avisa e sai com código 3 (o watchdog relança);
    /// sem watchdog, lança uma nova instância (que espera o mutex) e sai.
    /// </summary>
    public void RestartNow(string reason)
    {
        if (_link?.IsConnected == true)
        {
            _link.Send(WdProtocol.Simple("restart_requested", reason));
        }
        else if (!_ctx.Smoke)
        {
            try
            {
                var psi = new ProcessStartInfo(_ctx.ExePath) { UseShellExecute = false, WorkingDirectory = _ctx.ExeDir };
                psi.ArgumentList.Add("--relaunch");
                foreach (var a in _ctx.Args.ToShellArgs(_ctx.Args.Supervised)) psi.ArgumentList.Add(a);
                Process.Start(psi)?.Dispose();
            }
            catch (Exception ex)
            {
                _ctx.Log.Error("main", "falha ao relançar a casca", ex);
            }
        }
        ExitWith(ShellExitCodes.RestartRequested, "reinício: " + reason);
    }

    private void RequestApplyUpdate(string version, string stagedDir)
    {
        // dá tempo para a resposta da ponte/command_ack sair antes de encerrar
        var t = new WinFormsTimer { Interval = 2_000 };
        t.Tick += (_, _) =>
        {
            t.Stop();
            t.Dispose();
            if (_link?.IsConnected == true)
            {
                _link.Send(WdProtocol.ApplyUpdate(version, stagedDir));
            }
            else
            {
                var exe = _ctx.WatchdogExePath;
                try
                {
                    var psi = new ProcessStartInfo(exe) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(exe)! };
                    psi.ArgumentList.Add("--apply-update");
                    foreach (var a in _ctx.Args.ToShellArgs(supervised: false)) psi.ArgumentList.Add(a);
                    Process.Start(psi)?.Dispose();
                }
                catch (Exception ex)
                {
                    _ctx.Log.Error("update", "não foi possível iniciar o updater (watchdog) — atualização fica pendente", ex);
                    return;
                }
            }
            ExitWith(ShellExitCodes.ApplyUpdate, "aplicar atualização " + version);
        };
        t.Start();
    }
}
