using System.Text.RegularExpressions;

namespace RdPlayer.Core.Update;

/// <summary>Versão semântica simplificada: MAJOR.MINOR.PATCH[-pre.release].</summary>
public readonly partial record struct AppVersion(int Major, int Minor, int Patch, string? Prerelease) : IComparable<AppVersion>
{
    [GeneratedRegex(@"^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")]
    private static partial Regex Pattern();

    public static bool TryParse(string? s, out AppVersion v)
    {
        v = default;
        if (string.IsNullOrEmpty(s) || s.Length > 64) return false;
        var m = Pattern().Match(s);
        if (!m.Success) return false;
        v = new AppVersion(int.Parse(m.Groups[1].Value), int.Parse(m.Groups[2].Value), int.Parse(m.Groups[3].Value),
            m.Groups[4].Success ? m.Groups[4].Value : null);
        return true;
    }

    public static AppVersion Parse(string s) => TryParse(s, out var v) ? v : throw new FormatException("versão inválida: " + s);

    public int CompareTo(AppVersion other)
    {
        int c = Major.CompareTo(other.Major);
        if (c != 0) return c;
        c = Minor.CompareTo(other.Minor);
        if (c != 0) return c;
        c = Patch.CompareTo(other.Patch);
        if (c != 0) return c;
        if (Prerelease is null) return other.Prerelease is null ? 0 : 1;
        if (other.Prerelease is null) return -1;
        var a = Prerelease.Split('.');
        var b = other.Prerelease.Split('.');
        for (int i = 0; i < Math.Min(a.Length, b.Length); i++)
        {
            bool an = long.TryParse(a[i], out var ai);
            bool bn = long.TryParse(b[i], out var bi);
            if (an && bn) c = ai.CompareTo(bi);
            else if (an) c = -1;
            else if (bn) c = 1;
            else c = string.CompareOrdinal(a[i], b[i]);
            if (c != 0) return c;
        }
        return a.Length.CompareTo(b.Length);
    }

    public static bool operator <(AppVersion a, AppVersion b) => a.CompareTo(b) < 0;
    public static bool operator >(AppVersion a, AppVersion b) => a.CompareTo(b) > 0;
    public static bool operator <=(AppVersion a, AppVersion b) => a.CompareTo(b) <= 0;
    public static bool operator >=(AppVersion a, AppVersion b) => a.CompareTo(b) >= 0;

    public override string ToString() => Prerelease is null ? $"{Major}.{Minor}.{Patch}" : $"{Major}.{Minor}.{Patch}-{Prerelease}";
}
