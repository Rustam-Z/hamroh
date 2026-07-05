"""Dependency-light utilities used across the codebase.

Small, mostly self-contained modules with no ownership of runtime state:
YAML frontmatter parsing (``frontmatter``), traversal-hardened path
resolution (``path_safety``), credential redaction (``secrets_scrubber``),
Unicode-injection defang (``input_normalizer``), and Markdown→Telegram-HTML
plus message chunking (``formatting``). App-domain helpers — logging wiring
and the transcript model — live in :mod:`hamroh.helpers`. Imported directly
(``from hamroh.utils.path_safety import resolve_under_root``); this package
intentionally re-exports nothing.
"""

from __future__ import annotations
