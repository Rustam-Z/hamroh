"""Disk-backed stores for the bot's file state.

Each module is a thin wrapper around one storage folder — the git-tracked
``memories/`` at the repo root, plus ``data/attachments/``,
``data/renders/``, the operator-curated ``skills/`` (``skills_store``) and
``prompts/`` (``instructions_store``) — that does path-safety hardening,
size capping, and read/write helpers. The shape is the same everywhere: a
``Store`` class with ``ensure_root``, ``resolve_path``, plus per-kind
read/write methods.

The core stores re-export their public API here so callers can write
``from hamroh.storage import MemoryStore`` etc. without caring which
submodule houses each class. ``skills_store`` and ``instructions_store``
are imported from their submodule directly (``from hamroh.storage.skills_store
import SkillsStore``).
"""

from __future__ import annotations

from .attachments_store import (
    MAX_TEXT_BYTES,
    AttachmentPathError,
    AttachmentStore,
    ImageAttachment,
    TextAttachment,
)
from .memory_store import (
    MAX_MEMORY_BYTES,
    MemoryFile,
    MemoryPathError,
    MemoryStore,
)
from .render_store import Render, RenderPathError, RenderStore

__all__ = [
    "MAX_MEMORY_BYTES",
    "MAX_TEXT_BYTES",
    "AttachmentPathError",
    "AttachmentStore",
    "ImageAttachment",
    "MemoryFile",
    "MemoryPathError",
    "MemoryStore",
    "Render",
    "RenderPathError",
    "RenderStore",
    "TextAttachment",
]
