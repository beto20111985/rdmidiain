using System.Text;

namespace RdPlayer.Core.IO;

public static class FileUtil
{
    /// <summary>Escrita atômica: grava em arquivo temporário no mesmo diretório e troca por rename.</summary>
    public static void AtomicWriteAllText(string path, string content)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(path))!;
        Directory.CreateDirectory(dir);
        var tmp = Path.Combine(dir, "." + Path.GetFileName(path) + "." + Guid.NewGuid().ToString("N") + ".tmp");
        using (var fs = new FileStream(tmp, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            var bytes = new UTF8Encoding(false).GetBytes(content);
            fs.Write(bytes, 0, bytes.Length);
            fs.Flush(true);
        }
        File.Move(tmp, path, overwrite: true);
    }

    /// <summary>Executa uma operação de arquivo com novas tentativas (antivírus/indexador podem segurar handles no Windows).</summary>
    public static void Retry(Action action, int attempts = 10, int delayMs = 500)
    {
        for (int i = 1; ; i++)
        {
            try
            {
                action();
                return;
            }
            catch (Exception ex) when (i < attempts && (ex is IOException || ex is UnauthorizedAccessException))
            {
                Thread.Sleep(delayMs);
            }
        }
    }

    public static void DeleteDirectoryQuiet(string dir)
    {
        try
        {
            if (Directory.Exists(dir)) Retry(() => Directory.Delete(dir, recursive: true), attempts: 5, delayMs: 300);
        }
        catch
        {
            // melhor esforço
        }
    }
}
