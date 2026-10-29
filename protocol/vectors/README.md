# Vetores de conformidade — RD Player Protocol v1 (RDPP/1 §9)

Gerados pela implementação de REFERÊNCIA do backend (Python), de forma **determinística**:

```
docker exec -w /app rd-backend python -m app.modules.playback.vectors --out /app/.vectors_tmp --write-lock
# copiar /app/.vectors_tmp/*.json (= backend/.vectors_tmp) para protocol/vectors/
docker exec -w /app rd-backend python -m app.modules.playback.vectors --check /app/.vectors_tmp
```

- Gerador: `backend/app/modules/playback/vectors.py`; hashes publicados: `backend/app/modules/playback/vectors.lock.json`.
- Testes do backend: `backend/tests/unit/test_vectors_files.py` (regenera e compara com o lock e, se o diretório
  `protocol/vectors` estiver acessível — variável `RD_VECTORS_DIR` —, byte a byte com os arquivos) e
  `backend/tests/integration/test_vectors_pop_db.py` (reexecuta cada caso de PoP pelo caminho real com PostgreSQL).
- Mudança incompatível = nova versão do protocolo (não editar estes arquivos à mão).

**As chaves privadas presentes aqui são SOMENTE DE TESTE** (derivadas de rótulos fixos). Nunca usar em produção.

## Arquivos

Todos têm `{"version": 1, "generated_by": "...", "cases": [...]}`.

| Arquivo | Conteúdo | Quem consome |
|---|---|---|
| `canonical_json.json` | `cases[] = {name, input, canonical}` ou `{name, input, error: "FLOAT_NOT_ALLOWED"}`. Floats aparecem na entrada como `{"$float": "1.5"}`: converta para um número de ponto flutuante REAL e confirme que a canonicalização o REJEITA. | Python, TS, Kotlin |
| `es256_signatures.json` | `test_key`/`other_key` (`private_pkcs8_b64u`, `public_spki_b64u`); `cases[] = {name, message, canonical, signature_b64u, public_spki_b64u, valid}`. Verifique cada assinatura dada (r‖s 64 bytes, base64url SEM padding) e faça round-trip próprio de assinar+verificar (ECDSA não é determinístico em produção; aqui foi usado RFC 6979 só para reprodutibilidade). Inclui: mensagem adulterada, chave errada, `s = 0`, `r ≥ n`, 63 bytes, DER (não aceito), padding `=` (não aceito) e **high-S** (aceito — não exigir low-S). | Python, TS, Kotlin |
| `cycle_resolution.json` | `cases[] = {name, manifest, anchor_ms, local_state, queries[{t_ms, expected{block, kind, creative_key, start_ms, end_ms}}]}` — função pura `resolve()` (§5.2, §8.2). Casos: 2+2, 3+1, âncora 08:07 com consultas 08:47 e 08:49:10 (`manifest.anchor_ms = null`), fora do horizonte (repetição semanal ±7 d), horizonte curto sem correspondência, vencido/não vigente, revogado (manifesto e local), mídia ausente, emergência (primeira ativa vence; última repetição parcial = FILLER), fallback vazio, interno vazio, entradas sobrepostas/extrapolando o bloco, item interno maior que o bloco. Os manifestos são SINTÉTICOS (alguns casos são de defesa e nunca saem do gerador do servidor). | Python, TS, Kotlin |
| `pop_validation.json` | `device_key`/`wrong_key`; `cases[] = {name, event, canonical, signature_b64u, context, expected{status, valid, reason, flags}}` e `batch_cases[] = {name, context, items[{event, signature}], expected{results, ack_upto_seq}, expected_retry}`. `context` = estado do servidor antes do evento (`now_ms`, `device`, `last_seq`, `last_hash`, `known_events`, `kills`, `manifests`). Players usam a parte de FORMA (evento, RDJ1, assinatura, `prev_hash` = sha256 hex do RDJ1 do evento anterior); o backend valida tudo. | Backend (players: forma) |

## Regras de PoP cobertas (`expected.reason`)

`SCHEMA_INVALID`, `BAD_SIGNATURE`, `DEVICE_MISMATCH` (REJECTED) · `DUPLICATE_UUID_MISMATCH` (DUPLICATE) ·
`DEVICE_NOT_BOUND`, `BEFORE_PAIRING`, `CLOCK_FUTURE` (> 120 s), `TIME_INCONSISTENT` (finished ≠ started + actual),
`DURATION_IMPLAUSIBLE`, `SEQ_ROLLBACK`, `NONCE_REUSED`, `NOT_COMPLETED`, `INCOMPLETE_DURATION` (29 999/30 000 é
inválido — 100% sem tolerância), `MANIFEST_UNKNOWN`, `CREATIVE_NOT_IN_MANIFEST`, `CREATIVE_MISMATCH`,
`CAMPAIGN_MISMATCH`, `INSERTION_NOT_IN_MANIFEST`, `EXPECTED_MISMATCH`, `CAMPAIGN_NOT_ACTIVE`, `CREATIVE_REVOKED`,
`OVERLAP` (mesmo dispositivo), `IMPOSSIBLE_VOLUME` (soma recortada > 1 h na hora, por TELA) (ACCEPTED, `valid=false`).
Anomalias em `flags` (não invalidam): `SEQ_GAP`, `CHAIN_BREAK`, `LARGE_CLOCK_OFFSET`. Eventos offline NÃO são
invalidados por chegarem tarde.

## Observação de ordenação de chaves (RDJ1)

O protocolo define ordenação por **ponto de código**. Para chaves fora do BMP (ex.: emoji) isso difere da ordem por
unidades UTF-16 (RFC 8785 / `Array.prototype.sort` do JS). Os vetores evitam esse caso; implementações TS/Kotlin devem
ordenar por ponto de código (ex.: comparar `codePointAt`) se algum dia usarem tais chaves. Os objetos do protocolo só
usam chaves ASCII.
