"""Checks for replacing a term in file names."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgdupe import renamer  # noqa: E402
from imgdupe.renamer import RenameOptions, new_name_for  # noqa: E402


class NameTest(unittest.TestCase):
    def test_basic_replacement(self) -> None:
        opts = RenameOptions("Bday", "B-day")
        self.assertEqual(new_name_for("20020128 Oma Bday-01.jpg", opts), "20020128 Oma B-day-01.jpg")
        self.assertEqual(new_name_for("Bday Bday.jpg", opts), "B-day B-day.jpg")
        # Already converted names are left alone, so running it twice is harmless.
        self.assertEqual(new_name_for("B-day.jpg", opts), "B-day.jpg")

    def test_match_case(self) -> None:
        self.assertEqual(new_name_for("bday.jpg", RenameOptions("Bday", "B-day")), "bday.jpg")
        self.assertEqual(new_name_for("bday.jpg", RenameOptions("Bday", "B-day", match_case=False)), "B-day.jpg")

    def test_whole_word(self) -> None:
        opts = RenameOptions("Bday", "B-day", whole_word=True)
        self.assertEqual(new_name_for("2002_Bday-01.jpg", opts), "2002_B-day-01.jpg")
        self.assertEqual(new_name_for("Bdays.jpg", opts), "Bdays.jpg")

    def test_extension_is_kept(self) -> None:
        self.assertEqual(new_name_for("photo.jpg", RenameOptions("jpg", "png")), "photo.jpg")

    def test_replacement_is_literal(self) -> None:
        self.assertEqual(new_name_for("a.b x.jpg", RenameOptions(".", r"\1")), r"a\1b x.jpg")

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            renamer.validate(RenameOptions("", "x"))
        with self.assertRaises(ValueError):
            renamer.validate(RenameOptions("Bday", "B/day"))


class RenameFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sub").mkdir()
        for name in ("20020128 Oma Bday-01.JPG", "20020128 Oma Bday-02.JPG", "sub/2010 Bday cake.jpg",
                     "Bday clash.jpg", "B-day clash.jpg", "holiday.jpg"):
            img = Image.new("RGB", (64, 48), (90, 120, 150))
            exif = Image.Exif()
            exif[0x0132] = "2002:01:28 12:00:00"
            img.save(self.root / name, exif=exif)
        (self.root / "Bday notes.txt").write_text("not an image")
        stamp = datetime(2002, 1, 28, 12, 0).timestamp()
        for path in self.root.rglob("*.*"):
            os.utime(path, (stamp, stamp))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _by_name(self, items: list[renamer.RenameItem]) -> dict[str, renamer.RenameItem]:
        return {os.path.basename(i.path): i for i in items}

    def test_plan_changes_nothing(self) -> None:
        before = sorted(p.name for p in self.root.rglob("*"))
        items = renamer.plan(str(self.root), RenameOptions("Bday", "B-day"))
        self.assertEqual(sorted(p.name for p in self.root.rglob("*")), before)
        names = self._by_name(items)
        self.assertEqual(set(names), {"20020128 Oma Bday-01.JPG", "20020128 Oma Bday-02.JPG", "Bday clash.jpg"})
        self.assertEqual(names["Bday clash.jpg"].status, renamer.CONFLICT)

    def test_options_widen_the_search(self) -> None:
        items = renamer.plan(str(self.root), RenameOptions("Bday", "B-day", recurse=True, images_only=False))
        self.assertIn("2010 Bday cake.jpg", self._by_name(items))
        self.assertIn("Bday notes.txt", self._by_name(items))

    def test_duplicate_targets_conflict(self) -> None:
        (self.root / "Oma_Bday.jpg").write_bytes(b"x" * 2048)
        (self.root / "OmaB_day.jpg").write_bytes(b"y" * 2048)
        items = self._by_name(renamer.plan(str(self.root), RenameOptions("_", "")))
        self.assertEqual(items["Oma_Bday.jpg"].status, renamer.READY)
        self.assertEqual(items["OmaB_day.jpg"].status, renamer.CONFLICT)

    def test_rename_preserves_contents_times_and_metadata(self) -> None:
        tracked = {p.name: p for p in self.root.glob("20020128*")}
        tracked["20020128 Oma Bday-02.JPG"].chmod(stat.S_IREAD)
        before = {}
        for name, path in tracked.items():
            st = path.stat()
            before[name] = (path.read_bytes(), st.st_mtime_ns, getattr(st, "st_birthtime_ns", None), st.st_mode)

        items = renamer.plan(str(self.root), RenameOptions("Bday", "B-day"))
        renamer.apply(items)
        results = self._by_name(items)
        self.assertEqual(results["20020128 Oma Bday-01.JPG"].status, renamer.RENAMED)
        self.assertEqual(results["Bday clash.jpg"].status, renamer.CONFLICT)
        self.assertTrue((self.root / "Bday clash.jpg").exists())

        for name, (data, mtime, birth, mode) in before.items():
            moved = self.root / name.replace("Bday", "B-day")
            self.assertFalse((self.root / name).exists())
            st = moved.stat()
            self.assertEqual(st.st_mtime_ns, mtime, name)
            self.assertEqual(getattr(st, "st_birthtime_ns", None), birth, name)
            self.assertEqual(st.st_mode, mode, name)
            self.assertEqual(moved.read_bytes(), data, name)
            with Image.open(moved) as img:
                self.assertEqual(img.getexif()[0x0132], "2002:01:28 12:00:00")
        (self.root / "20020128 Oma B-day-02.JPG").chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_case_only_rename(self) -> None:
        items = renamer.plan(str(self.root), RenameOptions("holiday", "Holiday"))
        self.assertEqual([i.status for i in items], [renamer.READY])
        renamer.apply(items)
        self.assertIn("Holiday.jpg", os.listdir(self.root))

    def test_undo(self) -> None:
        before = sorted(os.listdir(self.root))
        items = renamer.plan(str(self.root), RenameOptions("Bday", "B-day"))
        renamer.apply(items)
        self.assertNotEqual(sorted(os.listdir(self.root)), before)
        renamer.undo(items)
        self.assertEqual(sorted(os.listdir(self.root)), before)
        self.assertTrue(all(i.status in (renamer.UNDONE, renamer.CONFLICT) for i in items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
