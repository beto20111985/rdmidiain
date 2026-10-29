import { createPrivateKey, createPublicKey, generateKeyPairSync, createHash, sign as nodeSign, verify as nodeVerify } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { canonicalBytes } from '../src/core/canonical';
import {
  createSha256,
  derToRaw,
  generateEs256KeyPair,
  importPkcs8Signer,
  importSpkiVerifier,
  rawToDer,
  sha256Hex,
  signCanonical,
  verifyCanonical,
  WebCryptoSigner,
} from '../src/core/crypto';
import { b64uDecode, b64uEncode } from '../src/core/util';

const msg = { challenge: 'abc', device_id: 'd-1', purpose: 'rd-player-auth-v1' };

describe('ES256 — WebCrypto, formato bruto r‖s', () => {
  it('gera par NÃO exportável, assina e verifica; assinatura tem 64 bytes (r‖s)', async () => {
    const kp = await generateEs256KeyPair(false);
    expect(kp.privateKey.extractable).toBe(false);
    const signer = new WebCryptoSigner(kp);
    const sigB64u = await signCanonical(signer, msg);
    expect(sigB64u).not.toMatch(/[=+/]/);
    expect(b64uDecode(sigB64u)).toHaveLength(64);
    const verifier = await importSpkiVerifier(b64uEncode(await signer.publicKeySpki()));
    expect(await verifyCanonical(verifier, msg, sigB64u)).toBe(true);
    // Qualquer alteração no objeto invalida.
    expect(await verifyCanonical(verifier, { ...msg, device_id: 'd-2' }, sigB64u)).toBe(false);
    // Assinatura malformada ⇒ false (sem exceção).
    expect(await verifyCanonical(verifier, msg, 'não-b64')).toBe(false);
    expect(await verifyCanonical(verifier, msg, b64uEncode(new Uint8Array(63)))).toBe(false);
  });

  it('a privada não pode ser exportada', async () => {
    const kp = await generateEs256KeyPair(false);
    await expect(globalThis.crypto.subtle.exportKey('pkcs8', kp.privateKey)).rejects.toBeTruthy();
  });

  it('SPKI DER em base64url sem padding é aceito pelo OpenSSL (Node)', async () => {
    const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    const spki = await signer.publicKeySpki();
    const key = createPublicKey({ key: Buffer.from(spki), format: 'der', type: 'spki' });
    expect(key.asymmetricKeyType).toBe('ec');
    expect(key.asymmetricKeyDetails?.namedCurve).toBe('prime256v1');
  });

  it('interoperabilidade: assinatura DER do OpenSSL convertida para raw verifica no WebCrypto', async () => {
    const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
    const data = canonicalBytes(msg);
    for (let i = 0; i < 20; i++) {
      const der = nodeSign('sha256', data, privateKey); // DER por padrão
      const raw = derToRaw(new Uint8Array(der));
      expect(raw).toHaveLength(64);
      const spki = publicKey.export({ format: 'der', type: 'spki' });
      const verifier = await importSpkiVerifier(new Uint8Array(spki));
      expect(await verifyCanonical(verifier, msg, b64uEncode(raw))).toBe(true);
      // ida e volta raw→DER bate com o OpenSSL
      expect(nodeVerify('sha256', data, publicKey, Buffer.from(rawToDer(raw)))).toBe(true);
    }
  });

  it('interoperabilidade: assinatura raw do WebCrypto convertida para DER verifica no OpenSSL', async () => {
    const kp = await generateEs256KeyPair(true);
    const signer = new WebCryptoSigner(kp);
    const sigRaw = b64uDecode(await signCanonical(signer, msg));
    const spki = await signer.publicKeySpki();
    const pub = createPublicKey({ key: Buffer.from(spki), format: 'der', type: 'spki' });
    expect(nodeVerify('sha256', canonicalBytes(msg), pub, Buffer.from(rawToDer(sigRaw)))).toBe(true);
    // e direto no formato ieee-p1363 (= r‖s) do próprio Node
    expect(nodeVerify('sha256', canonicalBytes(msg), { key: pub, dsaEncoding: 'ieee-p1363' }, Buffer.from(sigRaw))).toBe(true);
  });

  it('importa PKCS#8 de teste (vetores) e assina de forma verificável', async () => {
    const { privateKey, publicKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
    const pkcs8 = b64uEncode(new Uint8Array(privateKey.export({ format: 'der', type: 'pkcs8' })));
    const spki = b64uEncode(new Uint8Array(publicKey.export({ format: 'der', type: 'spki' })));
    const signer = await importPkcs8Signer(pkcs8, spki);
    const sig = await signCanonical(signer, msg);
    expect(
      nodeVerify(
        'sha256',
        canonicalBytes(msg),
        { key: createPublicKey({ key: Buffer.from(b64uDecode(spki)), format: 'der', type: 'spki' }), dsaEncoding: 'ieee-p1363' },
        Buffer.from(b64uDecode(sig)),
      ),
    ).toBe(true);
    expect(createPrivateKey({ key: Buffer.from(b64uDecode(pkcs8)), format: 'der', type: 'pkcs8' }).type).toBe('private');
  });

  it('DER com inteiros de 33 bytes (bit alto) e componentes curtos', () => {
    const raw = new Uint8Array(64);
    raw[0] = 0x80; // r com bit alto ⇒ DER precisa de 0x00 à frente
    raw[63] = 0x01; // s = 1 (curto)
    const der = rawToDer(raw);
    expect(der[3]).toBe(33);
    expect(Array.from(derToRaw(der))).toEqual(Array.from(raw));
    expect(() => derToRaw(new Uint8Array([0x30, 0x02, 0x02, 0x00]))).toThrow();
  });
});

describe('SHA-256 incremental', () => {
  it('bate com o SHA-256 do Node em blocos arbitrários', () => {
    const data = new Uint8Array(1_000_003);
    for (let i = 0; i < data.length; i++) data[i] = (i * 31 + 7) & 0xff;
    const h = createSha256();
    let o = 0;
    for (const size of [1, 63, 64, 65, 4096, 100_000]) {
      h.update(data.subarray(o, o + size));
      o += size;
    }
    h.update(data.subarray(o));
    const expected = createHash('sha256').update(data).digest('hex');
    expect(h.digestHex()).toBe(expected);
    expect(sha256Hex(data)).toBe(expected);
  });
});
