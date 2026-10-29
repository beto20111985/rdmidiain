using System.Text;
using System.Text.RegularExpressions;

namespace RdPlayer.Core.Logging;

public enum LogLevel
{
    Debug = 0,
    Info = 1,
    Warn = 2,
    Error = 3,
}

/// <summary>Remove segredos de mensagens de log (tokens, JWT, tickets, PIN, senhas, assinaturas).</summary>
public static partial class LogRedactor
{
    [GeneratedRegex(@"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*")]
    private static partial Regex BearerRx();

    [GeneratedRegex(@"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")]
    private static partial Regex JwtRx();

    [GeneratedRegex(@"(?i)""(access_token|refresh_token|token|ticket|password|senha|pin|secret|signature|totp|authorization|private_key)""\s*:\s*""[^""]*""")]
    private static partial Regex JsonSecretRx();

    [GeneratedRegex(@"(?i)([?&](?:ticket|token|access_token|signature|sig)=)[^&\s""']+")]
    private static partial Regex QuerySecretRx();

    public static string Redact(string? message)
    {
        if (string.IsNullOrEmpty(message)) return "";
        var s = BearerRx().Replace(message, "Bearer ***");
        s = JwtRx().Replace(s, "***jwt***");
        s = JsonSecretRx().Replace(s, m => $"\"{m.Groups[1].Value}\":\"***\"");
        s = QuerySecretRx().Replace(s, "$1***");
        return s;
    }
}

/// <summary>Log local em arquivo, com rotação por tamanho, redação de segredos e thread-safe.</summary>
public sealed class RotatingFileLog : IDisposable
{
    private readonly object _lock = new();
    private readonly string _dir;
    private readonly string _baseName;
    private readonly long _maxBytes;
    private readonly int _maxFiles;
    private FileStream? _stream;
    private long _size;

    public RotatingFileLog(string directory, string baseName, LogLevel minLevel = LogLevel.Info, long maxBytes = 5 * 1024 * 1024, int maxFiles = 10)
    {
        _dir = directory;
        _baseName = baseName;
        MinLevel = minLevel;
        _maxBytes = Math.Max(16 * 1024, maxBytes);
        _maxFiles = Math.Max(1, maxFiles);
        Directory.CreateDirectory(_dir);
    }

    public LogLevel MinLevel { get; set; }
    public string CurrentPath => Path.Combine(_dir, _baseName + ".log");

    public static LogLevel ParseLevel(string? s) => (s ?? "").ToLowerInvariant() switch
    {
        "debug" => LogLevel.Debug,
        "warn" or "warning" => LogLevel.Warn,
        "error" => LogLevel.Error,
        _ => LogLevel.Info,
    };

    public void Debug(string component, string msg) => Write(LogLevel.Debug, component, msg);
    public void Info(string component, string msg) => Write(LogLevel.Info, component, msg);
    public void Warn(string component, string msg) => Write(LogLevel.Warn, component, msg);
    public void Error(string component, string msg, Exception? ex = null) =>
        Write(LogLevel.Error, component, ex is null ? msg : $"{msg} | {ex.GetType().Name}: {ex.Message}");

    public void Write(LogLevel level, string component, string message)
    {
        if (level < MinLevel) return;
        var text = LogRedactor.Redact(message).Replace('\r', ' ').Replace('\n', ' ');
        if (text.Length > 4000) text = text[..4000] + "…";
        var line = $"{DateTimeOffset.UtcNow:yyyy-MM-ddTHH:mm:ss.fffZ} [{level.ToString().ToUpperInvariant()}] [{component}] {text}{Environment.NewLine}";
        var bytes = Encoding.UTF8.GetBytes(line);
        lock (_lock)
        {
            try
            {
                EnsureOpen();
                if (_size + bytes.Length > _maxBytes)
                {
                    Rotate();
                    EnsureOpen();
                }
                _stream!.Write(bytes, 0, bytes.Length);
                _stream.Flush();
                _size += bytes.Length;
            }
            catch (IOException)
            {
                // log nunca derruba o player
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }

    private void EnsureOpen()
    {
        if (_stream is not null) return;
        _stream = new FileStream(CurrentPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
        _size = _stream.Length;
    }

    private void Rotate()
    {
        _stream?.Dispose();
        _stream = null;
        for (int i = _maxFiles - 1; i >= 1; i--)
        {
            var src = Path.Combine(_dir, $"{_baseName}.{i}.log");
            var dst = Path.Combine(_dir, $"{_baseName}.{i + 1}.log");
            if (File.Exists(src))
            {
                if (i + 1 > _maxFiles - 1) File.Delete(src);
                else File.Move(src, dst, overwrite: true);
            }
        }
        if (_maxFiles > 1) File.Move(CurrentPath, Path.Combine(_dir, $"{_baseName}.1.log"), overwrite: true);
        else File.Delete(CurrentPath);
    }

    public void Dispose()
    {
        lock (_lock)
        {
            _stream?.Dispose();
            _stream = null;
        }
    }
}
