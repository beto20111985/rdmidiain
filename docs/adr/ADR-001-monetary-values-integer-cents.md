> **Revisado na AÇÃO 002 (2026-10-08): VÁLIDO.** Valores monetários continuam em centavos inteiros (`*_cents`); a comissão usa micro-centavos internamente com piso no fechamento (ver `stack`/ADR_009 do projeto e `backend/app/modules/financial/`).

# ADR-001: Valores Monetários em Centavos Inteiros

**Status:** Aceito  
**Data:** 2026-10-07  
**Contexto:** Sistema de pagamento SaaS com múltiplas transações diárias

## Decisão

Todos os valores monetários no sistema são armazenados e processados como **centavos inteiros (INTEGER)**. Nenhum campo monetário usa float, double, Decimal ou string.

## Justificativa

- Aritmética de ponto flutuante produz erros de arredondamento em valores financeiros (ex: `0.1 + 0.2 ≠ 0.3` em IEEE 754).
- INTEGER em PostgreSQL garante exatidão absoluta e desempenho superior a DECIMAL para comparações e somas.
- Auditoria regulatória requer que cálculos sejam deterministicos e reproduzíveis.
- APIs externas (gateways de pagamento) normalmente usam centavos.

## Consequências

- Toda exibição para o usuário divide por 100: `R$ {value / 100:.2f}`.
- Validação obrigatória em todo ponto de entrada: `if not isinstance(v, int): raise ValueError`.
- Testes devem verificar que nenhum cálculo retorna float.

## Exceções

Nenhuma. Sem exceções à esta regra.
