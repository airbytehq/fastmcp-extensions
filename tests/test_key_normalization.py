# Copyright (c) 2025 Airbyte, Inc., all rights reserved.
"""Tests for `fastmcp_extensions.key_normalization`."""

from __future__ import annotations

import re

import pytest
from key_value.aio.stores.memory import MemoryStore

from fastmcp_extensions.key_normalization import (
    DEFAULT_KEY_PREFIX,
    HashKeyNormalizer,
    KeyNormalizer,
    NormalizedKeysWrapper,
)

# Keys that are illegal as a Firestore document ID (or another backend's key)
# when used verbatim, plus already-legal controls. These are exactly the shapes
# `OIDCProxy` feeds its client store: URL `client_id`s and base64 tokens.
_TRICKY_KEYS = [
    pytest.param(
        "https://goose-docs.ai/oauth/client-metadata.json", id="url_client_id"
    ),
    pytest.param("a/b/c", id="slashes"),
    pytest.param("tok+en/with=base64", id="std_base64_token"),
    pytest.param(".", id="dot"),
    pytest.param("..", id="dotdot"),
    pytest.param("__reserved__", id="reserved"),
    pytest.param("simple-uuid-1234", id="already_legal"),
    pytest.param("x" * 5000, id="very_long"),
    pytest.param("ünîcodé-key", id="unicode"),
    pytest.param("", id="empty"),
]

# A legal Firestore document ID: no `/`, not `.`/`..`, not `__.*__`, non-empty.
_FIRESTORE_ILLEGAL = re.compile(r"/|^\.\.?$|^__.*__$")


@pytest.mark.parametrize("key", _TRICKY_KEYS)
def test_hash_normalizer_produces_legal_key(key: str) -> None:
    normalized = HashKeyNormalizer().normalize(key)
    assert "/" not in normalized
    assert not _FIRESTORE_ILLEGAL.search(normalized)
    assert normalized.startswith(DEFAULT_KEY_PREFIX)
    assert re.fullmatch(r"k-[A-Za-z0-9_-]+", normalized)


@pytest.mark.parametrize("key", _TRICKY_KEYS)
def test_hash_normalizer_is_deterministic(key: str) -> None:
    assert HashKeyNormalizer().normalize(key) == HashKeyNormalizer().normalize(key)


def test_hash_normalizer_is_fixed_length_regardless_of_input() -> None:
    normalizer = HashKeyNormalizer()
    lengths = {len(normalizer.normalize(k)) for k in ("a", "x" * 5000, "a/b/c")}
    assert len(lengths) == 1


def test_hash_normalizer_maps_distinct_keys_distinctly() -> None:
    normalizer = HashKeyNormalizer()
    assert normalizer.normalize("client-a") != normalizer.normalize("client-b")


def test_hash_normalizer_custom_prefix() -> None:
    assert HashKeyNormalizer(prefix="oauth-").normalize("k").startswith("oauth-")


def test_hash_normalizer_satisfies_protocol() -> None:
    assert isinstance(HashKeyNormalizer(), KeyNormalizer)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", _TRICKY_KEYS)
async def test_wrapper_round_trips_tricky_keys(key: str) -> None:
    store = MemoryStore()
    wrapped = NormalizedKeysWrapper(key_value=store)

    await wrapped.put(key=key, value={"v": 1})
    assert await wrapped.get(key=key) == {"v": 1}

    deleted = await wrapped.delete(key=key)
    assert deleted is True
    assert await wrapped.get(key=key) is None


@pytest.mark.asyncio
async def test_wrapper_stores_under_normalized_key_only() -> None:
    key = "https://goose-docs.ai/oauth/client-metadata.json"
    store = MemoryStore()
    wrapped = NormalizedKeysWrapper(key_value=store)

    await wrapped.put(key=key, value={"v": 1})

    # The raw (illegal) key never reaches the underlying store; the normalized
    # one does.
    assert await store.get(key=key) is None
    assert await store.get(key=HashKeyNormalizer().normalize(key)) == {"v": 1}


@pytest.mark.asyncio
async def test_wrapper_defaults_to_hash_normalizer() -> None:
    assert isinstance(
        NormalizedKeysWrapper(key_value=MemoryStore()).normalizer, HashKeyNormalizer
    )


@pytest.mark.asyncio
async def test_wrapper_many_operations_round_trip() -> None:
    keys = ["a/b", "https://x.example/y", "plain"]
    store = MemoryStore()
    wrapped = NormalizedKeysWrapper(key_value=store)

    await wrapped.put_many(keys=keys, values=[{"n": i} for i in range(len(keys))])
    got = await wrapped.get_many(keys=keys)
    assert got == [{"n": 0}, {"n": 1}, {"n": 2}]

    assert await wrapped.delete_many(keys=keys) == len(keys)
    assert await wrapped.get_many(keys=keys) == [None, None, None]
