# -*- coding: utf-8 -*-
"""Context variable for request correlation ID (x-request-id by default).

Provides end-to-end correlation across the proxy/gateway access logs, tool_call
events and SQL metrics without introducing external dependencies.

NEVER raises exceptions - all operations are defensive and fail silently.
"""

import contextvars
import os

_corr_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("corr_id", default=None)

# x-request-id e' o cabecalho de correlacao sem dono. Atras de um proxy ou CDN que
# gere o proprio id, aponte MCP_CORR_HEADER para o cabecalho dele — e' o que faz o
# log do servidor casar com o log do proxy.
CORR_HEADER = os.getenv("MCP_CORR_HEADER", "x-request-id").lower()
MAX_CORR = 200


def _read_headers(include: set[str] | None = None, include_all: bool = False) -> dict:
    """Read HTTP headers from current FastMCP request context. Never raises.

    Args:
        include: Specific header names to include (bypasses default stripping).
        include_all: If True, return ALL headers (bypasses default stripping).

    By default, get_http_headers() strips host/content-length/authorization/etc.
    Using include or include_all ensures we get the headers we need.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        return get_http_headers(include=include, include_all=include_all) or {}
    except Exception:
        return {}


def current_correlation_id() -> str | None:
    """Return correlation ID for current request (CORR_HEADER, x-request-id by default).

    Returns None if unavailable. Never raises.
    """
    v = _corr_var.get()
    if v is not None:
        return v
    try:
        # Request the correlation header explicitly to bypass default stripping
        headers = _read_headers(include={CORR_HEADER})
        # get_http_headers returns lowercased keys, but be robust:
        low = {str(k).lower(): val for k, val in headers.items()}
        raw = low.get(CORR_HEADER)
        if raw:
            v = str(raw)[:MAX_CORR]
            _corr_var.set(v)
            return v
    except Exception:
        pass
    return None


