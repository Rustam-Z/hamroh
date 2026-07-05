"""SkillsStore — read + write, path-hardened, Agent Skills spec-compliant.

Covers spec conformance (frontmatter required, name/description rules,
directory/name match) plus the hamroh-specific hardening (path
traversal, symlinks, size cap) and the write() rails (frontmatter,
cap, read-before-write gate).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from hamroh.storage.skills_store import (
    MAX_SKILL_BYTES,
    SkillsError,
    SkillsStore,
    render_skills_index,
)


_VALID_FRONTMATTER = textwrap.dedent(
    """\
    ---
    name: {name}
    description: A test skill used by the SkillsStore test suite.
    ---

    # {name}

    Body content.
    """
)


def _make_store(tmp_path: Path) -> SkillsStore:
    root = tmp_path / "skills"
    (root / "self-reflection").mkdir(parents=True)
    (root / "self-reflection" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="self-reflection")
    )
    (root / "self-reflection" / "README.md").write_text("readme\n")
    (root / "another").mkdir()
    (root / "another" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="another")
    )
    # A dir without SKILL.md should be ignored.
    (root / "docs-only").mkdir()
    (root / "docs-only" / "notes.md").write_text("notes\n")
    store = SkillsStore(root=root)
    store.ensure_root()
    return store


# ---------------------------------------------------------------------------
# list() — spec-compliant discovery
# ---------------------------------------------------------------------------


def test_list_returns_only_dirs_with_valid_skill_md(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    names = [f.name for f in store.list()]
    assert sorted(names) == ["another", "self-reflection"]
    assert "docs-only" not in names


def test_list_includes_description_from_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    files = {f.name: f for f in store.list()}
    assert "SkillsStore test suite" in files["self-reflection"].description


def test_list_skips_skill_with_invalid_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Break 'another' by stripping frontmatter entirely.
    (store.root / "another" / "SKILL.md").write_text("# No frontmatter here\n")
    names = [f.name for f in store.list()]
    assert names == ["self-reflection"]  # 'another' silently dropped


def test_list_skips_skill_with_name_mismatch(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Frontmatter name doesn't match dir name.
    (store.root / "another" / "SKILL.md").write_text(
        _VALID_FRONTMATTER.format(name="totally-different")
    )
    names = [f.name for f in store.list()]
    assert names == ["self-reflection"]


def test_list_on_missing_root_returns_empty(tmp_path: Path) -> None:
    store = SkillsStore(root=tmp_path / "does-not-exist")
    assert store.list() == []


# ---------------------------------------------------------------------------
# read() — returns body, validates frontmatter
# ---------------------------------------------------------------------------


def test_read_returns_skill_content_including_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    text = store.read("self-reflection")
    assert text.startswith("---")
    assert "name: self-reflection" in text
    assert "# self-reflection" in text


def test_read_rejects_invalid_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text("# No frontmatter\n")
    with pytest.raises(SkillsError, match="frontmatter"):
        store.read("self-reflection")


def test_read_unknown_skill_raises(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="skill not found"):
        store.read("does-not-exist")


# ---------------------------------------------------------------------------
# Frontmatter validation — spec rules
# ---------------------------------------------------------------------------


def test_frontmatter_missing_name_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\ndescription: No name field\n---\n\nbody\n"
    )
    with pytest.raises(SkillsError, match="'name'"):
        store.read("self-reflection")


def test_frontmatter_missing_description_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\nname: self-reflection\n---\n\nbody\n"
    )
    with pytest.raises(SkillsError, match="'description'"):
        store.read("self-reflection")


def test_frontmatter_name_format_rejected(tmp_path: Path) -> None:
    # Uppercase is a spec violation.
    store = _make_store(tmp_path)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        "---\nname: Self-Reflection\ndescription: bad name case.\n---\n"
    )
    with pytest.raises(SkillsError, match="name"):
        store.read("self-reflection")


def test_frontmatter_leading_hyphen_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "-badname").mkdir()
    (store.root / "-badname" / "SKILL.md").write_text(
        "---\nname: -badname\ndescription: leading hyphen is forbidden.\n---\n"
    )
    with pytest.raises(SkillsError, match="hyphen"):
        store.read("-badname")


def test_frontmatter_consecutive_hyphens_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    (store.root / "bad--name").mkdir()
    (store.root / "bad--name" / "SKILL.md").write_text(
        "---\nname: bad--name\ndescription: consecutive hyphens forbidden.\n---\n"
    )
    with pytest.raises(SkillsError, match="hyphen"):
        store.read("bad--name")


def test_frontmatter_description_too_long(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    huge = "x" * 1100  # spec cap is 1024
    (store.root / "self-reflection" / "SKILL.md").write_text(
        f"---\nname: self-reflection\ndescription: {huge}\n---\n"
    )
    with pytest.raises(SkillsError, match="description exceeds"):
        store.read("self-reflection")


# ---------------------------------------------------------------------------
# Path hardening (unchanged from hamroh's own rules)
# ---------------------------------------------------------------------------


def test_read_rejects_traversal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="may not contain"):
        store.read("../something")


def test_read_rejects_nested_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="single directory"):
        store.read("self-reflection/SKILL.md")


def test_read_rejects_absolute_path(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="must be relative"):
        store.read("/etc/passwd")


def test_read_rejects_empty_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="non-empty"):
        store.read("")


def test_read_rejects_symlink(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    other = tmp_path / "outside"
    other.mkdir()
    (other / "SKILL.md").write_text(_VALID_FRONTMATTER.format(name="evil"))
    (store.root / "evil").symlink_to(other)
    with pytest.raises(SkillsError, match="symlink"):
        store.read("evil")


def test_read_truncates_at_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # Write a skill with valid frontmatter but a giant body.
    body = "y" * (MAX_SKILL_BYTES + 100)
    (store.root / "self-reflection" / "SKILL.md").write_text(
        f"---\nname: self-reflection\ndescription: big body.\n---\n{body}"
    )
    text = store.read("self-reflection")
    assert "[truncated" in text


# ---------------------------------------------------------------------------
# render_skills_index() — the preloaded "level 1" system-prompt block
# ---------------------------------------------------------------------------


def test_render_skills_index_lists_each_skill(tmp_path: Path) -> None:
    """given several valid skills, when rendered, then each appears as
    '- **name** — description' under the Available skills header."""
    store = _make_store(tmp_path)

    block = render_skills_index(store)

    assert block.startswith("# Available skills"), "block must carry the header"
    assert "- **self-reflection** — A test skill" in block, "self-reflection listed"
    assert "- **another** — A test skill" in block, "another listed"
    assert "skill_read" in block, "block must tell the agent how to load a body"


def test_render_skills_index_empty_when_no_skills(tmp_path: Path) -> None:
    """An empty skills/ dir renders to '' so the caller appends nothing."""
    root = tmp_path / "skills"
    store = SkillsStore(root=root)
    store.ensure_root()

    assert render_skills_index(store) == "", "no skills → no dangling header"


# ---------------------------------------------------------------------------
# write() — create / update with the same rails
# ---------------------------------------------------------------------------


def test_write_creates_new_skill_and_is_immediately_readable(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    content = _VALID_FRONTMATTER.format(name="weekly-digest")

    written = store.write("weekly-digest", content)

    assert written == len(content.encode("utf-8")), "returns bytes written"
    assert store.read("weekly-digest") == content, "readable live, no restart"
    assert "weekly-digest" in [f.name for f in store.list()], "listed live"


def test_write_rejects_name_mismatch(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    content = _VALID_FRONTMATTER.format(name="something-else")
    with pytest.raises(SkillsError, match="must match parent directory"):
        store.write("weekly-digest", content)


def test_write_rejects_missing_frontmatter(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="frontmatter"):
        store.write("weekly-digest", "# no frontmatter here\n")


def test_write_rejects_over_cap(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    content = _VALID_FRONTMATTER.format(name="big") + "z" * MAX_SKILL_BYTES
    with pytest.raises(SkillsError, match="exceeds cap"):
        store.write("big", content)


def test_write_allows_self_reflection(tmp_path: Path) -> None:
    # No skill is off-limits: the bot may overwrite self-reflection too,
    # subject only to the normal read-before-write gate.
    store = _make_store(tmp_path)
    content = _VALID_FRONTMATTER.format(name="self-reflection")
    store.read("self-reflection")
    assert store.write("self-reflection", content) == len(content.encode("utf-8"))


def test_overwrite_requires_prior_read(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    content = _VALID_FRONTMATTER.format(name="another")

    # 'another' already exists and hasn't been read this session.
    with pytest.raises(SkillsError, match="call skill_read"):
        store.write("another", content)

    # After a read, the overwrite is allowed.
    store.read("another")
    assert store.write("another", content) == len(content.encode("utf-8"))


def test_write_rejects_traversal(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="may not contain"):
        store.write("../evil", "anything")


def test_write_rejects_nested_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="single directory"):
        store.write("nested/SKILL.md", "anything")


def test_write_rejects_absolute_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(SkillsError, match="must be relative"):
        store.write("/etc/passwd", "anything")
