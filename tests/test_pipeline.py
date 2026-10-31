"""End-to-end checks against a generated library of known duplicates."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgdupe import engine  # noqa: E402
from imgdupe.cache import SignatureCache  # noqa: E402


def make_photo(seed: int, size: tuple[int, int] = (1600, 1200)) -> Image.Image:
    """A deterministic, structurally rich synthetic photograph."""
    rng = np.random.default_rng(seed)
    height, width = 60, 80
    base = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    img = Image.fromarray(base, "RGB").resize(size, Image.Resampling.BICUBIC)
    detail = rng.integers(0, 40, size=(size[1], size[0], 3), dtype=np.uint8)
    return Image.fromarray(np.clip(np.asarray(img).astype(np.int16) + detail, 0, 255).astype(np.uint8), "RGB")


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "album" / "nested").mkdir(parents=True)

        original = make_photo(1)
        # The intended keeper: full resolution, lossless.
        original.save(root / "album" / "sunset_master.png")
        # Same picture, downscaled and heavily compressed.
        original.resize((800, 600), Image.Resampling.LANCZOS).save(
            root / "album" / "nested" / "sunset_small.jpg", quality=35
        )
        # Same picture as a "scan" with a white margin around it.
        scan = Image.new("RGB", (1800, 1400), (252, 252, 250))
        scan.paste(original.resize((1500, 1125), Image.Resampling.LANCZOS), (150, 137))
        scan.save(root / "album" / "sunset_scan.jpg", quality=80)

        # An unrelated photo that must not join the group.
        make_photo(99).save(root / "album" / "other.jpg", quality=90)

        # A byte-identical pair in two different folders.
        twin = make_photo(7, (900, 900))
        twin.save(root / "album" / "twin_a.png")
        twin.save(root / "album" / "nested" / "twin_b.png")

        cls.root = root
        cls.cache = SignatureCache(root / "cache.db")
        cls.result = engine.scan(
            str(root), engine.SENSITIVITY_PRESETS[engine.DEFAULT_SENSITIVITY], cache=cls.cache
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cache.close()
        cls._tmp.cleanup()

    def _group_containing(self, filename: str):
        for group in self.result.groups:
            if any(m.name == filename for m in group.members):
                return group
        self.fail(f"{filename} was not placed in any duplicate group")

    def test_all_images_analysed(self) -> None:
        self.assertEqual(self.result.scanned, 6)
        self.assertEqual(self.result.failed, [])

    def test_variants_group_together(self) -> None:
        group = self._group_containing("sunset_master.png")
        names = {m.name for m in group.members}
        self.assertEqual(names, {"sunset_master.png", "sunset_small.jpg", "sunset_scan.jpg"})

    def test_unrelated_photo_is_not_grouped(self) -> None:
        for group in self.result.groups:
            self.assertNotIn("other.jpg", {m.name for m in group.members})

    def test_highest_quality_is_suggested(self) -> None:
        group = self._group_containing("sunset_master.png")
        self.assertEqual(group.keeper.name, "sunset_master.png")
        scores = [m.quality.score for m in group.members]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_identical_files_flagged_exact(self) -> None:
        group = self._group_containing("twin_a.png")
        self.assertTrue(group.exact)
        self.assertEqual(len(group.members), 2)

    def test_cache_is_reused(self) -> None:
        again = engine.scan(
            str(self.root), engine.SENSITIVITY_PRESETS[engine.DEFAULT_SENSITIVITY], cache=self.cache
        )
        self.assertEqual(
            [g.keeper.path for g in again.groups],
            [g.keeper.path for g in self.result.groups],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
