import { describe, expect, it } from 'vitest';
import { computeAlerts } from '../src/core/alerts';
import { Clock } from '../src/core/clock';
import { DAY_MS } from '../src/core/util';

const T0 = Date.UTC(2026, 9, 7, 12, 0, 0);

describe('alertas de offline 7 d / 28 d (tempo simulado)', () => {
  it('sem alerta antes de 7 dias', () => {
    const a = computeAlerts({ now: T0 + 7 * DAY_MS - 1, lastSyncMs: T0 });
    expect(a.codes).toEqual([]);
    expect(a.footer).toBeNull();
    expect(a.overlay).toBeNull();
    expect(a.offlineDays).toBe(6);
  });

  it('≥ 7 dias: aviso destacado no rodapé, sem overlay', () => {
    const a = computeAlerts({ now: T0 + 7 * DAY_MS, lastSyncMs: T0 });
    expect(a.codes).toEqual(['OFFLINE_7D']);
    expect(a.footer?.code).toBe('OFFLINE_7D');
    expect(a.footer?.text).toContain('7 DIAS');
    expect(a.overlay).toBeNull();
  });

  it('≥ 28 dias: overlay central grande + rodapé', () => {
    const a = computeAlerts({ now: T0 + 28 * DAY_MS + 5, lastSyncMs: T0 });
    expect(a.codes).toEqual(['OFFLINE_7D', 'OFFLINE_28D']);
    expect(a.overlay?.code).toBe('OFFLINE_28D');
    expect(a.overlay?.text).toContain('28 dias');
    expect(a.footer?.code).toBe('OFFLINE_7D');
  });

  it('ambos somem após sincronizar (lastSync atualizado)', () => {
    const now = T0 + 40 * DAY_MS;
    expect(computeAlerts({ now, lastSyncMs: T0 }).overlay).not.toBeNull();
    const after = computeAlerts({ now, lastSyncMs: now });
    expect(after.codes).toEqual([]);
    expect(after.overlay).toBeNull();
    expect(after.footer).toBeNull();
  });

  it('limiares configuráveis pelo manifesto (offline_alert_days)', () => {
    const a = computeAlerts({ now: T0 + 3 * DAY_MS, lastSyncMs: T0, offlineAlertDays: [2, 3] });
    expect(a.codes).toEqual(['OFFLINE_7D', 'OFFLINE_28D']);
  });

  it('relógio inválido tem prioridade no rodapé; armazenamento não persistente é alerta técnico', () => {
    const a = computeAlerts({ now: T0, lastSyncMs: T0, clock: { valid: false, issues: ['BEFORE_GENERATED'] }, storagePersistent: false });
    expect(a.footer?.text).toBe('CORRIJA A DATA E HORA DO EQUIPAMENTO');
    expect(a.codes).toEqual(['CLOCK_INVALID', 'STORAGE_NOT_PERSISTENT']);
    const b = computeAlerts({ now: T0, lastSyncMs: T0, storagePersistent: false });
    expect(b.footer?.code).toBe('STORAGE_NOT_PERSISTENT');
  });
});

describe('relógio corrigido (X-Server-Time-Ms)', () => {
  it('offset pelo ponto médio do RTT; t = local + offset', () => {
    let wall = T0;
    let mono = 1_000;
    const c = new Clock({ wallNow: () => wall, mono: () => mono });
    // requisição enviada em mono=1000, resposta em mono=1200 (RTT 200 ms); servidor marcou T0+5000+100
    mono = 1_200;
    wall = T0 + 200;
    expect(c.observe(T0 + 5_100, 1_000, 1_200)).toBe(true);
    expect(c.offsetMs).toBe(5_000);
    expect(c.now()).toBe(T0 + 200 + 5_000);
  });

  it('usa a amostra de menor RTT e descarta RTT absurdo', () => {
    let mono = 0;
    const c = new Clock({ wallNow: () => T0, mono: () => mono });
    c.observe(T0 + 1_000 - 400, -800, 0); // RTT 800 ⇒ offset 1000
    expect(c.offsetMs).toBe(1_000);
    c.observe(T0 + 2_000 - 10, -20, 0); // RTT 20 ⇒ offset 2000 (melhor)
    expect(c.offsetMs).toBe(2_000);
    c.observe(T0 + 3_000 - 300, -600, 0); // RTT 600 ⇒ pior que a melhor da janela: mantém 2000
    expect(c.offsetMs).toBe(2_000);
    c.observe(T0 + 9_000, -6_000, 0); // RTT 6 s descartado
    expect(c.offsetMs).toBe(2_000);
    mono = 0;
  });

  it('alerta: offset > 120 s persistido sem rede para corrigir; some ao medir de novo', () => {
    const c = new Clock({ wallNow: () => T0 });
    c.load({ offset_ms: 300_000, measured_at_ms: T0 - DAY_MS, hwm_ms: T0 });
    expect(c.status().issues).toContain('OFFSET_UNCORRECTED');
    c.observe(T0 + 300_000);
    expect(c.status().valid).toBe(true);
  });

  it('alerta: data anterior ao generated_at_ms do manifesto', () => {
    const c = new Clock({ wallNow: () => T0 });
    expect(c.status(T0 + DAY_MS).issues).toContain('BEFORE_GENERATED');
    expect(c.status(T0 - DAY_MS).valid).toBe(true);
  });

  it('alerta: relógio voltou (RTC zerado após queda de energia, sem rede)', () => {
    const c = new Clock({ wallNow: () => Date.UTC(2000, 0, 1) });
    c.load({ offset_ms: 0, measured_at_ms: T0, hwm_ms: T0 });
    expect(c.status().issues).toContain('WENT_BACKWARDS');
  });

  it('snapshot persiste offset e marca d’água', () => {
    const c = new Clock({ wallNow: () => T0 });
    c.observe(T0 + 1_000);
    expect(c.snapshot()).toEqual({ offset_ms: 1_000, measured_at_ms: T0 + 1_000, hwm_ms: T0 + 1_000 });
  });
});
