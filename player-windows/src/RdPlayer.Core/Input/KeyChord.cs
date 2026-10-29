namespace RdPlayer.Core.Input;

/// <summary>Combinação de teclas (códigos virtuais do Windows).</summary>
public readonly record struct KeyChord(int VirtualKey, bool Ctrl, bool Alt, bool Shift, bool Win = false)
{
    public bool HasModifier => Ctrl || Alt || Shift || Win;
    public bool IsFunctionKey => VirtualKey >= Vk.F1 && VirtualKey <= Vk.F24;

    public static bool TryParse(string? text, out KeyChord chord)
    {
        chord = default;
        if (string.IsNullOrWhiteSpace(text)) return false;
        bool ctrl = false, alt = false, shift = false, win = false;
        int? key = null;
        foreach (var raw in text.Split('+'))
        {
            var t = raw.Trim();
            switch (t.ToLowerInvariant())
            {
                case "ctrl": case "control": ctrl = true; continue;
                case "alt": alt = true; continue;
                case "shift": shift = true; continue;
                case "win": case "windows": win = true; continue;
            }
            if (key is not null) return false;
            var vk = Vk.FromName(t);
            if (vk is null) return false;
            key = vk;
        }
        if (key is null) return false;
        chord = new KeyChord(key.Value, ctrl, alt, shift, win);
        return true;
    }

    public override string ToString()
    {
        var parts = new List<string>();
        if (Ctrl) parts.Add("Ctrl");
        if (Alt) parts.Add("Alt");
        if (Shift) parts.Add("Shift");
        if (Win) parts.Add("Win");
        parts.Add(Vk.ToName(VirtualKey));
        return string.Join("+", parts);
    }
}

/// <summary>Códigos de tecla virtual (winuser.h).</summary>
public static class Vk
{
    public const int Back = 0x08, Tab = 0x09, Enter = 0x0D, Shift = 0x10, Control = 0x11, Menu = 0x12, Pause = 0x13,
        Escape = 0x1B, Space = 0x20, PageUp = 0x21, PageDown = 0x22, End = 0x23, Home = 0x24,
        Left = 0x25, Up = 0x26, Right = 0x27, Down = 0x28, Insert = 0x2D, Delete = 0x2E,
        LWin = 0x5B, RWin = 0x5C, Apps = 0x5D, Sleep = 0x5F,
        F1 = 0x70, F4 = 0x73, F5 = 0x74, F11 = 0x7A, F12 = 0x7B, F24 = 0x87,
        Add = 0x6B, Subtract = 0x6D, OemPlus = 0xBB, OemMinus = 0xBD,
        BrowserBack = 0xA6, BrowserHome = 0xAC, LaunchMail = 0xB4, LaunchApp2 = 0xB7;

    public static int? FromName(string name)
    {
        var n = name.Trim();
        if (n.Length == 1)
        {
            char c = char.ToUpperInvariant(n[0]);
            if (c is >= 'A' and <= 'Z') return c;
            if (c is >= '0' and <= '9') return c;
        }
        var u = n.ToUpperInvariant();
        if (u.Length >= 2 && u[0] == 'F' && int.TryParse(u.AsSpan(1), out int f) && f is >= 1 and <= 24) return F1 + f - 1;
        return u switch
        {
            "ESC" or "ESCAPE" => Escape,
            "TAB" => Tab,
            "SPACE" => Space,
            "ENTER" or "RETURN" => Enter,
            "BACKSPACE" => Back,
            "HOME" => Home,
            "END" => End,
            "INSERT" or "INS" => Insert,
            "DELETE" or "DEL" => Delete,
            "PAGEUP" or "PGUP" => PageUp,
            "PAGEDOWN" or "PGDN" => PageDown,
            "LEFT" => Left,
            "RIGHT" => Right,
            "UP" => Up,
            "DOWN" => Down,
            "PAUSE" => Pause,
            _ => null,
        };
    }

    public static string ToName(int vk)
    {
        if (vk is >= 'A' and <= 'Z' or >= '0' and <= '9') return ((char)vk).ToString();
        if (vk is >= F1 and <= F24) return "F" + (vk - F1 + 1);
        return vk switch
        {
            Escape => "Esc", Tab => "Tab", Space => "Space", Enter => "Enter", Back => "Backspace",
            Home => "Home", End => "End", Insert => "Insert", Delete => "Delete", PageUp => "PageUp", PageDown => "PageDown",
            Left => "Left", Right => "Right", Up => "Up", Down => "Down", Pause => "Pause",
            _ => "VK_0x" + vk.ToString("X2"),
        };
    }
}
