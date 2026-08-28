"""Resources and host hooks for the package's Alembic revision branch.

Hosts keep ownership of their Alembic environment and database credentials.
They can append :func:`package_version_location` to ``version_locations`` and
call :func:`configure_host_alembic` before constructing ``ScriptDirectory``.
The revision reads the validated schema from ``Config.attributes``.
"""

from __future__ import annotations

from importlib.resources import files
import os
from pathlib import Path
from typing import Any

from alembic.config import Config

from ..schema import DEFAULT_SCHEMA, tables_for_schema, validate_schema_name

SCHEMA_ATTRIBUTE = "agent_filetree_memory_schema"


def package_version_location() -> Path:
    """Return the installed filesystem path containing package revisions."""

    resource = files(__package__).joinpath("versions")
    path = Path(str(resource))
    if not path.is_dir():
        raise RuntimeError("packaged Alembic revisions are unavailable")
    return path


def configure_host_alembic(
    config: Config,
    *,
    schema: str = DEFAULT_SCHEMA,
    append_version_location: bool = True,
) -> Config:
    """Configure a host ``Config`` for this independent revision branch.

    This helper never reads an environment variable or opens a database. The
    host remains responsible for its connection, version table, and schema
    creation policy.
    """

    schema = validate_schema_name(schema)
    config.attributes[SCHEMA_ATTRIBUTE] = schema
    if append_version_location:
        location = str(package_version_location())
        current = config.get_main_option("version_locations", "").strip()
        separator_mode = config.get_main_option("path_separator", "").strip()
        if not separator_mode:
            separator_mode = "os"
            config.set_main_option("path_separator", separator_mode)
        separator = {
            "os": os.pathsep,
            "space": " ",
            "newline": "\n",
        }.get(separator_mode, " ")
        locations = current.split(separator) if current else []
        if not current:
            # Setting version_locations disables Alembic's implicit
            # <script_location>/versions lookup. Preserve it before appending
            # this package's independent revision branch.
            script_location = config.get_main_option("script_location", "").strip()
            if script_location:
                locations.append(str(Path(script_location) / "versions"))
        if location not in locations:
            locations.append(location)
        config.set_main_option("version_locations", separator.join(locations))
    return config


def schema_from_config(config: Config | Any) -> str:
    """Resolve the schema selected explicitly by the host."""

    value = config.attributes.get(SCHEMA_ATTRIBUTE, DEFAULT_SCHEMA)
    return validate_schema_name(value)


def migration_metadata(schema: str = DEFAULT_SCHEMA):
    """Return metadata for host Alembic autogenerate configuration."""

    return tables_for_schema(validate_schema_name(schema)).metadata


__all__ = [
    "SCHEMA_ATTRIBUTE",
    "configure_host_alembic",
    "migration_metadata",
    "package_version_location",
    "schema_from_config",
]
