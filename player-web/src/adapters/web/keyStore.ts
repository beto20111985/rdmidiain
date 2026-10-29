/**
 * Par ES256 do dispositivo em IndexedDB (RDPP §1, ADR_007 §2): gerado com `extractable:false`.
 * O CryptoKey é guardado por clonagem estrutural — a privada nunca vira bytes acessíveis ao JS (XSS pode
 * USAR enquanto roda, mas não exfiltrar). localStorage é proibido para a chave.
 */

import { generateEs256KeyPair, WebCryptoSigner, type Signer } from '../../core/crypto';
import type { KeyStore } from '../../core/storage';
import { reqP, txDone } from './idb';

const KEY_ID = 'device';

interface StoredPair {
  privateKey: CryptoKey;
  publicKey: CryptoKey;
  created_at_ms: number;
}

export class IdbKeyStore implements KeyStore {
  private cached: Signer | null = null;

  constructor(private readonly db: IDBDatabase) {}

  async load(): Promise<Signer | null> {
    if (this.cached) return this.cached;
    const tx = this.db.transaction('keys', 'readonly');
    const rec = (await reqP(tx.objectStore('keys').get(KEY_ID))) as StoredPair | undefined;
    if (!rec?.privateKey || !rec.publicKey) return null;
    if (rec.privateKey.extractable) throw new Error('chave do dispositivo exportável — recusada');
    this.cached = new WebCryptoSigner({ privateKey: rec.privateKey, publicKey: rec.publicKey });
    return this.cached;
  }

  async create(): Promise<Signer> {
    const pair = await generateEs256KeyPair(false);
    const tx = this.db.transaction('keys', 'readwrite');
    tx.objectStore('keys').put({ privateKey: pair.privateKey, publicKey: pair.publicKey, created_at_ms: Date.now() } satisfies StoredPair, KEY_ID);
    await txDone(tx);
    this.cached = new WebCryptoSigner(pair);
    return this.cached;
  }

  async clear(): Promise<void> {
    this.cached = null;
    const tx = this.db.transaction('keys', 'readwrite');
    tx.objectStore('keys').delete(KEY_ID);
    await txDone(tx);
  }
}
