/**
 * Pareamento (RDPP §2, §8.2.5–§8.2.6):
 *  2.1 POST /pairing/requests com a chave pública SPKI (b64u) → código "XXXX-XXXX" + panel_pair_url (vira QR local).
 *  2.2 Aguarda por WS /ws/player/pairing?request_id=<id> (sem segredo; envelope padrão: pairing_claimed /
 *      pairing_expired). Sem WS: GET /pairing/requests/{id} com backoff exponencial (≥ 5 s, máx. 60 s, jitter).
 *  2.3b Técnico no próprio dispositivo: POST /pairing/requests/{id}/claim (credenciais nunca armazenadas).
 *  2.5 GET /pairing/requests/{id}/result assinado: X-RD-Device-Ts-Ms + X-RD-Device-Signature sobre RDJ1
 *      {"pairing_request_id","purpose":"rd-pairing-result-v1","ts_ms"}.
 */

import type { ApiClient } from './api';
import { signCanonical, type Signer } from './crypto';
import {
  HEADER_DEVICE_SIGNATURE,
  HEADER_DEVICE_TS,
  PROTOCOL_PATHS,
  PURPOSE_PAIRING_RESULT,
  type ClaimOnDeviceBody,
  type DeviceInfo,
  type PairingRequestResponse,
  type PairingResult,
  type PairingStatusResponse,
  type Platform,
} from './protocol';
import { b64uEncode, backoffDelay, errorMessage, realTimers, sleep, type Timers } from './util';
import { parseEnvelope, type WebSocketFactory, type WebSocketLike } from './ws';

export type ClaimWaitResult = 'CLAIMED' | 'EXPIRED';

export interface PairingDeps {
  api: ApiClient;
  signer: Signer;
  platform: Platform;
  appVersion: string;
  deviceInfo: () => DeviceInfo;
  /** Base ws(s)://host (sem barra final). */
  wsBase: string;
  wsFactory?: WebSocketFactory;
  /** Instante corrigido. */
  now: () => number;
  timers?: Timers;
  random?: () => number;
  log?: (msg: string) => void;
}

export class PairingFlow {
  private readonly timers: Timers;
  private readonly random: () => number;

  constructor(private readonly deps: PairingDeps) {
    this.timers = deps.timers ?? realTimers;
    this.random = deps.random ?? Math.random;
  }

  async createRequest(): Promise<PairingRequestResponse> {
    const publicKey = b64uEncode(await this.deps.signer.publicKeySpki());
    const res = await this.deps.api.post<PairingRequestResponse>(
      PROTOCOL_PATHS.pairingRequests,
      {
        public_key: publicKey,
        key_alg: 'ES256',
        platform: this.deps.platform,
        app_version: this.deps.appVersion,
        device_info: this.deps.deviceInfo(),
      },
      { auth: false },
    );
    if (!res.data?.pairing_request_id || !res.data.pairing_code) throw new Error('resposta de pareamento incompleta');
    return res.data;
  }

  /** URL do WS de pareamento (sem segredo). */
  pairingWsUrl(req: PairingRequestResponse): string {
    const path = req.ws_path || PROTOCOL_PATHS.wsPairing;
    if (/^wss?:\/\//.test(path)) return path.includes('request_id=') ? path : `${path}${path.includes('?') ? '&' : '?'}request_id=${encodeURIComponent(req.pairing_request_id)}`;
    const sep = path.includes('?') ? '&' : '?';
    return path.includes('request_id=')
      ? `${this.deps.wsBase}${path}`
      : `${this.deps.wsBase}${path}${sep}request_id=${encodeURIComponent(req.pairing_request_id)}`;
  }

  /** Aguarda o pedido ser reivindicado (WS; fallback para GET com backoff). */
  async waitForClaim(req: PairingRequestResponse, signal: AbortSignal): Promise<ClaimWaitResult> {
    if (this.deps.wsFactory) {
      const viaWs = await this.waitViaWs(req, signal).catch((e) => {
        this.deps.log?.(`pareamento: WS indisponível (${errorMessage(e)}); usando consulta com backoff`);
        return null;
      });
      if (viaWs) return viaWs;
    }
    return this.waitViaRetry(req, signal);
  }

  private waitViaWs(req: PairingRequestResponse, signal: AbortSignal): Promise<ClaimWaitResult | null> {
    return new Promise<ClaimWaitResult | null>((resolve, reject) => {
      let ws: WebSocketLike;
      try {
        ws = this.deps.wsFactory!(this.pairingWsUrl(req));
      } catch (e) {
        reject(e);
        return;
      }
      let settled = false;
      const expiryDelay = Math.max(1_000, req.expires_at_ms - this.deps.now() + 5_000);
      const expiryTimer = this.timers.setTimeout(() => finish(null), expiryDelay);
      const finish = (v: ClaimWaitResult | null, err?: unknown): void => {
        if (settled) return;
        settled = true;
        this.timers.clearTimeout(expiryTimer);
        signal.removeEventListener('abort', onAbort);
        ws.onclose = null;
        ws.onmessage = null;
        try {
          ws.close(1000, 'done');
        } catch {
          /* ignore */
        }
        if (err) reject(err);
        else resolve(v);
      };
      const onAbort = (): void => finish(null, new Error('aborted'));
      signal.addEventListener('abort', onAbort, { once: true });
      ws.onmessage = (ev) => {
        const env = parseEnvelope(ev.data);
        if (env?.type === 'pairing_claimed') finish('CLAIMED');
        else if (env?.type === 'pairing_expired') finish('EXPIRED');
      };
      ws.onerror = () => undefined;
      // Evita perder um "claimed" ocorrido antes da conexão: uma consulta única ao abrir.
      ws.onopen = () => {
        void this.status(req)
          .then((st) => {
            if (st.status === 'CLAIMED') finish('CLAIMED');
            else if (st.status === 'EXPIRED') finish('EXPIRED');
          })
          .catch(() => undefined);
      };
      // Fechou antes de concluir: o chamador cai na consulta com backoff (null).
      ws.onclose = () => finish(null);
    });
  }

  private async waitViaRetry(req: PairingRequestResponse, signal: AbortSignal): Promise<ClaimWaitResult> {
    for (let attempt = 0; ; attempt++) {
      if (signal.aborted) throw new Error('aborted');
      try {
        const st = await this.status(req);
        if (st.status === 'CLAIMED') return 'CLAIMED';
        if (st.status === 'EXPIRED') return 'EXPIRED';
      } catch (e) {
        this.deps.log?.(`pareamento: consulta falhou (${errorMessage(e)})`);
      }
      if (this.deps.now() > req.expires_at_ms + 30_000) return 'EXPIRED';
      // Retry com backoff (≥ 5 s, máx. 60 s, com jitter) — não é polling contínuo.
      const delay = Math.max(5_000, backoffDelay(attempt, 5_000, 60_000, this.random));
      await sleep(delay, this.timers, signal);
    }
  }

  async status(req: PairingRequestResponse): Promise<PairingStatusResponse> {
    const res = await this.deps.api.get<PairingStatusResponse>(PROTOCOL_PATHS.pairingRequest(req.pairing_request_id), { auth: false });
    const status = String(res.data?.status ?? 'PENDING').toUpperCase();
    return {
      status: status === 'CLAIMED' || status === 'EXPIRED' ? status : 'PENDING',
      expires_at_ms: Number(res.data?.expires_at_ms ?? req.expires_at_ms),
    };
  }

  /** 2.5 — resultado assinado pela chave do dispositivo. */
  async fetchResult(req: PairingRequestResponse): Promise<PairingResult> {
    const ts = Math.trunc(this.deps.now());
    const signature = await signCanonical(this.deps.signer, {
      pairing_request_id: req.pairing_request_id,
      purpose: PURPOSE_PAIRING_RESULT,
      ts_ms: ts,
    });
    const res = await this.deps.api.get<PairingResult>(PROTOCOL_PATHS.pairingResult(req.pairing_request_id), {
      auth: false,
      headers: { [HEADER_DEVICE_TS]: String(ts), [HEADER_DEVICE_SIGNATURE]: signature },
    });
    const r = res.data;
    if (!r?.device_id || !r.screen_id || !Array.isArray(r.server_keys)) throw new Error('resultado de pareamento incompleto');
    return r;
  }

  /** 2.3b — login controlado do técnico no dispositivo (credenciais só em memória, nesta chamada). */
  async claimOnDevice(req: PairingRequestResponse, body: ClaimOnDeviceBody): Promise<void> {
    const payload: ClaimOnDeviceBody = { login: body.login, password: body.password, screen_code: body.screen_code };
    if (body.totp) payload.totp = body.totp;
    await this.deps.api.post(PROTOCOL_PATHS.pairingClaim(req.pairing_request_id), payload, { auth: false });
  }
}
