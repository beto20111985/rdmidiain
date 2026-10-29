namespace RdPlayer.Core.Update;

/// <summary>
/// Transições de estado da atualização executadas pelo watchdog/updater (puras de UI, testáveis em Linux):
/// pendente → promovida (probação) → aprovada (COMMITTED) | revertida (ROLLED_BACK, versão na lista negra).
/// O controle de processos (encerrar a casca antes de trocar os slots) fica com o chamador.
/// </summary>
public sealed class UpdateApplier
{
    private readonly UpdateSlots _slots;
    private readonly string _statePath;
    private readonly Func<long> _unixNowMs;

    public UpdateApplier(UpdateSlots slots, string statePath, Func<long>? unixNowMs = null)
    {
        _slots = slots;
        _statePath = statePath;
        _unixNowMs = unixNowMs ?? (() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
    }

    public UpdateState Load() => StateStore.LoadUpdateState(_statePath);

    public sealed record ApplyResult(bool Ok, string Detail, string? Version, string? PreviousVersion);

    /// <summary>Promove o pacote pendente para app\current e inicia a probação.</summary>
    public ApplyResult ApplyPending()
    {
        var st = Load();
        var p = st.Pending;
        if (p is null) return new(false, "NO_PENDING", null, null);
        st.Pending = null;
        try
        {
            if (!_slots.IsSlotted) throw new InvalidOperationException("layout portátil não suporta atualização (instale com install.ps1)");
            var staged = Path.GetFullPath(p.StagedDir);
            var stagingRoot = Path.GetFullPath(_slots.StagingRoot) + Path.DirectorySeparatorChar;
            var cmp = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
            if (!staged.StartsWith(stagingRoot, cmp)) throw new InvalidOperationException("staging fora de app/staging: " + staged);
            var problem = UpdateSlots.ValidatePackageDir(staged, p.Version);
            if (problem is not null) throw new InvalidDataException(problem);

            var previous = UpdateSlots.ReadVersion(_slots.CurrentDir);
            _slots.Promote(staged);
            st.Probation = new ProbationInfo { Version = p.Version, PreviousVersion = previous, StartedMs = _unixNowMs(), Crashes = 0 };
            StateStore.SaveUpdateState(_statePath, st);
            return new(true, "PROMOTED", p.Version, previous);
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is InvalidOperationException || ex is InvalidDataException || ex is ArgumentException)
        {
            st.LastResult = new UpdateResultInfo { Version = p.Version, Result = "FAILED", Detail = ex.Message, AtMs = _unixNowMs() };
            if (!st.BadVersions.Contains(p.Version)) st.BadVersions.Add(p.Version);
            StateStore.SaveUpdateState(_statePath, st);
            try { if (Directory.Exists(p.StagedDir)) Directory.Delete(p.StagedDir, true); } catch (Exception) { /* melhor esforço */ }
            return new(false, ex.Message, p.Version, null);
        }
    }

    /// <summary>Registra uma queda durante a probação; retorna o total (0 se não há probação).</summary>
    public int RegisterProbationCrash()
    {
        var st = Load();
        if (st.Probation is null) return 0;
        st.Probation.Crashes++;
        StateStore.SaveUpdateState(_statePath, st);
        return st.Probation.Crashes;
    }

    /// <summary>A nova versão ficou saudável: encerra a probação (a anterior continua em app\previous).</summary>
    public string Commit()
    {
        var st = Load();
        var version = st.Probation?.Version ?? UpdateSlots.ReadVersion(_slots.CurrentDir) ?? "?";
        st.Probation = null;
        st.LastResult = new UpdateResultInfo { Version = version, Result = "COMMITTED", Detail = "nova versão saudável", AtMs = _unixNowMs() };
        StateStore.SaveUpdateState(_statePath, st);
        _slots.CleanStaging();
        return version;
    }

    /// <summary>Volta para app\previous e coloca a versão na lista negra (não será baixada de novo).</summary>
    public (bool RolledBack, string Version) Rollback(string why)
    {
        var st = Load();
        var version = st.Probation?.Version ?? UpdateSlots.ReadVersion(_slots.CurrentDir) ?? "?";
        bool ok;
        try
        {
            ok = _slots.Rollback();
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            ok = false;
            why += " | rollback falhou: " + ex.Message;
        }
        st.Probation = null;
        if (!st.BadVersions.Contains(version)) st.BadVersions.Add(version);
        st.LastResult = new UpdateResultInfo { Version = version, Result = ok ? "ROLLED_BACK" : "FAILED", Detail = why, AtMs = _unixNowMs() };
        StateStore.SaveUpdateState(_statePath, st);
        return (ok, version);
    }
}
