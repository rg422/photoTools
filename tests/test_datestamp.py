"""Checks for stamping the file-name date into photos."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgdupe import datestamp  # noqa: E402
from imgdupe.datestamp import StampOptions, UnreadableName, date_from_name  # noqa: E402


class NameParsingTest(unittest.TestCase):
    def test_recognised_prefixes(self) -> None:
        noon = datetime(2002, 1, 28, 12, 0)
        for name in ("20020128 Oma-01.JPG", "2002-01-28 Oma.jpg", "2002_01_28 Oma.jpg", "20020128.jpg"):
            self.assertEqual(date_from_name(name), noon, name)

    def test_time_in_name_wins(self) -> None:
        self.assertEqual(date_from_name("20020128_134500 x.jpg", time(9, 0)), datetime(2002, 1, 28, 13, 45))
        self.assertEqual(date_from_name("20020128-134500.jpg"), datetime(2002, 1, 28, 13, 45))

    def test_default_time_is_used(self) -> None:
        self.assertEqual(date_from_name("20020128 x.jpg", time(9, 30)), datetime(2002, 1, 28, 9, 30))

    def test_unreadable_names(self) -> None:
        for name in ("holiday.jpg", "IMG_20020128.jpg", "200201281.jpg", "17990101 old.jpg"):
            with self.assertRaises(UnreadableName, msg=name):
                date_from_name(name)

    def test_impossible_date_is_reported(self) -> None:
        with self.assertRaises(UnreadableName) as caught:
            date_from_name("20020230 x.jpg")
        self.assertIn("calendar", str(caught.exception))
        self.assertEqual(caught.exception.prefix, "20020230")

    def test_time_of_day_parsing(self) -> None:
        self.assertEqual(datestamp.parse_time_of_day("9:30"), time(9, 30))
        self.assertEqual(datestamp.parse_time_of_day("23:59:58"), time(23, 59, 58))
        for bad in ("12", "24:00", "noon", "9:3"):
            with self.assertRaises(ValueError, msg=bad):
                datestamp.parse_time_of_day(bad)


class StampingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sub").mkdir()
        Image.new("RGB", (64, 48), (200, 30, 30)).save(self.root / "20020128 Oma-01.JPG", quality=90)
        Image.new("RGB", (64, 48), (30, 30, 200)).save(self.root / "1965-07-04_093000 fourth.jpg")
        Image.new("RGB", (64, 48)).save(self.root / "2010_05_06 beach.png")
        Image.new("RGB", (64, 48)).save(self.root / "20020230 bad.jpg")
        Image.new("RGB", (64, 48)).save(self.root / "holiday.jpg")
        Image.new("RGB", (64, 48)).save(self.root / "sub" / "19991231 party.jpg")
        (self.root / "20020128 notes.txt").write_text("not an image")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _by_name(self, report: datestamp.StampReport) -> dict[str, datestamp.StampResult]:
        return {os.path.basename(r.path): r for r in report.results}

    def test_dry_run_changes_nothing(self) -> None:
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        report = datestamp.run(str(self.root), StampOptions(recurse=True, dry_run=True))
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(len(report.results), 6)
        self.assertEqual(self._by_name(report)["20020128 Oma-01.JPG"].file_times, "would set")

    def test_recurse_flag(self) -> None:
        flat = datestamp.run(str(self.root), StampOptions(recurse=False))
        self.assertNotIn("19991231 party.jpg", self._by_name(flat))
        deep = datestamp.run(str(self.root), StampOptions(recurse=True))
        self.assertIn("19991231 party.jpg", self._by_name(deep))

    def test_problems_are_reported_and_untouched(self) -> None:
        bad = self.root / "20020230 bad.jpg"
        before = (bad.read_bytes(), bad.stat().st_mtime_ns)
        report = datestamp.run(str(self.root), StampOptions(dry_run=False))
        results = self._by_name(report)
        self.assertEqual(results["20020230 bad.jpg"].status, datestamp.UNREADABLE)
        self.assertEqual(results["holiday.jpg"].status, datestamp.UNREADABLE)
        self.assertEqual(results["2010_05_06 beach.png"].date_taken, "n/a")
        self.assertEqual(results["2010_05_06 beach.png"].status, datestamp.OK)
        self.assertEqual((bad.read_bytes(), bad.stat().st_mtime_ns), before)
        self.assertEqual({r.path for r in report.problems},
                         {str(self.root / "20020230 bad.jpg"), str(self.root / "holiday.jpg")})

    def test_file_times_are_set(self) -> None:
        datestamp.run(str(self.root), StampOptions(dry_run=False, write_metadata=False))
        oma = self.root / "20020128 Oma-01.JPG"
        self.assertEqual(datestamp.local_mtime(str(oma)), datetime(2002, 1, 28, 12, 0))
        # Pre-1970 dates are common for scans and must not trip up the conversion.
        old = self.root / "1965-07-04_093000 fourth.jpg"
        self.assertEqual(datestamp.local_mtime(str(old)), datetime(1965, 7, 4, 9, 30))
        if sys.platform == "win32":
            created = oma.stat().st_birthtime_ns
            self.assertEqual(created, oma.stat().st_mtime_ns)

    def test_use_file_time_keeps_time_of_day(self) -> None:
        beach = self.root / "2010_05_06 beach.png"
        stamp = datetime(2020, 3, 3, 17, 25, 40).timestamp()
        os.utime(beach, (stamp, stamp))
        datestamp.run(str(self.root), StampOptions(dry_run=False, use_file_time=True))
        self.assertEqual(datestamp.local_mtime(str(beach)), datetime(2010, 5, 6, 17, 25, 40))

    def test_csv_report(self) -> None:
        report = datestamp.run(str(self.root), StampOptions())
        target = self.root / "report.csv"
        datestamp.write_csv(report, str(target))
        lines = target.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines[0], "File,Date,DateTaken,FileTimes,Status,Note")
        self.assertEqual(len(lines), 1 + len(report.results))

    @unittest.skipUnless(datestamp.metadata_available(), "EXIF writing needs Windows")
    def test_exif_date_taken_without_reencoding(self) -> None:
        oma = self.root / "20020128 Oma-01.JPG"
        original = oma.read_bytes()
        oma.chmod(0o444)  # read-only files are written and stay read-only
        report = datestamp.run(str(self.root), StampOptions(dry_run=False))
        result = self._by_name(report)["20020128 Oma-01.JPG"]
        self.assertEqual((result.date_taken, result.file_times, result.status), ("set", "set", datestamp.OK))

        stamped = oma.read_bytes()
        scan = b"\xff\xda"
        self.assertEqual(stamped[stamped.index(scan):], original[original.index(scan):])
        with Image.open(oma) as img:
            self.assertEqual(img.getexif().get_ifd(0x8769).get(36867), "2002:01:28 12:00:00")
        self.assertFalse(os.access(oma, os.W_OK))
        oma.chmod(0o666)


if __name__ == "__main__":
    unittest.main(verbosity=2)
