"""Two memory stores addressed by full project paths — distinct namespaces.

The store overlays a git-tracked committed root alongside the runtime root.
These tests pin the contract: every memory is named by its full project path
(``data/memories/...`` or ``memories/...``), the two stores are separate
namespaces that never collide or shadow each other, edits land in the store
named by the path's prefix, and a bare or unknown-prefixed path is rejected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hamroh.storage.memory import MemoryPathError, MemoryStore

_TEMPLATE = "---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Return ``(runtime_root, committed_root)``, both created and empty."""
    runtime = tmp_path / "runtime"
    committed = tmp_path / "committed"
    runtime.mkdir()
    committed.mkdir()
    return runtime, committed


@pytest.fixture()
def store(roots: tuple[Path, Path]) -> MemoryStore:
    runtime, committed = roots
    s = MemoryStore(runtime, committed_root=committed)
    s.ensure_root()
    return s


def _seed(root: Path, subpath: str, *, name: str, desc: str, body: str) -> None:
    """Write a templated memory file under ``root``."""
    path = root / subpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_TEMPLATE.format(name=name, desc=desc, body=body))


# ---------------------------------------------------------------------------
# list / read / search see both stores by full path — no shadowing
# ---------------------------------------------------------------------------


def test_list_shows_both_stores_by_full_path(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A file in each store is listed under its full, prefixed project path."""
    runtime, committed = roots
    _seed(runtime, "live.md", name="live", desc="runtime note", body="a")
    _seed(committed, "notes/ref.md", name="ref", desc="committed note", body="b")

    listed = {f.relative_path: f.description for f in store.list()}

    assert listed == {
        "data/memories/live.md": "runtime note",
        "memories/notes/ref.md": "committed note",
    }, "each store's files are listed by their full project path"


def test_same_subpath_is_two_distinct_memories(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """The same subpath in both stores yields two separate, fully-visible files."""
    runtime, committed = roots
    _seed(committed, "dup.md", name="dup", desc="committed", body="COMMITTED")
    _seed(runtime, "dup.md", name="dup", desc="runtime", body="RUNTIME")

    listed = {f.relative_path: f.description for f in store.list()}

    assert listed == {
        "data/memories/dup.md": "runtime",
        "memories/dup.md": "committed",
    }, "both copies appear — nothing is shadowed or masked"
    assert "RUNTIME" in store.read("data/memories/dup.md")
    assert "COMMITTED" in store.read("memories/dup.md")


def test_read_committed_file_by_prefix(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A committed file is read via its ``memories/`` path."""
    _, committed = roots
    _seed(committed, "notes/ref.md", name="ref", desc="committed", body="hello")

    assert "hello" in store.read("memories/notes/ref.md")


def test_search_spans_both_stores(store: MemoryStore, roots: tuple[Path, Path]) -> None:
    """Keyword search finds matches in both stores, reported by full path."""
    runtime, committed = roots
    _seed(committed, "ref.md", name="ref", desc="d", body="the budget was approved")
    _seed(runtime, "plan.md", name="plan", desc="d", body="budget draft pending")

    paths = {h.relative_path for h in store.search("budget")}

    assert paths == {"memories/ref.md", "data/memories/plan.md"}, (
        "search must span both stores and name hits by full path"
    )


# ---------------------------------------------------------------------------
# writes touch only the runtime store; the committed store is read-only
# ---------------------------------------------------------------------------


def test_write_new_runtime_file(store: MemoryStore, roots: tuple[Path, Path]) -> None:
    """A data/memories/ path creates the file in the runtime store only."""
    runtime, committed = roots

    store.write("data/memories/new.md", _TEMPLATE.format(name="n", desc="d", body="x"))

    assert (runtime / "new.md").is_file(), "runtime path lands in the runtime root"
    assert not (committed / "new.md").exists(), "it must not touch the committed root"


def test_write_to_committed_is_rejected(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A memories/ write is rejected even after reading the file — read-only."""
    runtime, committed = roots
    _seed(committed, "notes/ref.md", name="ref", desc="old", body="OLD")

    store.read("memories/notes/ref.md")  # reading does NOT unlock a committed write
    with pytest.raises(MemoryPathError, match="read-only"):
        store.write(
            "memories/notes/ref.md",
            _TEMPLATE.format(name="ref", desc="new", body="NEW"),
        )

    assert "OLD" in (committed / "notes/ref.md").read_text(), "committed file untouched"
    assert not (runtime / "notes/ref.md").exists(), "no runtime copy is spawned"


def test_append_to_committed_is_rejected(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """A memories/ append is rejected — the committed store is read-only."""
    runtime, committed = roots
    _seed(committed, "notes/ref.md", name="ref", desc="old", body="line 1\n")

    store.read("memories/notes/ref.md")
    with pytest.raises(MemoryPathError, match="read-only"):
        store.append("memories/notes/ref.md", "line 2\n", "updated")

    assert (committed / "notes/ref.md").read_text().endswith("line 1\n"), "unchanged"
    assert not (runtime / "notes/ref.md").exists(), "no runtime copy is spawned"


def test_read_committed_does_not_unlock_runtime_twin(
    store: MemoryStore, roots: tuple[Path, Path]
) -> None:
    """Reading a committed file does not unlock its runtime same-subpath twin.

    The read-before-write gate is keyed to the exact file, so reading
    ``memories/dup.md`` leaves ``data/memories/dup.md`` (never read) protected.
    """
    runtime, committed = roots
    _seed(committed, "dup.md", name="dup", desc="committed", body="committed body")
    _seed(runtime, "dup.md", name="dup", desc="runtime", body="runtime body")

    store.read("memories/dup.md")  # unlocks only the committed file

    with pytest.raises(MemoryPathError, match="read-before-write"):
        store.write(
            "data/memories/dup.md", _TEMPLATE.format(name="dup", desc="d", body="z")
        )
    assert "runtime body" in (runtime / "dup.md").read_text(), "runtime copy untouched"


# ---------------------------------------------------------------------------
# path prefix is mandatory and store-scoped
# ---------------------------------------------------------------------------


def test_bare_path_without_prefix_is_rejected(store: MemoryStore) -> None:
    """A path missing the store prefix is rejected, naming the valid prefixes."""
    with pytest.raises(MemoryPathError, match="must start with"):
        store.read("notes/ref.md")


def test_unknown_prefix_is_rejected(store: MemoryStore) -> None:
    """A path under some other top-level folder is not a memory path."""
    with pytest.raises(MemoryPathError, match="must start with"):
        store.write("secrets/x.md", _TEMPLATE.format(name="x", desc="d", body="z"))


def test_committed_prefix_rejected_without_committed_root(tmp_path: Path) -> None:
    """With no committed store configured, only data/memories/ paths resolve."""
    s = MemoryStore(tmp_path / "mem")
    s.ensure_root()
    _seed(s.root, "a.md", name="a", desc="only", body="x")

    assert [f.relative_path for f in s.list()] == ["data/memories/a.md"]
    with pytest.raises(MemoryPathError, match="must start with"):
        s.read("memories/a.md")


def test_resolve_readable_raises_for_missing(store: MemoryStore) -> None:
    """A prefixed path absent from its store raises a clear not-found error."""
    with pytest.raises(MemoryPathError, match="not found"):
        store.resolve_readable("memories/nope.md")
