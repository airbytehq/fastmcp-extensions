# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for the argument-tracing engine."""

from __future__ import annotations

import inspect
import json
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, SecretStr

from fastmcp_extensions.otel import _arg_digests as t
from fastmcp_extensions.otel._arg_digests import ArgClass, ArgTracer, TraceArg

P = "acme.mcp"
MASTER = bytes(range(32, 64))
PRINCIPAL = "https://issuer.example\x00user-0001"
DIGEST = "ab" * 32
CANARY = "zz-CANARY-7f3a-do-not-export"
ZEROS = "0" * 32


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


class _Model(BaseModel):
    a: int = 0


class _HostileList(list):
    def __iter__(self):
        raise RuntimeError(CANARY)


def synthetic_tool(
    flag: bool,
    mode: Literal["a", "b"],
    color: _Color,
    modes: list[Literal["x", "y"]] | None,
    text: str,
    number: int,
    ratio: float,
    uid: UUID,
    described: Annotated[str, Field(description="d")],
    names: list[str] | None,
    either: str | list[str] | None,
    mixed: str | Literal["auto"],
    config: dict,
    model: _Model,
    anything: Any,
    ints: list[int],
    untyped,
    pw: SecretStr | None,
    api_key: str,
    use_token: bool,
    omit: Annotated[str, TraceArg.OMIT],
    name: Annotated[str, Field(description="d"), TraceArg.FINGERPRINT],
    similar_list: Annotated[list[str], TraceArg.FINGERPRINT],
    limit: Annotated[int, TraceArg.VALUE] = 20,
    label: Annotated[str | None, TraceArg.VALUE] = None,
    nested: Annotated[str, TraceArg.OMIT] | None = None,
    twice: Annotated[Annotated[str, TraceArg.OMIT], TraceArg.OMIT] = "",
    my_secret: Annotated[str, TraceArg.HASH] = "",
    blob: Annotated[dict, TraceArg.HASH] | None = None,
    clash: Annotated[str, TraceArg.OMIT, TraceArg.VALUE] = "",
) -> None: ...


NAMES = list(inspect.signature(synthetic_tool).parameters)
# `ArgClass.describe()` -> the arguments of `synthetic_tool` classified that way.
PLAN = {
    "value allowed=[false,true]": "flag",
    'value allowed=["a","b"]': "mode",
    'value allowed=["blue","red"]': "color",
    'value allowed=["x","y"] list': "modes",
    "value int": "limit",
    "value str": "label",
    "hash": "text number ratio uid described mixed my_secret blob",
    "hash list": "names either",
    "fingerprint": "name",
    "fingerprint list": "similar_list",
    "presence": "config model anything ints untyped api_key use_token clash",
    "omit": "pw omit nested twice",
}
# Argument -> (value sent, keys of its record when hashing is on; `None` for no record).
SENT: dict[str, tuple[Any, str | None]] = {
    "flag": (True, "value"),
    "mode": ("a", "value"),
    "color": ("green", "digest"),
    "modes": (["y", "x"], "value"),
    "text": (CANARY, "digest"),
    "number": (0, "digest"),
    "names": (["b", "a", "a"], "count digest"),
    "config": ({"password": CANARY}, "present"),
    "pw": (CANARY, None),
    "api_key": (CANARY, "present"),
    "omit": (CANARY, None),
    "name": ("synthetic-value-0001", "digest similarity"),
    "similar_list": (["alpha", "beta", "gamma"], "count digest similarity"),
    "limit": (20, "value"),
    "label": ("x" * 300, "digest"),
    "blob": ({1: CANARY}, "present"),
    "untyped": (None, None),
    "invented": (CANARY, None),
}
CALL = {name: value for name, (value, _) in SENT.items()}


def _record(tracer: ArgTracer, args: Any = CALL, **scope: Any) -> dict[str, str]:
    scope = {"principal": PRINCIPAL, "session_digest": DIGEST, "now": 0.0, **scope}
    return tracer.record("synthetic_tool", synthetic_tool, NAMES, args, **scope)


def _records(attrs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    family = f"{P}.arg."
    return {k[len(family) :]: json.loads(v) for k, v in attrs.items() if family in k}


def test_classification_table(caplog: pytest.LogCaptureFixture) -> None:
    """Markers, secret types and names, and type hints pick each argument's mode."""
    classes = t.classify_tool(synthetic_tool, NAMES, tool="synthetic_tool")
    assert {name: cls.describe() for name, cls in classes.items()} == {
        name: plan for plan, names in PLAN.items() for name in names.split()
    }
    assert "'clash'" in caplog.text
    assert t.classify_arg("text", str, TraceArg.FINGERPRINT).describe() == "fingerprint"
    assert t.classify_arg("text", str, TraceArg.VALUE).describe() == "hash"
    assert t.classify_arg("apiKey", str).describe() == "presence"
    # A `VALUE` marker exports only the hinted scalar types; without one it is `HASH`.
    value = TraceArg.VALUE
    assert t.classify_arg("x", Annotated[float, value]).describe() == "value float,int"
    assert t.classify_arg("x", Annotated[dict, value]).describe() == "hash"

    def unresolvable(a) -> None: ...

    unresolvable.__annotations__["a"] = "NotDefinedAnywhere"
    presence = {"a": ArgClass(TraceArg.PRESENCE)}
    assert t.classify_tool(None, ["a"]) == presence
    assert t.classify_tool(unresolvable, ["a"]) == presence

    tracer = ArgTracer(P, skip=["intent"])
    cached = tracer.classes("synthetic_tool", synthetic_tool, [*NAMES, "intent"])
    assert cached == classes
    assert tracer.classes("synthetic_tool", synthetic_tool, NAMES) is cached
    assert tracer.classes("synthetic_tool", None, ["a"]) == presence
    # A mistyped `arg_default` is coerced, so it cannot record more than asked.
    for default, plan in (("omit", "omit"), (None, "presence")):
        mistyped = ArgTracer(P, default=default).classes("t", synthetic_tool, ["text"])
        assert mistyped["text"].describe() == plan


def test_golden_vector() -> None:
    """`digest`, `similarity`, and the scope id stay byte-compatible with the lifted engine."""
    # The prefix feeds the hash labels; this one has hashes already in use.
    p = "airbyte.mcp"
    args = {"text": "synthetic-value", "name": "synthetic-value-0001"}
    assert _record(ArgTracer(p, key=MASTER), args) == {
        f"{p}.arg.text": '{"digest":"45397e3f11b5a5f44d59848b2f6a3121"}',
        f"{p}.arg.name": '{"digest":"9cb6f450030cd7fefd16633ed474d693",'
        '"similarity":"210a0000405025000402000c00022041"}',
        f"{p}.arg_hash_status": "ok",
        f"{p}.arg_key_scope": "transport_session",
        f"{p}.arg_scope_id": "ba5c45977e11f294",
    }


def test_record_forms() -> None:
    """Each mode yields its record form and no raw value outside `VALUE`."""
    tracer = ArgTracer(P, key=MASTER)
    attrs = _record(tracer)
    records = _records(attrs)
    assert {name: " ".join(sorted(record)) for name, record in records.items()} == {
        name: keys for name, (_, keys) in SENT.items() if keys
    }
    assert records["modes"] == {"value": ["x", "y"]}
    assert records["limit"] == {"value": 20}
    assert records["names"]["count"] == 3
    assert CANARY not in json.dumps(attrs)

    reordered = _records(_record(tracer, {"names": ["a", "b", "a"]}))["names"]
    assert reordered == records["names"]
    assert reordered != _records(_record(tracer, {"names": ["a", "b"]}))["names"]

    # A wrong-typed `VALUE` is never exported raw; a failure stays in its argument.
    wrong = _record(
        tracer,
        {"limit": CANARY, "label": "ok", "names": _HostileList(["x"]), "text": "y"},
    )
    assert CANARY not in json.dumps(wrong)
    assert {name: " ".join(record) for name, record in _records(wrong).items()} == {
        "limit": "digest",
        "label": "value",
        "names": "present",
        "text": "digest",
    }
    oversized = ArgClass(TraceArg.VALUE, allowed=frozenset({"x" * 390}))
    assert t.build_records(P, {"m": "x" * 390}, {"m": oversized}, None) == {
        f"{P}.arg.m": '{"present":true}'
    }


@pytest.mark.parametrize(
    ("key", "scope", "status", "kind"),
    [
        (None, {}, "no_key", "none"),
        (MASTER[:31], {}, "no_key", "none"),
        (lambda: 1 / 0, {}, "no_key", "none"),
        (MASTER, {"principal": None}, "no_scope", "none"),
        (lambda: MASTER, {"session_digest": None}, "ok", "approximate"),
    ],
    ids=["no key", "short key", "raising key", "no principal", "no session"],
)
def test_key_and_scope_fallbacks(
    key: Any, scope: dict[str, Any], status: str, kind: str
) -> None:
    """Without a key or a principal, hashed modes degrade to presence."""
    attrs = _record(ArgTracer(P, key=key), **scope)
    records = _records(attrs)
    hashed = {"digest"} if status == "ok" else {"present"}
    assert (attrs[f"{P}.arg_hash_status"], attrs[f"{P}.arg_key_scope"]) == (
        status,
        kind,
    )
    assert (f"{P}.arg_scope_id" in attrs) == (status == "ok")
    assert records["flag"] == {"value": True}
    assert set(records["text"]) == hashed
    assert set(records["names"]) == {"count"} | hashed
    assert any("digest" in record for record in records.values()) == (status == "ok")


def test_scope_isolation() -> None:
    """The scope changes with the principal, session, time bucket, and client."""
    tracer = ArgTracer(P, key=MASTER)

    def scope_id(**overrides: Any) -> str:
        scope = {"session_digest": None, "client_name": "c", "client_version": "1.2"}
        return _record(tracer, {}, **{**scope, **overrides})[f"{P}.arg_scope_id"]

    assert scope_id() == scope_id(client_version="1.9", now=1799.0)
    assert scope_id(session_digest=DIGEST) == scope_id(session_digest=DIGEST, now=1e9)
    variants = {
        scope_id(),
        scope_id(principal=PRINCIPAL + "2"),
        scope_id(session_digest=DIGEST),
        scope_id(session_digest="cd" * 32),
        scope_id(now=1800.0),
        scope_id(client_name="d"),
        scope_id(client_version="2.0"),
    }
    assert len(variants) == 7


@pytest.mark.parametrize("key", [MASTER, None], ids=["keyed", "keyless"])
def test_revalidate_accepts_recorded_output(key: bytes | None) -> None:
    """The boundary keeps what the engine wrote and ignores keys outside the family."""
    tracer = ArgTracer(P, key=key)
    attrs = _record(tracer)
    noise = {f"{P}.arg_trace_dropped": 99, f"{P}.args.supplied": ("text",), "x": 1}
    assert tracer.revalidate("synthetic_tool", {**attrs, **noise}) == attrs
    assert not t.is_arg_key(P, f"{P}.args.supplied")
    # Another version of the tool is called before this span is exported.
    tracer.classes("synthetic_tool", lambda text: None, ["text"])
    assert tracer.revalidate("synthetic_tool", attrs) == attrs
    # A tool that was never classified on this app keeps nothing.
    assert tracer.revalidate("other", attrs) == {f"{P}.arg_trace_dropped": len(attrs)}
    assert tracer.revalidate(None, {}) == {}


FORGED = {
    "value on a hashed class": ({"arg.text": json.dumps({"value": CANARY})}, 1),
    "value outside the closed set": ({"arg.mode": '{"value":"c"}'}, 1),
    "value out of bounds": ({"arg.label": json.dumps({"value": "x" * 300})}, 1),
    "value of the wrong type": ({"arg.limit": json.dumps({"value": CANARY})}, 1),
    "omitted argument": ({"arg.omit": '{"present":true}'}, 1),
    "undeclared argument": ({f"arg.{CANARY}": '{"present":true}'}, 1),
    "similarity on hash": (
        {"arg.number": f'{{"digest":"{ZEROS}","similarity":"{ZEROS}"}}'},
        1,
    ),
    "bad count": ({"arg.names": f'{{"count":-1,"digest":"{ZEROS}"}}'}, 1),
    "non-canonical JSON": ({"arg.flag": '{"value": true}'}, 1),
    "digest on presence": ({"arg.config": f'{{"digest":"{ZEROS}"}}'}, 1),
    # Seven digests and the scope id.
    "digest without ok": ({"arg_hash_status": "no_key", "arg_key_scope": "none"}, 8),
}


@pytest.mark.parametrize(("forged", "dropped"), FORGED.values(), ids=list(FORGED))
def test_revalidate_rejects_forged(forged: dict[str, str], dropped: int) -> None:
    """The boundary drops and counts records that do not match the argument's class."""
    tracer = ArgTracer(P, key=MASTER)
    forged = {f"{P}.{key}": value for key, value in forged.items()}
    out = tracer.revalidate("synthetic_tool", {**_record(tracer), **forged})
    ok = out[f"{P}.arg_hash_status"] == "ok"
    assert out[f"{P}.arg_trace_dropped"] == dropped
    assert CANARY not in json.dumps(out)
    assert not any(key in out for key in forged if ".arg." in key)
    assert (f"{P}.arg_scope_id" in out) == ok
    assert any("digest" in record for record in _records(out).values()) == ok
