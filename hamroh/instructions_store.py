"""Read, append, and rewrite the project prompt at ``prompts/project.md``.

system.md is intentionally not exposed: it's git-tracked, so any bot
edit there would land as a working-tree diff and pollute the repo.
All operator-driven customisations accumulate in project.md
(gitignored), which is concatenated after system.md to form the full
prompt.

``append`` grows the overlay; ``rewrite`` replaces it wholesale so a
rule that belongs elsewhere can be removed. Both share the same rails.

The owner-only policy is enforced in the system prompt, not here.
Code-level rails: file must already exist, 128 KiB cap, atomic write,
backup before mutate.
"""

from __future__ import annotations

import errno
import shutil
from datetime import datetime, timezone
from pathlib import Path


class InstructionsError(ValueError):
    """Raised for any rejection from the instructions store."""


#: 10× headroom over a typical project.md. Big enough to grow into,
#: small enough that a runaway append can't fill the disk.
MAX_INSTRUCTION_BYTES = 128 * 1024


class InstructionsStore:
    def __init__(self, project_md_path: Path, backup_dir: Path) -> None:
        self._path = project_md_path.resolve()
        self._backup_dir = backup_dir.resolve()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    def ensure_dirs(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> str:
        if not self._path.exists():
            raise InstructionsError(f"project.md not present at {self._path}")
        return self._path.read_text(encoding="utf-8")

    def append(self, content: str) -> tuple[int, Path]:
        """Append ``content`` to project.md. Returns (new_total_bytes, backup_path)."""
        if not self._path.exists():
            raise InstructionsError(f"project.md not present at {self._path}")
        new_bytes = self._path.read_bytes() + content.encode("utf-8")
        return self._commit(new_bytes, "append")

    def rewrite(self, content: str) -> tuple[int, Path]:
        """Replace project.md wholesale. Returns (new_total_bytes, backup_path).

        The remove counterpart to :meth:`append`: read the body, drop a
        block, write the rest back. Same rails as append (file must exist,
        capped, backed up first). Takes effect on the next container restart.
        """
        if not self._path.exists():
            raise InstructionsError(f"project.md not present at {self._path}")
        return self._commit(content.encode("utf-8"), "rewrite")

    def _commit(self, new_bytes: bytes, op: str) -> tuple[int, Path]:
        """Cap-check, back up, then atomically write. Returns (size, backup)."""
        new_size = len(new_bytes)
        if new_size > MAX_INSTRUCTION_BYTES:
            raise InstructionsError(
                f"{op} would exceed cap: {new_size} bytes > {MAX_INSTRUCTION_BYTES}"
            )
        backup = self._backup()
        self._atomic_write(new_bytes)
        return new_size, backup

    def _atomic_write(self, new_bytes: bytes) -> None:
        """Atomically replace project.md with ``new_bytes`` (bind-mount safe)."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(new_bytes)
        try:
            tmp.replace(self._path)
        except OSError as exc:
            # Docker bind-mounted single files can't be replaced via
            # rename(2) — the destination is a mount point, kernel
            # returns EBUSY. Fall back to in-place truncate+write; the
            # backup taken by the caller covers crash-mid-write recovery.
            if exc.errno != errno.EBUSY:
                raise
            self._path.write_bytes(new_bytes)
            tmp.unlink(missing_ok=True)

    def _backup(self) -> Path:
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self._backup_dir / f"project-{ts}.md"
        shutil.copy2(self._path, backup_path)
        return backup_path
