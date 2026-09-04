"""Contexto temporal calculado no servidor."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATA_TZ = "America/Sao_Paulo"
_WEEKDAYS_PT_BR = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")


@dataclass(frozen=True)
class TemporalResolution:
    """Pergunta reescrita com expressoes relativas ancoradas no relogio do servidor."""

    rewritten: str
    notes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def data_timezone_name() -> str:
    """Retorna o fuso usado para interpretar datas dos dados."""
    return os.getenv("MCP_DATA_TZ", DEFAULT_DATA_TZ) or DEFAULT_DATA_TZ


def data_timezone() -> ZoneInfo:
    """Carrega o fuso de dados, com fallback conservador para America/Sao_Paulo."""
    name = data_timezone_name()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_DATA_TZ)


def server_today() -> date:
    """Data corrente no fuso dos dados."""
    return datetime.now(data_timezone()).date()


def temporal_footer() -> str:
    """Linha unica com a ancora temporal que acompanha toda resposta de tool."""
    tz = data_timezone()
    now_data = datetime.now(tz)
    now_utc = datetime.now(timezone.utc)
    today = now_data.date()
    yesterday = today - timedelta(days=1)
    weekday = _WEEKDAYS_PT_BR[today.weekday()]
    return (
        f"*[contexto_temporal: hoje={today.isoformat()} ({weekday}) | "
        f"ontem={yesterday.isoformat()} | agora={now_utc.strftime('%Y-%m-%dT%H:%MZ')} UTC | "
        f"fuso_dados={data_timezone_name()}]*"
    )


def resolve_temporal_expressions(question: str) -> TemporalResolution:
    """Substitui expressoes relativas por datas ISO absolutas."""
    today = server_today()
    rewritten = str(question)
    notes: list[str] = []

    def replace(pattern: str, resolver) -> None:
        nonlocal rewritten

        def repl(match: re.Match[str]) -> str:
            replacement, note = resolver(match)
            notes.append(note)
            return replacement

        rewritten = re.sub(pattern, repl, rewritten, flags=re.IGNORECASE)

    replace(
        r"\b[u\u00fa]ltimos?\s+(\d{1,3})\s+(dias?|semanas?|m[e\u00ea]s(?:es)?|anos?)\b",
        lambda match: _resolve_recent(match, today),
    )
    replace(
        r"\b(?:ultima|\u00faltima)\s+semana\b|\bsemana\s+passada\b",
        lambda match: _resolve_last_week(match, today),
    )
    replace(r"\b(?:este|esse)\s+m[e\u00ea]s\b", lambda match: _resolve_this_month(match, today))
    replace(r"\bm[e\u00ea]s\s+passado\b", lambda match: _resolve_last_month(match, today))
    replace(r"\bontem\b", lambda match: _resolve_single_day(match, today - timedelta(days=1)))
    replace(r"\bhoje\b", lambda match: _resolve_single_day(match, today))

    return TemporalResolution(rewritten=rewritten, notes=tuple(dict.fromkeys(notes)))


def _resolve_single_day(match: re.Match[str], target: date) -> tuple[str, str]:
    label = match.group(0).lower()
    iso = target.isoformat()
    return iso, f"{label} -> {iso}"


def _resolve_recent(match: re.Match[str], today: date) -> tuple[str, str]:
    label = match.group(0).lower()
    amount = max(int(match.group(1)), 1)
    unit = _normalize_recent_unit(match.group(2))
    if unit == "day":
        start = today - timedelta(days=amount - 1)
    elif unit == "week":
        start = today - timedelta(days=(amount * 7) - 1)
    elif unit == "month":
        start = _add_months(today.replace(day=1), -(amount - 1))
    else:
        start = today.replace(year=today.year - (amount - 1), month=1, day=1)
    return _range_replacement(label, start, today)


def _resolve_last_week(match: re.Match[str], today: date) -> tuple[str, str]:
    label = match.group(0).lower()
    current_monday = today - timedelta(days=today.weekday())
    start = current_monday - timedelta(days=7)
    end = start + timedelta(days=6)
    return _range_replacement(label, start, end)


def _resolve_this_month(match: re.Match[str], today: date) -> tuple[str, str]:
    label = match.group(0).lower()
    return _range_replacement(label, today.replace(day=1), today)


def _resolve_last_month(match: re.Match[str], today: date) -> tuple[str, str]:
    label = match.group(0).lower()
    first_this_month = today.replace(day=1)
    end = first_this_month - timedelta(days=1)
    start = end.replace(day=1)
    return _range_replacement(label, start, end)


def _range_replacement(label: str, start: date, end: date) -> tuple[str, str]:
    rendered = f"de {start.isoformat()} a {end.isoformat()}"
    return rendered, f"{label} -> {start.isoformat()} a {end.isoformat()}"


def _normalize_recent_unit(unit_text: str) -> str:
    unit = unit_text.lower()
    if unit.startswith("semana"):
        return "week"
    if unit.startswith("mes") or unit.startswith("m\u00eas"):
        return "month"
    if unit.startswith("ano"):
        return "year"
    return "day"


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month)
