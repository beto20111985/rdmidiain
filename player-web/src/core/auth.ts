/**
 * Autenticação do dispositivo (RDPP §3): desafio de uso único assinado com a chave ES256 não exportável →
 * JWT de dispositivo de 15 min. Renovação antecipada (margem de 60 s) e "single flight".
 * 401 DEVICE_REVOKED ⇒ callback (o app limpa credenciais, PARA a reprodução e volta ao pareamento).
 */

import { ApiError, type ApiClient, type TokenSource } from './api';
import { signCanonical, type Signer } from './crypto';
import { PROTOCOL_PATHS, PURPOSE_AUTH, type ChallengeResponse, type TokenResponse } from './protocol';
import { realMono, type MonoClock } from './util';

export interface DeviceAuthDeps {
  /** Cliente SEM auth (challenge/token não usam Bearer). */
  api: ApiClient;
  signer: () => Signer | null;
  deviceId: () => string | null;
  mono?: MonoClock;
  onRevoked?: () => void;
  /** Observa server_time_ms do token. */
  onServerTime?: (serverMs: number) => void;
}

export class DeviceAuth implements TokenSource {
  private cached: { token: string; expiresAtMono: number } | null = null;
  private inflight: Promise<string> | null = null;
  private readonly mono: MonoClock;
  static readonly RENEW_MARGIN_MS = 60_000;

  constructor(private readonly deps: DeviceAuthDeps) {
    this.mono = deps.mono ?? realMono;
  }

  invalidate(): void {
    this.cached = null;
  }

  get hasToken(): boolean {
    return this.cached !== null && this.cached.expiresAtMono - DeviceAuth.RENEW_MARGIN_MS > this.mono();
  }

  token(): Promise<string> {
    if (this.cached && this.cached.expiresAtMono - DeviceAuth.RENEW_MARGIN_MS > this.mono()) {
      return Promise.resolve(this.cached.token);
    }
    if (!this.inflight) {
      this.inflight = this.fetchToken().finally(() => {
        this.inflight = null;
      });
    }
    return this.inflight;
  }

  private async fetchToken(): Promise<string> {
    const deviceId = this.deps.deviceId();
    const signer = this.deps.signer();
    if (!deviceId || !signer) throw new ApiError(0, 'NOT_PAIRED', 'dispositivo não pareado');
    try {
      const ch = await this.deps.api.post<ChallengeResponse>(PROTOCOL_PATHS.authChallenge, { device_id: deviceId }, { auth: false });
      if (!ch.data?.challenge) throw new ApiError(ch.status, 'BAD_CHALLENGE', 'desafio ausente');
      const signature = await signCanonical(signer, {
        challenge: ch.data.challenge,
        device_id: deviceId,
        purpose: PURPOSE_AUTH,
      });
      const started = this.mono();
      const tk = await this.deps.api.post<TokenResponse>(
        PROTOCOL_PATHS.authToken,
        { device_id: deviceId, challenge: ch.data.challenge, signature },
        { auth: false },
      );
      if (!tk.data?.access_token) throw new ApiError(tk.status, 'BAD_TOKEN', 'token ausente');
      if (Number.isFinite(tk.data.server_time_ms)) this.deps.onServerTime?.(tk.data.server_time_ms);
      const ttl = Math.max(30, Number(tk.data.expires_in) || 900) * 1000;
      this.cached = { token: tk.data.access_token, expiresAtMono: started + ttl };
      return tk.data.access_token;
    } catch (e) {
      if (e instanceof ApiError && (e.isRevoked || (e.status === 401 && e.code === 'DEVICE_REVOKED'))) {
        this.cached = null;
        this.deps.onRevoked?.();
      }
      throw e;
    }
  }
}
