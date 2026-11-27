"""shadow-memory — 零依赖跨 Agent / 跨 Session 影子记忆服务。

    from shadow_memory import Store, open_store, compile_project
    from shadow_memory.redact import redact

``redact`` is deliberately *not* re-exported at package level: doing so would
shadow the ``shadow_memory.redact`` submodule and break ``import
shadow_memory.redact``.
"""

__version__ = "0.1.0"

from .redact import contains_secret, redact as redact_text  # noqa: F401
from .store import (  # noqa: F401
    Store,
    open_store,
    project_id_for,
    resolve_db_path,
    resolve_home,
    similarity,
)

__all__ = [
    "__version__",
    "redact_text",
    "contains_secret",
    "Store",
    "open_store",
    "resolve_home",
    "resolve_db_path",
    "project_id_for",
    "similarity",
]