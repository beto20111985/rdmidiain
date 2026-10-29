/**
 * Configuração do player — SEM domínio hardcoded (P001 §12). Prioridade:
 *  1. `window.RD_NATIVE.config` (casca Windows; `config_ok !== false`): api_base_url / ws_base_url / panel_url;
 *  2. `./config.json` servido junto ao app (Smart TVs, instalações sem proxy same-origin):
 *     {"api_base_url":"https://api.exemplo","ws_base_url":"wss://api.exemplo","panel_url":"https://painel.exemplo"};
 *  3. `window.location.origin` (padrão: origem do player com proxy same-origin de /api/v1/player/** e /ws/player/**).
 * A base é a ORIGEM (esquema://host[:porta]); os caminhos /api/v1/... e /ws/... são acrescentados pelo núcleo.
 */

import type { PlatformBridge } from './adapters/web/platform';
import type { Platform } from './core/protocol';

export interface PlayerConfig {
  apiBase: string;
  wsBase: string;
  panelUrl: string | null;
  platform: Platform;
  appVersion: string;
  source: 'native' | 'config.json' | 'location';
}

interface ConfigJson {
  api_base_url?: string;
  api_base?: string;
  ws_base_url?: string;
  ws_base?: string;
  panel_url?: string;
}

/** Normaliza para origem sem barra final. */
export function normalizeBase(url: string, base?: string): string {
  const u = new URL(url, base);
  return u.origin;
}

/** http→ws, https→wss (mesma origem). */
export function deriveWsBase(apiBase: string): string {
  return wsOrigin(apiBase);
}

function wsOrigin(url: string): string {
  const u = new URL(url);
  if (u.protocol === 'http:') u.protocol = 'ws:';
  if (u.protocol === 'https:') u.protocol = 'wss:';
  return `${u.protocol}//${u.host}`;
}

export async function loadConfig(opts: {
  location: Pick<Location, 'href' | 'origin'>;
  fetch: typeof fetch;
  bridge: PlatformBridge;
  appVersion: string;
}): Promise<PlayerConfig> {
  const platform = opts.bridge.platform;
  const nc = opts.bridge.config;
  if (nc && nc.config_ok !== false && nc.api_base_url) {
    const apiBase = normalizeBase(nc.api_base_url);
    return {
      apiBase,
      wsBase: nc.ws_base_url ? wsOrigin(nc.ws_base_url) : deriveWsBase(apiBase),
      panelUrl: nc.panel_url ?? null,
      platform,
      appVersion: opts.bridge.shellVersion ?? opts.appVersion,
      source: 'native',
    };
  }
  try {
    const res = await opts.fetch(new URL('config.json', opts.location.href).href, { cache: 'no-store', credentials: 'omit' });
    if (res.ok) {
      const j = (await res.json()) as ConfigJson;
      const api = j.api_base_url ?? j.api_base;
      if (api) {
        const apiBase = normalizeBase(api, opts.location.href);
        const ws = j.ws_base_url ?? j.ws_base;
        return {
          apiBase,
          wsBase: ws ? wsOrigin(new URL(ws, opts.location.href).href) : deriveWsBase(apiBase),
          panelUrl: j.panel_url ?? null,
          platform,
          appVersion: opts.appVersion,
          source: 'config.json',
        };
      }
    }
  } catch {
    /* sem config.json: usa a origem */
  }
  const apiBase = normalizeBase(opts.location.origin);
  return { apiBase, wsBase: deriveWsBase(apiBase), panelUrl: null, platform, appVersion: opts.appVersion, source: 'location' };
}
