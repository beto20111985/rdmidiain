using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using RdPlayer.Core;
using RdPlayer.Core.Bridge;
using RdPlayer.Core.Update;

namespace RdPlayer.Shell;

/// <summary>
/// Atualização do app (RDPP/1 §8): consulta app-update, verifica assinatura ES256 (chave de release fixada no config),
/// baixa com retomada e SHA-256 incremental, extrai com proteção zip-slip para app\staging\&lt;versão&gt; e entrega ao
/// watchdog/updater a troca de slots (com rollback automático se a nova versão não subir).
/// </summary>
internal sealed class UpdateManager
{
    private static readonly HttpClient Http = new(new SocketsHttpHandler
    {
        ConnectTimeout = TimeSpan.FromSeconds(15),
        AutomaticDecompression = DecompressionMethods.None,
        AllowAutoRedirect = true,
        MaxAutomaticRedirections = 3,
    })
    { Timeout = Timeout.InfiniteTimeSpan };

    private readonly ShellContext _ctx;
    private readonly Action<string, JsonObject> _emit;
    private readonly Action<string, string> _requestApply;
    private int _busy;

    public UpdateManager(ShellContext ctx, Action<string, JsonObject> emitEvent, Action<string, string> requestApply)
    {
        _ctx = ctx;
        _emit = emitEvent;
        _requestApply = requestApply;
    }

    private static JsonObject R(string status, string detail, string? version = null)
    {
        var o = new JsonObject { ["status"] = status, ["detail"] = detail };
        if (version is not null) o["version"] = version;
        return o;
    }

    /// <summary>
    /// Fase 1 (rápida, responde à ponte em poucos segundos): obtém/valida o anúncio (assinatura, versão, tamanho).
    /// Fase 2 (em segundo plano): download, verificação, staging e aplicação — progresso pelo evento update_status.
    /// Assim o request/command da ponte não estoura o timeout do bundle durante downloads longos.
    /// </summary>
    public async Task<JsonObject> RunAsync(JsonObject prm, CancellationToken ct)
    {
        var cfg = _ctx.Config.Update;
        if (_ctx.Smoke) return R("REJECTED", "SMOKE_TEST");
        if (!cfg.Enabled) return R("REJECTED", "UPDATE_DISABLED");
        var root = _ctx.InstallRoot;
        if (root is null) return R("FAILED", "LAYOUT_NOT_UPDATABLE: execute a partir da instalação (app/current)");
        if (Interlocked.Exchange(ref _busy, 1) == 1) return R("REJECTED", "UPDATE_IN_PROGRESS");
        bool background = false;
        try
        {
            var mode = BridgeProtocol.Str(prm, "mode") ?? "apply";
            var token = BridgeProtocol.Str(prm, "access_token");

            AppUpdateInfo? info;
            if (prm["update"] is JsonObject u)
            {
                if (!AppUpdateInfo.TryParse(u.ToJsonString(), out info, out var err)) return R("REJECTED", "BAD_UPDATE_INFO: " + err);
            }
            else
            {
                var (status, fetched, detail) = await FetchInfoAsync(token, ct).ConfigureAwait(true);
                if (status == "NO_UPDATE") return R("NO_UPDATE", detail);
                if (fetched is null) return R(status, detail);
                info = fetched;
            }

            var verifier = new UpdateVerifier(cfg.PublicKeys.Select(k => new UpdatePublicKey(k.KeyId, k.PublicKey)));
            var state = StateStore.LoadUpdateState(_ctx.Paths.UpdateStatePath);
            var verdict = verifier.Evaluate(info!, AppInfo.Version, cfg.AllowDowngrade, state.BadVersions);
            _ctx.Log.Info("update", $"pacote anunciado {info!.Version} ({info.Size} bytes): {verdict}");
            switch (verdict)
            {
                case UpdateVerdict.Ok: break;
                case UpdateVerdict.SameVersion: return R("NO_UPDATE", "SAME_VERSION", info.Version);
                case UpdateVerdict.KeysNotConfigured: return R("REJECTED", "UPDATE_KEYS_NOT_CONFIGURED", info.Version);
                case UpdateVerdict.UnknownKey:
                case UpdateVerdict.SignatureInvalid: return R("REJECTED", "SIGNATURE_INVALID", info.Version);
                case UpdateVerdict.Downgrade: return R("REJECTED", "DOWNGRADE", info.Version);
                case UpdateVerdict.BlacklistedVersion: return R("REJECTED", "BLACKLISTED_VERSION", info.Version);
                default: return R("REJECTED", verdict.ToString(), info.Version);
            }
            if (info.Size > (long)cfg.MaxPackageMb * 1024 * 1024) return R("REJECTED", "PACKAGE_TOO_LARGE", info.Version);
            if (mode == "check")
            {
                var avail = R("AVAILABLE", "OK", info.Version);
                avail["mandatory"] = info.Mandatory;
                return avail;
            }

            bool applyNow = cfg.AutoApply || info.Mandatory || mode == "apply_now";
            background = true;
            _ = DownloadAndApplyAsync(root, info, token, applyNow, ct); // continua na thread de UI entre os awaits
            return R("ACCEPTED", "download em segundo plano; acompanhe o evento update_status", info.Version);
        }
        catch (HttpRequestException ex)
        {
            _ctx.Log.Warn("update", "falha de rede: " + ex.Message);
            return R("FAILED", "NETWORK");
        }
        catch (OperationCanceledException)
        {
            return R("FAILED", "TIMEOUT");
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("update", "falha inesperada", ex);
            return R("FAILED", "INTERNAL");
        }
        finally
        {
            if (!background) Interlocked.Exchange(ref _busy, 0);
        }
    }

    private void Status(string state, string version, string detail) =>
        _emit("update_status", new JsonObject { ["state"] = state, ["version"] = version, ["detail"] = detail });

    private async Task DownloadAndApplyAsync(string root, AppUpdateInfo info, string? token, bool applyNow, CancellationToken ct)
    {
        try
        {
            Status("DOWNLOADING", info.Version, "");
            var zip = await DownloadAsync(info, token, ct).ConfigureAwait(true);
            if (zip is null)
            {
                Status("FAILED", info.Version, "HASH_MISMATCH");
                return;
            }

            Status("VERIFYING", info.Version, "");
            var slots = new UpdateSlots(root);
            var staged = slots.StagingDirFor(info.Version);
            await Task.Run(() =>
            {
                if (Directory.Exists(staged)) Directory.Delete(staged, true);
                SafeZip.ExtractToDirectory(zip, staged);
            }, ct).ConfigureAwait(true);
            var problem = UpdateSlots.ValidatePackageDir(staged, info.Version);
            if (problem is not null)
            {
                try { Directory.Delete(staged, true); } catch { /* melhor esforço */ }
                Status("FAILED", info.Version, "BAD_PACKAGE: " + problem);
                return;
            }

            var state = StateStore.LoadUpdateState(_ctx.Paths.UpdateStatePath);
            state.Pending = new PendingUpdate { Version = info.Version, StagedDir = staged, Sha256 = info.Sha256, CreatedMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() };
            StateStore.SaveUpdateState(_ctx.Paths.UpdateStatePath, state);
            try { File.Delete(zip); } catch { /* melhor esforço */ }
            Status("STAGED", info.Version, applyNow ? "" : "aplicação na próxima reinicialização do app");
            _ctx.Log.Info("update", $"versão {info.Version} preparada em {staged}");
            if (!applyNow) return;

            Status("APPLYING", info.Version, "o app será reiniciado na nova versão");
            _requestApply(info.Version, staged);
        }
        catch (HttpRequestException ex)
        {
            _ctx.Log.Warn("update", "falha de rede no download: " + ex.Message);
            Status("FAILED", info.Version, "NETWORK");
        }
        catch (InvalidDataException ex)
        {
            _ctx.Log.Warn("update", "pacote inválido: " + ex.Message);
            Status("FAILED", info.Version, "BAD_PACKAGE");
        }
        catch (OperationCanceledException)
        {
            Status("FAILED", info.Version, "CANCELLED");
        }
        catch (Exception ex)
        {
            _ctx.Log.Error("update", "falha inesperada no download/aplicação", ex);
            Status("FAILED", info.Version, "INTERNAL");
        }
        finally
        {
            Interlocked.Exchange(ref _busy, 0);
        }
    }

    private Uri? ApiBase => Uri.TryCreate(_ctx.Config.ApiBaseUrl, UriKind.Absolute, out var u) ? u : null;

    private async Task<(string Status, AppUpdateInfo? Info, string Detail)> FetchInfoAsync(string? token, CancellationToken ct)
    {
        var api = ApiBase;
        if (api is null) return ("FAILED", null, "CONFIG: api_base_url ausente");
        var url = new Uri(api, $"{api.AbsolutePath.TrimEnd('/')}/api/v1/player/app-update?platform=windows&version={Uri.EscapeDataString(AppInfo.Version)}");
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        if (!string.IsNullOrEmpty(token)) req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(8)); // responde à ponte antes do timeout de 10 s do bundle
        using var resp = await Http.SendAsync(req, cts.Token).ConfigureAwait(false);
        if (resp.StatusCode == HttpStatusCode.NoContent) return ("NO_UPDATE", null, "NO_UPDATE");
        if (resp.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden) return ("FAILED", null, "UNAUTHORIZED");
        if (!resp.IsSuccessStatusCode) return ("FAILED", null, "HTTP_" + (int)resp.StatusCode);
        var body = await resp.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
        if (body.Length > 64 * 1024) return ("FAILED", null, "RESPONSE_TOO_LARGE");
        return AppUpdateInfo.TryParse(body, out var info, out var err) ? ("OK", info, "") : ("REJECTED", null, "BAD_UPDATE_INFO: " + err);
    }

    /// <summary>Baixa o pacote com retomada (Range) e SHA-256 incremental. Retorna o caminho do zip verificado ou null se o hash não conferir.</summary>
    private async Task<string?> DownloadAsync(AppUpdateInfo info, string? token, CancellationToken ct)
    {
        var api = ApiBase;
        Uri url = Uri.TryCreate(info.Url, UriKind.Absolute, out var abs) ? abs
            : api is not null && Uri.TryCreate(api, info.Url, out var rel) ? rel
            : throw new InvalidDataException("url do pacote inválida");
        bool insecureOk = _ctx.Config.Dev.AllowInsecureApi;
        if (url.Scheme != Uri.UriSchemeHttps && !(insecureOk && url.Scheme == Uri.UriSchemeHttp))
            throw new InvalidDataException("pacote deve ser servido por https");
        bool sameOrigin = api is not null && Uri.Compare(url, api, UriComponents.SchemeAndServer, UriFormat.Unescaped, StringComparison.OrdinalIgnoreCase) == 0;

        Directory.CreateDirectory(_ctx.Paths.UpdatesDir);
        var final = Path.Combine(_ctx.Paths.UpdatesDir, $"rdplayer-windows-{info.Version}.zip");
        var part = final + ".part";
        if (File.Exists(final))
        {
            var (ok, _, _) = await UpdateVerifier.VerifyFileAsync(final, info.Sha256, info.Size, ct).ConfigureAwait(false);
            if (ok) return final;
            File.Delete(final);
        }

        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        long have = 0;
        if (File.Exists(part))
        {
            have = new FileInfo(part).Length;
            if (have >= info.Size) { File.Delete(part); have = 0; }
        }

        for (int attempt = 1; ; attempt++)
        {
            try
            {
                hash.GetHashAndReset();
                if (have > 0)
                {
                    await using var existing = new FileStream(part, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, true);
                    var b = new byte[81920];
                    int n;
                    while ((n = await existing.ReadAsync(b, ct).ConfigureAwait(false)) > 0) hash.AppendData(b, 0, n);
                }

                using var req = new HttpRequestMessage(HttpMethod.Get, url);
                if (sameOrigin && !string.IsNullOrEmpty(token)) req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                if (have > 0) req.Headers.Range = new RangeHeaderValue(have, null);
                using var resp = await Http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
                if (have > 0 && resp.StatusCode == HttpStatusCode.OK)
                {
                    // servidor ignorou Range: recomeça do zero
                    have = 0;
                    hash.GetHashAndReset();
                }
                else if (have > 0 && resp.StatusCode == HttpStatusCode.RequestedRangeNotSatisfiable)
                {
                    File.Delete(part);
                    have = 0;
                    continue;
                }
                resp.EnsureSuccessStatusCode();

                await using (var src = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false))
                await using (var dst = new FileStream(part, have > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write, FileShare.None, 81920, true))
                {
                    var buf = new byte[81920];
                    int n;
                    long lastLog = 0;
                    while ((n = await src.ReadAsync(buf, ct).ConfigureAwait(false)) > 0)
                    {
                        have += n;
                        if (have > info.Size) throw new InvalidDataException("pacote maior que o anunciado");
                        hash.AppendData(buf, 0, n);
                        await dst.WriteAsync(buf.AsMemory(0, n), ct).ConfigureAwait(false);
                        if (have - lastLog > 8 * 1024 * 1024)
                        {
                            lastLog = have;
                            _ctx.Log.Debug("update", $"download {have}/{info.Size}");
                        }
                    }
                }
                break;
            }
            catch (HttpRequestException) when (attempt < 5)
            {
                // backoff exponencial com jitter (sem efeito metralhadora)
                var delay = TimeSpan.FromMilliseconds(Math.Min(60_000, 2_000 * Math.Pow(2, attempt - 1)) * (0.5 + Random.Shared.NextDouble()));
                _ctx.Log.Warn("update", $"download interrompido (tentativa {attempt}); retomando em {delay.TotalSeconds:F0} s");
                await Task.Delay(delay, ct).ConfigureAwait(false);
                have = File.Exists(part) ? new FileInfo(part).Length : 0;
            }
            catch (IOException) when (attempt < 5)
            {
                await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
                have = File.Exists(part) ? new FileInfo(part).Length : 0;
            }
        }

        var actual = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
        if (have != info.Size || actual != info.Sha256)
        {
            _ctx.Log.Warn("update", $"sha256/tamanho divergentes (esperado {info.Sha256}/{info.Size}, obtido {actual}/{have}); descartando");
            File.Delete(part);
            return null;
        }
        File.Move(part, final, overwrite: true);
        return final;
    }
}
