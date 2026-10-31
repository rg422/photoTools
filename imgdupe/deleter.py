"""Removal of rejected duplicates.

Nothing is ever hard-deleted. Files go to the OS recycle bin when available,
otherwise they are moved into a quarantine folder next to the scan root.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from send2trash import send2trash
except ImportError:  # pragma: no cover - optional dependency
    send2trash = None

QUARANTINE_DIRNAME = ".imgdupe_quarantine"


@dataclass(slots=True)
class DeleteOutcome:
    removed: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    destination: str = ""
    freed_bytes: int = 0


def recycle_bin_available() -> bool:
    return send2trash is not None


def _quarantine_root(scan_root: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(scan_root) / QUARANTINE_DIRNAME / stamp


def remove(paths: list[str], scan_root: str, use_recycle_bin: bool = True) -> DeleteOutcome:
    """Recycle or quarantine ``paths``. Returns what actually happened."""
    outcome = DeleteOutcome()
    to_recycle = use_recycle_bin and recycle_bin_available()
    quarantine: Path | None = None

    if not to_recycle:
        quarantine = _quarantine_root(scan_root)
        quarantine.mkdir(parents=True, exist_ok=True)
        outcome.destination = str(quarantine)
    else:
        outcome.destination = "Recycle Bin"

    for path in paths:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        try:
            if to_recycle:
                send2trash(os.path.abspath(path))
            else:
                assert quarantine is not None
                shutil.move(path, _unique_target(quarantine, scan_root, path))
            outcome.removed.append(path)
            outcome.freed_bytes += size
        except Exception as exc:  # noqa: BLE001 - report per-file and continue
            outcome.errors.append((path, f"{type(exc).__name__}: {exc}"))

    return outcome


def _unique_target(quarantine: Path, scan_root: str, path: str) -> str:
    """Mirror the file's position under the scan root to avoid name collisions."""
    try:
        relative = Path(path).resolve().relative_to(Path(scan_root).resolve())
    except ValueError:
        relative = Path(Path(path).name)
    target = quarantine / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    counter = 1
    while target.exists():
        target = target.with_name(f"{target.stem}_{counter}{target.suffix}")
        counter += 1
    return str(target)
