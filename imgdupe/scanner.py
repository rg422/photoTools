"""Recursive discovery of image files under a root folder."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png", ".gif", ".bmp", ".dib",
    ".tif", ".tiff",
    ".webp", ".avif",
    ".heic", ".heif",
    ".ppm", ".pgm", ".pbm",
    ".tga", ".ico",
}

SKIP_DIR_NAMES = {
    "$RECYCLE.BIN", "System Volume Information", ".git", ".svn",
    "__pycache__", ".imgdupe_quarantine", "node_modules",
}


@dataclass(slots=True)
class ImageFile:
    path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def find_images(root: str | Path, min_bytes: int = 1024) -> Iterator[ImageFile]:
    """Yield every image file below ``root``.

    ``min_bytes`` skips thumbnails and sprite-sized junk that would only add
    noise to the results.
    """
    root = os.fspath(root)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith("$")]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            full = os.path.join(dirpath, filename)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            if stat.st_size < min_bytes:
                continue
            yield ImageFile(path=full, size=stat.st_size, mtime=stat.st_mtime)
