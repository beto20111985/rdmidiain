using System.Diagnostics;
using System.Security;
using System.Text;

namespace RdPlayer.Core.Platform;

/// <summary>
/// Autostart configurável (ETAPA2 #221), desligado por padrão.
/// Preferido: tarefa agendada "ao fazer logon" do usuário atual (sem elevação), com reinício em falha.
/// Alternativa: valor em HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
/// </summary>
public static class Autostart
{
    public const string TaskName = "RdMidiaIndoor Player";
    public const string RunValueName = "RdMidiaIndoorPlayer";
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";

    public static string BuildTaskXml(string exePath, string userId, string arguments = "--autostart")
    {
        static string X(string s) => SecurityElement.Escape(s) ?? "";
        var workDir = Path.GetDirectoryName(exePath) ?? "";
        var sb = new StringBuilder();
        sb.AppendLine("<?xml version=\"1.0\" encoding=\"UTF-16\"?>");
        sb.AppendLine("<Task version=\"1.2\" xmlns=\"http://schemas.microsoft.com/windows/2004/02/mit/task\">");
        sb.AppendLine("  <RegistrationInfo>");
        sb.AppendLine("    <Author>RD Midia Indoor</Author>");
        sb.AppendLine("    <Description>Inicia o RD Midia Indoor Player (watchdog + player) ao fazer logon.</Description>");
        sb.AppendLine("  </RegistrationInfo>");
        sb.AppendLine("  <Triggers>");
        sb.AppendLine("    <LogonTrigger>");
        sb.AppendLine("      <Enabled>true</Enabled>");
        sb.AppendLine($"      <UserId>{X(userId)}</UserId>");
        sb.AppendLine("      <Delay>PT10S</Delay>");
        sb.AppendLine("    </LogonTrigger>");
        sb.AppendLine("  </Triggers>");
        sb.AppendLine("  <Principals>");
        sb.AppendLine("    <Principal id=\"Author\">");
        sb.AppendLine($"      <UserId>{X(userId)}</UserId>");
        sb.AppendLine("      <LogonType>InteractiveToken</LogonType>");
        sb.AppendLine("      <RunLevel>LeastPrivilege</RunLevel>");
        sb.AppendLine("    </Principal>");
        sb.AppendLine("  </Principals>");
        sb.AppendLine("  <Settings>");
        sb.AppendLine("    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>");
        sb.AppendLine("    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>");
        sb.AppendLine("    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>");
        sb.AppendLine("    <AllowHardTerminate>true</AllowHardTerminate>");
        sb.AppendLine("    <StartWhenAvailable>true</StartWhenAvailable>");
        sb.AppendLine("    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>");
        sb.AppendLine("    <IdleSettings>");
        sb.AppendLine("      <StopOnIdleEnd>false</StopOnIdleEnd>");
        sb.AppendLine("      <RestartOnIdle>false</RestartOnIdle>");
        sb.AppendLine("    </IdleSettings>");
        sb.AppendLine("    <AllowStartOnDemand>true</AllowStartOnDemand>");
        sb.AppendLine("    <Enabled>true</Enabled>");
        sb.AppendLine("    <Hidden>false</Hidden>");
        sb.AppendLine("    <RunOnlyIfIdle>false</RunOnlyIfIdle>");
        sb.AppendLine("    <WakeToRun>false</WakeToRun>");
        sb.AppendLine("    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>");
        sb.AppendLine("    <Priority>4</Priority>");
        sb.AppendLine("    <RestartOnFailure>");
        sb.AppendLine("      <Interval>PT1M</Interval>");
        sb.AppendLine("      <Count>999</Count>");
        sb.AppendLine("    </RestartOnFailure>");
        sb.AppendLine("  </Settings>");
        sb.AppendLine("  <Actions Context=\"Author\">");
        sb.AppendLine("    <Exec>");
        sb.AppendLine($"      <Command>{X(exePath)}</Command>");
        sb.AppendLine($"      <Arguments>{X(arguments)}</Arguments>");
        sb.AppendLine($"      <WorkingDirectory>{X(workDir)}</WorkingDirectory>");
        sb.AppendLine("    </Exec>");
        sb.AppendLine("  </Actions>");
        sb.AppendLine("</Task>");
        return sb.ToString();
    }

    public static string RunValue(string exePath) => $"\"{exePath}\" --autostart";

    public static string CurrentUserId() =>
        string.IsNullOrEmpty(Environment.UserDomainName) ? Environment.UserName : $"{Environment.UserDomainName}\\{Environment.UserName}";

    // ---------- operações reais (somente Windows) ----------

    public static (bool Ok, string Detail) Enable(string method, string exePath, string tempDir)
    {
        if (!OperatingSystem.IsWindows()) return (false, "somente Windows");
        if (method == "run")
        {
            using var k = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(RunKey, writable: true);
            k.SetValue(RunValueName, RunValue(exePath));
            return (true, "HKCU Run definido");
        }
        Directory.CreateDirectory(tempDir);
        var xmlPath = Path.Combine(tempDir, "rd-autostart-task.xml");
        File.WriteAllText(xmlPath, BuildTaskXml(exePath, CurrentUserId()), Encoding.Unicode);
        try
        {
            var (code, output) = RunSchtasks($"/Create /TN \"{TaskName}\" /XML \"{xmlPath}\" /F");
            return (code == 0, code == 0 ? "tarefa agendada criada" : $"schtasks falhou ({code}): {output.Trim()}");
        }
        finally
        {
            try { File.Delete(xmlPath); } catch { /* melhor esforço */ }
        }
    }

    public static (bool Ok, string Detail) Disable()
    {
        if (!OperatingSystem.IsWindows()) return (false, "somente Windows");
        var details = new List<string>();
        bool ok = true;
        if (IsTaskPresent())
        {
            var (code, output) = RunSchtasks($"/Delete /TN \"{TaskName}\" /F");
            ok &= code == 0;
            details.Add(code == 0 ? "tarefa removida" : $"schtasks /Delete falhou ({code}): {output.Trim()}");
        }
        using (var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey, writable: true))
        {
            if (k?.GetValue(RunValueName) is not null)
            {
                k.DeleteValue(RunValueName, throwOnMissingValue: false);
                details.Add("HKCU Run removido");
            }
        }
        return (ok, details.Count == 0 ? "autostart já estava desligado" : string.Join("; ", details));
    }

    public static bool IsEnabled() => OperatingSystem.IsWindows() && (IsTaskPresent() || IsRunPresent());

    public static bool IsRunPresent()
    {
        if (!OperatingSystem.IsWindows()) return false;
        using var k = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey, writable: false);
        return k?.GetValue(RunValueName) is not null;
    }

    public static bool IsTaskPresent()
    {
        if (!OperatingSystem.IsWindows()) return false;
        var (code, _) = RunSchtasks($"/Query /TN \"{TaskName}\"");
        return code == 0;
    }

    private static (int Code, string Output) RunSchtasks(string args)
    {
        var psi = new ProcessStartInfo("schtasks.exe", args)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        using var p = Process.Start(psi)!;
        var outTask = p.StandardOutput.ReadToEndAsync();
        var errTask = p.StandardError.ReadToEndAsync();
        if (!p.WaitForExit(15_000))
        {
            try { p.Kill(); } catch { /* ignora */ }
            return (-1, "timeout");
        }
        return (p.ExitCode, outTask.Result + errTask.Result);
    }
}
