> **⚠ SUPERADO (AÇÃO 002, 2026-10-08).** A abordagem descrita aqui (`CREATE RULE … DO INSTEAD NOTHING`) ignorava UPDATE/DELETE em silêncio e foi substituída: o ledger atual é de partidas dobradas, append-only por **trigger que recusa** UPDATE/DELETE, com verificação de soma zero diferida e `REVOKE` para o usuário da aplicação (`rd_app`) — migrations `0004`/`0006`, `backend/app/db/ddl.py`. Mantido só como histórico.

# ADR-002: Ledger Financeiro Imutável

**Status:** Aceito  
**Data:** 2026-10-07  
**Contexto:** Auditoria financeira e conformidade regulatória

## Decisão

O ledger financeiro (`ledger_entries`) é **imutável**: registros nunca são atualizados ou deletados. Reversões criam novas entradas do tipo `REVERSAL`. O saldo é sempre calculado somando todas as entradas (nunca coluna `balance` mutável).

## Justificativa

- Audit trail completo para conformidade fiscal e regulatória.
- Impossibilidade de fraude por alteração retroativa de registros.
- Idempotência via `idempotency_key` evita duplicação sem violar imutabilidade.
- Padrão de mercado em sistemas financeiros (double-entry bookkeeping).

## Consequências

- `LedgerService.create_entry()` é o único ponto de escrita.
- Nunca usar `UPDATE` ou `DELETE` em `ledger_entries`.
- Performance de leitura de saldo: `SELECT SUM(amount_cents)` — aceitável para volumes esperados; particionar por `org_id` se necessário.
- Movimentações de saída (comissões, saques) usam tipos negativos (COMMISSION, WITHDRAWAL) mas `amount_cents > 0` — a direção é inferida pelo tipo.
