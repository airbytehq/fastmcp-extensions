# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Telemetry middleware for MCP tool call instrumentation.

Intercepts every `tools/call` invocation and records structured telemetry:

- `tool_name`, `timestamp`, `duration_ms`, `success`/`failure`, `error_type`
- `outcome`, and for a failure `error_category`, `error_fault`,
  `upstream_status_code`, `error_cause_types`, and `error_reason`
- `tool_group` (the tool's `mcp_module`) and `mutation_class`
  (`read` / `mutate` / `destructive` / `unknown`)
- `package_version` (when a `package_name` is provided)
- Optional attribution properties supplied through `extra_properties`

Three telemetry sinks, each independently toggled:

1. **Structured JSON log** - always on (Python `logging`, `INFO` level).
2. **Sentry breadcrumb** - enabled when a `sentry_dsn` is supplied.
3. **Segment analytics event** - enabled when a `segment_write_key` is supplied.

`mcp_server()` registers this middleware automatically from a `TelemetryConfig`;
see `fastmcp_extensions.server` for the user-facing configuration docs.
Registration goes through `register_tool_call_telemetry()`, which is idempotent:
it skips the app when an instance is already installed, so an app built with
`telemetry=False` can be instrumented later without duplicating log lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastmcp.exceptions import DisabledError, NotFoundError, ToolError
from fastmcp.server.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.server.providers.addressing import parse_hashed_backend_name
from fastmcp.tools import Tool, ToolResult
from fastmcp.utilities.versions import VersionSpec

from fastmcp_extensions._attribution import _AnonymizedAttribution
from fastmcp_extensions._telemetry import (
    DEFAULT_SEGMENT_USER_ID,
    TelemetryConfig,
    TelemetryRecord,
    TelemetrySinks,
    resolve_extra_properties,
)
from fastmcp_extensions.tool_traits import MutationClass, ToolTraits, get_tool_traits

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp import types as mt

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
ToolCallTelemetryRecord = TelemetryRecord


def tool_telemetry_properties(app: FastMCP, tool_name: str) -> dict[str, str | None]:
    """Return the `tool_group` and `mutation_class` recorded for a tool.

    Reads the in-process traits registry. `register_mcp_tools` records both
    values and `mcp_provider` records only the group, since a provider tool's
    hints can differ per version. Other tools report a `None` group and an
    `unknown` mutation class; the telemetry middleware classifies those from
    the tool each call resolves to.
    """
    traits = get_tool_traits(app, tool_name)
    return {
        "tool_group": traits.mcp_module,
        "mutation_class": (traits.mutation_class or MutationClass.UNKNOWN).value,
    }


def unwrap_tool_error(exc: BaseException) -> BaseException:
    """Return the real failure behind a `ToolError` that wraps a tool exception.

    FastMCP 3.4+ wraps tool exceptions in `ToolError`; the cause carries the
    real failure type. `UserFacingErrorMiddleware` suppresses the cause and
    leaves the original on `user_facing_cause`.
    """
    if isinstance(exc, ToolError):
        cause = exc.__cause__
        if cause is None:
            # `vars`, not `getattr`: a tool's subclass property must not run here.
            cause = vars(exc).get("user_facing_cause")
        if isinstance(cause, BaseException):
            return cause
    return exc


def _requested_version(meta: Mapping[str, object] | None) -> VersionSpec | None:
    """Return the tool version a `tools/call` requested through `_meta.fastmcp`."""
    fastmcp_meta = (meta or {}).get("fastmcp")
    if not isinstance(fastmcp_meta, Mapping):
        return None
    version = fastmcp_meta.get("version")
    if isinstance(version, str):
        return VersionSpec(eq=version)
    if isinstance(version, Mapping):
        bounds = (version.get("gte"), version.get("lt"), version.get("eq"))
        if all(bound is None or isinstance(bound, str) for bound in bounds):
            gte, lt, eq = bounds
            return VersionSpec(gte=gte, lt=lt, eq=eq)
    return None


@dataclass(slots=True)
class ToolCallFacts:
    """What is known about one `tools/call`, computed once.

    The telemetry event and the trace span are both derived from this, so
    they cannot disagree. Nothing here raises into the tool call.
    """

    context: MiddlewareContext[mt.CallToolRequestParams]
    traits: ToolTraits = field(default_factory=ToolTraits)
    outcome: str | None = None
    """`success`, `tool_error`, `exception`, `cancelled`, or `unknown_tool`."""
    cause: BaseException | None = None
    attribution: Mapping[str, object] = field(default_factory=dict)
    """Anonymized attribution, resolved once for the event and the span."""
    span: dict[str, object] = field(default_factory=dict)
    """Identifiers of the span that traced this call; they join the event to it."""
    extra_source: Mapping[str, object] | Callable[[], Mapping[str, object]] | None = (
        None
    )
    owned: bool = False
    """Whether a telemetry middleware has put its properties on these facts."""
    classifier: Callable[[BaseException], str | None] | None = None
    """The tracing config's `error_classifier`, set by the tracing middleware."""
    reason: Callable[[BaseException], str | None] | None = None
    """The tracing config's `error_reason`, set by the tracing middleware."""
    _extra: Mapping[str, object] | None = None
    _error: dict[str, object] | None = None
    _tool: Tool | None = None
    _resolved: bool = False

    @property
    def error_type(self) -> str | None:
        """The failure's class name, `ToolError` for a returned error."""
        if self.cause is not None:
            return type(self.cause).__name__
        return "ToolError" if self.outcome == "tool_error" else None

    async def tool(self) -> Tool | None:
        """Return the tool (and version) this call resolves to, or `None`."""
        if not self._resolved:
            self._resolved = True
            try:
                ctx = self.context.fastmcp_context
                if ctx is not None:
                    message = self.context.message
                    self._tool = await ctx.fastmcp.get_tool(
                        message.name, version=_requested_version(message.meta)
                    )
                    # FastMCP falls back to hashed-name dispatch, so do the same.
                    hashed = parse_hashed_backend_name(message.name)
                    if self._tool is None and hashed is not None:
                        self._tool = await ctx.fastmcp.get_tool_by_hash(*hashed)
            except Exception:
                logger.debug("Failed to resolve the called tool", exc_info=True)
        return self._tool

    async def mutation_class(self) -> MutationClass:
        """Return the registered class, else the resolved tool's hints."""
        if self.traits.mutation_class is not None:
            return self.traits.mutation_class
        try:
            tool = await self.tool()
            if tool is not None:
                return MutationClass.from_annotations(tool.annotations)
        except Exception:
            logger.debug("Failed to classify the called tool", exc_info=True)
        return MutationClass.UNKNOWN

    def extra_properties(self) -> Mapping[str, object]:
        """Return the server's `extra_properties`, resolved once per call."""
        if self._extra is None:
            # A copy, so the event and the span read the same values even if
            # the server's mapping changes in between.
            self._extra = dict(resolve_extra_properties(self.extra_source))
        return self._extra

    def settle(self, result: ToolResult | None, error: BaseException | None) -> None:
        """Record how the call ended. The first (innermost) caller wins."""
        if self.outcome is not None:
            return
        if error is None:
            failed = result is not None and result.is_error
            self.outcome = "tool_error" if failed else "success"
        elif isinstance(error, asyncio.CancelledError):
            self.outcome, self.cause = "cancelled", error
        elif isinstance(error, (NotFoundError, DisabledError)):
            self.outcome, self.cause = "unknown_tool", error
        else:
            self.outcome, self.cause = "exception", unwrap_tool_error(error)

    def error_facts(self) -> Mapping[str, object]:
        """Return `error_attributes` for a failed call, computed once.

        Empty for a successful call and before `settle`. Never raises.
        """
        if self.outcome is None:
            return {}
        if self._error is None:
            self._error = {}
            if self.outcome != "success":
                try:
                    # Imported here because `otel.middleware` imports this module.
                    from fastmcp_extensions.otel._extras import (
                        error_attributes,
                        user_facing_error_types,
                    )

                    user_facing: tuple[type[BaseException], ...] = ()
                    ctx = self.context.fastmcp_context
                    with contextlib.suppress(Exception):
                        if ctx is not None:
                            user_facing = user_facing_error_types(ctx.fastmcp)
                    self._error = error_attributes(
                        self.cause,
                        unknown_tool=self.outcome == "unknown_tool",
                        user_facing_errors=user_facing,
                        classifier=self.classifier,
                        reason=self.reason,
                    )
                except (Exception, asyncio.CancelledError):
                    logger.debug(
                        "Failed to classify the tool call failure", exc_info=True
                    )
        return self._error


_FACTS: ContextVar[ToolCallFacts | None] = ContextVar(
    "fastmcp_extensions_tool_call", default=None
)


def tool_call_facts(
    context: MiddlewareContext[mt.CallToolRequestParams],
) -> ToolCallFacts:
    """Return the facts an outer middleware holds for this call, or new ones."""
    facts = _FACTS.get()
    # By message identity: a nested `app.call_tool()` is a different call.
    if facts is not None and facts.context.message is context.message:
        return facts
    traits = ToolTraits()
    try:
        if context.fastmcp_context is not None:
            app, name = context.fastmcp_context.fastmcp, context.message.name
            traits = get_tool_traits(app, name)
            # A hashed backend name (`<hash>_<name>`) resolves to the tool
            # registered as `<name>`, so that tool's traits and opt-out apply.
            hashed = parse_hashed_backend_name(name)
            if hashed is not None and traits == ToolTraits():
                traits = get_tool_traits(app, hashed[1])
    except Exception:
        logger.debug("Failed to read tool traits", exc_info=True)
    return ToolCallFacts(context, traits)


class ToolCallTelemetryMiddleware(Middleware):
    """Middleware that records telemetry for every MCP tool invocation.

    Captured fields per call:

    - `tool_name` - the MCP tool that was invoked
    - `timestamp` - ISO-8601 UTC timestamp of the call start
    - `duration_ms` - wall-clock execution time in milliseconds
    - `success` - whether the call completed without raising or returning an error
    - `error_type` - the exception class name, `ToolError` for a returned error,
      or `None` on success
    - `outcome` - `success`, `tool_error`, `exception`, `cancelled`, or
      `unknown_tool`
    - `error_category`, `error_fault` - the failure's category and who is at
      fault, the same values as the trace span; absent on success
    - `upstream_status_code` - the HTTP status a failure carries, when it has one
    - `error_cause_types` - class names of the exceptions chained behind the
      failure, at most four
    - `error_reason` - the slug the tracing config's `error_reason` hook
      returned for the failure, when it returned one
    - `tool_group` - the tool's `mcp_module` (`None` when not registered
      through fastmcp-extensions)
    - `mutation_class` - `read`, `mutate`, `destructive`, or `unknown`,
      from the tool's `readOnlyHint` / `destructiveHint`
    - `package_version` - the installed version of `package_name`
    - `extra` - optional attribution properties

    Telemetry is emitted to up to three sinks:

    1. **Structured JSON log** at `INFO` level (always on).
    2. **Sentry breadcrumb** (`mcp_tool_call` category) when `sentry_dsn` is set.
    3. **Segment event** (`mcp_tool_call`) when `segment_write_key` is set.

    Example:

    ```python
    app = mcp_server(
        display_name="my-server",
        package_name="my-package",
        telemetry=TelemetryConfig(
            sentry_dsn="https://...@sentry.io/...",
            segment_write_key="hnWfMdE...",
            extra_properties={"is_hosted_mcp": True},
        ),
    )
    ```
    """

    def __init__(
        self,
        *,
        package_name: str | None = None,
        sentry_dsn: str | None = None,
        segment_write_key: str | None = None,
        segment_user_id: str | Callable[[], str | None] = DEFAULT_SEGMENT_USER_ID,
        segment_anonymous_id: str | Callable[[], str | None] | None = None,
        extra_properties: (
            Mapping[str, object] | Callable[[], Mapping[str, object]] | None
        ) = None,
        known_public_mcp_domains: Sequence[str] = (),
        anonymization_salt: str | Callable[[], str | None] | None = None,
        anonymized_attribution: bool = True,
    ) -> None:
        """Initialize the telemetry middleware.

        Sentry and Segment sinks are configured here - if the corresponding
        SDK is not installed the sink is silently skipped and a debug log is
        emitted.
        """
        self._sinks = TelemetrySinks(
            package_name=package_name,
            sentry_dsn=sentry_dsn,
            segment_write_key=segment_write_key,
            segment_user_id=segment_user_id,
            segment_anonymous_id=segment_anonymous_id,
        )
        self._extra_properties = extra_properties
        self._attribution = (
            _AnonymizedAttribution(
                known_public_mcp_domains=known_public_mcp_domains,
                anonymization_salt=anonymization_salt,
            )
            if anonymized_attribution
            else None
        )

    @property
    def _sentry_enabled(self) -> bool:
        return self._sinks.sentry_enabled

    @property
    def _segment_enabled(self) -> bool:
        return self._sinks.segment_enabled

    @property
    def _package_version(self) -> str:
        return self._sinks.package_version

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Wrap tool execution with telemetry collection."""
        timestamp = datetime.now(tz=timezone.utc)
        facts = tool_call_facts(context)
        # A second telemetry middleware on the app reports its own properties.
        owner = not facts.owned
        attribution = resolve_extra_properties(self._attribution)
        if owner:
            facts.owned = True
            facts.extra_source = self._extra_properties
            facts.attribution = attribution
        mutation_class = await facts.mutation_class()
        token = _FACTS.set(facts)
        start = time.monotonic()
        result: ToolResult | None = None
        error: BaseException | None = None
        try:
            result = await call_next(context)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            _FACTS.reset(token)
            facts.settle(result, error)
            record = TelemetryRecord(
                invocation_type="mcp_tool_call",
                name=context.message.name,
                timestamp=timestamp.isoformat(),
                duration_ms=round((time.monotonic() - start) * 1000, 2),
                success=facts.outcome == "success",
                error_type=facts.error_type,
                package_version=self._sinks.package_version,
                extra={
                    "tool_group": facts.traits.mcp_module,
                    "mutation_class": mutation_class.value,
                    **attribution,
                    **(
                        facts.extra_properties()
                        if owner
                        else resolve_extra_properties(self._extra_properties)
                    ),
                    # After the server's properties, so one of the same name
                    # cannot make the event disagree with the span.
                    "outcome": facts.outcome,
                    **{
                        key.replace(".", "_"): value
                        for key, value in facts.error_facts().items()
                    },
                    # Last, so a server property cannot break the join to the span.
                    **facts.span,
                },
            )
            self._sinks.emit(record)


def register_tool_call_telemetry(app: FastMCP, config: TelemetryConfig) -> None:
    """Register tool-call telemetry on `app` unless it is already present.

    Also registers tool-call tracing when `config.tool_tracing` is set.
    """
    if not config.enabled:
        return

    # An app built by `mcp_server()` knows its package; the config can override it.
    package_name = config.package_name or getattr(
        getattr(app, "x_mcp_server_config", None), "package_name", None
    )
    if not any(
        isinstance(middleware, ToolCallTelemetryMiddleware)
        for middleware in app.middleware
    ):
        app.add_middleware(
            ToolCallTelemetryMiddleware(
                package_name=package_name,
                sentry_dsn=config.sentry_dsn,
                segment_write_key=config.segment_write_key,
                segment_user_id=config.segment_user_id,
                segment_anonymous_id=config.segment_anonymous_id,
                extra_properties=config.extra_properties,
                known_public_mcp_domains=config.known_public_mcp_domains,
                anonymization_salt=config.anonymization_salt,
                anonymized_attribution=config.anonymized_attribution,
            )
        )

    if config.tool_tracing:
        # Imported here because `otel.middleware` imports this module.
        from fastmcp_extensions.otel.middleware import register_tool_call_tracing
        from fastmcp_extensions.otel.models import ToolTracingConfig

        register_tool_call_tracing(
            app,
            ToolTracingConfig() if config.tool_tracing is True else config.tool_tracing,
            package_name=package_name,
        )
