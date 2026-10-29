/**
 * Instância única por origem (achado do E2E real): duas abas do player na mesma origem compartilham a MESMA
 * identidade (chave ES256 e device_id no IndexedDB) ⇒ dois WS do mesmo dispositivo se derrubando (close 4000
 * "replaced") e dois motores gravando PoP sobrepostos. Usa a Web Locks API: a aba que segura o lock exclusivo
 * roda; as demais esperam (mensagem na tela) e assumem automaticamente quando a primeira fechar.
 * Sem Web Locks (navegadores antigos): roda sem a proteção (registrado em log).
 */

export const INSTANCE_LOCK = 'rd-player-instance';

export interface LockManagerLike {
  request(name: string, options: { mode?: 'exclusive' | 'shared'; ifAvailable?: boolean }, cb: (lock: unknown) => Promise<unknown>): Promise<unknown>;
}

export type InstanceOutcome = 'ACQUIRED' | 'WAITED' | 'UNSUPPORTED';

/**
 * Resolve quando esta aba for a instância ativa. O lock fica retido até a página fechar.
 * `onWaiting` é chamado (uma vez) se outra aba já detém o lock.
 */
export async function acquireSingleInstance(locks: LockManagerLike | undefined, onWaiting: () => void): Promise<InstanceOutcome> {
  if (!locks?.request) return 'UNSUPPORTED';
  const hold = (): Promise<never> => new Promise<never>(() => undefined);
  // 1ª tentativa sem esperar
  const got = await new Promise<boolean>((resolve, reject) => {
    locks
      .request(INSTANCE_LOCK, { mode: 'exclusive', ifAvailable: true }, async (lock) => {
        if (!lock) {
          resolve(false);
          return undefined;
        }
        resolve(true);
        return hold();
      })
      .catch(reject);
  });
  if (got) return 'ACQUIRED';
  onWaiting();
  await new Promise<void>((resolve, reject) => {
    locks
      .request(INSTANCE_LOCK, { mode: 'exclusive' }, async () => {
        resolve();
        return hold();
      })
      .catch(reject);
  });
  return 'WAITED';
}
