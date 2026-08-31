"""Bundled management UI and FastAPI composition helpers."""

from .frontend import (
    FrontendAuthConfig,
    ManagementFrontendConfig,
    create_management_frontend,
)
from .application import create_web_application

__all__ = [
    "FrontendAuthConfig",
    "ManagementFrontendConfig",
    "create_management_frontend",
    "create_web_application",
]
