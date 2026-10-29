/**
 * Trusted Types: com a CSP `require-trusted-types-for 'script'`, `new Worker(url)` e
 * `serviceWorker.register(url)` só aceitam TrustedScriptURL. A política "rd-player" só libera URLs da
 * própria origem (CSP: `trusted-types rd-player`). Sem suporte a Trusted Types, devolve a string.
 */

interface TrustedTypePolicyLike {
  createScriptURL(url: string): unknown;
}

interface TrustedTypesLike {
  createPolicy(name: string, rules: { createScriptURL: (u: string) => string }): TrustedTypePolicyLike;
}

let policy: TrustedTypePolicyLike | null | undefined;

export const TRUSTED_TYPES_POLICY = 'rd-player';

function getPolicy(): TrustedTypePolicyLike | null {
  if (policy !== undefined) return policy;
  const tt = (globalThis as unknown as { trustedTypes?: TrustedTypesLike }).trustedTypes;
  if (!tt?.createPolicy) {
    policy = null;
    return policy;
  }
  try {
    policy = tt.createPolicy(TRUSTED_TYPES_POLICY, {
      createScriptURL: (u: string) => {
        const parsed = new URL(u, globalThis.location.href);
        if (parsed.origin !== globalThis.location.origin) throw new Error(`script de origem não permitida: ${parsed.origin}`);
        return parsed.href;
      },
    });
  } catch {
    policy = null;
  }
  return policy;
}

/** URL de script confiável (mesma origem). */
export function trustedScriptUrl(url: string): string {
  const p = getPolicy();
  return (p ? p.createScriptURL(url) : url) as string;
}
