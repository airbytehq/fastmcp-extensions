# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Privacy-preserving records of MCP tool arguments.

Each argument of a traced tool call becomes one compact JSON record under
`<prefix>.arg.<name>`, chosen by the argument's `TraceArg` mode:

| Mode | With keys | Without keys |
|---|---|---|
| `OMIT` | nothing | nothing |
| `PRESENCE` | `{"present": true}` | same |
| `HASH` | `{"digest": h}`; lists add `"count"` | `{"present": true}`; lists add `"count"` |
| `FINGERPRINT` | `HASH` plus `"similarity"` for short text and string lists | as `HASH` |
| `VALUE` | `{"value": v}` when `v` is in the closed set or in bounds, else as `HASH` | same |

`digest` is a keyed equality digest and `similarity` a keyed 128-bit similarity
bitset. Both are scoped to the verified principal and a session, so values
compare only within one scope and are never recoverable. Without a key no digest is made.

This module imports only the standard library. `validate` re-checks every
record at the export boundary, so forged or malformed attributes never leave.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import math
import re
import types
import typing
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal, Union, get_args, get_origin
from uuid import UUID

from fastmcp_extensions.otel.models import TraceArg

logger = logging.getLogger(__name__)

HASH_STATUSES = frozenset({"ok", "no_key", "no_scope", "error"})
KEY_SCOPES = frozenset({"transport_session", "approximate", "none"})
ALLOWED_RECORD_KEYS = frozenset({"value", "present", "digest", "similarity", "count"})
SECRET_NAME_PARTS = (
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "access_key",
    "private_key",
    "authorization",
    "session_state",
)

KEY_LENGTH = 32
APPROXIMATE_BUCKET_SECONDS = 1800
MAX_RECORD_LENGTH = 400
MAX_RAW_STRING = 65_536
MAX_CANONICAL_BYTES = 65_536
MAX_DEPTH = 20
MAX_NODES = 5_000
MAX_LIST_ITEMS = 1_000
MAX_COUNT = 1_000_000
MAX_VALUE_LIST = 16
MAX_VALUE_LENGTH = 256
MAX_TOOL_VERSIONS = 8
FP_RAW_MAX = 160
FP_TEXT_MAX = 40
FP_LIST_MAX = 32
FP_LIST_ITEM_MAX = 200
FP_BITS = 128
_MAX_SAFE_INT = 2**53
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ArgClass:
    """Static trace classification of one tool argument.

    `allowed` is the closed value set of a `VALUE` argument. When it is `None`,
    a `VALUE` argument exports a bounded scalar whose type is in `scalars` (the
    scalar types of its hint), so a wrong-typed value is never exported raw.
    `is_list` lets the argument carry a list: a sorted list of `allowed`
    members for `VALUE`, a counted record for the hashed modes.
    """

    mode: TraceArg
    allowed: frozenset[str | int | bool] | None = None
    is_list: bool = False
    scalars: frozenset[type] = frozenset()

    def describe(self) -> str:
        """Return a stable one-line summary for `trace_plan`."""
        parts = [self.mode.value]
        if self.allowed is not None:
            parts.append("allowed=" + _dump(sorted(self.allowed, key=_dump)))
        if self.scalars:
            parts.append(",".join(sorted(kind.__name__ for kind in self.scalars)))
        if self.is_list:
            parts.append("list")
        return " ".join(parts)


_PRESENT = ArgClass(TraceArg.PRESENCE)
_OMIT = ArgClass(TraceArg.OMIT)
_SCALARS = (str, int, float, UUID)
_VALUE_TYPES = (bool, int, float, str)


class _Unsupported(Exception):  # noqa: N818  # Internal control flow, never raised to callers.
    """A cap, cycle, or unsupported value: the argument becomes `present`."""


# ---------------------------------------------------------------- classification


def classify_tool(
    func: Callable[..., object] | None,
    names: Iterable[str],
    *,
    default: TraceArg = TraceArg.HASH,
    tool: str = "",
) -> dict[str, ArgClass]:
    """Classify the client-supplied arguments `names` of a tool; never raises.

    `func` is the tool's function, or `None` for a tool without one, whose
    arguments are all `PRESENCE`.
    """
    hints: dict[str, object] = {}
    if func is not None:
        try:
            hints = typing.get_type_hints(func, include_extras=True)
        except Exception as exc:
            logger.warning(
                "Argument tracing: type hints of tool %r did not resolve (%s); "
                "its arguments are recorded as presence only",
                tool,
                type(exc).__name__,
            )
    result: dict[str, ArgClass] = {}
    for name in sorted(names):
        try:
            result[name] = classify_arg(name, hints.get(name), default)
        except Exception as exc:
            logger.warning(
                "Argument tracing: argument %r of tool %r is recorded as "
                "presence only: %s",
                name,
                tool,
                exc,
            )
            result[name] = _PRESENT
    return result


def classify_arg(
    name: str, hint: object, default: TraceArg = TraceArg.HASH
) -> ArgClass:
    """Apply the first matching rule: marker, secret type, secret name, type."""
    markers = list(dict.fromkeys(_markers(hint)))
    if len(markers) > 1:
        raise ValueError("conflicting TraceArg markers")
    members = _union_members(hint)
    allowed, is_list, simple = _shape(members)
    # Underscores are ignored so `apiKey` matches like `api_key`.
    flat = name.lower().replace("_", "")
    if markers:
        mode = markers[0]
    elif any(_is_secret_type(member) for member in members):
        mode = TraceArg.OMIT
    elif any(part.replace("_", "") in flat for part in SECRET_NAME_PARTS):
        mode = TraceArg.PRESENCE
    elif allowed is not None:
        mode = TraceArg.VALUE
    elif simple:
        mode = TraceArg.HASH if default is TraceArg.VALUE else default
    else:
        mode = TraceArg.PRESENCE
    if mode is TraceArg.OMIT:
        return _OMIT
    if mode is TraceArg.PRESENCE:
        return _PRESENT
    if mode is not TraceArg.VALUE:
        return ArgClass(mode, None, is_list)
    if allowed is not None:
        return ArgClass(mode, allowed, is_list)
    # `VALUE` without a closed set: only values of the hinted scalar types are
    # exported, so a string sent where an `int` is declared never leaves raw.
    scalars = {kind for kind in _VALUE_TYPES if any(m is kind for m in members)}
    if float in scalars:
        scalars.add(int)
    if not scalars:
        return ArgClass(TraceArg.HASH, None, is_list)
    return ArgClass(mode, None, is_list, frozenset(scalars))


def _markers(hint: object) -> list[TraceArg]:
    """Return the `TraceArg` markers on a hint and on its union members."""
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        own = [meta for meta in args[1:] if isinstance(meta, TraceArg)]
        return own + _markers(args[0])
    if get_origin(hint) in {Union, types.UnionType}:
        return [marker for arg in get_args(hint) for marker in _markers(arg)]
    return []


def _unwrap(hint: object) -> object:
    while get_origin(hint) is Annotated:
        hint = get_args(hint)[0]
    return hint


def _union_members(hint: object) -> list[object]:
    hint = _unwrap(hint)
    if get_origin(hint) in {Union, types.UnionType}:
        members: list[object] = []
        for arg in get_args(hint):
            members.extend(_union_members(arg))
        return members
    return [] if hint is None or hint is type(None) else [hint]


def _closed_values(hint: object) -> frozenset[str | int | bool] | None:
    """Return the closed value set of `bool`, a `Literal`, or an `Enum`, else `None`."""
    if hint is bool:
        return frozenset({True, False})
    if get_origin(hint) is Literal:
        values = get_args(hint)
        if values and all(isinstance(value, (str, int, bool)) for value in values):
            return frozenset(values)
        return None
    if inspect.isclass(hint) and issubclass(hint, Enum):
        values = [member.value for member in hint]
        if values and all(isinstance(value, (str, int, bool)) for value in values):
            return frozenset(values)
    return None


def _list_item(hint: object) -> object | None:
    if get_origin(hint) is list and len(get_args(hint)) == 1:
        return _unwrap(get_args(hint)[0])
    return None


def _is_secret_type(hint: object) -> bool:
    """Return whether `hint` is pydantic's `SecretStr` / `SecretBytes` or a subclass."""
    return inspect.isclass(hint) and any(
        base.__name__ in {"SecretStr", "SecretBytes"}
        and base.__module__.split(".")[0] == "pydantic"
        for base in hint.__mro__
    )


def _shape(
    members: list[object],
) -> tuple[frozenset[str | int | bool] | None, bool, bool]:
    """Return `(closed value set, has a list member, is hashable by default)`.

    The closed set is `None` unless every member is `bool`, a `Literal`, an
    `Enum`, or a list of one. "Hashable by default" means every member is one
    of those, a `str`, `int`, `float`, `UUID`, or a `list[str]`.
    """
    closed: set[str | int | bool] = set()
    all_closed = simple = bool(members)
    is_list = False
    for member in members:
        item = _list_item(member)
        is_list = is_list or member is list or get_origin(member) is list
        values = _closed_values(member if item is None else item)
        if values is not None:
            closed |= values
            continue
        all_closed = False
        if not (item is str or any(member is scalar for scalar in _SCALARS)):
            simple = False
    return (frozenset(closed) if all_closed else None), is_list, simple


# ---------------------------------------------------------------- canonicalization


def _dump(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _raw_too_long(value: str) -> bool:
    return len(value) > MAX_RAW_STRING or (
        len(value.encode("utf-8", "surrogatepass")) > MAX_RAW_STRING
    )


def _normalize(value: object, depth: int, budget: list[int]) -> object:
    budget[0] -= 1
    if budget[0] < 0:
        raise _Unsupported
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Unsupported
        return (
            int(value) if value.is_integer() and abs(value) < _MAX_SAFE_INT else value
        )
    if isinstance(value, dict):
        if depth >= MAX_DEPTH or not all(isinstance(key, str) for key in value):
            raise _Unsupported
        return {key: _normalize(item, depth + 1, budget) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH or len(value) > MAX_LIST_ITEMS:
            raise _Unsupported
        return [_normalize(item, depth + 1, budget) for item in value]
    raise _Unsupported


def _encode(normalized: object) -> bytes:
    try:
        encoded = _dump(normalized).encode("utf-8")
    except (UnicodeEncodeError, ValueError) as exc:
        raise _Unsupported from exc
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise _Unsupported
    return encoded


def _canonical_items(items: list[object]) -> bytes:
    """Return the order-free canonical encoding of list items (duplicates kept)."""
    budget = [MAX_NODES - 1]
    encoded = sorted(_encode(_normalize(item, 1, budget)) for item in items)
    joined = b"[" + b",".join(encoded) + b"]"
    if len(joined) > MAX_CANONICAL_BYTES:
        raise _Unsupported
    return joined


def canonical_bytes(value: object) -> bytes | None:
    """Return the canonical encoding used for `digest`, or `None` for `present`."""
    try:
        if isinstance(value, str) and _raw_too_long(value):
            return None
        return _encode(_normalize(value, 0, [MAX_NODES]))
    except Exception:
        return None


# ---------------------------------------------------------------- keys


@dataclass(frozen=True)
class ArgKeys:
    """Per-call derived keys. Holds no master key."""

    kind: str
    k_eq: bytes
    k_fp: Mapping[str, bytes]

    def __repr__(self) -> str:
        """Never render key material."""
        return f"ArgKeys(kind={self.kind!r})"


def _hmac(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def scope_input(
    principal: str,
    session_digest: str | None,
    client_name: str,
    client_version: str,
    now: float,
) -> tuple[str, bytes]:
    """Return `(kind, scope bytes)`: the session when there is one, else a bucket.

    The approximate scope is the principal, the client name, the client's major
    version, and a 30-minute window.
    """
    if session_digest and _HEX64.fullmatch(session_digest):
        kind = "transport_session"
        return kind, f"{kind}|{principal}|{session_digest}".encode()
    major = re.match(r"[0-9]+", client_version)
    client = hashlib.sha256(
        f"{client_name}\x00{major.group() if major else ''}".encode()
    ).hexdigest()
    bucket = int(now // APPROXIMATE_BUCKET_SECONDS)
    return "approximate", f"approximate|{principal}|{client}|{bucket}".encode()


def derive_keys(
    master: bytes,
    prefix: str,
    kind: str,
    scope: bytes,
    tool: str,
    fp_args: Iterable[str],
) -> ArgKeys:
    """Derive the equality key and one similarity key per argument in `fp_args`."""
    label = prefix.encode() + b".v1|arg-"
    return ArgKeys(
        kind=kind,
        k_eq=_hmac(master, label + b"eq|" + scope),
        k_fp={
            arg: _hmac(
                master,
                label + b"fp|" + scope + b"|" + tool.encode() + b"|" + arg.encode(),
            )
            for arg in fp_args
        },
    )


def scope_id(prefix: str, k_eq: bytes) -> str:
    """Return the exported scope identifier: records compare only within one."""
    return _hmac(k_eq, prefix.encode() + b".v1|arg-scope-id")[:8].hex()


def eq_hex(k_eq: bytes, canonical: bytes) -> str:
    """Return the keyed equality digest of canonical bytes."""
    return _hmac(k_eq, canonical)[:16].hex()


def _bitset(features: Iterable[str], key: bytes) -> str | None:
    bits = 0
    for feature in features:
        bits |= 1 << (_hmac(key, feature.encode("utf-8", "surrogatepass"))[0] % FP_BITS)
    return f"{bits:032x}" if bits else None


def fp_text(s: str, key: bytes) -> str | None:
    """Return a keyed trigram bitset for short text, else `None`."""
    if len(s) > FP_RAW_MAX:
        return None
    text = " ".join(unicodedata.normalize("NFKC", s).casefold().split())
    if not 1 <= len(text) <= FP_TEXT_MAX:
        return None
    marked = "\x02" + text + "\x03"
    return _bitset((marked[i : i + 3] for i in range(len(marked) - 2)), key)


def fp_list(items: list[object], key: bytes) -> str | None:
    """Return a keyed item bitset for 1-32 strings, else `None`."""
    if not 1 <= len(items) <= FP_LIST_MAX or not all(isinstance(i, str) for i in items):
        return None
    return _bitset(
        (
            "i:" + unicodedata.normalize("NFKC", item).casefold()
            for item in typing.cast("list[str]", items)
            if len(item) <= FP_LIST_ITEM_MAX
        ),
        key,
    )


# ---------------------------------------------------------------- records


_PRESENT_RECORD: dict[str, object] = {"present": True}


def _member(value: object, allowed: frozenset[str | int | bool]) -> bool:
    # Exact type match: `True == 1` and `1 == 1.0` must not alias closed values.
    return type(value) in {str, int, bool} and any(
        type(item) is type(value) and item == value for item in allowed
    )


def value_in_bounds(value: object) -> bool:
    """Return whether a `VALUE` argument outside a closed set may be exported."""
    if type(value) is bool:
        return True
    if type(value) is int:
        return abs(value) < _MAX_SAFE_INT
    if type(value) is float:
        return math.isfinite(value)
    return (
        type(value) is str
        and 0 < len(value) <= MAX_VALUE_LENGTH
        and value.isprintable()
        and value == value.strip()
    )


def _value_record(value: object, cls: ArgClass) -> dict[str, object] | None:
    if cls.allowed is None:
        ok = type(value) in cls.scalars and value_in_bounds(value)
        return {"value": value} if ok else None
    if _member(value, cls.allowed):
        return {"value": value}
    if (
        cls.is_list
        and isinstance(value, list)
        and len(value) <= MAX_VALUE_LIST
        and all(_member(item, cls.allowed) for item in value)
    ):
        return {"value": sorted(value, key=_dump)}
    return None


def _hashed_record(
    name: str, value: object, cls: ArgClass, keys: ArgKeys | None
) -> dict[str, object]:
    items = list(value) if cls.is_list and isinstance(value, (list, tuple)) else None
    record: dict[str, object] = {}
    if items is not None:
        if len(items) > MAX_LIST_ITEMS:
            raise _Unsupported
        record["count"] = len(items)
    if keys is None:
        return {**record, "present": True}
    canonical = canonical_bytes(value) if items is None else _canonical_items(items)
    if canonical is None:
        return _PRESENT_RECORD
    record["digest"] = eq_hex(keys.k_eq, canonical)
    k_fp = keys.k_fp.get(name) if cls.mode is TraceArg.FINGERPRINT else None
    if k_fp is not None:
        fingerprint = (
            fp_list(items, k_fp)
            if items is not None
            else fp_text(value, k_fp)
            if isinstance(value, str)
            else None
        )
        if fingerprint is not None:
            record["similarity"] = fingerprint
    return record


def _record(
    name: str, value: object, cls: ArgClass, keys: ArgKeys | None
) -> dict[str, object]:
    if cls.mode is TraceArg.PRESENCE:
        return _PRESENT_RECORD
    if cls.mode is TraceArg.VALUE and (record := _value_record(value, cls)) is not None:
        return record
    return _hashed_record(name, value, cls, keys)


def build_records(
    prefix: str,
    args: Mapping[str, object],
    classes: Mapping[str, ArgClass],
    keys: ArgKeys | None,
) -> dict[str, str]:
    """Return `{"<prefix>.arg.<name>": json}` for each traced argument present.

    Arguments that are absent, `None`, `OMIT`, or not in `classes` get no record.
    A failure is confined to its argument, which becomes `{"present": true}`.
    """
    records: dict[str, str] = {}
    for name, cls in classes.items():
        if cls.mode is TraceArg.OMIT or args.get(name) is None:
            continue
        try:
            encoded = _dump(_record(name, args[name], cls, keys))
            if len(encoded) > MAX_RECORD_LENGTH:
                encoded = _dump(_PRESENT_RECORD)
        except Exception:
            encoded = _dump(_PRESENT_RECORD)
        records[f"{prefix}.arg.{name}"] = encoded
    return records


# ---------------------------------------------------------------- validation


def is_arg_key(prefix: str, key: str) -> bool:
    """Return whether `key` belongs to the argument-tracing family."""
    family = prefix + ".arg"
    return key.startswith(family + ".") or key in {
        family + "_hash_status",
        family + "_key_scope",
        family + "_scope_id",
        family + "_trace_dropped",
    }


def _parse_record(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, str) or len(raw) > MAX_RECORD_LENGTH:
        return None
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if (
        not isinstance(record, dict)
        or not record
        or not record.keys() <= ALLOWED_RECORD_KEYS
    ):
        return None
    try:
        if _dump(record) != raw:
            return None
    except ValueError:
        return None
    return record


def _shapes(cls: ArgClass) -> list[set[str]]:
    """Return every key set a non-`value` record of this class may have."""
    shapes = [{"present"}]
    if cls.mode is TraceArg.PRESENCE:
        return shapes
    similar = cls.mode is TraceArg.FINGERPRINT
    shapes.append({"digest"})
    if similar:
        shapes.append({"digest", "similarity"})
    if cls.is_list:
        shapes += [{"count", "present"}, {"count", "digest"}]
        if similar:
            shapes.append({"count", "digest", "similarity"})
    return shapes


def _is_hex32(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX32.fullmatch(value))


def _record_ok(record: dict[str, object], cls: ArgClass) -> bool:
    keys = set(record)
    if "value" in keys:
        return (
            cls.mode is TraceArg.VALUE
            and keys == {"value"}
            and _value_record(record["value"], cls) == record
        )
    count = record.get("count", 0)
    return (
        keys in _shapes(cls)
        and record.get("present", True) is True
        and ("digest" not in keys or _is_hex32(record["digest"]))
        and ("similarity" not in keys or _is_hex32(record["similarity"]))
        and type(count) is int
        and 0 <= count <= MAX_COUNT
    )


def validate(
    prefix: str,
    attrs: Mapping[str, object],
    classes: Mapping[str, ArgClass] | None,
) -> tuple[dict[str, str], int]:
    """Return the argument-tracing attributes that may be exported, and the dropped count.

    `classes` is the tool's classification, or `None` for a tool that has none,
    which drops everything. Keys outside the family are ignored. An incoming
    `arg_trace_dropped` is never trusted or counted.
    """
    family = prefix + ".arg"
    new = {
        key: value
        for key, value in attrs.items()
        if is_arg_key(prefix, key) and key != family + "_trace_dropped"
    }
    if classes is None:
        return {}, len(new)
    tracing = new.get(family + "_hash_status")
    if not (isinstance(tracing, str) and tracing in HASH_STATUSES):
        tracing = None
    accepted: dict[str, str] = {}
    for key, value in new.items():
        if key.startswith(family + "."):
            cls = classes.get(key[len(family) + 1 :])
            record = _parse_record(value)
            # Digests only exist with keys; anything else is forged or a bug.
            ok = (
                cls is not None
                and cls.mode is not TraceArg.OMIT
                and record is not None
                and _record_ok(record, cls)
                and ("digest" not in record or tracing == "ok")
            )
        elif key == family + "_hash_status":
            ok = tracing is not None
        elif key == family + "_key_scope":
            ok = (
                isinstance(value, str)
                and value in KEY_SCOPES
                and tracing is not None
                and (value == "none") == (tracing != "ok")
            )
        else:
            ok = (
                isinstance(value, str)
                and bool(_HEX16.fullmatch(value))
                and tracing == "ok"
            )
        if ok:
            accepted[key] = value
    if family + "_key_scope" not in accepted:
        accepted.pop(family + "_scope_id", None)
    return accepted, len(new) - len(accepted)


# ---------------------------------------------------------------- per-app state


class ArgTracer:
    """Argument tracing for one app: configuration, key, and classification cache."""

    def __init__(
        self,
        prefix: str,
        *,
        default: TraceArg = TraceArg.HASH,
        key: bytes | Callable[[], bytes | None] | None = None,
        skip: Iterable[str] = (),
    ) -> None:
        """Bind the attribute prefix, the default mode, the key, and names to skip.

        `skip` names are never arguments (the captured `intent`, for example).
        """
        self.prefix = prefix
        try:
            # Also accepts a marker's string value, such as `"omit"`.
            self._default = TraceArg(default)
        except ValueError:
            logger.warning(
                "Argument tracing: `arg_default` is not a `TraceArg`; "
                "undeclared arguments are recorded as presence"
            )
            self._default = TraceArg.PRESENCE
        self._key = key
        self._key_resolved = False
        self._master: bytes | None = None
        self._skip = frozenset(skip)
        self._classes: dict[str, list[tuple[object, dict[str, ArgClass]]]] = {}

    def classes(
        self, tool: str, func: Callable[..., object] | None, names: Iterable[str]
    ) -> dict[str, ArgClass]:
        """Return the tool's classification, computed once per registered function."""
        wanted = frozenset(names) - self._skip
        versions = self._classes.setdefault(tool, [])
        for known, classes in versions:
            if known is func and classes.keys() == wanted:
                return classes
        classes = classify_tool(func, wanted, default=self._default, tool=tool)
        # The newest few are kept, not only the last: a span is re-validated
        # when it is exported, after another version of the tool may have run.
        versions.append((func, classes))
        del versions[:-MAX_TOOL_VERSIONS]
        return classes

    def _master_key(self) -> bytes | None:
        if not self._key_resolved:
            self._key_resolved = True
            try:
                literal = isinstance(self._key, bytes) or self._key is None
                key = self._key if literal else self._key()
            except Exception:
                key = b""
            if isinstance(key, bytes) and len(key) == KEY_LENGTH:
                self._master = key
            elif key is not None:
                logger.warning(
                    "Argument tracing: `arg_key` is not %d bytes; "
                    "argument hashes are disabled",
                    KEY_LENGTH,
                )
        return self._master

    def record(
        self,
        tool: str,
        func: Callable[..., object] | None,
        names: Iterable[str],
        args: Mapping[str, object] | None,
        *,
        principal: str | None,
        session_digest: str | None = None,
        client_name: str = "",
        client_version: str = "",
        now: float,
    ) -> dict[str, str]:
        """Return the argument records and tracing state for one call; never raises."""
        family = self.prefix + ".arg"
        try:
            classes = self.classes(tool, func, names)
            master = self._master_key()
            keys: ArgKeys | None = None
            status = "no_key" if master is None else "no_scope"
            if master is not None and principal:
                kind, scope = scope_input(
                    principal, session_digest, client_name, client_version, now
                )
                fp_args = [
                    name
                    for name, cls in classes.items()
                    if cls.mode is TraceArg.FINGERPRINT
                ]
                keys = derive_keys(master, self.prefix, kind, scope, tool, fp_args)
                status = "ok"
            attrs = build_records(self.prefix, args or {}, classes, keys)
            attrs[family + "_hash_status"] = status
            attrs[family + "_key_scope"] = keys.kind if keys else "none"
            if keys is not None:
                attrs[family + "_scope_id"] = scope_id(self.prefix, keys.k_eq)
        except Exception as exc:
            logger.debug("Argument tracing failed: %s", type(exc).__name__)
            return {family + "_hash_status": "error", family + "_key_scope": "none"}
        return attrs

    def revalidate(
        self, tool: str | None, attrs: Mapping[str, object]
    ) -> dict[str, str | int]:
        """Return the argument-tracing attributes to export for a span; never raises.

        Called at the export boundary. The caller removes every `is_arg_key`
        attribute from the span and adds back only what this returns. A tool
        name registered in several versions is checked against each, and the
        one that keeps the most wins.
        """
        try:
            versions = self._classes.get(tool, []) if tool is not None else []
            candidates = [classes for _, classes in versions] or [None]
            accepted, dropped = min(
                (validate(self.prefix, attrs, classes) for classes in candidates),
                key=lambda outcome: outcome[1],
            )
        except Exception:
            return {}
        result: dict[str, str | int] = dict(accepted)
        if dropped:
            result[self.prefix + ".arg_trace_dropped"] = dropped
        return result
