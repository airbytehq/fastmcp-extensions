# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Redact `Authorization` credentials from log output.

MCP transport auth rides the `Authorization` header — a `Bearer <token>` for the
headless path, and (with the opt-in client-credentials path) a
`Basic base64(client_id:client_secret)` carrying a long-lived secret on *every*
request. If any log record echoes that header (a middleware debug line, an ASGI
scope dump, an exception traceback that captured request headers), the credential
leaks verbatim into logs.

This module installs a generic `logging.Filter` that scrubs credential values
out of log records before a handler emits them, while leaving the auth *scheme*
(`Bearer` / `Basic`) visible so the log still reads sensibly. It is
provider-neutral: it keys off the standard HTTP auth schemes and the
`authorization` header name, not on any issuer, realm, or app-specific value.

Redaction is defense-in-depth, not a substitute for not logging credentials in
the first place, and it cannot scrub records emitted by processes it does not
control (external reverse proxies, gateways, access logs) — keep those from
logging the `Authorization` header too.

Wire it in at HTTP startup with `install_authorization_redaction()`.
"""

from __future__ import annotations

import logging
import re

REDACTION_PLACEHOLDER = "<redacted>"
"""Text substituted in place of a credential value."""

# Credential values always ride behind a standard auth scheme keyword. Matching
# the scheme + following token covers every real form the header takes — a raw
# header line (`Authorization: Bearer eyJ...`), a dict/tuple repr
# (`'authorization': 'Bearer eyJ...'`), and the ASGI byte form
# (`(b'authorization', b'Basic dXNlcjpz')`) — because they all contain
# `<scheme> <token>`. The token class covers JWT (`-._~`) and base64 (`+/=`)
# alphabets; matching stops at the first quote/whitespace/delimiter so the
# surrounding repr punctuation is preserved.
_SCHEME_CREDENTIAL_RE = re.compile(
    r"(?i)\b(Bearer|Basic)([ \t]+)([A-Za-z0-9\-._~+/]+={0,2})",
)

# Defensive fallback for a value presented *without* a scheme keyword, e.g.
# `authorization=eyJ...` or `"authorization": "eyJ..."`. The negative lookahead
# leaves scheme-prefixed values to the pattern above (which keeps the scheme
# visible) instead of redacting the scheme keyword itself.
_AUTHORIZATION_KEY_RE = re.compile(
    r"(?i)(authorization\b[\"']?[ \t]*[:=][ \t]*[\"']?)"
    r"(?!(?:Bearer|Basic)\b)"
    r"([A-Za-z0-9\-._~+/]+={0,2})",
)


def redact_authorization(text: str) -> str:
    """Return `text` with any `Authorization` credential value replaced.

    Preserves the auth scheme (`Bearer` / `Basic`) and the surrounding text;
    only the credential token itself is swapped for `REDACTION_PLACEHOLDER`.
    Idempotent — re-running it on already-redacted text is a no-op.
    """
    redacted = _SCHEME_CREDENTIAL_RE.sub(
        rf"\1\2{REDACTION_PLACEHOLDER}",
        text,
    )
    return _AUTHORIZATION_KEY_RE.sub(
        rf"\1{REDACTION_PLACEHOLDER}",
        redacted,
    )


class AuthorizationRedactionFilter(logging.Filter):
    """A `logging.Filter` that scrubs `Authorization` credentials from records.

    Attach to a handler (or logger) so credential values never reach the
    emitted output. The filter always returns `True` — it mutates the record
    in place rather than dropping it. It rewrites the fully-rendered message
    (collapsing any `%`-args), the exception traceback, and any `stack_info`.

    A credential can ride inside an exception traceback (a handler that logged
    `logger.exception(...)` after capturing request headers). Filters run
    *before* a handler's formatter renders `exc_info` into `exc_text`, so at
    filter time `exc_text` is usually empty. To close that gap the filter
    renders the traceback itself, redacts it, and caches the result on
    `record.exc_text`; `logging.Formatter.format` reuses a pre-populated
    `exc_text` instead of re-rendering, so the emitted traceback is the redacted
    one. (This uses the default traceback rendering rather than a custom
    formatter's `formatException`, which is an acceptable trade-off for not
    leaking credentials.)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact credential values on `record`; always keep the record."""
        message = record.getMessage()
        redacted = redact_authorization(message)
        if redacted != message:
            record.msg = redacted
            record.args = None

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_authorization(record.exc_text)

        if record.stack_info:
            record.stack_info = redact_authorization(record.stack_info)

        return True


def install_authorization_redaction(
    *logger_names: str,
) -> AuthorizationRedactionFilter:
    """Install the redaction filter on the given loggers and their handlers.

    Adds a single shared `AuthorizationRedactionFilter` to each named logger and
    to every handler currently attached to it. Handler-level attachment matters
    because a logger's own filters do not run for records propagated up from
    child loggers, whereas ancestor *handlers* (and their filters) do — so
    filtering the root logger's handlers catches records from child loggers too.

    Idempotent: a logger/handler that already carries an
    `AuthorizationRedactionFilter` is skipped, so calling this more than once
    (or across overlapping logger families) never stacks duplicate filters.

    Defaults to the root logger plus the `uvicorn` logger family when no names
    are given. Call after logging is configured (e.g. at HTTP startup) and
    before serving. Returns the installed (or already-present) filter.
    """
    names = logger_names or ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    redaction_filter = AuthorizationRedactionFilter()
    for name in names:
        target = logging.getLogger(name)
        _add_filter_once(target, redaction_filter)
        for handler in target.handlers:
            _add_filter_once(handler, redaction_filter)
    return redaction_filter


def _add_filter_once(
    target: logging.Logger | logging.Handler,
    redaction_filter: AuthorizationRedactionFilter,
) -> None:
    """Attach `redaction_filter` to `target` unless one is already present.

    Dedupes by type rather than identity so a filter added by a *previous*
    `install_authorization_redaction` call (a different instance) still counts,
    keeping repeated installs idempotent.
    """
    if any(isinstance(f, AuthorizationRedactionFilter) for f in target.filters):
        return
    target.addFilter(redaction_filter)
