"""FastMCP adapter for capability-scoped agent memory."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from ..domain.models import MemoryAction, VerifiedInvocation
from ..ports.capabilities import InvocationResolver
from .payloads import (
    DeletePayload,
    DocumentPayload,
    MemoryListPayload,
    WritePayload,
    delete_payload,
    document_payload,
    list_payload,
    write_payload,
)

if TYPE_CHECKING:
    from ..application import MemoryService


MemoryPath = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 4096,
            "description": "Absolute or root-relative virtual Markdown path.",
        }
    ),
]
WriteMarkdownContent = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "maxLength": 1_048_576,
            "description": "UTF-8 Markdown content stored in the encrypted tree.",
        }
    ),
]
AppendMarkdownContent = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 262_144,
            "description": "UTF-8 Markdown appended to an existing document.",
        }
    ),
]
IdempotencyKey = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
            "description": "Caller-stable opaque key used to make retries safe.",
        }
    ),
]
ExpectedVersion = Annotated[
    Any,
    WithJsonSchema(
        {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Exact active document version required by compare-and-swap."
            ),
        }
    ),
]
OptionalExpectedVersion = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
            "description": (
                "Omit only when creating a new path; provide the exact active "
                "version when replacing an existing document."
            ),
        }
    ),
]

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_APPEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_DELETE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

_HEADLESS_TOOL_BOUNDARIES = {
    "memory_list": (MemoryAction.LIST, frozenset({"path"}), frozenset()),
    "memory_read": (
        MemoryAction.READ,
        frozenset({"path"}),
        frozenset({"path"}),
    ),
    "memory_write": (
        MemoryAction.WRITE,
        frozenset({"path", "content", "idempotency_key", "expected_version"}),
        frozenset({"path", "content", "idempotency_key"}),
    ),
    "memory_append": (
        MemoryAction.APPEND,
        frozenset({"path", "content", "expected_version", "idempotency_key"}),
        frozenset({"path", "content", "expected_version", "idempotency_key"}),
    ),
    "memory_delete": (
        MemoryAction.DELETE,
        frozenset({"path", "expected_version", "idempotency_key"}),
        frozenset({"path", "expected_version", "idempotency_key"}),
    ),
    "memory_browse": (MemoryAction.LIST, frozenset(), frozenset()),
}
_APP_TOOL_BOUNDARIES = {
    "ui_memory_list": (
        MemoryAction.LIST,
        frozenset({"app_instance_id", "path"}),
        frozenset({"app_instance_id"}),
    ),
    "ui_memory_read": (
        MemoryAction.READ,
        frozenset({"app_instance_id", "path"}),
        frozenset({"app_instance_id", "path"}),
    ),
    "ui_memory_save": (
        MemoryAction.WRITE,
        frozenset(
            {
                "app_instance_id",
                "path",
                "content",
                "idempotency_key",
                "expected_version",
            }
        ),
        frozenset({"app_instance_id", "path", "content", "idempotency_key"}),
    ),
    "ui_memory_append": (
        MemoryAction.APPEND,
        frozenset(
            {
                "app_instance_id",
                "path",
                "content",
                "expected_version",
                "idempotency_key",
            }
        ),
        frozenset(
            {
                "app_instance_id",
                "path",
                "content",
                "expected_version",
                "idempotency_key",
            }
        ),
    ),
    "ui_memory_delete": (
        MemoryAction.DELETE,
        frozenset(
            {"app_instance_id", "path", "expected_version", "idempotency_key"}
        ),
        frozenset(
            {"app_instance_id", "path", "expected_version", "idempotency_key"}
        ),
    ),
}
_APP_TOOL_NAME = re.compile(
    r"^[0-9a-f]{12}_(ui_memory_(?:list|read|save|append|delete))$"
)

_SERVER_INSTRUCTIONS = """\
This server provides a private, versioned Markdown file tree for the currently
verified agent invocation. Workspace, authenticated principal, and stable agent
profile identity always come from the trusted invocation resolver; never ask a
user or model to provide those identifiers as tool arguments.

Use memory_list and memory_read for discovery. memory_write creates a new path
when expected_version is omitted and replaces an existing document only with
its exact active version. memory_append and memory_delete also require exact
active versions. Reuse the same idempotency key only when retrying the identical
mutation.
"""


async def _verified_invocation(
    resolver: InvocationResolver,
    ctx: Context,
    action: MemoryAction,
) -> VerifiedInvocation:
    invocation = await resolver(ctx, action)
    if not isinstance(invocation, VerifiedInvocation):
        raise TypeError("invocation_resolver must return VerifiedInvocation")
    invocation.require(action)
    return invocation


def _tool_boundary(
    name: str,
) -> tuple[MemoryAction, frozenset[str], frozenset[str]] | None:
    boundary = _HEADLESS_TOOL_BOUNDARIES.get(name)
    if boundary is not None:
        return boundary
    match = _APP_TOOL_NAME.fullmatch(name)
    if match is None:
        return None
    return _APP_TOOL_BOUNDARIES[match.group(1)]


class _AuthorizationFirstArgumentMiddleware(Middleware):
    """Keep FastMCP reflection errors from echoing private unexpected values.

    FastMCP validates function signatures before entering a tool and includes
    unexpected values in both its warning and MCP error. For memory tools, turn
    that case into a deliberately invalid path while retaining or filling the
    declared arguments. The tool then resolves its capability (and, for MCP App
    helpers, its app-instance binding) before normal content-free path
    validation rejects the call. Tools without a path authorize here and return
    one fixed error without inspecting or rendering argument values.
    """

    def __init__(self, resolver: InvocationResolver) -> None:
        self._resolver = resolver

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> Any:
        boundary = _tool_boundary(context.message.name)
        arguments = context.message.arguments or {}
        if boundary is None:
            return await call_next(context)

        action, allowed, required = boundary
        if set(arguments).issubset(allowed):
            return await call_next(context)

        if "path" not in allowed:
            if context.fastmcp_context is None:
                raise ToolError("memory operation is not authorized")
            await _verified_invocation(
                self._resolver,
                context.fastmcp_context,
                action,
            )
            raise ToolError("invalid memory tool arguments")

        sanitized = {
            name: value for name, value in arguments.items() if name in allowed
        }
        for name in required:
            sanitized.setdefault(name, None)
        sanitized["path"] = None
        message = context.message.model_copy(update={"arguments": sanitized})
        return await call_next(context.copy(message=message))


def create_mcp_server(
    service: MemoryService,
    invocation_resolver: InvocationResolver,
    auth: Any = None,
    include_app: bool = False,
    app_instance_signing_key: bytes | None = None,
) -> FastMCP:
    """Create a server whose tools derive scope only from verified context.

    ``service`` may use any persistence adapter. ``invocation_resolver`` is a
    trusted host callback that binds each FastMCP request context to a
    :class:`VerifiedInvocation` for the requested action.
    """

    mcp = FastMCP(
        "Agent Filetree Memory",
        instructions=_SERVER_INSTRUCTIONS,
        auth=auth,
    )
    mcp.add_middleware(_AuthorizationFirstArgumentMiddleware(invocation_resolver))

    @mcp.tool(
        name="memory_list",
        title="List agent memory",
        annotations=_READ_ONLY,
    )
    async def memory_list(
        ctx: Context,
        path: MemoryPath = "/",
    ) -> MemoryListPayload:
        """List direct children in the current verified agent's memory tree."""
        invocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.LIST
        )
        entries = await service.list(invocation, path)
        return list_payload(path, entries)

    @mcp.tool(
        name="memory_read",
        title="Read agent memory",
        annotations=_READ_ONLY,
    )
    async def memory_read(
        ctx: Context,
        path: MemoryPath,
    ) -> DocumentPayload:
        """Read one Markdown document from the current verified agent's tree."""
        invocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.READ
        )
        snapshot = await service.read(invocation, path)
        return document_payload(snapshot)

    @mcp.tool(
        name="memory_write",
        title="Create or replace agent memory",
        annotations=_WRITE,
    )
    async def memory_write(
        ctx: Context,
        path: MemoryPath,
        content: WriteMarkdownContent,
        idempotency_key: IdempotencyKey,
        expected_version: OptionalExpectedVersion = None,
    ) -> WritePayload:
        """Create a new document or CAS-replace an existing document.

        Omit ``expected_version`` only for a new path. To replace a document,
        pass the exact version returned by ``memory_read``. Reuse an
        ``idempotency_key`` only for an identical retry.
        """
        invocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.WRITE
        )
        result = await service.write(
            invocation,
            path,
            content,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        return write_payload(result)

    @mcp.tool(
        name="memory_append",
        title="Append agent memory",
        annotations=_APPEND,
    )
    async def memory_append(
        ctx: Context,
        path: MemoryPath,
        content: AppendMarkdownContent,
        expected_version: ExpectedVersion,
        idempotency_key: IdempotencyKey,
    ) -> WritePayload:
        """Append Markdown using exact-version CAS and an idempotency key."""
        invocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.APPEND
        )
        result = await service.append(
            invocation,
            path,
            content,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        return write_payload(result)

    @mcp.tool(
        name="memory_delete",
        title="Delete agent memory",
        annotations=_DELETE,
    )
    async def memory_delete(
        ctx: Context,
        path: MemoryPath,
        expected_version: ExpectedVersion,
        idempotency_key: IdempotencyKey,
    ) -> DeletePayload:
        """Deny access immediately and mark a CAS-selected version for later purge."""
        invocation = await _verified_invocation(
            invocation_resolver, ctx, MemoryAction.DELETE
        )
        result = await service.delete(
            invocation,
            path,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
        return delete_payload(result)

    if include_app:
        try:
            from ..mcp_app import create_memory_browser_app
        except ImportError as exc:  # pragma: no cover - depends on optional install
            if exc.name and (
                exc.name == "prefab_ui" or exc.name.startswith("prefab_ui.")
            ):
                raise RuntimeError(
                    "The MCP App requires the 'app' extra; install "
                    "agent-filetree-memory-mcp[app] or pass include_app=False."
                ) from exc
            raise

        mcp.add_provider(
            create_memory_browser_app(
                service,
                invocation_resolver,
                app_instance_signing_key=app_instance_signing_key,
            )
        )

    return mcp


__all__ = ["create_mcp_server"]
