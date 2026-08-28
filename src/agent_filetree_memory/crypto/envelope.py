"""AES-256-GCM envelope encryption for immutable memory values."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..domain.errors import ConfigurationError, IntegrityFailure
from ..ports.crypto import DekProvider, EncryptedPayload, EncryptionContext

_FORMAT_VERSION = 1
_CONTENT_PREFIX = b"AFMC\x01"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CONTEXT_FIELDS = frozenset(
    {
        "format",
        "purpose",
        "workspace",
        "agent",
        "object",
        "kind",
        "namespace",
        "version",
    }
)
_MAX_CONTEXT_BYTES = 16 * 1024


class EnvelopeEncryptor:
    """Encrypt each value with a fresh data-encryption key."""

    def __init__(self, provider: DekProvider) -> None:
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise ConfigurationError("data-key provider configuration is invalid")
        self._provider = provider
        self.provider_id = provider_id

    async def encrypt(
        self, plaintext: bytes, context: EncryptionContext
    ) -> EncryptedPayload:
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        try:
            context_mapping = _context_mapping(context)
            aad = _canonical_aad(context_mapping, domain="content")
        except (TypeError, ValueError):
            raise ConfigurationError("encryption context is invalid") from None

        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = _CONTENT_PREFIX + nonce + AESGCM(dek).encrypt(
            nonce, plaintext, aad
        )
        try:
            wrapped_dek, key_id = await self._provider.wrap_dek(dek, context_mapping)
        except (ConfigurationError, IntegrityFailure):
            raise
        except (TypeError, ValueError):
            raise ConfigurationError("data-key provider failed") from None
        if (
            not isinstance(wrapped_dek, bytes)
            or not wrapped_dek
            or not isinstance(key_id, str)
            or not key_id
        ):
            raise ConfigurationError("data-key provider returned invalid data")
        return EncryptedPayload(
            ciphertext=ciphertext,
            wrapped_dek=wrapped_dek,
            provider_id=self.provider_id,
            key_id=key_id,
            format_version=_FORMAT_VERSION,
        )

    async def decrypt(
        self, payload: EncryptedPayload, context: EncryptionContext
    ) -> bytes:
        try:
            if not isinstance(payload, EncryptedPayload):
                raise ValueError
            if (
                payload.provider_id != self.provider_id
                or payload.format_version != _FORMAT_VERSION
            ):
                raise ValueError
            context_mapping = _context_mapping(context)
            aad = _canonical_aad(context_mapping, domain="content")
            nonce, ciphertext = _split_framed(
                payload.ciphertext, prefix=_CONTENT_PREFIX
            )
            dek = await self._provider.unwrap_dek(
                payload.wrapped_dek,
                key_id=payload.key_id,
                context=context_mapping,
            )
            if not isinstance(dek, bytes) or len(dek) != 32:
                raise ValueError
            return AESGCM(dek).decrypt(nonce, ciphertext, aad)
        except IntegrityFailure:
            raise
        except (InvalidTag, KeyError, TypeError, ValueError):
            raise IntegrityFailure("encrypted value failed authentication") from None

    async def encrypt_text(
        self, plaintext: str, context: EncryptionContext
    ) -> EncryptedPayload:
        if not isinstance(plaintext, str):
            raise TypeError("plaintext must be text")
        try:
            encoded = plaintext.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("plaintext must be valid UTF-8 text") from None
        return await self.encrypt(encoded, context)

    async def decrypt_text(
        self, payload: EncryptedPayload, context: EncryptionContext
    ) -> str:
        plaintext = await self.decrypt(payload, context)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise IntegrityFailure("encrypted value failed authentication") from None


def canonical_encryption_context(context: EncryptionContext) -> bytes:
    """Return the stable, versioned JSON representation used for AAD."""
    try:
        return _canonical_json(_context_mapping(context))
    except (TypeError, ValueError):
        raise ValueError("encryption context is invalid") from None


def _context_mapping(context: EncryptionContext) -> Mapping[str, str]:
    if not isinstance(context, EncryptionContext):
        raise TypeError
    if (
        not isinstance(context.format_version, int)
        or isinstance(context.format_version, bool)
        or context.format_version != _FORMAT_VERSION
        or not isinstance(context.version, int)
        or isinstance(context.version, bool)
        or context.version < 1
    ):
        raise ValueError
    return context.as_mapping()


def _canonical_json(context: Mapping[str, str]) -> bytes:
    if not isinstance(context, Mapping) or set(context) != _CONTEXT_FIELDS:
        raise ValueError
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not value
        or "\x00" in value
        for key, value in context.items()
    ):
        raise ValueError
    encoded = json.dumps(
        dict(context),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_CONTEXT_BYTES:
        raise ValueError
    return encoded


def _canonical_aad(context: Mapping[str, str], *, domain: str) -> bytes:
    # Domain separation prevents a wrapped DEK from authenticating as content,
    # while both remain bound to the identical canonical context.
    return (
        b"agent-filetree-memory\x00"
        + domain.encode("ascii")
        + b"\x00"
        + _canonical_json(context)
    )


def _split_framed(value: bytes, *, prefix: bytes) -> tuple[bytes, bytes]:
    minimum = len(prefix) + _NONCE_BYTES + _TAG_BYTES
    if (
        not isinstance(value, bytes)
        or len(value) < minimum
        or not value.startswith(prefix)
    ):
        raise ValueError
    nonce_start = len(prefix)
    ciphertext_start = nonce_start + _NONCE_BYTES
    return value[nonce_start:ciphertext_start], value[ciphertext_start:]
