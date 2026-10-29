/**
 * Alertas exibidos pelo player (RDPP §5.1.10; ETAPA2 #262–#265, #273; P001 §64).
 *  - ≥ offline_alert_days[0] (padrão 7) dias sem sincronizar → aviso destacado no RODAPÉ (não para a reprodução).
 *  - ≥ offline_alert_days[1] (padrão 28) dias → OVERLAY central grande (atrapalha de propósito), até sincronizar.
 *  - Relógio inválido → "Corrija a data e hora".
 *  - Armazenamento não persistente / disco cheio → alerta técnico no rodapé.
 * Ambos os alertas de offline somem assim que houver sincronização (lastSync atualizado).
 */

import type { ClockStatus } from './clock';
import { DAY_MS } from './util';

export type AlertCode = 'OFFLINE_7D' | 'OFFLINE_28D' | 'CLOCK_INVALID' | 'STORAGE_NOT_PERSISTENT' | 'DISK_FULL';

export interface AlertInput {
  /** Instante corrigido. */
  now: number;
  /** Última sincronização/contato confirmado com o servidor (instante corrigido), null se nunca. */
  lastSyncMs: number | null;
  offlineAlertDays?: readonly number[];
  clock?: ClockStatus;
  storagePersistent?: boolean;
  diskFull?: boolean;
}

export interface AlertView {
  code: AlertCode;
  text: string;
}

export interface AlertState {
  /** Códigos para o heartbeat (`alerts`). */
  codes: AlertCode[];
  /** Mensagem destacada no rodapé (prioridade: relógio > offline > técnico). */
  footer: AlertView | null;
  /** Overlay central (28 dias) — null se não houver. */
  overlay: AlertView | null;
  /** Dias inteiros sem sincronização. */
  offlineDays: number;
}

export const DEFAULT_ALERT_DAYS: readonly [number, number] = [7, 28];

export function computeAlerts(input: AlertInput): AlertState {
  const days = input.offlineAlertDays && input.offlineAlertDays.length >= 2 ? input.offlineAlertDays : DEFAULT_ALERT_DAYS;
  const footerDays = days[0]!;
  const overlayDays = days[1]!;
  const codes: AlertCode[] = [];
  let footer: AlertView | null = null;
  let overlay: AlertView | null = null;

  const elapsed = input.lastSyncMs === null ? 0 : Math.max(0, input.now - input.lastSyncMs);
  const offlineDays = Math.floor(elapsed / DAY_MS);

  if (input.lastSyncMs !== null && elapsed >= overlayDays * DAY_MS) {
    codes.push('OFFLINE_28D');
    overlay = {
      code: 'OFFLINE_28D',
      text: `Este equipamento está há ${offlineDays} dias sem atualização. Conecte-o à internet para continuar recebendo a programação.`,
    };
  }
  if (input.lastSyncMs !== null && elapsed >= footerDays * DAY_MS) {
    codes.unshift('OFFLINE_7D');
    footer = {
      code: 'OFFLINE_7D',
      text: `SEM ATUALIZAÇÃO HÁ ${offlineDays} DIAS — CONECTE O EQUIPAMENTO À INTERNET`,
    };
  }
  if (input.clock && !input.clock.valid) {
    codes.push('CLOCK_INVALID');
    footer = { code: 'CLOCK_INVALID', text: 'CORRIJA A DATA E HORA DO EQUIPAMENTO' };
  }
  if (input.diskFull) {
    codes.push('DISK_FULL');
    footer ??= { code: 'DISK_FULL', text: 'ARMAZENAMENTO CHEIO — CONTATE O SUPORTE RD' };
  }
  if (input.storagePersistent === false) {
    codes.push('STORAGE_NOT_PERSISTENT');
    footer ??= { code: 'STORAGE_NOT_PERSISTENT', text: 'ARMAZENAMENTO NÃO PERSISTENTE — CONTATE O SUPORTE RD' };
  }
  return { codes, footer, overlay, offlineDays };
}
