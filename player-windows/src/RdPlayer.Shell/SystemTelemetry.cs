using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32;
using RdPlayer.Core;
using RdPlayer.Core.Telemetry;

namespace RdPlayer.Shell;

/// <summary>Coleta de telemetria do sistema (RDPP/1 §4 telemetry.data) — CPU, RAM, disco, uptime, Windows, tela, Wi-Fi. Sem IP.</summary>
internal sealed class SystemTelemetry
{
    private readonly object _cpuLock = new();
    private long _prevIdle, _prevKernel, _prevUser;
    private bool _hasPrev;
    private string? _osCache;
    private WifiInfo? _wifiCache;
    private long _wifiCacheAt = long.MinValue;
    private readonly Action<string>? _debug;

    public SystemTelemetry(Action<string>? debug = null) => _debug = debug;

    public TelemetrySnapshot Collect(int screenWidth, int screenHeight, string dataRoot, bool includeWifi)
    {
        var (ramUsed, ramTotal) = Memory();
        var (diskFree, diskTotal) = Disk(dataRoot);
        return new TelemetrySnapshot(
            CpuPct: Cpu(),
            RamUsedMb: ramUsed,
            RamTotalMb: ramTotal,
            DiskFreeMb: diskFree,
            DiskTotalMb: diskTotal,
            UptimeS: Environment.TickCount64 / 1000,
            Os: Os(),
            AppVersion: AppInfo.Version,
            Resolution: TelemetryMath.Resolution(screenWidth, screenHeight),
            Orientation: TelemetryMath.Orientation(screenWidth, screenHeight),
            Wifi: includeWifi ? Wifi() : null);
    }

    public int? Cpu()
    {
        lock (_cpuLock)
        {
            if (!Native.GetSystemTimes(out var idle, out var kernel, out var user)) return null;
            if (!_hasPrev)
            {
                _prevIdle = idle; _prevKernel = kernel; _prevUser = user; _hasPrev = true;
                Thread.Sleep(250);
                if (!Native.GetSystemTimes(out idle, out kernel, out user)) return null;
            }
            var pct = TelemetryMath.CpuPercent(idle - _prevIdle, kernel - _prevKernel, user - _prevUser);
            _prevIdle = idle; _prevKernel = kernel; _prevUser = user;
            return pct;
        }
    }

    private static (long? Used, long? Total) Memory()
    {
        var m = new Native.MEMORYSTATUSEX { dwLength = (uint)Marshal.SizeOf<Native.MEMORYSTATUSEX>() };
        if (!Native.GlobalMemoryStatusEx(ref m)) return (null, null);
        long total = (long)(m.ullTotalPhys / (1024 * 1024));
        long avail = (long)(m.ullAvailPhys / (1024 * 1024));
        return (total - avail, total);
    }

    private static (long? Free, long? Total) Disk(string path)
    {
        try
        {
            var root = Path.GetPathRoot(Path.GetFullPath(path));
            if (string.IsNullOrEmpty(root)) return (null, null);
            var d = new DriveInfo(root);
            return (d.AvailableFreeSpace / (1024 * 1024), d.TotalSize / (1024 * 1024));
        }
        catch (Exception)
        {
            return (null, null);
        }
    }

    public string Os()
    {
        if (_osCache is not null) return _osCache;
        try
        {
            using var k = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
            var product = k?.GetValue("ProductName") as string;
            var display = (k?.GetValue("DisplayVersion") as string) ?? (k?.GetValue("ReleaseId") as string);
            var build = k?.GetValue("CurrentBuild") as string ?? k?.GetValue("CurrentBuildNumber") as string;
            int? ubr = k?.GetValue("UBR") is int u ? u : null;
            _osCache = TelemetryMath.FormatWindowsVersion(product, display, build, ubr);
        }
        catch (Exception)
        {
            _osCache = RuntimeInformation.OSDescription;
        }
        return _osCache;
    }

    /// <summary>SSID/sinal: WLAN API nativa; se indisponível, "netsh wlan show interfaces". Cache de 30 s.</summary>
    public WifiInfo? Wifi()
    {
        long now = Environment.TickCount64;
        if (now - _wifiCacheAt < 30_000) return _wifiCache;
        WifiInfo? w = null;
        try
        {
            w = WlanQuery();
        }
        catch (Exception ex) when (ex is DllNotFoundException || ex is EntryPointNotFoundException || ex is ExternalException)
        {
            _debug?.Invoke("WLAN API indisponível: " + ex.GetType().Name);
        }
        if (w is null)
        {
            try
            {
                w = NetshWlanParser.Parse(RunNetsh());
            }
            catch (Exception ex)
            {
                _debug?.Invoke("netsh falhou: " + ex.GetType().Name);
            }
        }
        _wifiCache = w;
        _wifiCacheAt = now;
        return w;
    }

    private static WifiInfo? WlanQuery()
    {
        int rc = Native.WlanOpenHandle(2, IntPtr.Zero, out _, out var handle);
        if (rc != 0) return null; // ex.: serviço WLAN AutoConfig parado (desktop cabeado)
        IntPtr list = IntPtr.Zero;
        try
        {
            if (Native.WlanEnumInterfaces(handle, IntPtr.Zero, out list) != 0) return null;
            int count = Marshal.ReadInt32(list, 0);
            const int itemSize = 16 + 512 + 4; // GUID + WCHAR[256] + WLAN_INTERFACE_STATE
            for (int i = 0; i < count; i++)
            {
                var item = list + 8 + i * itemSize;
                var guid = Marshal.PtrToStructure<Guid>(item);
                int state = Marshal.ReadInt32(item, 16 + 512);
                if (state != Native.WLAN_INTERFACE_STATE_CONNECTED) continue;

                if (Native.WlanQueryInterface(handle, ref guid, Native.WLAN_INTF_OPCODE_CURRENT_CONNECTION, IntPtr.Zero, out _, out var conn, IntPtr.Zero) != 0)
                    continue;
                try
                {
                    // WLAN_CONNECTION_ATTRIBUTES: isState(4) mode(4) strProfileName(512) | DOT11_SSID @520 (len 4 + 32 bytes)
                    // dot11BssType @556, bssid @560 (6), phyType @568, phyIndex @572, wlanSignalQuality @576
                    int ssidLen = Math.Clamp(Marshal.ReadInt32(conn, 520), 0, 32);
                    var ssidBytes = new byte[ssidLen];
                    Marshal.Copy(conn + 524, ssidBytes, 0, ssidLen);
                    int quality = Marshal.ReadInt32(conn, 576);
                    string ssid = DecodeSsid(ssidBytes);
                    int? rssi = null;
                    if (Native.WlanQueryInterface(handle, ref guid, Native.WLAN_INTF_OPCODE_RSSI, IntPtr.Zero, out var sz, out var rssiPtr, IntPtr.Zero) == 0)
                    {
                        try
                        {
                            if (sz >= 4)
                            {
                                int v = Marshal.ReadInt32(rssiPtr);
                                if (v is < 0 and > -130) rssi = v;
                            }
                        }
                        finally
                        {
                            Native.WlanFreeMemory(rssiPtr);
                        }
                    }
                    rssi ??= TelemetryMath.QualityToDbm(quality);
                    return new WifiInfo(ssid, Math.Clamp(quality, 0, 100), rssi);
                }
                finally
                {
                    Native.WlanFreeMemory(conn);
                }
            }
            return null;
        }
        finally
        {
            if (list != IntPtr.Zero) Native.WlanFreeMemory(list);
            Native.WlanCloseHandle(handle, IntPtr.Zero);
        }
    }

    private static string DecodeSsid(byte[] bytes)
    {
        try
        {
            return new UTF8Encoding(false, true).GetString(bytes);
        }
        catch (DecoderFallbackException)
        {
            return Encoding.Latin1.GetString(bytes);
        }
    }

    private static string RunNetsh()
    {
        var psi = new ProcessStartInfo("netsh.exe", "wlan show interfaces")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.GetEncoding(System.Globalization.CultureInfo.CurrentCulture.TextInfo.OEMCodePage),
        };
        using var p = Process.Start(psi)!;
        var output = p.StandardOutput.ReadToEndAsync();
        if (!p.WaitForExit(5_000))
        {
            try { p.Kill(); } catch { /* ignora */ }
            return "";
        }
        return output.Result;
    }
}
