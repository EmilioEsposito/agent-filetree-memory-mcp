from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from agent_filetree_memory.crypto import EnvelopeEncryptor, LocalKeyringDekProvider
from agent_filetree_memory.domain.errors import IntegrityFailure
from agent_filetree_memory.ports.crypto import EncryptedPayload, EncryptionContext


KEY = base64.b64encode(bytes(range(32))).decode("ascii")
OTHER_KEY = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")
CONTEXT = EncryptionContext(
    purpose="document-version",
    workspace_id="workspace-1",
    agent_profile_id="agent-1",
    object_id="opaque-object-1",
    object_kind="markdown-body",
    version=3,
)


def flip_last(value: bytes) -> bytes:
    return value[:-1] + bytes([value[-1] ^ 1])


@pytest.mark.parametrize(
    "field",
    [
        "purpose",
        "workspace_id",
        "agent_profile_id",
        "object_id",
        "object_kind",
        "service_namespace",
        "version",
    ],
)
async def test_ciphertext_cannot_be_moved_to_another_context(field: str) -> None:
    encryptor = EnvelopeEncryptor(
        LocalKeyringDekProvider({"key-1": KEY}, active_key_id="key-1")
    )
    payload = await encryptor.encrypt_text("do not disclose", CONTEXT)
    replacement: object = 4 if field == "version" else f"different-{field}"

    with pytest.raises(IntegrityFailure, match="failed authentication"):
        await encryptor.decrypt_text(payload, replace(CONTEXT, **{field: replacement}))


@pytest.mark.parametrize("target", ["ciphertext", "wrapped_dek"])
async def test_tampering_is_detected_with_one_error_shape(target: str) -> None:
    encryptor = EnvelopeEncryptor(
        LocalKeyringDekProvider({"key-1": KEY}, active_key_id="key-1")
    )
    payload = await encryptor.encrypt_text("plaintext-marker", CONTEXT)
    tampered = replace(payload, **{target: flip_last(getattr(payload, target))})

    with pytest.raises(IntegrityFailure) as error:
        await encryptor.decrypt_text(tampered, CONTEXT)

    assert str(error.value) == "encrypted value failed authentication"
    assert "plaintext-marker" not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "change",
    [
        {"provider_id": "unexpected-provider"},
        {"key_id": "missing-key"},
        {"format_version": 999},
        {"ciphertext": b"malformed"},
        {"wrapped_dek": b"malformed"},
    ],
)
async def test_malformed_envelopes_fail_without_disclosing_details(
    change: dict[str, object],
) -> None:
    encryptor = EnvelopeEncryptor(
        LocalKeyringDekProvider({"key-1": KEY}, active_key_id="key-1")
    )
    payload = await encryptor.encrypt_text("plaintext-marker", CONTEXT)
    malformed = replace(payload, **change)

    with pytest.raises(IntegrityFailure) as error:
        await encryptor.decrypt_text(malformed, CONTEXT)

    assert str(error.value) == "encrypted value failed authentication"
    assert error.value.__cause__ is None


async def test_removed_rotation_key_fails_closed() -> None:
    original = EnvelopeEncryptor(
        LocalKeyringDekProvider({"old": KEY}, active_key_id="old")
    )
    payload = await original.encrypt_text("old content", CONTEXT)
    after_removal = EnvelopeEncryptor(
        LocalKeyringDekProvider({"new": OTHER_KEY}, active_key_id="new")
    )

    with pytest.raises(IntegrityFailure, match="failed authentication"):
        await after_removal.decrypt_text(payload, CONTEXT)


async def test_wrong_key_material_under_same_key_id_fails_closed() -> None:
    original = EnvelopeEncryptor(
        LocalKeyringDekProvider({"key-1": KEY}, active_key_id="key-1")
    )
    payload = await original.encrypt_text("old content", CONTEXT)
    wrong_process = EnvelopeEncryptor(
        LocalKeyringDekProvider({"key-1": OTHER_KEY}, active_key_id="key-1")
    )

    with pytest.raises(IntegrityFailure, match="failed authentication"):
        await wrong_process.decrypt_text(payload, CONTEXT)
