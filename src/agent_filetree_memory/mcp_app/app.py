"""Prefab MCP App for browsing and editing only the current capability."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import TYPE_CHECKING, Annotated, Any
from uuid import uuid4

from fastmcp import Context, FastMCPApp
from fastmcp.apps.app import _make_resolver
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema
from prefab_ui.actions import CloseOverlay, SetState
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Alert,
    AlertDescription,
    AlertTitle,
    Badge,
    Button,
    Card,
    CardContent,
    CardHeader,
    CardTitle,
    Column,
    Dialog,
    Field,
    FieldContent,
    FieldDescription,
    FieldTitle,
    ForEach,
    Grid,
    Heading,
    If,
    Input,
    Loader,
    Markdown,
    Muted,
    Row,
    Separator,
    Text,
    Textarea,
)
from prefab_ui.rx import RESULT, STATE, Rx

from ..domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    NotFoundOrDenied,
    VersionConflict,
)
from ..domain.models import MemoryAction, VerifiedInvocation
from ..domain.paths import normalize_memory_path
from ..mcp.payloads import (
    delete_payload,
    document_payload,
    entry_payload,
    write_payload,
)
from ..mcp.server import (
    AppendMarkdownContent,
    ExpectedVersion,
    IdempotencyKey,
    MemoryPath,
    OptionalExpectedVersion,
    WriteMarkdownContent,
    _verified_invocation,
)
from ..ports.capabilities import InvocationResolver

if TYPE_CHECKING:
    from ..application import MemoryService


_APP_NAME = "Agent Filetree Memory Browser"
_APP_INSTANCE_PREFIX = b"AFMA\x01"
_APP_INSTANCE_NONCE_BYTES = 16
_APP_INSTANCE_MAC_BYTES = hashlib.sha256().digest_size
AppInstanceId = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 64,
            "maxLength": 96,
            "description": "Opaque binding for the currently open memory app.",
        }
    ),
]
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _prefer_bundled_renderer() -> None:
    """Use Prefab's self-contained renderer unless a host chose a dev URL."""
    if not os.environ.get("PREFAB_RENDERER_URL"):
        os.environ.setdefault("PREFAB_BUNDLED_RENDERER", "1")


def _invocation_binding(invocation: VerifiedInvocation) -> bytes:
    """Return a canonical server-side binding without exposing its fields."""
    scope = invocation.scope
    return json.dumps(
        {
            "audience": invocation.audience,
            "issuer": invocation.issuer,
            "principal_id": invocation.principal_id,
            "scope": {
                "agent_profile_id": scope.agent_profile_id,
                "workspace_id": scope.workspace_id,
            },
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _issue_app_instance(invocation: VerifiedInvocation, signing_key: bytes) -> str:
    nonce = os.urandom(_APP_INSTANCE_NONCE_BYTES)
    framed = _APP_INSTANCE_PREFIX + nonce
    mac = hmac.digest(
        signing_key,
        framed + b"\x00" + _invocation_binding(invocation),
        "sha256",
    )
    return base64.urlsafe_b64encode(framed + mac).rstrip(b"=").decode("ascii")


def _require_app_instance(
    invocation: VerifiedInvocation,
    app_instance_id: Any,
    signing_key: bytes,
) -> None:
    """Reject stale, forged, or cross-agent app state with one safe error."""
    try:
        if not isinstance(app_instance_id, str) or not 64 <= len(app_instance_id) <= 96:
            raise ValueError
        padding = "=" * (-len(app_instance_id) % 4)
        raw = base64.b64decode(
            (app_instance_id + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(app_instance_id, canonical):
            raise ValueError
        expected_length = (
            len(_APP_INSTANCE_PREFIX)
            + _APP_INSTANCE_NONCE_BYTES
            + _APP_INSTANCE_MAC_BYTES
        )
        if len(raw) != expected_length or not raw.startswith(_APP_INSTANCE_PREFIX):
            raise ValueError
        framed = raw[:-_APP_INSTANCE_MAC_BYTES]
        supplied_mac = raw[-_APP_INSTANCE_MAC_BYTES:]
        expected_mac = hmac.digest(
            signing_key,
            framed + b"\x00" + _invocation_binding(invocation),
            "sha256",
        )
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ValueError
    except (UnicodeError, ValueError, binascii.Error):
        raise AuthorizationDenied("memory operation is not authorized") from None


async def _verified_app_invocation(
    resolver: InvocationResolver,
    ctx: Context,
    action: MemoryAction,
    app_instance_id: Any,
    signing_key: bytes,
) -> VerifiedInvocation:
    # Resolve and authorize before inspecting any app-controlled argument.
    invocation = await _verified_invocation(resolver, ctx, action)
    _require_app_instance(invocation, app_instance_id, signing_key)
    return invocation


def _parent_path(path: str) -> str:
    normalized = normalize_memory_path(path)
    if normalized == "/":
        return "/"
    parent = normalized.rsplit("/", 1)[0]
    return parent or "/"


def _browser_listing(path: str, entries: Any) -> dict[str, Any]:
    normalized = normalize_memory_path(path)
    serialized = [entry_payload(entry) for entry in entries]
    return {
        "path": normalized,
        "parent_path": _parent_path(normalized),
        "folder_input": normalized.strip("/"),
        "directories": [
            entry for entry in serialized if entry["kind"] == "directory"
        ],
        "documents": [
            entry for entry in serialized if entry["kind"] == "document"
        ],
    }


async def _refresh_document(
    service: MemoryService,
    resolver: InvocationResolver,
    ctx: Context,
    app_instance_id: Any,
    signing_key: bytes,
    path: str,
) -> dict[str, Any] | None:
    try:
        invocation = await _verified_app_invocation(
            resolver,
            ctx,
            MemoryAction.READ,
            app_instance_id,
            signing_key,
        )
        snapshot = await service.read(invocation, path)
        return document_payload(snapshot)
    except (AuthorizationDenied, NotFoundOrDenied):
        return None


async def _refresh_listing(
    service: MemoryService,
    resolver: InvocationResolver,
    ctx: Context,
    app_instance_id: Any,
    signing_key: bytes,
    path: str,
) -> dict[str, Any]:
    parent = _parent_path(path)
    try:
        invocation = await _verified_app_invocation(
            resolver,
            ctx,
            MemoryAction.LIST,
            app_instance_id,
            signing_key,
        )
        entries = await service.list(invocation, parent)
        return _browser_listing(parent, entries)
    except (AuthorizationDenied, NotFoundOrDenied):
        return _browser_listing(parent, ())


async def _refresh_document_and_listing(
    service: MemoryService,
    resolver: InvocationResolver,
    ctx: Context,
    app_instance_id: Any,
    signing_key: bytes,
    path: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    selected = await _refresh_document(
        service, resolver, ctx, app_instance_id, signing_key, path
    )
    listing = await _refresh_listing(
        service, resolver, ctx, app_instance_id, signing_key, path
    )
    return selected, listing


def _next_key() -> str:
    return uuid4().hex


def _mutation_response(
    *,
    ok: bool,
    code: str,
    message: str,
    result: dict[str, Any] | None,
    selected: dict[str, Any] | None,
    listing: dict[str, Any],
    draft_path: str,
    draft_content: str,
    append_content: str,
    current_version: int | None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "result": result or {},
        "selected": selected or {},
        "listing": listing,
        "draft_path": draft_path,
        "draft_content": draft_content,
        "append_content": append_content,
        "current_version": current_version,
        "next_idempotency_key": _next_key(),
    }


def _load_directory_actions(
    tool: Any, path: Any, app_instance_id: str
) -> list[Any]:
    return [
        SetState("loading", True),
        SetState("load_error", ""),
        CallTool(
            tool,
            arguments={"path": path, "app_instance_id": app_instance_id},
            on_success=[
                SetState("listing", RESULT),
                SetState("selected", {}),
                SetState("draft_path", ""),
                SetState("draft_content", ""),
                SetState("append_content", ""),
                SetState("current_version", None),
                SetState("mutation", {}),
                SetState("editing", False),
                SetState("new_file_open", False),
                SetState("new_folder", ""),
                SetState("new_filename", ""),
                SetState("loading", False),
            ],
            on_error=[
                SetState("loading", False),
                SetState(
                    "load_error",
                    "The directory could not be loaded for this verified capability.",
                ),
            ],
        ),
    ]


def _open_document_actions(
    tool: Any, path: Any, app_instance_id: str
) -> list[Any]:
    return [
        SetState("loading", True),
        SetState("load_error", ""),
        CallTool(
            tool,
            arguments={"path": path, "app_instance_id": app_instance_id},
            on_success=[
                SetState("selected", RESULT),
                SetState("draft_path", RESULT.path),
                SetState("draft_content", RESULT.content),
                SetState("append_content", ""),
                SetState("current_version", RESULT.version),
                SetState("mutation", {}),
                SetState("editing", False),
                SetState("new_file_open", False),
                SetState("loading", False),
            ],
            on_error=[
                SetState("loading", False),
                SetState(
                    "load_error",
                    (
                        "The document is unavailable or not authorized for this "
                        "capability."
                    ),
                ),
            ],
        ),
    ]


def _apply_mutation_actions() -> list[Any]:
    return [
        SetState("mutation", RESULT),
        SetState("listing", RESULT.listing),
        SetState("selected", RESULT.selected),
        SetState("draft_path", RESULT.draft_path),
        SetState("draft_content", RESULT.draft_content),
        SetState("append_content", RESULT.append_content),
        SetState("current_version", RESULT.current_version),
        SetState("idempotency_key", RESULT.next_idempotency_key),
        SetState("load_error", ""),
        SetState("saving", False),
    ]


def create_memory_browser_app(
    service: MemoryService,
    invocation_resolver: InvocationResolver,
    *,
    app_instance_signing_key: bytes | None = None,
) -> FastMCPApp:
    """Build a current-capability-only browser/editor MCP App provider."""
    _prefer_bundled_renderer()
    if app_instance_signing_key is None:
        signing_key = os.urandom(32)
    elif (
        not isinstance(app_instance_signing_key, bytes)
        or len(app_instance_signing_key) < 32
    ):
        raise ValueError("app_instance_signing_key must contain at least 32 bytes")
    else:
        signing_key = app_instance_signing_key
    app = FastMCPApp(_APP_NAME)

    @app.tool(name="ui_memory_list")
    async def ui_memory_list(
        ctx: Context,
        app_instance_id: AppInstanceId,
        path: MemoryPath = "/",
    ) -> dict[str, Any]:
        """List direct children for the current verified capability."""
        invocation = await _verified_app_invocation(
            invocation_resolver,
            ctx,
            MemoryAction.LIST,
            app_instance_id,
            signing_key,
        )
        entries = await service.list(invocation, path)
        return _browser_listing(path, entries)

    @app.tool(name="ui_memory_read")
    async def ui_memory_read(
        ctx: Context,
        app_instance_id: AppInstanceId,
        path: MemoryPath,
    ) -> dict[str, Any]:
        """Read one document for the current verified capability."""
        invocation = await _verified_app_invocation(
            invocation_resolver,
            ctx,
            MemoryAction.READ,
            app_instance_id,
            signing_key,
        )
        snapshot = await service.read(invocation, path)
        return document_payload(snapshot)

    @app.tool(name="ui_memory_save")
    async def ui_memory_save(
        ctx: Context,
        app_instance_id: AppInstanceId,
        path: MemoryPath,
        content: WriteMarkdownContent,
        idempotency_key: IdempotencyKey,
        expected_version: OptionalExpectedVersion = None,
    ) -> dict[str, Any]:
        """Create or CAS-replace one document from the browser."""
        invocation = await _verified_app_invocation(
            invocation_resolver,
            ctx,
            MemoryAction.WRITE,
            app_instance_id,
            signing_key,
        )
        try:
            result = await service.write(
                invocation,
                path,
                content,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except VersionConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="version_conflict",
                message=(
                    "This document changed after it was opened. Your draft was "
                    "kept; review the latest stored version before retrying."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=path,
                draft_content=content,
                append_content="",
                current_version=selected["version"] if selected else None,
            )
        except IdempotencyConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="idempotency_conflict",
                message=(
                    "That retry key belongs to a different change. A fresh key is "
                    "ready; review the draft and retry."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=path,
                draft_content=content,
                append_content="",
                current_version=selected["version"] if selected else None,
            )

        selected, listing = await _refresh_document_and_listing(
            service,
            invocation_resolver,
            ctx,
            app_instance_id,
            signing_key,
            path,
        )
        replay = result.idempotent_replay
        return _mutation_response(
            ok=True,
            code="idempotent_replay" if replay else "saved",
            message="Saved as a safe retry." if replay else "Saved.",
            result=write_payload(result),
            selected=selected,
            listing=listing,
            draft_path=selected["path"] if selected else result.path,
            draft_content=selected["content"] if selected else content,
            append_content="",
            current_version=selected["version"] if selected else result.version,
        )

    @app.tool(name="ui_memory_append")
    async def ui_memory_append(
        ctx: Context,
        app_instance_id: AppInstanceId,
        path: MemoryPath,
        content: AppendMarkdownContent,
        expected_version: ExpectedVersion,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        """CAS-append to one document from the browser."""
        invocation = await _verified_app_invocation(
            invocation_resolver,
            ctx,
            MemoryAction.APPEND,
            app_instance_id,
            signing_key,
        )
        try:
            result = await service.append(
                invocation,
                path,
                content,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except VersionConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="version_conflict",
                message=(
                    "This document changed after it was opened. The append text "
                    "was kept; review the latest version before retrying."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=selected["path"] if selected else path,
                draft_content=selected["content"] if selected else "",
                append_content=content,
                current_version=(
                    selected["version"] if selected else expected_version
                ),
            )
        except IdempotencyConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="idempotency_conflict",
                message=(
                    "That retry key belongs to a different append. A fresh key is "
                    "ready; review and retry."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=selected["path"] if selected else path,
                draft_content=selected["content"] if selected else "",
                append_content=content,
                current_version=(
                    selected["version"] if selected else expected_version
                ),
            )

        selected, listing = await _refresh_document_and_listing(
            service,
            invocation_resolver,
            ctx,
            app_instance_id,
            signing_key,
            path,
        )
        replay = result.idempotent_replay
        return _mutation_response(
            ok=True,
            code="idempotent_replay" if replay else "appended",
            message="Append replayed safely." if replay else "Appended.",
            result=write_payload(result),
            selected=selected,
            listing=listing,
            draft_path=selected["path"] if selected else result.path,
            draft_content=selected["content"] if selected else "",
            append_content="",
            current_version=selected["version"] if selected else result.version,
        )

    @app.tool(name="ui_memory_delete")
    async def ui_memory_delete(
        ctx: Context,
        app_instance_id: AppInstanceId,
        path: MemoryPath,
        expected_version: ExpectedVersion,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, Any]:
        """CAS-soft-delete one document from the browser."""
        invocation = await _verified_app_invocation(
            invocation_resolver,
            ctx,
            MemoryAction.DELETE,
            app_instance_id,
            signing_key,
        )
        try:
            result = await service.delete(
                invocation,
                path,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except VersionConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="version_conflict",
                message=(
                    "This document changed after it was opened. Review the latest "
                    "version before deleting it."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=selected["path"] if selected else path,
                draft_content=selected["content"] if selected else "",
                append_content="",
                current_version=(
                    selected["version"] if selected else expected_version
                ),
            )
        except IdempotencyConflict:
            selected, listing = await _refresh_document_and_listing(
                service,
                invocation_resolver,
                ctx,
                app_instance_id,
                signing_key,
                path,
            )
            return _mutation_response(
                ok=False,
                code="idempotency_conflict",
                message=(
                    "That retry key belongs to a different delete. A fresh key is "
                    "ready; review and retry."
                ),
                result=None,
                selected=selected,
                listing=listing,
                draft_path=selected["path"] if selected else path,
                draft_content=selected["content"] if selected else "",
                append_content="",
                current_version=(
                    selected["version"] if selected else expected_version
                ),
            )

        listing = await _refresh_listing(
            service,
            invocation_resolver,
            ctx,
            app_instance_id,
            signing_key,
            path,
        )
        replay = result.idempotent_replay
        return _mutation_response(
            ok=True,
            code="idempotent_replay" if replay else "deleted",
            message=(
                "Delete replayed safely."
                if replay
                else (
                    "Access denied immediately; encrypted data is eligible for "
                    "host-operated retention cleanup after its purge time."
                )
            ),
            result=delete_payload(result),
            selected=None,
            listing=listing,
            draft_path="",
            draft_content="",
            append_content="",
            current_version=None,
        )

    @app.ui(
        name="memory_browse",
        title="Browse current agent memory",
        description=(
            "Open a private file-tree browser and Markdown editor for only the "
            "currently verified agent capability. Use this when the user asks to "
            "browse, inspect, edit, append to, or delete their agent memory in a UI."
        ),
        annotations=_READ_ONLY,
    )
    async def memory_browse(ctx: Context) -> ToolResult:
        invocation: VerifiedInvocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.LIST
        )
        app_instance_id = _issue_app_instance(invocation, signing_key)
        has_version = STATE.current_version != None  # noqa: E711
        is_new_document = STATE.current_version == None  # noqa: E711
        is_editing = STATE.editing == True  # noqa: E712
        has_conflict_content = (
            STATE.mutation.selected.content != None  # noqa: E711
        )
        optional_folder_prefix = (STATE.new_folder == "").then(
            "", STATE.new_folder + "/"
        )
        new_document_path = "/" + optional_folder_prefix + STATE.new_filename
        delete_arguments = {
            "app_instance_id": app_instance_id,
            "path": STATE.draft_path,
            "expected_version": STATE.current_version,
            "idempotency_key": STATE.idempotency_key,
        }
        delete_success_actions = [
            *_apply_mutation_actions(),
            SetState("editing", False),
            CloseOverlay(),
        ]
        delete_error = (
            "The delete failed; the document remains available."
        )

        with Column(gap=4, css_class="p-4 md:p-6") as view:
            with Row(gap=3, align="center", justify="between"):
                with Column(gap=1):
                    Heading("Agent memory", level=1)
                    Muted(
                        "Browse and edit only the memory authorized for this "
                        "verified agent context."
                    )
                Badge("Current capability", variant="secondary")

            with Alert(variant="info", icon="shield-check"):
                AlertTitle("Private by capability")
                AlertDescription(
                    "The browser never accepts workspace or profile "
                    "identifiers. "
                    "Changing agent context requires a new verified invocation."
                )

            with If(STATE.load_error != ""):
                with Alert(variant="destructive", icon="triangle-alert"):
                    AlertTitle("Memory unavailable")
                    AlertDescription("{{ load_error }}")

            with Grid(columns={"default": 1, "lg": 2}, gap=4):
                with Card():
                    with CardHeader():
                        with Row(gap=2, align="center", justify="between"):
                            CardTitle("File tree")
                            Badge("{{ listing.path }}", variant="outline")
                    with CardContent():
                        with Column(gap=3):
                            with Row(gap=2):
                                Button(
                                    "Up",
                                    icon="corner-left-up",
                                    variant="outline",
                                    size="sm",
                                    disabled=STATE.listing.path == "/",
                                    on_click=_load_directory_actions(
                                        ui_memory_list,
                                        STATE.listing.parent_path,
                                        app_instance_id,
                                    ),
                                )
                                Button(
                                    "Refresh",
                                    icon="refresh-cw",
                                    variant="outline",
                                    size="sm",
                                    on_click=_load_directory_actions(
                                        ui_memory_list,
                                        STATE.listing.path,
                                        app_instance_id,
                                    ),
                                )

                            with If(~STATE.new_file_open):
                                Button(
                                    "Add file",
                                    icon="file-plus",
                                    variant="outline",
                                    size="sm",
                                    css_class="w-full",
                                    on_click=[
                                        SetState("new_file_open", True),
                                        SetState(
                                            "new_folder",
                                            STATE.listing.folder_input,
                                        ),
                                        SetState("new_filename", ""),
                                    ],
                                )

                            with If(STATE.new_file_open):
                                with Column(gap=2, css_class="rounded-md bg-muted/40 p-2"):
                                    with Grid(columns=2, gap=2):
                                        Input(
                                            name="new_folder",
                                            value=STATE.new_folder,
                                            placeholder="Folder (optional)",
                                            on_change=SetState(
                                                "new_folder", Rx("$event")
                                            ),
                                        )
                                        Input(
                                            name="new_filename",
                                            value=STATE.new_filename,
                                            placeholder="filename.md",
                                            on_change=SetState(
                                                "new_filename", Rx("$event")
                                            ),
                                        )
                                    Muted(
                                        "Folder path is from root. Leave it blank "
                                        "to add the file at root."
                                    )
                                    with Row(gap=2, justify="end"):
                                        Button(
                                            "Cancel",
                                            variant="outline",
                                            size="sm",
                                            on_click=[
                                                SetState("new_file_open", False),
                                                SetState("new_folder", ""),
                                                SetState("new_filename", ""),
                                            ],
                                        )
                                        Button(
                                            "Continue",
                                            icon="arrow-right",
                                            size="sm",
                                            disabled=STATE.new_filename == "",
                                            on_click=[
                                                SetState("selected", {}),
                                                SetState(
                                                    "draft_path", new_document_path
                                                ),
                                                SetState("draft_content", ""),
                                                SetState("append_content", ""),
                                                SetState("current_version", None),
                                                SetState("mutation", {}),
                                                SetState("editing", True),
                                                SetState("new_file_open", False),
                                                SetState("new_folder", ""),
                                                SetState("new_filename", ""),
                                            ],
                                        )

                            with If(STATE.loading):
                                with Row(gap=2, align="center"):
                                    Loader()
                                    Muted("Loading authorized memory…")

                            with If(
                                (~STATE.loading)
                                & (STATE.listing.directories.length() == 0)
                                & (STATE.listing.documents.length() == 0)
                            ):
                                Muted("This directory is empty.")

                            with ForEach("listing.directories") as entry:
                                Button(
                                    entry.name,
                                    icon="folder",
                                    variant="ghost",
                                    css_class="w-full justify-start",
                                    on_click=_load_directory_actions(
                                        ui_memory_list,
                                        entry.path,
                                        app_instance_id,
                                    ),
                                )

                            with ForEach("listing.documents") as entry:
                                Button(
                                    entry.name,
                                    icon="file-text",
                                    variant="ghost",
                                    css_class="w-full justify-start",
                                    on_click=_open_document_actions(
                                        ui_memory_read,
                                        entry.path,
                                        app_instance_id,
                                    ),
                                )

                with Card():
                    with CardHeader():
                        with Row(gap=2, align="center", justify="between"):
                            CardTitle("Memory document")
                            with If(has_version):
                                Badge(
                                    "Version {{ current_version }}",
                                    variant="outline",
                                )
                    with CardContent():
                        with Column(gap=4):
                            with If(STATE.draft_path == ""):
                                Muted(
                                    "Choose a document or create a new Markdown file."
                                )

                            with If(STATE.draft_path != ""):
                                with Row(gap=2, align="center", justify="between"):
                                    Text("{{ draft_path }}", code=True)
                                    with If(has_version & (~is_editing)):
                                        Button(
                                            "Edit",
                                            icon="pencil",
                                            variant="outline",
                                            size="sm",
                                            on_click=[
                                                SetState("editing", True),
                                                SetState("mutation", {}),
                                                SetState("load_error", ""),
                                            ],
                                        )

                                with If(~is_editing):
                                    with If(STATE.selected.content != ""):
                                        Markdown(
                                            "{{ selected.content }}",
                                            css_class="min-h-64",
                                        )
                                    with If(STATE.selected.content == ""):
                                        Muted("This file is empty.")

                                with If(is_editing):
                                    with Field():
                                        with FieldContent():
                                            FieldTitle("Markdown")
                                            FieldDescription(
                                                "Save uses create-only or exact-version "
                                                "compare-and-swap."
                                            )
                                        Textarea(
                                            name="memory_content",
                                            value=STATE.draft_content,
                                            rows=16,
                                            placeholder="# Memory\n",
                                            on_change=SetState(
                                                "draft_content", Rx("$event")
                                            ),
                                        )
                                    with Row(gap=2, justify="end"):
                                        with If(is_new_document):
                                            Button(
                                                "Cancel",
                                                variant="outline",
                                                on_click=[
                                                    SetState("draft_path", ""),
                                                    SetState("draft_content", ""),
                                                    SetState("editing", False),
                                                    SetState("mutation", {}),
                                                ],
                                            )
                                        with If(has_version):
                                            Button(
                                                "Cancel",
                                                variant="outline",
                                                on_click=[
                                                    SetState(
                                                        "draft_content",
                                                        STATE.selected.content,
                                                    ),
                                                    SetState("editing", False),
                                                    SetState("mutation", {}),
                                                ],
                                            )
                                        Button(
                                            is_new_document.then(
                                                "Create file", "Save"
                                            ),
                                            icon="save",
                                            disabled=STATE.saving,
                                            on_click=[
                                                SetState("saving", True),
                                                SetState("mutation", {}),
                                                SetState("load_error", ""),
                                                CallTool(
                                                    ui_memory_save,
                                                    arguments={
                                                        "app_instance_id": app_instance_id,
                                                        "path": STATE.draft_path,
                                                        "content": STATE.draft_content,
                                                        "expected_version": (
                                                            STATE.current_version
                                                        ),
                                                        "idempotency_key": (
                                                            STATE.idempotency_key
                                                        ),
                                                    },
                                                    on_success=[
                                                        *_apply_mutation_actions(),
                                                        SetState(
                                                            "editing",
                                                            RESULT.ok != True,  # noqa: E712
                                                        ),
                                                    ],
                                                    on_error=[
                                                        SetState("saving", False),
                                                        SetState(
                                                            "load_error",
                                                            (
                                                                "The save failed without "
                                                                "changing the open draft."
                                                            ),
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        )

                                with If(has_version & (~is_editing)):
                                    Separator()
                                    with Field():
                                        with FieldContent():
                                            FieldTitle("Append")
                                            FieldDescription(
                                                "Append also checks the open version "
                                                "and preserves this text on conflict."
                                            )
                                        Textarea(
                                            name="append_content",
                                            value=STATE.append_content,
                                            rows=5,
                                            placeholder="Add a dated note…",
                                            on_change=SetState(
                                                "append_content", Rx("$event")
                                            ),
                                        )
                                    with Row(gap=2):
                                        Button(
                                            "Append",
                                            icon="list-plus",
                                            variant="secondary",
                                            disabled=(STATE.saving)
                                            | (STATE.append_content == ""),
                                            on_click=[
                                                SetState("saving", True),
                                                SetState("mutation", {}),
                                                CallTool(
                                                    ui_memory_append,
                                                    arguments={
                                                        "app_instance_id": (
                                                            app_instance_id
                                                        ),
                                                        "path": STATE.draft_path,
                                                        "content": STATE.append_content,
                                                        "expected_version": (
                                                            STATE.current_version
                                                        ),
                                                        "idempotency_key": (
                                                            STATE.idempotency_key
                                                        ),
                                                    },
                                                    on_success=(
                                                        _apply_mutation_actions()
                                                    ),
                                                    on_error=[
                                                        SetState("saving", False),
                                                        SetState(
                                                            "load_error",
                                                            (
                                                                "The append failed "
                                                                "without changing its "
                                                                "draft text."
                                                            ),
                                                        ),
                                                    ],
                                                ),
                                            ],
                                        )
                                        with Dialog(
                                            title="Delete this memory?",
                                            description=(
                                                "Access is denied immediately. "
                                                "The host janitor removes encrypted "
                                                "versions after their purge time."
                                            ),
                                        ):
                                            Button(
                                                "Delete",
                                                icon="trash-2",
                                                variant="destructive",
                                            )
                                            Text(
                                                "This applies exact-version "
                                                "compare-and-swap, so a newer edit "
                                                "cannot be deleted accidentally."
                                            )
                                            with Row(gap=2, justify="end"):
                                                Button(
                                                    "Cancel",
                                                    variant="outline",
                                                    on_click=CloseOverlay(),
                                                )
                                                Button(
                                                    "Confirm delete",
                                                    variant="destructive",
                                                    on_click=[
                                                        SetState("saving", True),
                                                        SetState("mutation", {}),
                                                        CallTool(
                                                            ui_memory_delete,
                                                            arguments=(
                                                                delete_arguments
                                                            ),
                                                            on_success=(
                                                                delete_success_actions
                                                            ),
                                                            on_error=[
                                                                SetState(
                                                                    "saving", False
                                                                ),
                                                                SetState(
                                                                    "load_error",
                                                                    (
                                                                        delete_error
                                                                    ),
                                                                ),
                                                            ],
                                                        ),
                                                    ],
                                                )

                            with If(STATE.mutation.ok == True):  # noqa: E712
                                with Alert(variant="success", icon="circle-check"):
                                    AlertTitle("Memory updated")
                                    AlertDescription("{{ mutation.message }}")

                            with If(STATE.mutation.code == "version_conflict"):
                                with Alert(variant="warning", icon="git-compare"):
                                    AlertTitle("Version conflict")
                                    AlertDescription("{{ mutation.message }}")

                            with If(
                                STATE.mutation.code == "idempotency_conflict"
                            ):
                                with Alert(variant="warning", icon="key-round"):
                                    AlertTitle("Retry-key conflict")
                                    AlertDescription("{{ mutation.message }}")

                            with If(
                                (~STATE.mutation.ok)
                                & has_conflict_content
                            ):
                                Separator()
                                Muted(
                                    "Latest stored document (compare with the "
                                    "preserved draft):"
                                )
                                Text(
                                    "{{ mutation.selected.content }}",
                                    code=True,
                                )

        prefab = PrefabApp(
            title="Agent memory",
            view=view,
            state={
                "listing": {
                    "path": "/",
                    "parent_path": "/",
                    "folder_input": "",
                    "directories": [],
                    "documents": [],
                },
                "selected": {},
                "draft_path": "",
                "draft_content": "",
                "append_content": "",
                "current_version": None,
                "editing": False,
                "new_file_open": False,
                "new_folder": "",
                "new_filename": "",
                "idempotency_key": _next_key(),
                "mutation": {},
                "loading": True,
                "saving": False,
                "load_error": "",
            },
            on_mount=CallTool(
                ui_memory_list,
                arguments={
                    "app_instance_id": app_instance_id,
                    "path": "/",
                },
                on_success=[
                    SetState("listing", RESULT),
                    SetState("loading", False),
                ],
                on_error=[
                    SetState("loading", False),
                    SetState(
                        "load_error",
                        "Memory could not be loaded for this verified capability.",
                    ),
                ],
            ),
        )

        return ToolResult(
            content=(
                "Opened the private memory browser for the current verified agent "
                "capability. Memory paths and document contents load only inside "
                "the app."
            ),
            structured_content=prefab.to_json(
                tool_resolver=_make_resolver(_APP_NAME)
            ),
        )

    return app


__all__ = ["create_memory_browser_app"]
