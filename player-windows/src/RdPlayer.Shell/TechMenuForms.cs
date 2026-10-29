using System;
using System.Collections.Generic;
using System.Drawing;
using System.Windows.Forms;
using RdPlayer.Core.Security;

namespace RdPlayer.Shell;

/// <summary>Pede o PIN técnico (PBKDF2), com bloqueio após tentativas erradas.</summary>
internal sealed class PinPromptForm : Form
{
    private readonly TextBox _pin = new() { UseSystemPasswordChar = true, Width = 220, MaxLength = 32, Font = new Font("Segoe UI", 14f) };
    private readonly Label _msg = new() { AutoSize = true, ForeColor = Color.Firebrick, Font = new Font("Segoe UI", 9.5f) };
    private readonly PinGuard _guard;
    private readonly string _hash;

    public PinPromptForm(PinGuard guard, string hash)
    {
        _guard = guard;
        _hash = hash;
        Text = "RD Mídia Indoor — acesso técnico";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        TopMost = true;
        MaximizeBox = MinimizeBox = false;
        ShowInTaskbar = false;
        AutoScaleMode = AutoScaleMode.Dpi;
        Padding = new Padding(16);
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;

        var ok = new Button { Text = "Entrar", DialogResult = DialogResult.None, AutoSize = true };
        var cancel = new Button { Text = "Cancelar", DialogResult = DialogResult.Cancel, AutoSize = true };
        ok.Click += (_, _) => TryAccept();
        AcceptButton = ok;
        CancelButton = cancel;

        var layout = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, AutoSize = true, Dock = DockStyle.Fill };
        layout.Controls.Add(new Label { Text = "PIN técnico:", AutoSize = true, Font = new Font("Segoe UI", 10f) });
        layout.Controls.Add(_pin);
        layout.Controls.Add(_msg);
        var buttons = new FlowLayoutPanel { FlowDirection = FlowDirection.LeftToRight, AutoSize = true };
        buttons.Controls.Add(ok);
        buttons.Controls.Add(cancel);
        layout.Controls.Add(buttons);
        Controls.Add(layout);
        UpdateLockState();
    }

    private void UpdateLockState()
    {
        if (_guard.IsLocked(Environment.TickCount64, out var rem))
        {
            _pin.Enabled = false;
            _msg.Text = $"Muitas tentativas. Tente novamente em {Math.Ceiling(rem / 1000.0)} s.";
        }
    }

    /// <summary>Somente teste de fumaça: submete um PIN pelo MESMO caminho do botão "Entrar" (sem exibir a janela).</summary>
    internal bool SubmitForSmokeTest(string pin)
    {
        _pin.Text = pin;
        TryAccept();
        return DialogResult == DialogResult.OK;
    }

    private void TryAccept()
    {
        long now = Environment.TickCount64;
        if (_guard.IsLocked(now, out _))
        {
            UpdateLockState();
            return;
        }
        bool ok = PinHash.Verify(_pin.Text, _hash);
        _guard.Register(now, ok);
        _pin.Clear();
        if (ok)
        {
            DialogResult = DialogResult.OK;
            Close();
            return;
        }
        _msg.Text = "PIN incorreto.";
        UpdateLockState();
    }
}

internal enum TechAction
{
    None,
    ExitPlayer,
    Reload,
    RestartApp,
    ToggleAutostart,
    ToggleTopMost,
    Diagnostics,
    BackToPlayer,
    DevTools,
}

/// <summary>Menu técnico (após PIN). Ações sensíveis ficam aqui; o cliente final não tem como sair do player sem o PIN.</summary>
internal sealed class TechMenuForm : Form
{
    public TechAction Choice { get; private set; } = TechAction.None;

    public TechMenuForm(string statusText, bool autostartOn, bool topMostOn, bool devtoolsAllowed, bool showingDiagnostics)
    {
        Text = "RD Mídia Indoor — menu técnico";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        TopMost = true;
        MaximizeBox = MinimizeBox = false;
        ShowInTaskbar = false;
        AutoScaleMode = AutoScaleMode.Dpi;
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        Padding = new Padding(16);

        var layout = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, AutoSize = true, Dock = DockStyle.Fill };
        layout.Controls.Add(new Label { Text = statusText, AutoSize = true, MaximumSize = new Size(560, 0), Font = new Font("Consolas", 9f) });

        void Add(string text, TechAction action)
        {
            var b = new Button { Text = text, Width = 420, Height = 40, Font = new Font("Segoe UI", 10.5f) };
            b.Click += (_, _) => { Choice = action; DialogResult = DialogResult.OK; Close(); };
            layout.Controls.Add(b);
        }

        Add("Recarregar o player", TechAction.Reload);
        Add("Reiniciar o aplicativo", TechAction.RestartApp);
        Add(showingDiagnostics ? "Voltar ao player" : "Diagnóstico do sistema", showingDiagnostics ? TechAction.BackToPlayer : TechAction.Diagnostics);
        Add(autostartOn ? "Iniciar com o Windows: LIGADO (desligar)" : "Iniciar com o Windows: DESLIGADO (ligar)", TechAction.ToggleAutostart);
        Add(topMostOn ? "Sempre visível: LIGADO (desligar nesta sessão)" : "Sempre visível: DESLIGADO (ligar nesta sessão)", TechAction.ToggleTopMost);
        if (devtoolsAllowed) Add("Abrir ferramentas de desenvolvedor", TechAction.DevTools);
        Add("Fechar o player (encerra também o watchdog)", TechAction.ExitPlayer);
        var cancel = new Button { Text = "Cancelar", Width = 420, Height = 36, DialogResult = DialogResult.Cancel };
        layout.Controls.Add(cancel);
        CancelButton = cancel;
        Controls.Add(layout);
    }
}

internal static class UiText
{
    public static string Lines(IEnumerable<string> lines) => string.Join(Environment.NewLine, lines);
}
