"""Explicit local keyring provider for development and simple deployments."""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from types import MappingProxyType

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..domain.errors import ConfigurationError, IntegrityFailure
from ..domain.models import validate_opaque_id
from .envelope import _canonical_aad, _split_framed

_WRAPPED_DEK_PREFIX = b"AFMK\x01"
_NONCE_BYTES = 12


class LocalKeyringDekProvider:
    """Wrap data keys with one active KEK from an explicit base64 keyring.

    ``keys`` must map stable key identifiers to standard-base64 encodings of
    exactly 32 random bytes.  Keep retired keys present for reads while setting
    ``active_key_id`` to the key used for new writes.
    """

    provider_id = "local-keyring-aes256gcm-v1"

    def __init__(self, keys: Mapping[str, str], *, active_key_id: str) -> None:
        if not isinstance(keys, Mapping) or not keys or len(keys) > 64:
            raise ConfigurationError("local keyring configuration is invalid")
        parsed: dict[str, bytes] = {}
        try:
            for key_id, encoded_key in keys.items():
                validate_opaque_id(key_id, field="key_id")
                if not isinstance(encoded_key, str):
                    raise ValueError
                ascii_key = encoded_key.encode("ascii")
                decoded = base64.b64decode(ascii_key, validate=True)
                if len(decoded) != 32 or base64.b64encode(decoded) != ascii_key:
                    raise ValueError
                parsed[key_id] = decoded
            validate_opaque_id(active_key_id, field="active_key_id")
        except (TypeError, UnicodeError, ValueError):
            raise ConfigurationError("local keyring configuration is invalid") from None
        if active_key_id not in parsed:
            raise ConfigurationError("local keyring configuration is invalid")
        self._keys = MappingProxyType(parsed)
        self.active_key_id = active_key_id

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    async def wrap_dek(
        self, dek: bytes, context: Mapping[str, str]
    ) -> tuple[bytes, str]:
        if not isinstance(dek, bytes) or len(dek) != 32:
            raise ConfigurationError("data-encryption key is invalid")
        try:
            aad = _canonical_aad(context, domain="dek-wrap")
        except (TypeError, ValueError):
            raise ConfigurationError("encryption context is invalid") from None
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._keys[self.active_key_id]).encrypt(nonce, dek, aad)
        return _WRAPPED_DEK_PREFIX + nonce + wrapped, self.active_key_id

    async def unwrap_dek(
        self,
        wrapped_dek: bytes,
        *,
        key_id: str,
        context: Mapping[str, str],
    ) -> bytes:
        try:
            wrapping_key = self._keys.get(key_id)
            if wrapping_key is None:
                raise ValueError
            aad = _canonical_aad(context, domain="dek-wrap")
            nonce, ciphertext = _split_framed(
                wrapped_dek, prefix=_WRAPPED_DEK_PREFIX
            )
            dek = AESGCM(wrapping_key).decrypt(nonce, ciphertext, aad)
            if len(dek) != 32:
                raise ValueError
            return dek
        except (InvalidTag, KeyError, TypeError, ValueError):
            raise IntegrityFailure("encrypted value failed authentication") from None
