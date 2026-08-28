"""Ports through which trusted hosts provide verified invocation identity."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol

from ..domain.models import MemoryAction, VerifiedInvocation


class CapabilityVerifier(Protocol):
    def verify(
        self,
        token: str,
        *,
        required_action: MemoryAction,
        expected_principal_id: str,
        now: datetime | None = None,
    ) -> VerifiedInvocation: ...


InvocationResolver = Callable[[Any, MemoryAction], Awaitable[VerifiedInvocation]]
