# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Normalize arbitrary keys into ones every key-value backend will accept.

The interactive `OIDCProxy` keys its durable client store by the OAuth
`client_id`, and that value is attacker/peer-influenced: the CIMD flow (e.g.
Goose Desktop) passes a *metadata-document URL* as the `client_id`, and OAuth
tokens are commonly standard base64 — both contain `/`. Handed verbatim to a
durable store, such keys are frequently illegal: a `key_value.aio.stores.
firestore.FirestoreStore` uses the key as the Firestore *document ID*, and `/`
makes an illegal path (`InvalidArgument: ... lacks a collection id`), crashing
`/authorize` before any OAuth logic runs. Other backends have their own limits
(length caps, reserved names, charset rules).

This module provides a generic, backend-agnostic fix that composes into the
`key_value` wrapper chain like any other wrapper:

- `KeyNormalizer` — a one-way `normalize(key) -> str` strategy.
- `HashKeyNormalizer` — the default strategy: a fixed-length, always-legal
  digest of the key.
- `NormalizeKeysWrapper` — an `AsyncKeyValue` wrapper that routes every key
  through a `KeyNormalizer` before delegating, mirroring the shape of the
  upstream `PrefixKeysWrapper`.

Normalization is intentionally **one-way**. The stores are only ever read by a
known key (`get`/`put`/`delete`/`ttl` for a `client_id` the caller already
holds); nothing enumerates the raw keys back out. Committing to one-way lets the
default strategy *hash* — which yields a constant-length, always-legal result
regardless of the input's length or charset, so the "any key is storable"
guarantee is total rather than best-effort. A reversible (e.g. base64) strategy
can be plugged in when human-decodable document IDs are worth the trade-off.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any, Protocol, SupportsFloat, runtime_checkable

from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.wrappers.base import BaseWrapper

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Default prefix applied to every normalized key. A fixed, non-empty prefix
# guarantees the result can never be empty, `.`/`..`, or match a reserved
# pattern such as Firestore's `__.*__`, independent of the strategy's output.
DEFAULT_KEY_PREFIX = "k-"

# Default digest algorithm for `HashKeyNormalizer`. SHA-256 is collision
# resistant for this purpose and its 32-byte digest is short after encoding.
DEFAULT_HASH_ALGORITHM = "sha256"


@runtime_checkable
class KeyNormalizer(Protocol):
    """A one-way mapping from an arbitrary key to a backend-legal key.

    Implementations must be deterministic — the same input always maps to the
    same output — so that `get`/`put`/`delete` for a given logical key resolve
    the same stored key across processes and replicas. The mapping is not
    required to be reversible.
    """

    def normalize(self, key: str) -> str:
        """Return a deterministic, backend-legal key for `key`."""
        ...


class HashKeyNormalizer:
    """Normalize keys to a fixed-length, always-legal digest.

    Hashes the key and encodes the digest with URL-safe base64 (charset
    `A-Za-z0-9-_`, padding stripped), then prepends `prefix`. The result is
    constant-length and contains no `/`, `.`, or reserved sequences, so it is a
    legal key/document ID for every backend regardless of the input's length or
    charset. Being one-way, the original key cannot be recovered from the
    result — use a reversible normalizer if human-decodable IDs are needed.
    """

    def __init__(
        self,
        *,
        prefix: str = DEFAULT_KEY_PREFIX,
        algorithm: str = DEFAULT_HASH_ALGORITHM,
    ) -> None:
        self.prefix = prefix
        self.algorithm = algorithm

    def normalize(self, key: str) -> str:
        digest = hashlib.new(self.algorithm, key.encode("utf-8")).digest()
        encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return self.prefix + encoded


class NormalizeKeysWrapper(BaseWrapper):
    """Route every key through a `KeyNormalizer` before delegating.

    Wraps any `AsyncKeyValue` so that arbitrary string keys — URL `client_id`s,
    base64 tokens — become legal for the underlying store. Values are untouched;
    only the keyspace changes. Defaults to `HashKeyNormalizer`, which makes any
    key storable in any backend. Compose it in the wrapper chain like any other
    `key_value` wrapper, e.g. `FernetEncryptionWrapper(NormalizeKeysWrapper(
    store))` to normalize keys and encrypt values.
    """

    def __init__(
        self,
        key_value: AsyncKeyValue,
        normalizer: KeyNormalizer | None = None,
    ) -> None:
        self.key_value: AsyncKeyValue = key_value
        self.normalizer: KeyNormalizer = normalizer or HashKeyNormalizer()
        super().__init__()

    def _normalize(self, key: str) -> str:
        return self.normalizer.normalize(key)

    async def get(
        self, key: str, *, collection: str | None = None
    ) -> dict[str, Any] | None:
        return await self.key_value.get(key=self._normalize(key), collection=collection)

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        return await self.key_value.get_many(
            keys=[self._normalize(key) for key in keys], collection=collection
        )

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        return await self.key_value.ttl(key=self._normalize(key), collection=collection)

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        return await self.key_value.ttl_many(
            keys=[self._normalize(key) for key in keys], collection=collection
        )

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        return await self.key_value.put(
            key=self._normalize(key), value=value, collection=collection, ttl=ttl
        )

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        return await self.key_value.put_many(
            keys=[self._normalize(key) for key in keys],
            values=values,
            collection=collection,
            ttl=ttl,
        )

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        return await self.key_value.delete(
            key=self._normalize(key), collection=collection
        )

    async def delete_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> int:
        return await self.key_value.delete_many(
            keys=[self._normalize(key) for key in keys], collection=collection
        )
