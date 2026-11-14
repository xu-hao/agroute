"""Configurable local agent router with native usage-limit filtering and howhard integration."""

__version__ = "0.5.0"

from .adapters import AgentAdapter, get_adapter, list_adapters, register_adapter
from .config import load_config
from .router import Router
from .validation import AgRouteError

__all__ = [
    "AgRouteError",
    "AgentAdapter",
    "Router",
    "get_adapter",
    "list_adapters",
    "load_config",
    "register_adapter",
    "__version__",
]
