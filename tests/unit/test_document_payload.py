from __future__ import annotations

import pytest

from agent_filetree_memory.domain.errors import IntegrityFailure
from agent_filetree_memory.postgres.store import (
    _DOCUMENT_FRAME_PREFIX,
    _decode_document_payload,
    _encode_document_payload,
)


def test_version_metadata_frame_round_trips() -> None:
    framed = _encode_document_payload(
        b"# Memory\n",
        committed_by_principal_id="principal-1",
        co_authored_by=("agent:claude",),
        change_comment="Capture the decision",
    )

    assert _decode_document_payload(framed) == (
        b"# Memory\n",
        "principal-1",
        ("agent:claude",),
        "Capture the decision",
    )


def test_legacy_unframed_document_has_no_invented_provenance() -> None:
    legacy = b"# Legacy memory\n"

    assert _decode_document_payload(legacy) == (legacy, None, (), None)


def test_malformed_framed_metadata_fails_closed() -> None:
    with pytest.raises(IntegrityFailure, match="metadata is malformed"):
        _decode_document_payload(_DOCUMENT_FRAME_PREFIX + b"\x00\x00\x00\x10{}")
