from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os

import pytest
from sqlalchemy import func, insert, select, text, update

from agent_filetree_memory.domain.errors import (
    IdempotencyConflict,
    IntegrityFailure,
    InvalidMemoryPath,
    NotFoundOrDenied,
    QuotaExceeded,
    RateLimitExceeded,
    VersionConflict,
)
from agent_filetree_memory.domain.models import Scope
from agent_filetree_memory.crypto import EnvelopeEncryptor, LocalKeyringDekProvider
from agent_filetree_memory.janitor_cli import JanitorSettings, run_janitor_once
from agent_filetree_memory.postgres import (
    PostgresJanitor,
    PostgresMemoryStore,
    PostgresRuntime,
    PostgresStoreConfig,
)

pytestmark = pytest.mark.live
TEST_INDEX_KEY = b"test-only-idempotency-index-key-32"


def scope(
    *,
    workspace: str = "workspace-1",
    agent: str = "agent-1",
) -> Scope:
    return Scope(
        workspace_id=workspace,
        agent_profile_id=agent,
    )


async def test_tree_lifecycle_and_encrypted_raw_storage(
    postgres_store, postgres_runtime
):
    marker = "synthetic-canary-31df7"
    co_author_marker = "agent:private-coauthor-31df7"
    comment_marker = "private-change-comment-31df7"
    path = "/private-notes/launch-plan.md"
    created = await postgres_store.write(
        scope(),
        path,
        f"# Plan\n\n{marker}",
        expected_version=None,
        idempotency_key="create-1",
        invocation_id="run-1",
        principal_id="principal-writer",
        co_authored_by=(co_author_marker,),
        change_comment=comment_marker,
    )
    assert created.version == 1
    assert created.created

    root_entries = await postgres_store.list(
        scope(), "/", invocation_id="run-list-root"
    )
    assert [(entry.name, entry.kind) for entry in root_entries] == [
        ("private-notes", "directory")
    ]
    nested_entries = await postgres_store.list(
        scope(), "/private-notes", invocation_id="run-list-nested"
    )
    assert [(entry.name, entry.kind) for entry in nested_entries] == [
        ("launch-plan.md", "document")
    ]

    appended = await postgres_store.append(
        scope(),
        path,
        "\nnext",
        expected_version=1,
        idempotency_key="append-1",
        invocation_id="run-2",
    )
    assert appended.version == 2
    snapshot = await postgres_store.read(scope(), path, invocation_id="run-read")
    assert snapshot.content == f"# Plan\n\n{marker}\nnext"
    assert snapshot.version == 2
    assert [
        item.path
        for item in await postgres_store.export_markdown_tree(
            scope(), invocation_id="run-export"
        )
    ] == [path]

    # PostgreSQL's JSON rendering includes bytea only as an encoded value. This
    # checks every row, including encrypted manifests and idempotency records.
    async with postgres_runtime.session() as session:
        rendered: list[str] = []
        for table in postgres_runtime.tables.metadata.sorted_tables:
            value = (
                await session.execute(
                    text(
                        f"SELECT COALESCE(string_agg(row_to_json(t)::text, ''), '') "
                        f"FROM {postgres_runtime.schema}.{table.name} AS t"
                    )
                )
            ).scalar_one()
            rendered.append(value)
    raw_rows = "".join(rendered)
    assert marker not in raw_rows
    assert "launch-plan.md" not in raw_rows
    assert "/private-notes" not in raw_rows
    assert co_author_marker not in raw_rows
    assert comment_marker not in raw_rows
    async with postgres_runtime.session() as session:
        invocation_ids = set(
            (
                await session.execute(
                    select(postgres_runtime.tables.audit_events.c.invocation_id).where(
                        postgres_runtime.tables.audit_events.c.invocation_id.is_not(None)
                    )
                )
            ).scalars()
        )
    assert {"run-list-root", "run-list-nested", "run-read", "run-export"} <= (
        invocation_ids
    )


@pytest.mark.parametrize(
    "other_scope",
    [
        scope(agent="agent-2"),
        Scope("workspace-2", "agent-1"),
    ],
)
async def test_compound_scope_isolation(postgres_store, other_scope):
    await postgres_store.write(
        scope(),
        "/memory.md",
        "secret",
        expected_version=None,
        idempotency_key="create-isolation",
        invocation_id="run-isolation",
    )
    with pytest.raises(NotFoundOrDenied):
        await postgres_store.read(other_scope, "/memory.md")
    assert await postgres_store.list(other_scope, "/") == ()
    assert await postgres_store.export_markdown_tree(other_scope) == ()
    await postgres_store.write(
        other_scope,
        "/memory.md",
        "other scope",
        expected_version=None,
        idempotency_key="create-isolation",
        invocation_id="run-isolation-other",
    )
    assert (await postgres_store.read(scope(), "/memory.md")).content == "secret"
    assert (await postgres_store.read(other_scope, "/memory.md")).content == "other scope"


async def test_same_agent_memory_persists_across_invocations_and_audits_actors(
    postgres_store, postgres_runtime
):
    durable_scope = scope(workspace="shared-workspace", agent="shared-agent")
    await postgres_store.write(
        durable_scope,
        "/shared.md",
        "durable",
        expected_version=None,
        idempotency_key="shared-create",
        invocation_id="conversation-a",
        principal_id="principal-a",
    )

    snapshot = await postgres_store.read(
        durable_scope,
        "/shared.md",
        invocation_id="conversation-b",
        principal_id="principal-b",
    )

    assert snapshot.content == "durable"
    audit = postgres_runtime.tables.audit_events
    async with postgres_runtime.session() as session:
        actors = (
            await session.execute(
                select(audit.c.principal_id)
                .where(
                    audit.c.workspace_id == durable_scope.workspace_id,
                    audit.c.agent_profile_id == durable_scope.agent_profile_id,
                )
                .order_by(audit.c.occurred_at)
            )
        ).scalars().all()
    assert actors == ["principal-a", "principal-b"]


async def test_history_exposes_canonical_time_provenance_comment_and_diff(
    postgres_store, postgres_runtime
):
    history_scope = scope(workspace="history-provenance-workspace")
    path = "/decisions.md"
    await postgres_store.write(
        history_scope,
        path,
        "# Decision\n\nFirst choice\n",
        expected_version=None,
        idempotency_key="history-provenance-create",
        invocation_id="history-provenance-run-1",
        principal_id="principal-a",
        co_authored_by=("agent:claude",),
        change_comment="Record the initial choice",
    )
    replay = await postgres_store.write(
        history_scope,
        path,
        "# Decision\n\nFirst choice\n",
        expected_version=None,
        idempotency_key="history-provenance-create",
        invocation_id="history-provenance-run-1-retry",
        principal_id="principal-a",
        co_authored_by=("agent:claude",),
        change_comment="Record the initial choice",
    )
    assert replay.idempotent_replay is True
    with pytest.raises(IdempotencyConflict):
        await postgres_store.write(
            history_scope,
            path,
            "# Decision\n\nFirst choice\n",
            expected_version=None,
            idempotency_key="history-provenance-create",
            invocation_id="history-provenance-run-1-conflict",
            principal_id="principal-a",
            co_authored_by=("agent:claude",),
            change_comment="A different comment",
        )
    first = await postgres_store.read(history_scope, path)
    assert first.created_at == first.version_created_at
    await postgres_store.write(
        history_scope,
        path,
        "# Decision\n\nSecond choice\n",
        expected_version=1,
        idempotency_key="history-provenance-update",
        invocation_id="history-provenance-run-2",
        principal_id="principal-b",
        co_authored_by=("agent:codex",),
        change_comment="Revise after review",
    )

    current = await postgres_store.read(history_scope, path)
    assert current.version == 2
    assert current.version_created_at == current.updated_at
    assert current.committed_by_principal_id == "principal-b"
    assert current.co_authored_by == ("agent:codex",)
    assert current.change_comment == "Revise after review"

    newest_page = await postgres_store.list_history(
        history_scope,
        path,
        limit=1,
        invocation_id="history-list-run-1",
        principal_id="principal-reader",
    )
    assert newest_page.current_version == 2
    assert [item.version for item in newest_page.versions] == [2]
    assert newest_page.versions[0].version_created_at == current.version_created_at
    assert newest_page.versions[0].committed_by_principal_id == "principal-b"
    assert newest_page.versions[0].co_authored_by == ("agent:codex",)
    assert newest_page.versions[0].change_comment == "Revise after review"
    assert newest_page.next_before_version == 2

    older_page = await postgres_store.list_history(
        history_scope,
        path,
        limit=1,
        before_version=newest_page.next_before_version,
    )
    assert [item.version for item in older_page.versions] == [1]
    assert older_page.versions[0].version_created_at == first.version_created_at
    assert older_page.versions[0].committed_by_principal_id == "principal-a"
    assert older_page.versions[0].co_authored_by == ("agent:claude",)
    assert older_page.versions[0].change_comment == "Record the initial choice"
    assert older_page.next_before_version is None

    historical = await postgres_store.read_history(
        history_scope,
        path,
        2,
        compare_to_version=1,
        invocation_id="history-read-run",
        principal_id="principal-reader",
    )
    assert historical.content == "# Decision\n\nSecond choice\n"
    assert historical.version_created_at == current.version_created_at
    assert historical.committed_by_principal_id == "principal-b"
    assert historical.co_authored_by == ("agent:codex",)
    assert historical.change_comment == "Revise after review"
    assert historical.compared_to_version == 1
    assert historical.diff is not None
    assert f"--- {path}@v1" in historical.diff
    assert f"+++ {path}@v2" in historical.diff
    assert "-First choice" in historical.diff
    assert "+Second choice" in historical.diff

    audit = postgres_runtime.tables.audit_events
    async with postgres_runtime.session() as session:
        history_actions = set(
            (
                await session.execute(
                    select(audit.c.action).where(
                        audit.c.workspace_id == history_scope.workspace_id,
                        audit.c.agent_profile_id == history_scope.agent_profile_id,
                        audit.c.action.in_(
                            ["memory:history:list", "memory:history:read"]
                        ),
                    )
                )
            ).scalars()
        )
    assert history_actions == {"memory:history:list", "memory:history:read"}


async def test_compare_and_swap_and_idempotency(postgres_store):
    initial = await postgres_store.write(
        scope(),
        "/race.md",
        "v1",
        expected_version=None,
        idempotency_key="create-race",
        invocation_id="run-create",
    )
    replay = await postgres_store.write(
        scope(),
        "/race.md",
        "v1",
        expected_version=None,
        idempotency_key="create-race",
        invocation_id="run-retry",
    )
    assert replay.version == initial.version
    assert replay.idempotent_replay

    attempts = await asyncio.gather(
        postgres_store.write(
            scope(),
            "/race.md",
            "winner-a",
            expected_version=1,
            idempotency_key="race-a",
            invocation_id="run-a",
        ),
        postgres_store.write(
            scope(),
            "/race.md",
            "winner-b",
            expected_version=1,
            idempotency_key="race-b",
            invocation_id="run-b",
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, Exception) for value in attempts) == 1
    assert sum(isinstance(value, VersionConflict) for value in attempts) == 1
    assert (await postgres_store.read(scope(), "/race.md")).version == 2

    with pytest.raises(IdempotencyConflict):
        await postgres_store.write(
            scope(),
            "/race.md",
            "different-request",
            expected_version=2,
            idempotency_key="race-a",
            invocation_id="run-conflict",
        )


async def test_idempotency_blind_index_avoids_unrelated_corruption(
    postgres_store, postgres_runtime
):
    await postgres_store.write(
        scope(),
        "/first.md",
        "first",
        expected_version=None,
        idempotency_key="first-request",
        invocation_id="first-run",
    )
    table = postgres_runtime.tables.idempotency
    async with postgres_runtime.session() as session, session.begin():
        row = (await session.execute(select(table))).mappings().one()
        assert len(row["lookup_digest"]) == 64
        assert row["lookup_digest"] != "first-request"
        assert row["lookup_digest"] != hashlib.sha256(b"first-request").hexdigest()
        ciphertext = bytes(row["ciphertext"])
        await session.execute(
            update(table)
            .where(table.c.record_id == row["record_id"])
            .values(ciphertext=ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
        )

    # A direct blind-index lookup must not decrypt an unrelated damaged row.
    created = await postgres_store.write(
        scope(),
        "/second.md",
        "second",
        expected_version=None,
        idempotency_key="second-request",
        invocation_id="second-run",
    )
    assert created.created
    with pytest.raises(IntegrityFailure):
        await postgres_store.write(
            scope(),
            "/first.md",
            "first",
            expected_version=None,
            idempotency_key="first-request",
            invocation_id="first-retry",
        )


async def test_tamper_and_context_move_fail_authentication(
    postgres_store, postgres_runtime
):
    for name in ("a.md", "b.md"):
        await postgres_store.write(
            scope(),
            f"/{name}",
            name,
            expected_version=None,
            idempotency_key=f"create-{name}",
            invocation_id=f"run-{name}",
        )
    tables = postgres_runtime.tables
    async with postgres_runtime.session() as session:
        rate_before = (
            await session.execute(
                select(func.sum(tables.rate_buckets.c.operation_count)).where(
                    tables.rate_buckets.c.workspace_id == scope().workspace_id,
                    tables.rate_buckets.c.agent_profile_id == scope().agent_profile_id,
                )
            )
        ).scalar_one()
    async with postgres_runtime.session() as session, session.begin():
        objects = (
            await session.execute(
                select(tables.objects.c.object_id).where(
                    tables.objects.c.workspace_id == scope().workspace_id,
                    tables.objects.c.agent_profile_id == scope().agent_profile_id,
                    tables.objects.c.object_kind == "document",
                )
            )
        ).scalars().all()
        first = (
            await session.execute(
                select(tables.versions).where(
                    tables.versions.c.workspace_id == scope().workspace_id,
                    tables.versions.c.agent_profile_id == scope().agent_profile_id,
                    tables.versions.c.object_id == objects[0],
                    tables.versions.c.version == 1,
                )
            )
        ).mappings().one()
        # Moving an intact envelope to another opaque object must fail because
        # object identity is authenticated in both content and DEK AAD.
        await session.execute(
            update(tables.versions)
            .where(
                tables.versions.c.workspace_id == scope().workspace_id,
                tables.versions.c.agent_profile_id == scope().agent_profile_id,
                tables.versions.c.object_id == objects[1],
                tables.versions.c.version == 1,
            )
            .values(
                ciphertext=first["ciphertext"],
                wrapped_dek=first["wrapped_dek"],
                provider_id=first["provider_id"],
                key_id=first["key_id"],
                format_version=first["format_version"],
            )
        )
    failures = 0
    for name in ("a.md", "b.md"):
        try:
            await postgres_store.read(scope(), f"/{name}")
        except IntegrityFailure:
            failures += 1
    assert failures == 1
    async with postgres_runtime.session() as session:
        rate_after = (
            await session.execute(
                select(func.sum(tables.rate_buckets.c.operation_count)).where(
                    tables.rate_buckets.c.workspace_id == scope().workspace_id,
                    tables.rate_buckets.c.agent_profile_id == scope().agent_profile_id,
                )
            )
        ).scalar_one()
        audit_failures = (
            await session.execute(
                select(func.count())
                .select_from(tables.audit_events)
                .where(tables.audit_events.c.reason_code == "integrity_failure")
            )
        ).scalar_one()
    assert audit_failures == 1
    assert rate_after - rate_before == 2


async def test_service_namespace_is_authenticated_in_storage_context(
    postgres_runtime, encryptor
):
    first = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            service_namespace="deployment-a",
        ),
    )
    await first.write(
        scope(),
        "/namespaced.md",
        "bound",
        expected_version=None,
        idempotency_key="namespace-create",
        invocation_id="namespace-run",
    )
    moved = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            service_namespace="deployment-b",
        ),
    )
    with pytest.raises(IntegrityFailure):
        await moved.read(scope(), "/namespaced.md")


async def test_superseded_document_versions_expire_but_current_survives(
    postgres_runtime, encryptor
):
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            retention_window=timedelta(milliseconds=1),
        ),
    )
    await store.write(
        scope(),
        "/history.md",
        "v1",
        expected_version=None,
        idempotency_key="history-create",
        invocation_id="history-run-1",
    )
    await store.write(
        scope(),
        "/history.md",
        "v2",
        expected_version=1,
        idempotency_key="history-update",
        invocation_id="history-run-2",
    )
    tables = postgres_runtime.tables
    async with postgres_runtime.session() as session:
        object_id = (
            await session.execute(
                select(tables.objects.c.object_id).where(
                    tables.objects.c.object_kind == "document"
                )
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(
                    tables.versions.c.version,
                    tables.versions.c.purge_after,
                )
                .where(tables.versions.c.object_id == object_id)
                .order_by(tables.versions.c.version)
            )
        ).all()
    assert rows[0].purge_after is not None
    assert rows[1].purge_after is None

    async with postgres_runtime.session() as session, session.begin():
        await session.execute(
            update(tables.versions)
            .where(
                tables.versions.c.object_id == object_id,
                tables.versions.c.version == 1,
            )
            .values(purge_after=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    history = await store.list_history(scope(), "/history.md", limit=20)
    assert [item.version for item in history.versions] == [2]
    with pytest.raises(NotFoundOrDenied):
        await store.read_history(scope(), "/history.md", 1)

    assert await store.purge_due(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    ) == 0
    assert (await store.read(scope(), "/history.md")).content == "v2"
    async with postgres_runtime.session() as session:
        remaining = (
            await session.execute(
                select(tables.versions.c.version).where(
                    tables.versions.c.object_id == object_id
                )
            )
        ).scalars().all()
    assert remaining == [2]


async def test_each_object_retains_only_the_configured_version_ceiling(
    postgres_runtime, encryptor
):
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            max_versions_per_object=3,
        ),
    )
    versioned_scope = scope(workspace="version-ceiling-workspace")
    result = await store.write(
        versioned_scope,
        "/bounded.md",
        "version-1",
        expected_version=None,
        idempotency_key="bounded-version-1",
        invocation_id="bounded-run-1",
    )
    for version in range(2, 7):
        result = await store.write(
            versioned_scope,
            "/bounded.md",
            f"version-{version}",
            expected_version=result.version,
            idempotency_key=f"bounded-version-{version}",
            invocation_id=f"bounded-run-{version}",
        )

    tables = postgres_runtime.tables
    async with postgres_runtime.session() as session:
        object_id = (
            await session.execute(
                select(tables.objects.c.object_id).where(
                    tables.objects.c.workspace_id
                    == versioned_scope.workspace_id,
                    tables.objects.c.agent_profile_id
                    == versioned_scope.agent_profile_id,
                    tables.objects.c.object_kind == "document",
                )
            )
        ).scalar_one()
        retained = (
            await session.execute(
                select(tables.versions.c.version)
                .where(
                    tables.versions.c.workspace_id
                    == versioned_scope.workspace_id,
                    tables.versions.c.agent_profile_id
                    == versioned_scope.agent_profile_id,
                    tables.versions.c.object_id == object_id,
                )
                .order_by(tables.versions.c.version)
            )
        ).scalars().all()

    assert retained == [4, 5, 6]
    assert (await store.read(versioned_scope, "/bounded.md")).content == "version-6"
    history = await store.list_history(versioned_scope, "/bounded.md", limit=20)
    assert [item.version for item in history.versions] == [6, 5, 4]
    with pytest.raises(NotFoundOrDenied):
        await store.read_history(versioned_scope, "/bounded.md", 3)


async def test_janitor_entry_point_bounds_each_table_and_repeated_runs_drain(
    postgres_runtime, encryptor
):
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            retention_window=timedelta(milliseconds=1),
            idempotency_ttl=timedelta(milliseconds=1),
            audit_retention_window=timedelta(milliseconds=1),
        ),
    )
    janitor_scope = scope(workspace="janitor-workspace")
    for number in range(3):
        created = await store.write(
            janitor_scope,
            f"/due-{number}.md",
            f"due-{number}",
            expected_version=None,
            idempotency_key=f"janitor-create-{number}",
            invocation_id=f"janitor-create-run-{number}",
        )
        await store.delete(
            janitor_scope,
            f"/due-{number}.md",
            expected_version=created.version,
            idempotency_key=f"janitor-delete-{number}",
            invocation_id=f"janitor-delete-run-{number}",
        )

    cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    tables = postgres_runtime.tables
    async with postgres_runtime.session() as session, session.begin():
        for number in range(3):
            bucket_started = cutoff - timedelta(days=1, seconds=number)
            await session.execute(
                insert(tables.rate_buckets).values(
                    workspace_id="janitor-rate-workspace",
                    agent_profile_id="janitor-rate-agent",
                    bucket_started_at=bucket_started,
                    operation_count=1,
                    expires_at=cutoff - timedelta(seconds=1),
                )
            )

    assert postgres_runtime.engine is not None
    first = await run_janitor_once(
        JanitorSettings(
            database_url=postgres_runtime.engine.url.render_as_string(
                hide_password=False
            ),
            schema=postgres_runtime.schema,
            batch_limit=2,
        ),
        now=cutoff,
    )
    assert first.deleted_objects == 2
    assert first.deleted_versions == 2
    assert first.deleted_idempotency_records == 2
    assert first.deleted_rate_buckets == 2
    assert first.deleted_audit_events == 2
    assert first.total_deleted == 10

    janitor = PostgresJanitor(postgres_runtime)
    reports = [first]
    for _ in range(10):
        report = await janitor.purge_due(now=cutoff, limit=2)
        reports.append(report)
        if report.total_deleted == 0:
            break
    assert reports[-1].total_deleted == 0
    assert all(
        count <= 2
        for report in reports
        for count in (
            report.deleted_objects,
            report.deleted_versions,
            report.deleted_idempotency_records,
            report.deleted_rate_buckets,
            report.deleted_audit_events,
        )
    )

    async with postgres_runtime.session() as session:
        due_counts = []
        for table, expires in (
            (tables.objects, tables.objects.c.purge_after),
            (tables.versions, tables.versions.c.purge_after),
            (tables.idempotency, tables.idempotency.c.expires_at),
            (tables.rate_buckets, tables.rate_buckets.c.expires_at),
            (tables.audit_events, tables.audit_events.c.expires_at),
        ):
            due_counts.append(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(table)
                        .where(expires <= cutoff)
                    )
                ).scalar_one()
            )
        physical_objects = (
            await session.execute(
                select(tables.quotas.c.physical_object_count).where(
                    tables.quotas.c.workspace_id == janitor_scope.workspace_id,
                    tables.quotas.c.agent_profile_id
                    == janitor_scope.agent_profile_id,
                )
            )
        ).scalar_one()
    assert due_counts == [0, 0, 0, 0, 0]
    assert physical_objects == 1


async def test_path_depth_and_retained_physical_objects_are_bounded(
    postgres_runtime, encryptor
):
    depth_store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            max_path_depth=2,
        ),
    )
    with pytest.raises(InvalidMemoryPath):
        await depth_store.write(
            scope(workspace="depth-workspace"),
            "/one/two/three.md",
            "too deep",
            expected_version=None,
            idempotency_key="depth-request",
            invocation_id="depth-run",
        )

    retained_scope = scope(workspace="physical-workspace")
    bounded_store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            max_physical_objects=2,
            retention_window=timedelta(milliseconds=1),
        ),
    )
    await bounded_store.write(
        retained_scope,
        "/one.md",
        "one",
        expected_version=None,
        idempotency_key="physical-one",
        invocation_id="physical-run-1",
    )
    await bounded_store.delete(
        retained_scope,
        "/one.md",
        expected_version=1,
        idempotency_key="physical-delete",
        invocation_id="physical-run-2",
    )
    with pytest.raises(QuotaExceeded):
        await bounded_store.write(
            retained_scope,
            "/two.md",
            "two",
            expected_version=None,
            idempotency_key="physical-two-before-purge",
            invocation_id="physical-run-3",
        )
    quota = postgres_runtime.tables.quotas
    async with postgres_runtime.session() as session:
        physical_before = (
            await session.execute(
                select(quota.c.physical_object_count).where(
                    quota.c.workspace_id == retained_scope.workspace_id,
                    quota.c.agent_profile_id == retained_scope.agent_profile_id,
                )
            )
        ).scalar_one()
    assert physical_before == 2
    assert await bounded_store.purge_due(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    ) == 1
    await bounded_store.write(
        retained_scope,
        "/two.md",
        "two",
        expected_version=None,
        idempotency_key="physical-two-after-purge",
        invocation_id="physical-run-4",
    )


async def test_failed_guesses_consume_committed_rate_budget(
    postgres_runtime, encryptor
):
    guessed_scope = scope(workspace="guessed-workspace")
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            rate_limit_operations=2,
        ),
    )
    for path in ("/guess-one.md", "/guess-two.md"):
        with pytest.raises(NotFoundOrDenied):
            await store.read(guessed_scope, path)
    with pytest.raises(RateLimitExceeded):
        await store.read(guessed_scope, "/guess-three.md")
    buckets = postgres_runtime.tables.rate_buckets
    async with postgres_runtime.session() as session:
        consumed = (
            await session.execute(
                select(func.sum(buckets.c.operation_count)).where(
                    buckets.c.workspace_id == guessed_scope.workspace_id,
                    buckets.c.agent_profile_id == guessed_scope.agent_profile_id,
                )
            )
        ).scalar_one()
    assert consumed == 3


async def test_audit_events_have_configured_retention_and_are_purged(
    postgres_runtime, encryptor
):
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            audit_retention_window=timedelta(milliseconds=1),
        ),
    )
    assert await store.list(
        scope(workspace="audit-workspace"), "/", invocation_id="audit-run"
    ) == ()
    audit = postgres_runtime.tables.audit_events
    async with postgres_runtime.session() as session:
        expiry = (await session.execute(select(audit.c.expires_at))).scalar_one()
    assert expiry > datetime.now(timezone.utc) - timedelta(seconds=1)
    await store.purge_due(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    async with postgres_runtime.session() as session:
        remaining = (
            await session.execute(select(func.count()).select_from(audit))
        ).scalar_one()
    assert remaining == 0


async def test_delete_denies_immediately_and_janitor_hard_deletes(
    postgres_runtime, encryptor
):
    store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            retention_window=timedelta(milliseconds=1),
        ),
    )
    await store.write(
        scope(),
        "/delete-me.md",
        "gone",
        expected_version=None,
        idempotency_key="create-delete",
        invocation_id="run-create",
    )
    deleted = await store.delete(
        scope(),
        "/delete-me.md",
        expected_version=1,
        idempotency_key="delete-1",
        invocation_id="run-delete",
    )
    replay = await store.delete(
        scope(),
        "/delete-me.md",
        expected_version=1,
        idempotency_key="delete-1",
        invocation_id="run-delete-retry",
    )
    assert replay.idempotent_replay
    assert replay.purge_after == deleted.purge_after
    with pytest.raises(NotFoundOrDenied):
        await store.read(scope(), "/delete-me.md")
    assert await store.purge_due(
        now=datetime.now(timezone.utc) + timedelta(seconds=1)
    ) == 1
    tables = postgres_runtime.tables
    async with postgres_runtime.session() as session:
        document_count = (
            await session.execute(
                select(func.count())
                .select_from(tables.objects)
                .where(tables.objects.c.object_kind == "document")
            )
        ).scalar_one()
    assert document_count == 0


async def test_restart_durability_and_key_rotation(postgres_runtime):
    key_v1 = base64.b64encode(os.urandom(32)).decode("ascii")
    key_v2 = base64.b64encode(os.urandom(32)).decode("ascii")
    first_encryptor = EnvelopeEncryptor(
        LocalKeyringDekProvider({"v1": key_v1}, active_key_id="v1")
    )
    store_config = PostgresStoreConfig(idempotency_index_key=TEST_INDEX_KEY)
    first_store = PostgresMemoryStore(
        postgres_runtime, first_encryptor, config=store_config
    )
    await first_store.write(
        scope(),
        "/durable.md",
        "before rotation",
        expected_version=None,
        idempotency_key="durable-create",
        invocation_id="durable-run-1",
    )

    rotated_encryptor = EnvelopeEncryptor(
        LocalKeyringDekProvider(
            {"v1": key_v1, "v2": key_v2}, active_key_id="v2"
        )
    )
    rotated_store = PostgresMemoryStore(
        postgres_runtime, rotated_encryptor, config=store_config
    )
    assert (await rotated_store.read(scope(), "/durable.md")).content == "before rotation"
    await rotated_store.write(
        scope(),
        "/durable.md",
        "after rotation",
        expected_version=1,
        idempotency_key="durable-update",
        invocation_id="durable-run-2",
    )

    assert postgres_runtime.engine is not None
    restarted_runtime = PostgresRuntime.from_url(
        postgres_runtime.engine.url, schema=postgres_runtime.schema
    )
    try:
        restarted_store = PostgresMemoryStore(
            restarted_runtime, rotated_encryptor, config=store_config
        )
        snapshot = await restarted_store.read(scope(), "/durable.md")
        assert snapshot.content == "after rotation"
        assert snapshot.version == 2
    finally:
        await restarted_runtime.close()

    versions = postgres_runtime.tables.versions
    async with postgres_runtime.session() as session:
        key_ids = (
            await session.execute(
                select(versions.c.key_id).where(
                    versions.c.workspace_id == scope().workspace_id,
                    versions.c.agent_profile_id == scope().agent_profile_id,
                )
            )
        ).scalars().all()
    assert "v1" in key_ids
    assert "v2" in key_ids


async def test_quota_and_rate_limit_are_scoped_to_workspace_agent_profile(
    postgres_runtime, encryptor
):
    quota_store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            max_document_bytes=5,
            max_scope_bytes=5,
            max_documents=1,
        ),
    )
    await quota_store.write(
        scope(),
        "/one.md",
        "12345",
        expected_version=None,
        idempotency_key="quota-one",
        invocation_id="quota-run-one",
    )
    with pytest.raises(QuotaExceeded):
        await quota_store.write(
            scope(),
            "/two.md",
            "x",
            expected_version=None,
            idempotency_key="quota-two",
            invocation_id="quota-run-two",
        )
    rate_scope = scope(workspace="rate-workspace")
    rate_store = PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(
            idempotency_index_key=TEST_INDEX_KEY,
            rate_limit_operations=2,
        ),
    )
    assert await rate_store.list(rate_scope, "/") == ()
    assert await rate_store.list(rate_scope, "/") == ()
    with pytest.raises(RateLimitExceeded):
        await rate_store.list(rate_scope, "/")
