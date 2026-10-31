"""Scan orchestration: discovery, cached signature extraction, grouping."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import hashing, quality
from .cache import SignatureCache
from .grouping import DuplicateGroup, build_groups
from .scanner import ImageFile, find_images

# Bit distance out of 64 at which two perceptual hashes are treated as a match.
SENSITIVITY_PRESETS: dict[str, int] = {
    "Strict - near-identical only": 4,
    "Balanced - recommended": 8,
    "Loose - catches heavy edits": 14,
}
DEFAULT_SENSITIVITY = "Balanced - recommended"


@dataclass(slots=True)
class Progress:
    phase: str
    current: int = 0
    total: int = 0
    detail: str = ""


@dataclass(slots=True)
class ScanResult:
    groups: list[DuplicateGroup]
    scanned: int
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def reclaimable_bytes(self) -> int:
        return sum(g.reclaimable_bytes for g in self.groups)


ProgressCallback = Callable[[Progress], None]


def _analyse(item: ImageFile) -> tuple[ImageFile, hashing.ImageSignature | None, str]:
    try:
        return item, hashing.signature(item.path), ""
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the scan
        return item, None, f"{type(exc).__name__}: {exc}"


def scan(
    root: str,
    threshold: int,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    cache: SignatureCache | None = None,
    workers: int | None = None,
) -> ScanResult:
    cancel = cancel or threading.Event()
    report = on_progress or (lambda _p: None)
    owns_cache = cache is None
    cache = cache or SignatureCache()

    try:
        report(Progress("discover", detail="Looking for images..."))
        files: list[ImageFile] = []
        for item in find_images(root):
            if cancel.is_set():
                return ScanResult([], 0, cancelled=True)
            files.append(item)
            if len(files) % 200 == 0:
                report(Progress("discover", len(files), 0, f"Found {len(files)} images..."))

        total = len(files)
        report(Progress("discover", total, total, f"Found {total} images."))

        candidates: list[quality.Candidate] = []
        failed: list[tuple[str, str]] = []
        pending: list[ImageFile] = []

        for item in files:
            cached = cache.get(item.path, item.size, item.mtime)
            if cached is None:
                pending.append(item)
            else:
                candidates.append(_make_candidate(item, cached))

        done = len(candidates)
        report(Progress("analyse", done, total, f"{done} of {total} from cache"))

        workers = workers or min(16, (os.cpu_count() or 4) * 2)
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for item, sig, error in pool.map(_analyse, pending):
                    if cancel.is_set():
                        cache.commit()
                        return ScanResult([], done, failed, cancelled=True)
                    done += 1
                    if sig is None:
                        failed.append((item.path, error))
                    else:
                        cache.put(item.path, item.size, item.mtime, sig)
                        candidates.append(_make_candidate(item, sig))
                    if done % 25 == 0 or done == total:
                        report(Progress("analyse", done, total, os.path.basename(item.path)))
            cache.commit()

        report(Progress("group", total, total, "Comparing images..."))
        groups = build_groups(candidates, threshold)
        report(Progress("done", total, total, f"{len(groups)} groups found"))
        return ScanResult(groups=groups, scanned=len(candidates), failed=failed)
    finally:
        if owns_cache:
            cache.close()


def _make_candidate(item: ImageFile, sig: hashing.ImageSignature) -> quality.Candidate:
    return quality.Candidate(
        path=item.path,
        size=item.size,
        mtime=item.mtime,
        sig=sig,
        quality=quality.assess(item.path, item.size, sig),
    )
