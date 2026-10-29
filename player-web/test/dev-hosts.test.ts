/**
 * AÇÃO 003 (agente F) — `server.allowedHosts` do Vite do player derivado de
 * RD_DEV_PLAYER_ALLOWED_ORIGINS (+ VITE_ALLOWED_HOSTS legado), provado contra o dev server REAL do
 * Vite 5.4 (host check): localhost/túnel autorizado → 200; host não autorizado → 403.
 * Domínios de túnel FICTÍCIOS (nenhum domínio real de conta em código).
 */
import { mkdtempSync, writeFileSync } from 'node:fs';
import { request } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createServer, type ViteDevServer } from 'vite';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { DevHostsError, resolveAllowedHosts, validateHost } from '../dev-hosts';

const TUNNEL_HOST = 'rd-teste-player.ngrok-free.dev';
const BASE = ['localhost', '127.0.0.1', 'player-web', 'rd-player-web'];

describe('resolveAllowedHosts (player)', () => {
  it('sem configuração: só a base — array, nunca `true`', () => {
    const hosts = resolveAllowedHosts({ base: BASE, origins: undefined, hosts: undefined });
    expect(hosts).toEqual(BASE);
  });

  it('origens de dev + hosts legados (validados), sem duplicatas', () => {
    const hosts = resolveAllowedHosts({
      base: BASE,
      origins: `http://localhost:8088, https://${TUNNEL_HOST}`,
      hosts: 'localhost, .dev.cliente-exemplo.com.br',
    });
    expect(hosts).toEqual([...BASE, TUNNEL_HOST, '.dev.cliente-exemplo.com.br']);
  });

  it.each(['*', '.ngrok-free.dev', '.localtunnel.me', 'host:8088', 'https://x.com/p'])('VITE_ALLOWED_HOSTS rejeita %s', (bad) => {
    expect(() => resolveAllowedHosts({ base: BASE, hosts: bad, hostsSource: 'VITE_ALLOWED_HOSTS' })).toThrowError(DevHostsError);
  });

  it.each(['*', `https://*.ngrok-free.dev`, TUNNEL_HOST, 'https://user@x.com'])('RD_DEV_PLAYER_ALLOWED_ORIGINS rejeita %s', (bad) => {
    expect(() => resolveAllowedHosts({ base: BASE, origins: bad, originsSource: 'RD_DEV_PLAYER_ALLOWED_ORIGINS' }))
      .toThrowError(/RD_DEV_PLAYER_ALLOWED_ORIGINS: item 1/);
  });

  it('validateHost normaliza caixa', () => {
    expect(validateHost('Player-Web')).toBe('player-web');
  });
});

describe('host check do dev server REAL do Vite com o array derivado', () => {
  let server: ViteDevServer;
  let port = 0;

  beforeAll(async () => {
    const root = mkdtempSync(join(tmpdir(), 'rd-devhosts-'));
    writeFileSync(join(root, 'index.html'), '<!doctype html><title>ok</title>');
    server = await createServer({
      configFile: false,
      root,
      logLevel: 'silent',
      optimizeDeps: { noDiscovery: true, include: [] },
      server: {
        host: '127.0.0.1',
        port: 0,
        hmr: false,
        watch: null,
        allowedHosts: resolveAllowedHosts({ base: BASE, origins: `http://localhost:8088,https://${TUNNEL_HOST}` }),
      },
    });
    await server.listen();
    port = (server.httpServer?.address() as AddressInfo).port;
  }, 30_000);

  afterAll(async () => {
    await server?.close();
  });

  const get = (host: string) =>
    new Promise<{ status: number; body: string }>((resolve, reject) => {
      const req = request({ host: '127.0.0.1', port, path: '/', headers: { Host: host } }, (res) => {
        let body = '';
        res.on('data', (c: Buffer) => (body += c.toString()));
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }));
      });
      req.on('error', reject);
      req.end();
    });

  it.each([
    ['localhost:8088', 200],
    [`${TUNNEL_HOST}`, 200],
    [`${TUNNEL_HOST}:443`, 200],
    ['player-web:5174', 200],
    ['malicioso.example.com', 403],
    ['outro.ngrok-free.dev', 403],
    [`x.${TUNNEL_HOST}`, 403],
  ])('Host %s → %i', async (host, expected) => {
    const r = await get(host);
    expect(r.status).toBe(expected);
    if (expected === 403) expect(r.body).toContain('Blocked request');
  });
});
