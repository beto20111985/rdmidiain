"""
Templates seguros de notificação: somente substituição ``{{variavel}}``.

Sem expressões, filtros, laços ou acesso a atributos (nada de Jinja/eval): um
template administrável nunca executa código. Variáveis desconhecidas são
rejeitadas na gravação; na renderização, valores ausentes viram "—".
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from app.core.errors import DomainError

# Variáveis disponíveis em TODO template (preenchidas pelo serviço): marca da instância (white-label).
GLOBAL_VARIABLES = ("instance_name",)

# ─── Formatação de valores (helper ÚNICO para e-mail, central e push) ────────
# Produtores passam valores CRUS (datetime/date/ISO-8601, centavos ``*_cents``, códigos de enum) e o
# fuso IANA do estabelecimento quando o dado for de estabelecimento; a renderização formata em pt-BR.
DATETIME_FMT = "%d/%m/%Y %H:%M"
DATE_FMT = "%d/%m/%Y"
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENUM_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Códigos de enum → rótulos pt-BR, por NOME da variável (só estas variáveis são traduzidas; códigos
# públicos como RD-XXXXXX/TEL-UF-XXXX nunca são tocados).
VALUE_LABELS: dict[str, dict[str, str]] = {
    "priority": {"LOW": "baixa", "NORMAL": "normal", "HIGH": "alta", "URGENT": "urgente", "BAIXA": "baixa",
                 "MEDIA": "média", "ALTA": "alta", "CRITICA": "crítica"},
    "component": {"TV": "TV", "TVBOX": "TV Box", "CABLE": "cabo", "NETWORK": "rede", "OTHER": "outro"},
    "status": {"CONCLUIDO": "concluído", "SIMULADO": "simulado (sem deploy real)",
               "BLOQUEADO_DEPENDENCIA": "bloqueado por dependência externa", "FALHOU": "falhou",
               "CANCELADO": "cancelado", "EM_ANDAMENTO": "em andamento", "PAUSADO": "pausado",
               "ABERTO": "aberto", "EM_ATENDIMENTO": "em atendimento", "AGUARDANDO_CLIENTE": "aguardando cliente",
               "RESOLVIDO": "resolvido", "FECHADO": "fechado"},
    "decision": {"APPROVED": "aprovado", "REJECTED": "rejeitado", "APROVADO": "aprovado", "REJEITADO": "rejeitado"},
    "kind": {"EXTERNAL": "externo", "INTERNAL": "interno", "INSTITUTIONAL": "institucional", "FALLBACK": "padrão"},
}


def format_datetime(value: Any, tz: str) -> Any:
    """``datetime`` (aware ou ingênuo = UTC) ou texto ISO-8601 → ``dd/MM/yyyy HH:mm`` no fuso ``tz``.
    ``date``/ISO de data → ``dd/MM/yyyy``. Outros valores voltam inalterados."""
    from datetime import date, datetime, timezone

    from app.core.timeutils import DEFAULT_TIMEZONE, to_local

    if isinstance(value, str):
        v = value.strip()
        if ISO_DATE_RE.match(v):
            try:
                return date.fromisoformat(v).strftime(DATE_FMT)
            except ValueError:
                return value
        if ISO_DATETIME_RE.match(v):
            try:
                value = datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T", 1))
            except ValueError:
                return value
        else:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            return to_local(value, tz or DEFAULT_TIMEZONE).strftime(DATETIME_FMT)
        except ValueError:
            return to_local(value, DEFAULT_TIMEZONE).strftime(DATETIME_FMT)
    if isinstance(value, date):
        return value.strftime(DATE_FMT)
    return value


def format_value(key: str, value: Any, tz: str) -> Any:
    out = format_datetime(value, tz)
    if out is value and isinstance(value, str) and key in VALUE_LABELS and ENUM_RE.match(value):
        return VALUE_LABELS[key].get(value) or value.replace("_", " ").lower()
    return out


def screen_label(name: Any, code: Any) -> str:
    """Nome cadastrado da tela; código entre parênteses só quando diferente do nome."""
    n = (str(name).strip() if name else "")
    c = (str(code).strip() if code else "")
    if n and c and n != c:
        return f"{n} ({c})"
    return n or c or "—"

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_BRACES_RE = re.compile(r"\{\{|\}\}")
MAX_TITLE = 255
MAX_BODY = 4000


def placeholders(template: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(template or ""))


def validate_template(template: str, allowed: Iterable[str], *, max_len: int = MAX_BODY) -> None:
    if template is None:
        raise DomainError("TEMPLATE_INVALID", "Template vazio.", status=400)
    if len(template) > max_len:
        raise DomainError("TEMPLATE_INVALID", f"Template excede {max_len} caracteres.", status=400)
    stripped = PLACEHOLDER_RE.sub("", template)
    if _BRACES_RE.search(stripped):
        raise DomainError("TEMPLATE_INVALID", "Use somente variáveis no formato {{nome}}.", status=400)
    unknown = sorted(placeholders(template) - set(allowed) - set(GLOBAL_VARIABLES))
    if unknown:
        raise DomainError(
            "TEMPLATE_UNKNOWN_VARIABLE",
            "O template usa variáveis que não existem para este evento.",
            status=400,
            details={"unknown": unknown, "allowed": sorted(set(allowed) | set(GLOBAL_VARIABLES))},
        )


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def render(template: str, data: Mapping[str, Any], *, max_len: int = MAX_BODY) -> str:
    out = PLACEHOLDER_RE.sub(lambda m: _fmt(data.get(m.group(1))), template or "")
    return out[:max_len]
