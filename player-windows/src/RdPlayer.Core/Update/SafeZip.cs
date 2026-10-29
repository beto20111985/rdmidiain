using System.IO.Compression;

namespace RdPlayer.Core.Update;

/// <summary>Extração de zip com proteção contra zip-slip, caminhos absolutos e bomba de descompressão.</summary>
public static class SafeZip
{
    public static void ExtractToDirectory(string zipPath, string destDir, long maxTotalBytes = 2L * 1024 * 1024 * 1024, int maxEntries = 20000)
    {
        var cmp = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        var root = Path.GetFullPath(destDir);
        if (!root.EndsWith(Path.DirectorySeparatorChar)) root += Path.DirectorySeparatorChar;
        Directory.CreateDirectory(root);

        using var za = ZipFile.OpenRead(zipPath);
        if (za.Entries.Count > maxEntries) throw new InvalidDataException("pacote com entradas demais");
        long total = 0;
        var buf = new byte[81920];
        foreach (var entry in za.Entries)
        {
            var name = entry.FullName.Replace('\\', '/');
            if (name.Length == 0 || name.StartsWith('/') || name.Contains(':') || name.Contains('\0'))
                throw new InvalidDataException("entrada insegura no pacote: " + entry.FullName);
            var segs = name.Split('/');
            if (segs.Any(s => s == ".." || s == "."))
                throw new InvalidDataException("entrada insegura no pacote: " + entry.FullName);

            var target = Path.GetFullPath(Path.Combine(root, name.Replace('/', Path.DirectorySeparatorChar)));
            if (!target.StartsWith(root, cmp))
                throw new InvalidDataException("entrada fora do destino: " + entry.FullName);

            if (name.EndsWith('/'))
            {
                Directory.CreateDirectory(target);
                continue;
            }

            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            long declared = entry.Length;
            long written = 0;
            using (var src = entry.Open())
            using (var dst = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                int n;
                while ((n = src.Read(buf, 0, buf.Length)) > 0)
                {
                    written += n;
                    total += n;
                    if (written > declared) throw new InvalidDataException("entrada maior que o declarado: " + entry.FullName);
                    if (total > maxTotalBytes) throw new InvalidDataException("pacote excede o limite descompactado");
                    dst.Write(buf, 0, n);
                }
            }
        }
    }
}
