from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_filetree_memory.auth import (
    AsymmetricCapabilityVerifier,
    LocalTestCapabilityIssuer,
)
from agent_filetree_memory.domain.errors import AuthorizationDenied, ConfigurationError
from agent_filetree_memory.domain.models import MemoryAction, Scope


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
SCOPE = Scope("workspace-1", "agent-1")
PRINCIPAL_ID = "principal-1"


def issuer_and_key() -> tuple[LocalTestCapabilityIssuer, Ed25519PrivateKey]:
    private_key = Ed25519PrivateKey.generate()
    return (
        LocalTestCapabilityIssuer(
            private_key,
            key_id="signing-key-1",
            issuer="https://issuer.example",
            audience="memory-api",
        ),
        private_key,
    )


def resign(
    token: str,
    private_key: Ed25519PrivateKey,
    *,
    claim_changes: dict[str, object] | None = None,
    header_changes: dict[str, object] | None = None,
) -> str:
    claims = jwt.decode(token, options={"verify_signature": False})
    claims.update(claim_changes or {})
    headers: dict[str, object] = {"kid": "signing-key-1", "typ": "at+jwt"}
    headers.update(header_changes or {})
    return jwt.encode(claims, private_key, algorithm="EdDSA", headers=headers)


def test_issuer_and_verifier_bind_every_capability_field() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE,
        {MemoryAction.READ, MemoryAction.WRITE},
        principal_id=PRINCIPAL_ID,
        invocation_id="invocation-7",
        capability_id="capability-9",
        delegation_depth=1,
        ttl=timedelta(seconds=45),
        now=NOW,
    )

    verified = issuer.verifier(max_delegation_depth=1).verify(
        token,
        required_action=MemoryAction.READ,
        expected_principal_id=PRINCIPAL_ID,
        now=NOW,
    )

    assert verified.scope == SCOPE
    assert verified.principal_id == PRINCIPAL_ID
    assert verified.invocation_id == "invocation-7"
    assert verified.capability_id == "capability-9"
    assert verified.issuer == "https://issuer.example"
    assert verified.audience == "memory-api"
    assert verified.issued_at == NOW
    assert verified.expires_at == NOW + timedelta(seconds=45)
    assert verified.delegation_depth == 1
    assert verified.allowed_actions == {MemoryAction.READ, MemoryAction.WRITE}
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["scope"] == {
        "workspace_id": "workspace-1",
        "agent_profile_id": "agent-1",
    }


def test_distinct_authenticated_principals_can_receive_the_same_durable_scope() -> None:
    issuer, _ = issuer_and_key()

    for principal_id in ("principal-a", "principal-b"):
        token = issuer.issue(
            SCOPE,
            {MemoryAction.READ},
            principal_id=principal_id,
            invocation_id=f"invocation-{principal_id}",
            now=NOW,
        )
        verified = issuer.verifier().verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id=principal_id,
            now=NOW,
        )
        assert verified.scope == SCOPE
        assert verified.principal_id == principal_id


def test_history_metadata_and_content_are_independent_capabilities() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE,
        {MemoryAction.HISTORY_LIST},
        principal_id=PRINCIPAL_ID,
        now=NOW,
    )
    verifier = issuer.verifier()

    verified = verifier.verify(
        token,
        required_action=MemoryAction.HISTORY_LIST,
        expected_principal_id=PRINCIPAL_ID,
        now=NOW,
    )
    assert verified.allowed_actions == {MemoryAction.HISTORY_LIST}
    with pytest.raises(AuthorizationDenied, match="not authorized"):
        verifier.verify(
            token,
            required_action=MemoryAction.HISTORY_READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


def test_verifier_supports_explicit_signing_key_rotation() -> None:
    old, _ = issuer_and_key()
    new_private = Ed25519PrivateKey.generate()
    new = LocalTestCapabilityIssuer(
        new_private,
        key_id="signing-key-2",
        issuer=old.issuer,
        audience=old.audience,
    )
    verifier = AsymmetricCapabilityVerifier(
        {
            old.key_id: old.public_key,
            new.key_id: new.public_key,
        },
        issuer=old.issuer,
        audience=old.audience,
    )

    for issuer in (old, new):
        token = issuer.issue(
            SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
        )
        assert (
            verifier.verify(
                token,
                required_action=MemoryAction.READ,
                expected_principal_id=PRINCIPAL_ID,
                now=NOW,
            ).scope
            == SCOPE
        )


@pytest.mark.parametrize(
    "required_action",
    [
        MemoryAction.HISTORY_LIST,
        MemoryAction.HISTORY_READ,
        MemoryAction.WRITE,
        MemoryAction.APPEND,
        MemoryAction.DELETE,
        MemoryAction.EXPORT,
    ],
)
def test_action_set_cannot_be_widened(required_action: MemoryAction) -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        issuer.verifier().verify(
            token,
            required_action=required_action,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


def test_expired_and_future_capabilities_are_denied() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE,
        {MemoryAction.READ},
        principal_id=PRINCIPAL_ID,
        ttl=timedelta(seconds=30),
        now=NOW,
    )
    verifier = issuer.verifier(clock_skew=timedelta(0))

    with pytest.raises(AuthorizationDenied):
        verifier.verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW + timedelta(seconds=30),
        )
    with pytest.raises(AuthorizationDenied):
        verifier.verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW - timedelta(seconds=1),
        )


def test_delegation_depth_is_bounded_by_the_verifier() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE,
        {MemoryAction.READ},
        principal_id=PRINCIPAL_ID,
        delegation_depth=2,
        now=NOW,
    )

    with pytest.raises(AuthorizationDenied):
        issuer.verifier(max_delegation_depth=1).verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


@pytest.mark.parametrize(
    "claim_changes",
    [
        {"aud": "other-service"},
        {"iss": "https://other.example"},
        {"jti": "contains spaces"},
        {"invocation_id": "../not-opaque"},
        {"principal_id": "../not-opaque"},
        {"actions": ["memory:read", "memory:read"]},
        {"actions": ["memory:read", "memory:admin"]},
        {"delegation_depth": -1},
        {"capability_version": True},
        {"iat": True},
        {"exp": 2**63},
        {"scope": {"workspace_id": "workspace-1"}},
        {
            "scope": {
                "workspace_id": "workspace-1",
                "agent_profile_id": "agent-1",
                "unexpected": "widening-field",
            }
        },
    ],
)
def test_malformed_or_ambiguous_claims_are_denied(
    claim_changes: dict[str, object],
) -> None:
    issuer, private_key = issuer_and_key()
    good = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    token = resign(good, private_key, claim_changes=claim_changes)

    with pytest.raises(AuthorizationDenied) as error:
        issuer.verifier().verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )

    assert str(error.value) == "memory operation is not authorized"
    assert error.value.__cause__ is None


def test_unknown_kid_wrong_signature_and_algorithm_confusion_are_denied() -> None:
    issuer, private_key = issuer_and_key()
    token = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    unknown_kid = resign(
        token, private_key, header_changes={"kid": "untrusted-signing-key"}
    )
    attacker = LocalTestCapabilityIssuer.generate(
        key_id=issuer.key_id, issuer=issuer.issuer, audience=issuer.audience
    )
    wrong_signature = attacker.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    symmetric_attack = jwt.encode(
        claims,
        "attacker-controlled-secret-that-is-long-enough",
        algorithm="HS256",
        headers={"kid": issuer.key_id, "typ": "at+jwt"},
    )

    for bad_token in (unknown_kid, wrong_signature, symmetric_attack):
        with pytest.raises(AuthorizationDenied, match="not authorized"):
            issuer.verifier().verify(
                bad_token,
                required_action=MemoryAction.READ,
                expected_principal_id=PRINCIPAL_ID,
                now=NOW,
            )


def test_unrecognized_critical_header_is_denied() -> None:
    issuer, private_key = issuer_and_key()
    token = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    critical = resign(
        token,
        private_key,
        header_changes={"crit": ["custom"], "custom": "unsupported"},
    )

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        issuer.verifier().verify(
            critical,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


def test_optional_not_before_is_enforced_with_the_supplied_clock() -> None:
    issuer, private_key = issuer_and_key()
    token = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    future = resign(
        token,
        private_key,
        claim_changes={"nbf": int((NOW + timedelta(seconds=20)).timestamp())},
    )

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        issuer.verifier(clock_skew=timedelta(0)).verify(
            future,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


@pytest.mark.parametrize("field", ["invocation_id", "capability_id"])
def test_local_issuer_rejects_explicit_empty_ids(field: str) -> None:
    issuer, _ = issuer_and_key()

    with pytest.raises(ValueError, match="non-empty opaque identifier"):
        issuer.issue(
            SCOPE,
            {MemoryAction.READ},
            principal_id=PRINCIPAL_ID,
            now=NOW,
            **{field: ""},
        )


def test_scope_claims_cannot_be_moved_between_agent_profiles() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE, {MemoryAction.READ}, principal_id=PRINCIPAL_ID, now=NOW
    )
    header, _payload, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    moved_scope = {
        "workspace_id": SCOPE.workspace_id,
        "agent_profile_id": "different-agent-profile",
    }
    claims["scope"] = moved_scope
    moved_payload = jwt.utils.base64url_encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    moved = ".".join((header, moved_payload, signature))

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        issuer.verifier().verify(
            moved,
            required_action=MemoryAction.READ,
            expected_principal_id=PRINCIPAL_ID,
            now=NOW,
        )


def test_capability_is_bound_to_the_outer_authenticated_principal() -> None:
    issuer, _ = issuer_and_key()
    token = issuer.issue(
        SCOPE,
        {MemoryAction.READ},
        principal_id="authenticated-principal-1",
        now=NOW,
    )

    verified = issuer.verifier().verify(
        token,
        required_action=MemoryAction.READ,
        expected_principal_id="authenticated-principal-1",
        now=NOW,
    )
    assert verified.principal_id == "authenticated-principal-1"

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        issuer.verifier().verify(
            token,
            required_action=MemoryAction.READ,
            expected_principal_id="different-authenticated-principal",
            now=NOW,
        )


def test_verifier_rejects_non_ed25519_key_configuration() -> None:
    with pytest.raises(ConfigurationError, match="configuration is invalid"):
        AsymmetricCapabilityVerifier(
            {"key-1": b"not a public key"},
            issuer="issuer",
            audience="audience",
        )
