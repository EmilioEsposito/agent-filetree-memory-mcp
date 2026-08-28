from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from agent_filetree_memory.crypto import (
    EnvelopeEncryptor,
    LocalKeyringDekProvider,
    canonical_encryption_context,
)
from agent_filetree_memory.domain.errors import ConfigurationError, IntegrityFailure
from agent_filetree_memory.ports.crypto import EncryptionContext


def encoded_key(byte: int) -> str:
    return base64.b64encode(bytes([byte]) * 32).decode("ascii")


def context(**changes: object) -> EncryptionContext:
    value = EncryptionContext(
        purpose="document-version",
        workspace_id="workspace-1",
        agent_profile_id="agent-1",
        object_id="object-1",
        object_kind="markdown-body",
        version=1,
    )
    return replace(value, **changes)


async def test_round_trip_text_and_binary_values() -> None:
    provider = LocalKeyringDekProvider({"key-1": encoded_key(1)}, active_key_id="key-1")
    encryptor = EnvelopeEncryptor(provider)

    text_payload = await encryptor.encrypt_text("hello 🐿️", context())
    binary_payload = await encryptor.encrypt(
        b"\x00\xffbytes", context(object_kind="blob")
    )

    assert await encryptor.decrypt_text(text_payload, context()) == "hello 🐿️"
    assert (
        await encryptor.decrypt(binary_payload, context(object_kind="blob"))
        == b"\x00\xffbytes"
    )


async def test_each_value_uses_a_fresh_dek_and_nonces() -> None:
    provider = LocalKeyringDekProvider({"key-1": encoded_key(2)}, active_key_id="key-1")
    encryptor = EnvelopeEncryptor(provider)
    encryption_context = context()

    first = await encryptor.encrypt(b"same", encryption_context)
    second = await encryptor.encrypt(b"same", encryption_context)
    first_dek = await provider.unwrap_dek(
        first.wrapped_dek,
        key_id=first.key_id,
        context=encryption_context.as_mapping(),
    )
    second_dek = await provider.unwrap_dek(
        second.wrapped_dek,
        key_id=second.key_id,
        context=encryption_context.as_mapping(),
    )

    assert first.ciphertext != second.ciphertext
    assert first.wrapped_dek != second.wrapped_dek
    assert first_dek != second_dek


async def test_rotation_reads_old_values_and_writes_with_active_key() -> None:
    old_provider = LocalKeyringDekProvider(
        {"old": encoded_key(3)}, active_key_id="old"
    )
    old_payload = await EnvelopeEncryptor(old_provider).encrypt_text("old", context())

    rotated_provider = LocalKeyringDekProvider(
        {"old": encoded_key(3), "new": encoded_key(4)}, active_key_id="new"
    )
    restarted = EnvelopeEncryptor(rotated_provider)
    new_payload = await restarted.encrypt_text("new", context(version=2))

    assert await restarted.decrypt_text(old_payload, context()) == "old"
    assert await restarted.decrypt_text(new_payload, context(version=2)) == "new"
    assert old_payload.key_id == "old"
    assert new_payload.key_id == "new"


async def test_restart_with_same_explicit_keyring_can_decrypt() -> None:
    keys = {"key-1": encoded_key(5)}
    first_process = EnvelopeEncryptor(
        LocalKeyringDekProvider(keys, active_key_id="key-1")
    )
    payload = await first_process.encrypt_text("survives restart", context())
    second_process = EnvelopeEncryptor(
        LocalKeyringDekProvider(dict(keys), active_key_id="key-1")
    )

    assert await second_process.decrypt_text(payload, context()) == "survives restart"


def test_canonical_context_is_stable_and_versioned() -> None:
    assert canonical_encryption_context(context()) == (
        b'{"agent":"agent-1","format":"1","kind":"markdown-body",'
        b'"namespace":"agent-filetree-memory","object":"object-1",'
        b'"purpose":"document-version","version":"1","workspace":"workspace-1"}'
    )


@pytest.mark.parametrize(
    "keys,active",
    [
        ({}, "key-1"),
        ({"key-1": "not-base64"}, "key-1"),
        ({"key-1": base64.b64encode(b"short").decode()}, "key-1"),
        ({"key-1": encoded_key(1)}, "missing"),
    ],
)
def test_local_keyring_fails_closed_on_invalid_configuration(
    keys: dict[str, str], active: str
) -> None:
    with pytest.raises(ConfigurationError, match="configuration is invalid"):
        LocalKeyringDekProvider(keys, active_key_id=active)


async def test_decrypt_text_rejects_authenticated_non_utf8() -> None:
    provider = LocalKeyringDekProvider({"key-1": encoded_key(6)}, active_key_id="key-1")
    encryptor = EnvelopeEncryptor(provider)
    payload = await encryptor.encrypt(b"\xff", context())

    with pytest.raises(IntegrityFailure, match="failed authentication"):
        await encryptor.decrypt_text(payload, context())
