"""Quality scoring used to rank the members of a duplicate group."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from .hashing import ImageSignature

LOSSLESS_FORMATS = {"PNG", "TIFF", "BMP", "PPM", "TGA"}

# Reference points at which a component score saturates at 1.0.
_MEGAPIXEL_REFERENCE = 24.0
_SHARPNESS_REFERENCE = 60.0
_BPP_REFERENCE = 2.0

WEIGHTS = {
    "resolution": 0.42,
    "sharpness": 0.30,
    "encoding": 0.20,
    "color": 0.08,
}


@dataclass(slots=True)
class QualityReport:
    score: float
    components: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Candidate:
    """One image plus everything known about it."""

    path: str
    size: int
    mtime: float
    sig: ImageSignature
    quality: QualityReport

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def megapixels(self) -> float:
        return self.sig.width * self.sig.height / 1_000_000

    @property
    def dimensions(self) -> str:
        return f"{self.sig.width}x{self.sig.height}"


def _bits_per_pixel(size: int, sig: ImageSignature) -> float:
    pixels = sig.width * sig.height
    return (size * 8) / pixels if pixels else 0.0


def assess(path: str, size: int, sig: ImageSignature) -> QualityReport:
    megapixels = sig.width * sig.height / 1_000_000
    resolution = min(1.0, math.log1p(megapixels) / math.log1p(_MEGAPIXEL_REFERENCE))
    sharpness = min(1.0, sig.sharpness / _SHARPNESS_REFERENCE)

    lossless = (sig.fmt or "").upper() in LOSSLESS_FORMATS
    bpp = _bits_per_pixel(size, sig)
    encoding = 1.0 if lossless else min(1.0, bpp / _BPP_REFERENCE)

    color = min(1.0, sig.colorfulness / 40.0)

    components = {
        "resolution": resolution,
        "sharpness": sharpness,
        "encoding": encoding,
        "color": color,
    }
    score = sum(WEIGHTS[k] * v for k, v in components.items()) * 100

    notes: list[str] = [f"{sig.width}x{sig.height} ({megapixels:.1f} MP)"]
    if lossless:
        notes.append(f"lossless {sig.fmt}")
    else:
        notes.append(f"{sig.fmt or 'unknown'} @ {bpp:.2f} bpp")
    if sig.colorfulness < 6:
        notes.append("greyscale")
    if sharpness < 0.25:
        notes.append("soft / low detail")

    return QualityReport(score=score, components=components, notes=notes)


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Best first. Ties break toward the larger, then older, file."""
    return sorted(candidates, key=lambda c: (-c.quality.score, -c.size, c.mtime, c.path))


def explain_choice(best: Candidate, others: list[Candidate]) -> list[str]:
    """Short, concrete reasons this candidate was suggested as the keeper."""
    reasons: list[str] = []
    if not others:
        return ["only image in the group"]

    if all(best.megapixels > o.megapixels * 1.02 for o in others):
        reasons.append(f"highest resolution ({best.dimensions})")
    elif all(best.megapixels >= o.megapixels * 0.98 for o in others):
        reasons.append(f"tied for highest resolution ({best.dimensions})")

    if all(best.sig.sharpness > o.sig.sharpness * 1.1 for o in others):
        reasons.append("sharpest / most fine detail")

    best_lossless = (best.sig.fmt or "").upper() in LOSSLESS_FORMATS
    if best_lossless and any((o.sig.fmt or "").upper() not in LOSSLESS_FORMATS for o in others):
        reasons.append(f"lossless {best.sig.fmt}")
    elif all(best.size > o.size for o in others):
        reasons.append("largest file (least compressed)")

    if best.sig.colorfulness > 6 and all(o.sig.colorfulness < 6 for o in others):
        reasons.append("keeps colour where the others are greyscale")

    if not reasons:
        reasons.append("best overall quality score")
    return reasons
