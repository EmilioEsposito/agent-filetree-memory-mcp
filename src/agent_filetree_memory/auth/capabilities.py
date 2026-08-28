"""Ed25519-signed, short-lived invocation capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..domain.errors import AuthorizationDenied, ConfigurationError
from ..domain.models import (
    MemoryAction,
    Scope,
    VerifiedInvocation,
    validate_opaque_id,
)

_ALGORITHM = "EdDSA"
_TOKEN_TYPE = "at+jwt"
_CAPABILITY_VERSION = 1
_MAX_TOKEN_BYTES = 16 * 1024
_SCOPE_FIELDS = frozenset({"workspace_id", "agent_profile_id"})
_DENIED_MESSAGE = "memory operation is not authorized"


class AsymmetricCapabilityVerifier:
    """Verify explicitly trusted Ed25519 capability-signing keys.

    Keys are selected only by a protected JWT ``kid`` header.  The verifier
    never downloads keys and never accepts symmetric JWT algorithms, which
    keeps key provenance and rotation under the embedding host's control.
    """

    def __init__(
        self,
        public_keys: Mapping[str, Ed25519PublicKey | bytes | str],
        *,
        issuer: str,
        audience: str,
        max_ttl: timedelta = timedelta(minutes=5),
        max_delegation_depth: int = 0,
        clock_skew: timedelta = timedelta(seconds=5),
    ) -> None:
        if not isinstance(issuer, str) or not issuer:
            raise ConfigurationError("capability verifier configuration is invalid")
        if not isinstance(audience, str) or not audience:
            raise ConfigurationError("capability verifier configuration is invalid")
        if not isinstance(max_ttl, timedelta) or max_ttl <= timedelta(0):
            raise ConfigurationError("capability verifier configuration is invalid")
        if max_ttl > timedelta(hours=1):
            raise ConfigurationError("capability verifier configuration is invalid")
        if (
            not isinstance(clock_skew, timedelta)
            or clock_skew < timedelta(0)
            or clock_skew > timedelta(minutes=1)
        ):
            raise ConfigurationError("capability verifier configuration is invalid")
        if (
            not isinstance(max_delegation_depth, int)
            or isinstance(max_delegation_depth, bool)
            or max_delegation_depth < 0
        ):
            raise ConfigurationError("capability verifier configuration is invalid")
        if not isinstance(public_keys, Mapping) or not public_keys:
            raise ConfigurationError("capability verifier configuration is invalid")

        parsed_keys: dict[str, Ed25519PublicKey] = {}
        try:
            for key_id, public_key in public_keys.items():
                validate_opaque_id(key_id, field="key_id")
                parsed_keys[key_id] = self._load_public_key(public_key)
        except (TypeError, ValueError):
            raise ConfigurationError(
                "capability verifier configuration is invalid"
            ) from None

        self._public_keys = MappingProxyType(parsed_keys)
        self._issuer = issuer
        self._audience = audience
        self._max_ttl = max_ttl
        self._max_delegation_depth = max_delegation_depth
        self._clock_skew = clock_skew

    def verify(
        self,
        token: str,
        *,
        required_action: MemoryAction,
        expected_principal_id: str,
        now: datetime | None = None,
    ) -> VerifiedInvocation:
        """Return a frozen invocation or one non-disclosing denial."""
        try:
            if not isinstance(token, str) or not token:
                raise ValueError
            if len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
                raise ValueError
            if not isinstance(required_action, MemoryAction):
                raise ValueError
            validate_opaque_id(expected_principal_id, field="expected_principal_id")
            checked_at = _aware_utc_now(now)

            header = jwt.get_unverified_header(token)
            if not isinstance(header, dict):
                raise ValueError
            if "crit" in header:
                raise ValueError
            if header.get("alg") != _ALGORITHM or header.get("typ") != _TOKEN_TYPE:
                raise ValueError
            key_id = header.get("kid")
            if not isinstance(key_id, str):
                raise ValueError
            public_key = self._public_keys.get(key_id)
            if public_key is None:
                raise ValueError

            claims = jwt.decode(
                token,
                public_key,
                algorithms=[_ALGORITHM],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "iat",
                        "exp",
                        "jti",
                        "invocation_id",
                        "principal_id",
                        "actions",
                        "delegation_depth",
                        "scope",
                        "capability_version",
                    ],
                    # Numeric dates are checked below against the supplied clock.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            invocation = self._claims_to_invocation(
                claims,
                checked_at=checked_at,
                expected_principal_id=expected_principal_id,
            )
            invocation.require(required_action, now=checked_at)
            return invocation
        except (
            jwt.PyJWTError,
            AuthorizationDenied,
            KeyError,
            OSError,
            OverflowError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise AuthorizationDenied(_DENIED_MESSAGE) from None

    def _claims_to_invocation(
        self,
        claims: Mapping[str, Any],
        *,
        checked_at: datetime,
        expected_principal_id: str,
    ) -> VerifiedInvocation:
        if (
            not isinstance(claims.get("capability_version"), int)
            or isinstance(claims.get("capability_version"), bool)
            or claims.get("capability_version") != _CAPABILITY_VERSION
        ):
            raise ValueError
        # PyJWT accepts a list containing the audience.  Capabilities use one
        # exact audience to avoid broad multi-service bearer tokens.
        if claims.get("iss") != self._issuer or claims.get("aud") != self._audience:
            raise ValueError

        issued_at_seconds = _numeric_date(claims.get("iat"))
        expires_at_seconds = _numeric_date(claims.get("exp"))
        issued_at = datetime.fromtimestamp(issued_at_seconds, tz=timezone.utc)
        expires_at = datetime.fromtimestamp(expires_at_seconds, tz=timezone.utc)
        if expires_at <= issued_at or expires_at - issued_at > self._max_ttl:
            raise ValueError
        if issued_at > checked_at + self._clock_skew or checked_at >= expires_at:
            raise ValueError
        if "nbf" in claims:
            not_before = datetime.fromtimestamp(
                _numeric_date(claims.get("nbf")), tz=timezone.utc
            )
            if not_before > checked_at + self._clock_skew:
                raise ValueError

        scope_claim = claims.get("scope")
        if not isinstance(scope_claim, dict) or set(scope_claim) != _SCOPE_FIELDS:
            raise ValueError
        scope = Scope(
            workspace_id=scope_claim["workspace_id"],
            agent_profile_id=scope_claim["agent_profile_id"],
        )

        action_claims = claims.get("actions")
        if (
            not isinstance(action_claims, list)
            or not action_claims
            or len(action_claims) > len(MemoryAction)
            or any(not isinstance(action, str) for action in action_claims)
        ):
            raise ValueError
        actions = frozenset(MemoryAction(action) for action in action_claims)
        if len(actions) != len(action_claims):
            raise ValueError

        delegation_depth = claims.get("delegation_depth")
        if (
            not isinstance(delegation_depth, int)
            or isinstance(delegation_depth, bool)
            or delegation_depth < 0
            or delegation_depth > self._max_delegation_depth
        ):
            raise ValueError

        capability_id = claims.get("jti")
        invocation_id = claims.get("invocation_id")
        principal_id = claims.get("principal_id")
        validate_opaque_id(principal_id, field="principal_id")
        if principal_id != expected_principal_id:
            raise ValueError
        validate_opaque_id(capability_id, field="jti")
        validate_opaque_id(invocation_id, field="invocation_id")
        return VerifiedInvocation(
            scope=scope,
            principal_id=principal_id,
            invocation_id=invocation_id,
            capability_id=capability_id,
            issuer=self._issuer,
            audience=self._audience,
            allowed_actions=actions,
            issued_at=issued_at,
            expires_at=expires_at,
            delegation_depth=delegation_depth,
        )

    @staticmethod
    def _load_public_key(
        value: Ed25519PublicKey | bytes | str,
    ) -> Ed25519PublicKey:
        if isinstance(value, Ed25519PublicKey):
            return value
        encoded = value.encode("ascii") if isinstance(value, str) else value
        if not isinstance(encoded, bytes):
            raise TypeError
        loaded = load_pem_public_key(encoded)
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError
        return loaded


class LocalTestCapabilityIssuer:
    """Issue local development/test capabilities from an in-process key.

    Production hosts should issue capabilities at their authenticated boundary
    and expose only public verification keys to this package.
    """

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        key_id: str,
        issuer: str,
        audience: str,
        max_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        try:
            validate_opaque_id(key_id, field="key_id")
        except (TypeError, ValueError):
            raise ConfigurationError(
                "capability issuer configuration is invalid"
            ) from None
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ConfigurationError("capability issuer configuration is invalid")
        if not isinstance(issuer, str) or not issuer:
            raise ConfigurationError("capability issuer configuration is invalid")
        if not isinstance(audience, str) or not audience:
            raise ConfigurationError("capability issuer configuration is invalid")
        if (
            not isinstance(max_ttl, timedelta)
            or max_ttl <= timedelta(0)
            or max_ttl > timedelta(hours=1)
        ):
            raise ConfigurationError("capability issuer configuration is invalid")
        self._private_key = private_key
        self.key_id = key_id
        self.issuer = issuer
        self.audience = audience
        self.max_ttl = max_ttl

    @classmethod
    def generate(
        cls,
        *,
        key_id: str = "local-test-key",
        issuer: str = "agent-filetree-memory-local",
        audience: str = "agent-filetree-memory",
        max_ttl: timedelta = timedelta(minutes=5),
    ) -> LocalTestCapabilityIssuer:
        return cls(
            Ed25519PrivateKey.generate(),
            key_id=key_id,
            issuer=issuer,
            audience=audience,
            max_ttl=max_ttl,
        )

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def verifier(
        self,
        *,
        max_delegation_depth: int = 0,
        clock_skew: timedelta = timedelta(seconds=5),
    ) -> AsymmetricCapabilityVerifier:
        return AsymmetricCapabilityVerifier(
            {self.key_id: self.public_key},
            issuer=self.issuer,
            audience=self.audience,
            max_ttl=self.max_ttl,
            max_delegation_depth=max_delegation_depth,
            clock_skew=clock_skew,
        )

    def issue(
        self,
        scope: Scope,
        actions: Iterable[MemoryAction],
        *,
        principal_id: str,
        invocation_id: str | None = None,
        capability_id: str | None = None,
        ttl: timedelta = timedelta(minutes=1),
        delegation_depth: int = 0,
        now: datetime | None = None,
    ) -> str:
        if not isinstance(scope, Scope):
            raise ValueError("scope must be a Scope")
        validate_opaque_id(principal_id, field="principal_id")
        checked_at = _aware_utc_now(now)
        if (
            not isinstance(ttl, timedelta)
            or ttl < timedelta(seconds=1)
            or ttl > self.max_ttl
        ):
            raise ValueError("ttl is outside the configured capability limit")
        if (
            not isinstance(delegation_depth, int)
            or isinstance(delegation_depth, bool)
            or delegation_depth < 0
        ):
            raise ValueError("delegation_depth must be a non-negative integer")
        if isinstance(actions, (str, bytes)):
            raise ValueError("actions must contain MemoryAction values")
        try:
            action_set = frozenset(actions)
        except TypeError:
            raise ValueError("actions must contain MemoryAction values") from None
        if not action_set or any(
            not isinstance(action, MemoryAction) for action in action_set
        ):
            raise ValueError("actions must contain MemoryAction values")

        invocation_id = uuid4().hex if invocation_id is None else invocation_id
        capability_id = uuid4().hex if capability_id is None else capability_id
        validate_opaque_id(invocation_id, field="invocation_id")
        validate_opaque_id(capability_id, field="capability_id")

        # NumericDate has second precision.  Truncation ensures the verifier
        # reconstructs exactly the timestamps represented by the signed token.
        issued_at = int(checked_at.timestamp())
        expires_at = issued_at + int(ttl.total_seconds())
        claims = {
            "capability_version": _CAPABILITY_VERSION,
            "iss": self.issuer,
            "aud": self.audience,
            "iat": issued_at,
            "exp": expires_at,
            "jti": capability_id,
            "invocation_id": invocation_id,
            "principal_id": principal_id,
            "actions": sorted(action.value for action in action_set),
            "delegation_depth": delegation_depth,
            "scope": {
                "workspace_id": scope.workspace_id,
                "agent_profile_id": scope.agent_profile_id,
            },
        }
        return jwt.encode(
            claims,
            self._private_key,
            algorithm=_ALGORITHM,
            headers={"kid": self.key_id, "typ": _TOKEN_TYPE},
        )


def _aware_utc_now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return result.astimezone(timezone.utc)


def _numeric_date(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError
    return value
