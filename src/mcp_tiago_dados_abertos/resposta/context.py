"""Contexto temporal calculado no servidor."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATA_TZ = "America/Sao_Paulo"
_WEEKDAYS_PT_BR = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")


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


