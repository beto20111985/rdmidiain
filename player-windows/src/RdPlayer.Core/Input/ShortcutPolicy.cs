namespace RdPlayer.Core.Input;

public enum KeyDecision
{
    Allow,
    Block,
    TechMenu,
}

/// <summary>
/// Mapeamento de atalhos bloqueados no modo quiosque (impedir fechamento/recarga/devtools casuais).
/// Regra: o atalho técnico abre o menu com PIN; teclas de função, Alt+tecla, Ctrl+tecla (exceto edição),
/// teclas de navegador e zoom são bloqueadas; digitação comum (inclusive AltGr = Ctrl+Alt) passa para a página.
/// </summary>
public sealed class ShortcutPolicy
{
    private static readonly HashSet<int> CtrlEditingAllowed = new() { 'A', 'C', 'V', 'X', 'Z', 'Y' };

    public ShortcutPolicy(KeyChord techShortcut, bool kioskEnabled = true)
    {
        TechShortcut = techShortcut;
        KioskEnabled = kioskEnabled;
    }

    public KeyChord TechShortcut { get; }
    public bool KioskEnabled { get; }

    public KeyDecision Classify(KeyChord k)
    {
        if (Matches(k, TechShortcut)) return KeyDecision.TechMenu;
        // teclas modificadoras sozinhas (Ctrl, Shift, Alt) não fazem nada por si; bloqueá-las quebraria Ctrl+C/AltGr
        if (k.VirtualKey is Vk.Shift or Vk.Control or Vk.Menu or >= 0xA0 and <= 0xA5) return KeyDecision.Allow;
        if (!KioskEnabled)
        {
            // modo janela/desenvolvimento: ainda bloqueia recarga e devtools para não quebrar o player
            return IsReloadOrDevtools(k) ? KeyDecision.Block : KeyDecision.Allow;
        }

        // Alt+F4 e afins
        if (k.IsFunctionKey) return KeyDecision.Block;
        // teclas de navegador/mídia/apps (voltar, avançar, atualizar, home, e-mail, calculadora…) e menu de contexto
        if (k.VirtualKey is >= Vk.BrowserBack and <= Vk.BrowserHome) return KeyDecision.Block;
        if (k.VirtualKey is >= Vk.LaunchMail and <= Vk.LaunchApp2) return KeyDecision.Block;
        if (k.VirtualKey is Vk.Apps or Vk.Sleep) return KeyDecision.Block;
        if (k.Win) return KeyDecision.Block;

        bool altGr = k.Ctrl && k.Alt;
        if (altGr)
        {
            // AltGr produz caracteres em teclados ABNT2 (/, ?, °). Bloqueia apenas Ctrl+Alt+Del/Ins/Home/End/navegação.
            return IsPrintable(k.VirtualKey) ? KeyDecision.Allow : KeyDecision.Block;
        }
        if (k.Alt) return KeyDecision.Block; // Alt+Left/Right (histórico), Alt+Home, Alt+Space…
        if (k.Ctrl)
        {
            if (k.Shift) return KeyDecision.Block; // Ctrl+Shift+I/J/C/R/N/Delete…
            if (CtrlEditingAllowed.Contains(k.VirtualKey)) return KeyDecision.Allow; // copiar/colar em campos de login técnico
            return KeyDecision.Block; // Ctrl+W/R/P/S/O/N/T/F/L/U/+/-/0/Tab…
        }
        return KeyDecision.Allow;
    }

    /// <summary>Teclas de sistema (somente com kiosk.block_system_keys, via hook de baixo nível).</summary>
    public static bool IsBlockedSystemKey(int vk, bool ctrl, bool alt)
    {
        if (vk is Vk.LWin or Vk.RWin) return true;
        if (alt && vk is Vk.Tab or Vk.Escape or Vk.Space) return true;
        if (ctrl && vk == Vk.Escape) return true;
        return false;
    }

    private static bool IsReloadOrDevtools(KeyChord k) =>
        k.VirtualKey == Vk.F5 || k.VirtualKey == Vk.F12 ||
        (k.Ctrl && k.VirtualKey == 'R') ||
        (k.Ctrl && k.Shift && k.VirtualKey is 'I' or 'J' or 'C');

    private static bool IsPrintable(int vk) =>
        vk is >= 'A' and <= 'Z' or >= '0' and <= '9' or >= 0xBA and <= 0xC0 or >= 0xDB and <= 0xE2;

    private static bool Matches(KeyChord a, KeyChord b) =>
        a.VirtualKey == b.VirtualKey && a.Ctrl == b.Ctrl && a.Alt == b.Alt && a.Shift == b.Shift && a.Win == b.Win;
}
