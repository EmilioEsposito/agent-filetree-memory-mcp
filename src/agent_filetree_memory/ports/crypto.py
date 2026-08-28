"""Envelope-encryption provider contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class EncryptionContext:
    purpose: str
    workspace_id: str
    agent_profile_id: str
    object_id: str
    object_kind: str
    version: int
    service_namespace: str = "agent-filetree-memory"
    format_version: int = 1

    def as_mapping(self) -> Mapping[str, str]:
        return {
            "format": str(self.format_version),
            "purpose": self.purpose,
            "namespace": self.service_namespace,
            "workspace": self.workspace_id,
            "agent": self.agent_profile_id,
            "object": self.object_id,
            "kind": self.object_kind,
            "version": str(self.version),
        }


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    ciphertext: bytes
    wrapped_dek: bytes
    provider_id: str
    key_id: str
    format_version: int = 1


class DekProvider(Protocol):
    provider_id: str

    async def wrap_dek(
        self, dek: bytes, context: Mapping[str, str]
    ) -> tuple[bytes, str]: ...

    async def unwrap_dek(
        self, wrapped_dek: bytes, *, key_id: str, context: Mapping[str, str]
    ) -> bytes: ...
