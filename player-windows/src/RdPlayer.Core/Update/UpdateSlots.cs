using System.Text.Json;
using RdPlayer.Core.IO;

namespace RdPlayer.Core.Update;

/// <summary>
/// Layout de instalação com slots (troca atômica por rename de diretório, rollback para a versão anterior):
/// <code>
/// &lt;raiz&gt;\RdPlayer.Watchdog.exe
/// &lt;raiz&gt;\app\current\  (RdPlayer.Shell.exe, shell-ui\, wwwroot\, version.json)
/// &lt;raiz&gt;\app\previous\
/// &lt;raiz&gt;\app\staging\&lt;versão&gt;\
/// </code>
/// </summary>
public sealed class UpdateSlots
{
    public const string ShellExeName = "RdPlayer.Shell.exe";
    public const string WatchdogExeName = "RdPlayer.Watchdog.exe";
    public const string VersionFileName = "version.json";

    public UpdateSlots(string installRoot)
    {
        InstallRoot = Path.GetFullPath(installRoot);
        AppDir = Path.Combine(InstallRoot, "app");
    }

    public string InstallRoot { get; }
    public string AppDir { get; }
    public string CurrentDir => Path.Combine(AppDir, "current");
    public string PreviousDir => Path.Combine(AppDir, "previous");
    public string StagingRoot => Path.Combine(AppDir, "staging");
    public string FailedDir => Path.Combine(AppDir, "failed");

    public string StagingDirFor(string version)
    {
        if (!AppVersion.TryParse(version, out _)) throw new ArgumentException("versão inválida", nameof(version));
        return Path.Combine(StagingRoot, version);
    }

    public bool IsSlotted => Directory.Exists(CurrentDir);

    /// <summary>Promove o diretório preparado: current → previous (descarta o previous antigo), staged → current.</summary>
    public void Promote(string stagedDir)
    {
        var staged = Path.GetFullPath(stagedDir);
        if (!Directory.Exists(staged)) throw new DirectoryNotFoundException("staging inexistente: " + staged);
        Directory.CreateDirectory(AppDir);
        if (Directory.Exists(PreviousDir)) FileUtil.Retry(() => Directory.Delete(PreviousDir, true));
        bool movedCurrent = false;
        if (Directory.Exists(CurrentDir))
        {
            FileUtil.Retry(() => Directory.Move(CurrentDir, PreviousDir));
            movedCurrent = true;
        }
        try
        {
            FileUtil.Retry(() => Directory.Move(staged, CurrentDir));
        }
        catch
        {
            if (movedCurrent && !Directory.Exists(CurrentDir) && Directory.Exists(PreviousDir))
                FileUtil.Retry(() => Directory.Move(PreviousDir, CurrentDir));
            throw;
        }
    }

    /// <summary>Volta para a versão anterior. Retorna false se não houver previous.</summary>
    public bool Rollback()
    {
        if (!Directory.Exists(PreviousDir)) return false;
        FileUtil.DeleteDirectoryQuiet(FailedDir);
        if (Directory.Exists(CurrentDir)) FileUtil.Retry(() => Directory.Move(CurrentDir, FailedDir));
        FileUtil.Retry(() => Directory.Move(PreviousDir, CurrentDir));
        FileUtil.DeleteDirectoryQuiet(FailedDir);
        return true;
    }

    public void CleanStaging()
    {
        FileUtil.DeleteDirectoryQuiet(StagingRoot);
    }

    public static string? ReadVersion(string dir)
    {
        var f = Path.Combine(dir, VersionFileName);
        if (!File.Exists(f)) return null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(f));
            if (doc.RootElement.TryGetProperty("version", out var v) && v.ValueKind == JsonValueKind.String)
            {
                var s = v.GetString();
                return AppVersion.TryParse(s, out _) ? s : null;
            }
        }
        catch (Exception ex) when (ex is JsonException || ex is IOException)
        {
        }
        return null;
    }

    /// <summary>Valida o conteúdo de um pacote extraído: exe da casca presente e version.json coerente.</summary>
    public static string? ValidatePackageDir(string dir, string expectedVersion)
    {
        if (!File.Exists(Path.Combine(dir, ShellExeName))) return "pacote sem " + ShellExeName;
        var v = ReadVersion(dir);
        if (v is null) return "pacote sem version.json válido";
        if (v != expectedVersion) return $"version.json ({v}) difere da versão anunciada ({expectedVersion})";
        return null;
    }
}
