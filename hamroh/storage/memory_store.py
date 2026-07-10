"""File-backed memory in one git-tracked store at the repo root.

Every memory is addressed by its full project path, ``memories/<path>``. The
``memories/`` prefix is mandatory — a bare ``notes/ref.md`` is rejected. The
bot reads, searches, writes and appends here.

Path resolution is **traversal-hardened**: a missing/unknown prefix, a ``..``
component, an absolute path, a path that resolves outside the root, or one
that crosses a symlink all raise :class:`MemoryPathError` — on reads and
writes alike (see ``tests/unit/test_memory_path_safety.py``).

Writes obey the **read-before-write invariant**: you must read an existing
file this process before overwriting or appending, so the model can't destroy
notes it never saw. New files are exempt. The read set is per-instance, keyed
by resolved absolute path, and resets on restart.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..utils.frontmatter import (
    parse_frontmatter,
    render_frontmatter,
    require_name_and_description,
)
from ..utils.path_safety import resolve_under_root


class MemoryPathError(ValueError):
    """Raised when a memory path is rejected by safety checks."""


#: Project-root prefix that addresses the memory store. Fixed addressing
#: convention — independent of where the store physically lives.
MEMORY_PREFIX = "memories"


@dataclass(frozen=True)
class MemoryFile:
    relative_path: str
    size_bytes: int
    #: One-line summary from the file's frontmatter ``description``, or
    #: ``None`` for legacy files that predate the frontmatter template.
    description: str | None = None


@dataclass(frozen=True)
class MemorySearchHit:
    relative_path: str
    line_number: int
    line: str
    #: How many distinct query terms appear on this line. Used to rank hits.
    score: int


#: Alias for the search return type. The class below defines a method named
#: ``list``, which shadows the builtin ``list`` for type annotations *inside*
#: the class body — so ``list[MemorySearchHit]`` there resolves to the method,
#: not the generic. Spelling the type at module scope (where ``list`` is the
#: builtin) sidesteps that without renaming the public ``list`` method.
_HitList = list[MemorySearchHit]


#: Maximum size of any one memory file. Matches the read-truncation default
#: so a file the model can read fully can also be re-written fully.
MAX_MEMORY_BYTES = 64 * 1024

#: Wording for memory-file frontmatter errors (passed to the shared helpers).
_FM_LABEL = "memory file"

#: Canonical skeleton every memory file must follow. Mirrors the skills
#: protocol: ``name`` + ``description`` frontmatter so ``memory_list`` can
#: surface what a file is about without reading its body.
MEMORY_TEMPLATE = """\
---
name: <short human-friendly label>
description: <one-line summary used to find this memory without reading it>
---

<body — the actual remembered content>
"""


def _require_frontmatter(content: str) -> None:
    """Raise :class:`MemoryPathError` unless ``content`` carries valid frontmatter."""
    metadata, _ = parse_frontmatter(content, error_cls=MemoryPathError, label=_FM_LABEL)
    require_name_and_description(metadata, error_cls=MemoryPathError, label=_FM_LABEL)


def _read_description(path: Path) -> str | None:
    """Best-effort frontmatter ``description`` for ``path``, else ``None``.

    Never raises: a legacy file (no frontmatter), malformed frontmatter, or
    an unreadable file all yield ``None`` so one bad file can't blind the
    whole listing.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata, _ = parse_frontmatter(
            text, error_cls=MemoryPathError, label=_FM_LABEL
        )
    except (OSError, MemoryPathError):
        return None
    description = metadata.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return None


class MemoryStore:
    def __init__(self, root: Path) -> None:
        # ``resolve(strict=False)`` is fine: the root may not exist yet at
        # construction time. ``ensure_root`` creates it.
        self._root = root.resolve()
        #: Resolved absolute paths read in this process. The read-before-write
        #: rule rejects mutating writes to any file not in this set. New
        #: files (which don't yet exist) are exempt — there's nothing to have
        #: read.
        self._read_paths: set[Path] = set()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def read_paths_snapshot(self) -> frozenset[Path]:
        """Test/inspection helper — frozen snapshot of resolved paths read this run."""
        return frozenset(self._read_paths)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _strip_prefix(self, relative: str) -> str:
        """Return the in-store subpath for a project-root memory path.

        The path must start with the ``memories/`` prefix; a bare or
        unknown-prefixed path raises :class:`MemoryPathError` naming the
        valid prefix.
        """
        marker = f"{MEMORY_PREFIX}/"
        if relative.startswith(marker):
            return relative[len(marker) :]
        raise MemoryPathError(
            f"memory path must start with '{marker}': got {relative!r}"
        )

    def resolve_path(self, relative: str) -> Path:
        """Resolve a project-root memory path to its file, hardened.

        Strips the ``memories/`` prefix, then resolves the remainder inside
        the store root. See :func:`hamroh.utils.path_safety.resolve_under_root`
        for the traversal rules; any failure raises :class:`MemoryPathError`.
        """
        subpath = self._strip_prefix(relative)
        return resolve_under_root(self._root, subpath, MemoryPathError, "memory")

    def resolve_readable(self, relative: str) -> Path:
        """Resolve an existing memory file for reading.

        Same prefix-aware resolution as :meth:`resolve_path`, but the file
        must actually exist; otherwise raises :class:`MemoryPathError`.
        """
        path = self.resolve_path(relative)
        if not path.is_file():
            raise MemoryPathError(f"memory file not found: {relative}")
        return path

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def list(self) -> list[MemoryFile]:
        """List every file in the store, recursively, by full project path.

        Each file is named by its project-root path (``memories/…``). Hidden
        files (``.gitkeep``, dotfiles) are skipped. Symlinked entries are
        skipped silently — they cannot be read by ``read`` either. Each file's
        frontmatter ``description`` is surfaced when present (skills protocol);
        legacy files without it get ``description=None``.
        """
        out: list[MemoryFile] = []
        for rel, path in self._iter_files(self._root):
            out.append(
                MemoryFile(
                    relative_path=f"{MEMORY_PREFIX}/{rel}",
                    size_bytes=path.stat().st_size,
                    description=_read_description(path),
                )
            )
        return sorted(out, key=lambda f: f.relative_path)

    @staticmethod
    def _iter_files(root: Path) -> Iterator[tuple[str, Path]]:
        """Yield ``(subpath, path)`` for each readable file under ``root``.

        The subpath is relative to ``root`` (POSIX separators). Skips
        directories, symlinks and dotfiles. A missing root yields nothing.
        """
        if not root.exists():
            return
        for path in sorted(root.rglob("*")):
            if path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:  # pragma: no cover - rglob shouldn't escape
                continue
            yield rel.as_posix(), path

    def read(self, relative: str, max_bytes: int = MAX_MEMORY_BYTES) -> str:
        """Read a memory file as UTF-8, by its full project path.

        Files larger than ``max_bytes`` are truncated and the truncation is
        marked in the returned string so the model knows what happened.
        Records the resolved path in :attr:`_read_paths` so the read-before-
        write gate will allow subsequent writes to the *same file*.
        """
        path = self.resolve_readable(relative)
        raw = path.read_bytes()
        truncated = False
        if len(raw) > max_bytes:
            raw = raw[:max_bytes]
            truncated = True
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n\n[truncated to {max_bytes} bytes]"
        # Record the read AFTER we successfully decoded — so a path that
        # raised never gets credited.
        self._read_paths.add(path)
        return text

    def search(self, query: str, *, max_results: int = 50) -> _HitList:
        """Find lines matching ``query`` across every memory file in the store.

        The query is split into whitespace-separated terms; a line is a hit if
        it contains **at least one** term (case-insensitive), and lines that
        contain more distinct terms rank higher. Splitting per term — rather
        than matching the whole query as one substring — is what lets
        ``"acme deadline"`` find ``"deadline for the Acme project"``.

        Reads current bytes off disk, so results are never stale. Crucially
        this does **not** touch :attr:`_read_paths`: a search is not a "read"
        for the read-before-write gate, or grepping a file would silently
        unlock overwriting it.
        """
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        hits: _HitList = []
        for mf in self.list():
            hits.extend(self._scan_file(mf.relative_path, terms))
        # Rank globally, then truncate, so the best lines survive the cap.
        hits.sort(key=lambda h: (-h.score, h.relative_path, h.line_number))
        return hits[:max_results]

    def _scan_file(self, relative: str, terms: Sequence[str]) -> _HitList:
        """Return every line in one file (by full path) that matches a term."""
        try:
            text = self.resolve_readable(relative).read_text(
                encoding="utf-8", errors="replace"
            )
        except (OSError, MemoryPathError):
            return []
        out: _HitList = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            score = sum(1 for term in terms if term in lowered)
            if score:
                out.append(MemorySearchHit(relative, line_number, line.strip(), score))
        return out

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def write(self, relative: str, content: str) -> int:
        """Create or overwrite a memory file at ``relative`` (full project path).

        Returns the number of bytes written. ``content`` must begin with the
        frontmatter template (``name`` + ``description``); the path must start
        with the ``memories/`` prefix; the UTF-8 byte length must be ≤
        :data:`MAX_MEMORY_BYTES`; and an existing file must have been read
        first (read-before-write). See :data:`MEMORY_TEMPLATE`.
        """
        _require_frontmatter(content)
        path = self.resolve_path(relative)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_MEMORY_BYTES:
            raise MemoryPathError(
                f"memory file too large: {len(encoded)} bytes > {MAX_MEMORY_BYTES} cap"
            )
        if path.exists():
            if path not in self._read_paths:
                raise MemoryPathError(
                    f"refusing to overwrite {relative}: must call memory_read "
                    "first in this session (read-before-write invariant)"
                )
            if not path.is_file():
                raise MemoryPathError(f"{relative} exists but is not a regular file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        # We just wrote it — credit the read so subsequent overwrites in the
        # same session are allowed without an extra round-trip.
        self._read_paths.add(path)
        return len(encoded)

    def append(self, relative: str, content: str, description: str) -> int:
        """Append ``content`` to a memory file's body, refreshing its frontmatter.

        Returns the new total size in bytes.

        Unlike a raw byte-append, this keeps the file's frontmatter current:
        the body grows by ``content`` and the frontmatter ``description`` is
        set to ``description`` on every call, so ``memory_list`` always shows
        an up-to-date summary. ``name`` is preserved from the existing
        frontmatter, or derived from the filename stem for a new or legacy
        (frontmatter-less) file — the first append migrates a legacy file
        onto the template.

        Same path safety + read-before-write rules as :meth:`write`. The
        post-append size must still fit within :data:`MAX_MEMORY_BYTES`.
        """
        path = self.resolve_path(relative)
        name, body = self._existing_name_and_body(path, relative)
        rebuilt = render_frontmatter({"name": name, "description": description})
        rebuilt += f"\n{body}{content}"
        require_name_and_description(
            {"name": name, "description": description},
            error_cls=MemoryPathError,
            label=_FM_LABEL,
        )
        encoded = rebuilt.encode("utf-8")
        if len(encoded) > MAX_MEMORY_BYTES:
            raise MemoryPathError(
                f"append would exceed cap: {len(encoded)} bytes > {MAX_MEMORY_BYTES}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        self._read_paths.add(path)
        return len(encoded)

    def _existing_name_and_body(self, path: Path, relative: str) -> tuple[str, str]:
        """Return ``(name, body)`` for an append target.

        For an existing file the read-before-write gate applies and the
        current frontmatter is parsed: a templated file yields its stored
        ``name`` and body, a legacy file keeps its whole content as the body.
        A brand-new file starts empty. ``name`` falls back to the filename
        stem when the file has no frontmatter ``name``.
        """
        fallback_name = Path(relative).stem or relative
        if not path.exists():
            return fallback_name, ""
        if path not in self._read_paths:
            raise MemoryPathError(
                f"refusing to append to {relative}: must call memory_read "
                "first in this session (read-before-write invariant)"
            )
        if not path.is_file():
            raise MemoryPathError(f"{relative} exists but is not a regular file")
        existing = path.read_text(encoding="utf-8", errors="replace")
        try:
            metadata, body = parse_frontmatter(
                existing, error_cls=MemoryPathError, label=_FM_LABEL
            )
        except MemoryPathError:
            return fallback_name, existing  # legacy file: keep all of it as body
        name = metadata.get("name")
        return (name if isinstance(name, str) and name else fallback_name), body


def render_memory_index(store: MemoryStore) -> str:
    """Render the memory index preloaded into the system prompt.

    Lists every memory file's project path + one-line description so the
    agent always holds its standing context (user preferences, facts,
    ongoing projects) from the first turn — mirroring ``render_skills_index``.
    Baked in at spawn time, so it reloads on every session restart (boot,
    crash respawn, reset, resume) without the model having to call a tool.
    Returns an empty string when the store is empty, so the caller can
    append it unconditionally without leaving a dangling header.
    """
    files = store.list()
    if not files:
        return ""
    lines = "\n".join(
        f"- `{f.relative_path}`" + (f" — {f.description}" if f.description else "")
        for f in files
    )
    return (
        "# Your memory\n\n"
        "Standing context you already hold — carried across restarts under "
        "memories/. Every file's path and one-line description is listed here "
        "so you know what you know before you reply; read a full file with "
        'memory_read("<path>") when one is relevant. memory_list re-fetches '
        "this same list and memory_search finds lines across files — use them "
        "when you suspect a file changed since this session started.\n\n"
        f"{lines}\n"
    )
