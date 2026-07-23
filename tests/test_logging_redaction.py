# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Unit tests for the `Authorization`-credential logging redaction filter."""

from __future__ import annotations

import base64
import io
import logging

import pytest

from fastmcp_extensions import logging_redaction as lr

_SECRET = base64.b64encode(b"my-client-id:super-secret-value").decode()
_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl-x_y"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f"Authorization: Bearer {_JWT}",
            f"Authorization: Bearer {lr.REDACTION_PLACEHOLDER}",
            id="header_line_bearer",
        ),
        pytest.param(
            f"Authorization: Basic {_SECRET}",
            f"Authorization: Basic {lr.REDACTION_PLACEHOLDER}",
            id="header_line_basic",
        ),
        pytest.param(
            f"headers={{'authorization': 'Bearer {_JWT}'}}",
            f"headers={{'authorization': 'Bearer {lr.REDACTION_PLACEHOLDER}'}}",
            id="dict_repr_bearer",
        ),
        pytest.param(
            f"[(b'authorization', b'Basic {_SECRET}')]",
            f"[(b'authorization', b'Basic {lr.REDACTION_PLACEHOLDER}')]",
            id="asgi_byte_tuple_basic",
        ),
        pytest.param(
            f"lower bearer {_JWT} and BASIC {_SECRET}",
            (
                f"lower bearer {lr.REDACTION_PLACEHOLDER} "
                f"and BASIC {lr.REDACTION_PLACEHOLDER}"
            ),
            id="case_insensitive_schemes",
        ),
        pytest.param(
            f"authorization={_JWT}",
            f"authorization={lr.REDACTION_PLACEHOLDER}",
            id="key_without_scheme",
        ),
        pytest.param(
            "nothing sensitive here",
            "nothing sensitive here",
            id="no_credential_untouched",
        ),
    ],
)
def test_redact_authorization(text: str, expected: str) -> None:
    """Credential values are replaced while scheme and context are preserved."""
    assert lr.redact_authorization(text) == expected


def test_redact_authorization_is_idempotent() -> None:
    """Re-redacting already-redacted text does not corrupt the placeholder."""
    once = lr.redact_authorization(f"Authorization: Bearer {_JWT}")
    assert lr.redact_authorization(once) == once


def test_filter_redacts_percent_arg_message() -> None:
    """The filter collapses `%`-args and scrubs the rendered credential."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="inbound %s",
        args=(f"Authorization: Bearer {_JWT}",),
        exc_info=None,
    )
    redaction_filter = lr.AuthorizationRedactionFilter()

    assert redaction_filter.filter(record) is True
    assert _JWT not in record.getMessage()
    assert (
        record.getMessage()
        == f"inbound Authorization: Bearer {lr.REDACTION_PLACEHOLDER}"
    )


def test_filter_redacts_already_formatted_exception_text() -> None:
    """Already-formatted exception text is scrubbed too."""
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=None,
        exc_info=None,
    )
    record.exc_text = f"Traceback ... Basic {_SECRET}"
    redaction_filter = lr.AuthorizationRedactionFilter()

    assert redaction_filter.filter(record) is True
    assert _SECRET not in record.exc_text
    assert lr.REDACTION_PLACEHOLDER in record.exc_text


def test_filter_redacts_exception_traceback_at_emit_time() -> None:
    """A credential in a traceback is scrubbed through the real emit pipeline.

    Exercises emit-time ordering (filter runs before the formatter renders
    `exc_info`), which a pre-populated `exc_text` test cannot catch.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(lr.AuthorizationRedactionFilter())
    logger = logging.getLogger("test.exc.emit")
    logger.handlers.clear()
    logger.filters.clear()
    logger.addHandler(handler)
    logger.propagate = False

    try:
        raise ValueError(f"boom Authorization: Basic {_SECRET}")
    except ValueError:
        logger.exception("request failed")

    output = stream.getvalue()
    assert "Traceback" in output
    assert _SECRET not in output
    assert lr.REDACTION_PLACEHOLDER in output


def test_filter_keeps_clean_record_message_unchanged() -> None:
    """A record with no credential is passed through untouched."""
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    redaction_filter = lr.AuthorizationRedactionFilter()

    assert redaction_filter.filter(record) is True
    # args preserved (message rendered lazily) when nothing was redacted.
    assert record.getMessage() == "hello world"


def test_install_attaches_filter_to_logger_and_handlers() -> None:
    """`install_authorization_redaction` wires the filter onto logger + handlers."""
    logger = logging.getLogger("test.install.redaction")
    logger.handlers.clear()
    logger.filters.clear()
    handler = logging.StreamHandler()
    logger.addHandler(handler)

    installed = lr.install_authorization_redaction("test.install.redaction")

    assert installed in logger.filters
    assert installed in handler.filters


def test_install_is_idempotent() -> None:
    """Installing twice does not stack duplicate filters."""
    name = "test.install.idempotent"
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    handler = logging.StreamHandler()
    logger.addHandler(handler)

    lr.install_authorization_redaction(name)
    lr.install_authorization_redaction(name)

    def _redaction_filter_count(target: logging.Logger | logging.Handler) -> int:
        return sum(
            isinstance(f, lr.AuthorizationRedactionFilter) for f in target.filters
        )

    # A fresh instance is built per call, so counting by type (not identity)
    # is what actually proves repeated installs don't stack duplicates.
    assert _redaction_filter_count(logger) == 1
    assert _redaction_filter_count(handler) == 1


def test_installed_filter_scrubs_emitted_child_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A credential logged through a child logger is redacted at the handler."""
    root = logging.getLogger()
    installed = lr.install_authorization_redaction("")
    try:
        with caplog.at_level(logging.INFO):
            # caplog's handler must also carry the filter to observe redaction.
            caplog.handler.addFilter(installed)
            logging.getLogger("some.child").info(
                "sending Authorization: Bearer %s", _JWT
            )
        assert _JWT not in caplog.text
        assert lr.REDACTION_PLACEHOLDER in caplog.text
    finally:
        caplog.handler.removeFilter(installed)
        root.removeFilter(installed)
