"""File-backed memory across two stores, addressed by full project paths.

Memory lives in two folders, and every memory is addressed by its path from
the project root so the two never collide:

- ``data/memories/<path>`` — the runtime store: gitignored, on the Docker
  volume, the bot's day-to-day working memory.
- ``memories/<path>`` — the committed store: git-tracked at the repo root,
  survives a volume loss.

These are two **distinct namespaces**. ``data/memories/notes/ref.md`` and
``memories/notes/ref.md`` are different files and both show up, in full, in
every listing, search and read — nothing is ever shadowed or masked. The
leading folder prefix is mandatory: a bare ``notes/ref.md`` is rejected, so
the caller always states which store it means.

**Reads and searches span both stores; writes and appends touch only the
runtime store** (``data/memories/``). A ``memories/`` write is rejected — the
committed store is read-only to the bot, curated by the operator via git.

Path resolution is **path-traversal hardened** — any of the following must
raise :class:`MemoryPathError`:

- a missing or unknown store prefix
- a component containing ``..``
- an absolute path
- a path whose canonical resolution leaves its store root
- a path whose resolution traverses any symlink

These rules apply to **both reads and writes** and are tested in
``tests/unit/test_memory_path_safety.py``.

Writes are guarded by the **read-before-write invariant** (Claudir Part 3):
before overwriting or appending to an existing file you must first read it
in this process. This stops the model from blindly destroying operator-
curated notes whose content it never observed. Creating a new file (one
that doesn't yet exist) is always allowed because there's nothing to lose.
The "read paths" set lives in this instance and resets on process restart.
It is keyed by the file's **resolved absolute path**.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .frontmatter import (
    parse_frontmatter,
    render_frontmatter,
    require_name_and_description,
)
from .path_safety import resolve_under_root


class MemoryPathError(ValueError):
    """Raised when a memory path is rejected by safety checks."""


#: Project-root prefix that addresses the runtime store. Fixed addressing
#: convention — independent of where the runtime root physically lives.
RUNTIME_PREFIX = "data/memories"

#: Project-root prefix that addresses the committed store.
COMMITTED_PREFIX = "memories"


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
    def __init__(self, root: Path, *, committed_root: Path | None = None) -> None:
        # ``resolve(strict=False)`` is fine: the root may not exist yet at
        # construction time. ``ensure_root`` creates the runtime root.
        self._root = root.resolve()
        #: Ordered ``(prefix, root)`` stores. Only the runtime store when no
        #: committed root is configured. The prefix is how the caller names
        #: which store a path belongs to.
        self._stores: list[tuple[str, Path]] = [(RUNTIME_PREFIX, self._root)]
        if committed_root is not None:
            self._stores.append((COMMITTED_PREFIX, committed_root.resolve()))
        #: Resolved absolute paths read in this process. The read-before-write
        #: rule rejects mutating writes to any file not in this set. New
        #: files (which don't yet exist) are exempt — there's nothing to have
        #: read.
        self._read_paths: set[Path] = set()

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> None:
        # Only the runtime root is created — the committed root is tracked in
        # the repo and managed by the operator, so it already exists (or not).
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def read_paths_snapshot(self) -> frozenset[Path]:
        """Test/inspection helper — frozen snapshot of resolved paths read this run."""
        return frozenset(self._read_paths)

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _split_prefix(self, relative: str) -> tuple[Path, str]:
        """Return ``(store_root, subpath)`` for a project-root memory path.

        The path must start with a known store prefix (``data/memories/`` or
        ``memories/``); a bare or unknown-prefixed path raises
        :class:`MemoryPathError` naming the valid prefixes.
        """
        for prefix, root in self._stores:
            marker = f"{prefix}/"
            if relative.startswith(marker):
                return root, relative[len(marker) :]
        valid = " or ".join(f"'{prefix}/'" for prefix, _ in self._stores)
        raise MemoryPathError(f"memory path must start with {valid}: got {relative!r}")

    def resolve_path(self, relative: str) -> Path:
        """Resolve a project-root memory path to its file, hardened.

        Parses the store prefix to pick the root, then resolves the remainder
        inside it. See :func:`hamroh.storage.path_safety.resolve_under_root`
        for the traversal rules; any failure raises :class:`MemoryPathError`.
        """
        root, subpath = self._split_prefix(relative)
        return resolve_under_root(root, subpath, MemoryPathError, "memory")

    def resolve_readable(self, relative: str) -> Path:
        """Resolve an existing memory file for reading.

        Same prefix-aware resolution as :meth:`resolve_path`, but the file
        must actually exist; otherwise raises :class:`MemoryPathError`.
        """
        path = self.resolve_path(relative)
        if not path.is_file():
            raise MemoryPathError(f"memory file not found: {relative}")
        return path

    def _resolve_writable(self, relative: str) -> Path:
        """Resolve a write/append target — the runtime store only.

        Writes are confined to ``data/memories/``. A committed ``memories/``
        path (or any other prefix) is rejected: the committed store is
        read-only to the bot, edited by the operator via git. Reads, listings
        and searches still span both stores.
        """
        root, subpath = self._split_prefix(relative)
        if root != self._root:
            raise MemoryPathError(
                f"cannot write to {relative!r}: writes are limited to "
                f"'{RUNTIME_PREFIX}/'; the committed store is read-only to the bot"
            )
        return resolve_under_root(root, subpath, MemoryPathError, "memory")

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def list(self) -> list[MemoryFile]:
        """List every file across both stores, recursively, by full path.

        Each file is named by its project-root path (``data/memories/…`` or
        ``memories/…``), so the two stores never collide and both are shown in
        full. Hidden files (``.gitkeep``, dotfiles) are skipped. Symlinked
        entries are skipped silently — they cannot be read by ``read`` either.
        Each file's frontmatter ``description`` is surfaced when present
        (skills protocol); legacy files without it get ``description=None``.
        """
        out: list[MemoryFile] = []
        for prefix, root in self._stores:
            for rel, path in self._iter_files(root):
                out.append(
                    MemoryFile(
                        relative_path=f"{prefix}/{rel}",
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
        """Find lines matching ``query`` across every memory file in both stores.

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
        frontmatter template (``name`` + ``description``); the path must be a
        runtime ``data/memories/`` path (the committed store is read-only, see
        :meth:`_resolve_writable`); the UTF-8 byte length must be ≤
        :data:`MAX_MEMORY_BYTES`; and an existing file must have been read
        first (read-before-write). See :data:`MEMORY_TEMPLATE`.
        """
        _require_frontmatter(content)
        path = self._resolve_writable(relative)
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

        Same runtime-only, path safety + read-before-write rules as
        :meth:`write`. The post-append size must still fit within
        :data:`MAX_MEMORY_BYTES`.
        """
        path = self._resolve_writable(relative)
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
