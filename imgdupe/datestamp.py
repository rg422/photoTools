"""Stamp the YYYYMMDD date at the front of a file name into the photo itself.

A port of ``oldTool/Set-PhotoDateFromFilename.ps1``. The date goes into the
EXIF "Date taken" field and the file's Created / Modified times. Files are never
renamed and pixel data is never re-encoded: the EXIF write goes through the
Windows Property System, the same path File Explorer's Details tab uses.
"""

from __future__ import annotations

import csv
import os
import re
import stat
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Callable

from .engine import Progress, ProgressCallback
from .scanner import SKIP_DIR_NAMES

STAMP_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".tif", ".tiff",
    ".heic", ".heif", ".gif", ".bmp", ".webp",
}
# Extensions where an embedded EXIF/metadata date is actually supported.
METADATA_EXTENSIONS = {".jpg", ".jpeg", ".jpe", ".jfif", ".tif", ".tiff", ".heic", ".heif"}

DEFAULT_TIME = time(12, 0)

# YYYY[-_.]MM[-_.]DD with an optional _HHMMSS / -HHMMSS time, anchored at the start.
NAME_PATTERN = re.compile(
    r"^(?P<y>(?:18|19|20)\d{2})[-_.]?(?P<mo>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[_-](?P<h>[01]\d|2[0-3])(?P<mi>[0-5]\d)(?P<s>[0-5]\d))?(?![0-9])"
)

OK, ERROR, UNREADABLE = "OK", "ERROR", "UNREADABLE"


class UnreadableName(ValueError):
    """The file name carries no usable date; ``prefix`` is what matched, if anything."""

    def __init__(self, reason: str, prefix: str = "") -> None:
        super().__init__(reason)
        self.prefix = prefix


@dataclass(slots=True)
class StampOptions:
    recurse: bool = False
    time_of_day: time = DEFAULT_TIME
    use_file_time: bool = False
    write_metadata: bool = True
    dry_run: bool = True


@dataclass(slots=True)
class StampResult:
    path: str
    file: str
    date: str = ""
    date_taken: str = "-"
    file_times: str = "-"
    status: str = OK
    note: str = ""


@dataclass(slots=True)
class StampReport:
    root: str
    dry_run: bool
    results: list[StampResult] = field(default_factory=list)
    metadata_available: bool = True
    cancelled: bool = False

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def problems(self) -> list[StampResult]:
        return [r for r in self.results if r.status != OK]


def parse_time_of_day(text: str) -> time:
    """Parse ``HH:mm`` or ``HH:mm:ss`` (24 hour)."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", text)
    if not match:
        raise ValueError(f"'{text}' is not a valid time of day. Use HH:mm or HH:mm:ss.")
    hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
    try:
        return time(hour, minute, second)
    except ValueError:
        raise ValueError(f"'{text}' is not a valid time of day. Use HH:mm or HH:mm:ss.") from None


def date_from_name(name: str, time_of_day: time = DEFAULT_TIME) -> datetime:
    """The date encoded at the start of ``name``, raising :class:`UnreadableName`."""
    match = NAME_PATTERN.match(name)
    if not match:
        raise UnreadableName("no YYYYMMDD prefix found")
    try:
        day = datetime(int(match["y"]), int(match["mo"]), int(match["d"]))
        if match["h"]:
            return day.replace(hour=int(match["h"]), minute=int(match["mi"]), second=int(match["s"]))
    except ValueError:
        # An impossible date such as 20020230.
        raise UnreadableName("prefix is not a real calendar date", match.group(0)) from None
    return datetime.combine(day.date(), time_of_day)


def find_files(root: str, recurse: bool) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith("$")]
        found.extend(
            os.path.join(dirpath, f) for f in filenames
            if os.path.splitext(f)[1].lower() in STAMP_EXTENSIONS
        )
        if not recurse:
            break
    return sorted(found, key=str.lower)


# ----------------------------------------------------------------- Windows API

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, text: str = "") -> None:
            super().__init__()
            if text:
                ctypes.OleDLL("ole32").CLSIDFromString(f"{{{text}}}", ctypes.byref(self))

    class _PropertyKey(ctypes.Structure):
        _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]

    class _PropVariant(ctypes.Structure):
        _fields_ = [("vt", wintypes.USHORT), ("reserved", wintypes.WORD * 3),
                    ("filetime", ctypes.c_ulonglong), ("padding", ctypes.c_void_p)]

    class _SystemTime(ctypes.Structure):
        _fields_ = [(n, wintypes.WORD) for n in
                    ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]

    _ole32 = ctypes.OleDLL("ole32")
    _shell32 = ctypes.OleDLL("shell32")
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _IID_IPROPERTYSTORE = _GUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
    # System.Photo.DateTaken -> EXIF 0x9003 DateTimeOriginal
    _PKEY_DATE_TAKEN = _PropertyKey(_GUID("14B81DA1-0135-4D31-96D9-6CBFC9671A99"), 36867)
    _GPS_READWRITE = 0x2
    _VT_FILETIME = 64
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _to_filetime(local: datetime) -> int:
        """Local wall-clock time to a UTC FILETIME, using that date's DST rules."""
        st = _SystemTime(local.year, local.month, 0, local.day, local.hour, local.minute, local.second, 0)
        utc = _SystemTime()
        if not _kernel32.TzSpecificLocalTimeToSystemTime(None, ctypes.byref(st), ctypes.byref(utc)):
            raise ctypes.WinError(ctypes.get_last_error())
        ft = wintypes.FILETIME()
        if not _kernel32.SystemTimeToFileTime(ctypes.byref(utc), ctypes.byref(ft)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def local_mtime(path: str) -> datetime:
        """The file's Modified time as local wall-clock time.

        ``datetime.fromtimestamp`` rejects pre-1970 times on Windows, which old
        scans legitimately carry, so convert through the Win32 calendar instead.
        """
        value = os.stat(path).st_mtime_ns // 100 + 116444736000000000
        ft = wintypes.FILETIME(value & 0xFFFFFFFF, value >> 32)
        utc, local = _SystemTime(), _SystemTime()
        if not (_kernel32.FileTimeToSystemTime(ctypes.byref(ft), ctypes.byref(utc))
                and _kernel32.SystemTimeToTzSpecificLocalTime(None, ctypes.byref(utc), ctypes.byref(local))):
            raise ctypes.WinError(ctypes.get_last_error())
        return datetime(local.wYear, local.wMonth, local.wDay, local.wHour, local.wMinute, local.wSecond)

    def _com_call(obj: ctypes.c_void_p, index: int, *args: object,
                  argtypes: tuple = (), restype: type = ctypes.HRESULT) -> None:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        prototype(vtable[index])(obj, *args)

    def _set_date_taken(path: str, local: datetime) -> None:
        store = ctypes.c_void_p()
        _shell32.SHGetPropertyStoreFromParsingName(
            os.path.abspath(path), None, _GPS_READWRITE,
            ctypes.byref(_IID_IPROPERTYSTORE), ctypes.byref(store),
        )
        try:
            value = _PropVariant(vt=_VT_FILETIME, filetime=_to_filetime(local))
            # IPropertyStore vtable: 6 = SetValue, 7 = Commit.
            _com_call(store, 6, ctypes.byref(_PKEY_DATE_TAKEN), ctypes.byref(value),
                      argtypes=(ctypes.c_void_p, ctypes.c_void_p))
            _com_call(store, 7)
        finally:
            _com_call(store, 2, restype=ctypes.c_ulong)  # Release

    def _set_file_times(path: str, local: datetime) -> None:
        handle = _kernel32.CreateFileW(path, 0x100, 0x7, None, 3, 0, None)  # FILE_WRITE_ATTRIBUTES, OPEN_EXISTING
        if handle in (None, _INVALID_HANDLE):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            value = _to_filetime(local)
            ft = wintypes.FILETIME(value & 0xFFFFFFFF, value >> 32)
            if not _kernel32.SetFileTime(handle, ctypes.byref(ft), None, ctypes.byref(ft)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _kernel32.CloseHandle(handle)

    class _ComApartment:
        def __enter__(self) -> bool:
            try:
                _ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
                self._owned = True
            except OSError:
                self._owned = False  # already initialised in another mode; still usable
            return True

        def __exit__(self, *_exc: object) -> None:
            if self._owned:
                _ole32.CoUninitialize()

    def metadata_available() -> bool:
        return True

else:
    def _set_date_taken(path: str, local: datetime) -> None:
        raise OSError("writing Date taken needs Windows")

    def _set_file_times(path: str, local: datetime) -> None:
        stamp = local.timestamp()
        os.utime(path, (os.stat(path).st_atime, stamp))

    def local_mtime(path: str) -> datetime:
        return datetime.fromtimestamp(os.stat(path).st_mtime).replace(microsecond=0)

    class _ComApartment:
        def __enter__(self) -> bool:
            return False

        def __exit__(self, *_exc: object) -> None:
            pass

    def metadata_available() -> bool:
        return False


def _writable(path: str, action: Callable[[], None]) -> None:
    """Run ``action`` with the read-only attribute temporarily cleared."""
    mode = os.stat(path).st_mode
    read_only = not mode & stat.S_IWRITE
    if read_only:
        os.chmod(path, mode | stat.S_IWRITE)
    try:
        action()
    finally:
        if read_only:
            os.chmod(path, mode)


def _clean(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip()


# ---------------------------------------------------------------- stamping

def stamp_file(path: str, root: str, options: StampOptions, metadata_ok: bool = True) -> StampResult:
    result = StampResult(path=path, file=os.path.relpath(path, root))
    tod = options.time_of_day
    if options.use_file_time:
        tod = local_mtime(path).time()
    try:
        stamp = date_from_name(os.path.basename(path), tod)
    except UnreadableName as exc:
        result.date, result.status, result.note = exc.prefix, UNREADABLE, str(exc)
        return result

    result.date = stamp.strftime("%Y-%m-%d %H:%M:%S")
    notes: list[str] = []
    failed = False

    ext = os.path.splitext(path)[1].lower()
    if not options.write_metadata:
        result.date_taken = "skipped"
    elif not metadata_ok:
        result.date_taken = "unavailable"
    elif ext not in METADATA_EXTENSIONS:
        result.date_taken = "n/a"
        notes.append(f"{ext} has no EXIF date field")
    elif options.dry_run:
        result.date_taken = "would set"
    else:
        try:
            _writable(path, lambda: _set_date_taken(path, stamp))
            result.date_taken = "set"
        except Exception as exc:  # noqa: BLE001 - reported per file
            result.date_taken, failed = "FAILED", True
            notes.append(f"EXIF: {_clean(exc)}")

    if options.dry_run:
        result.file_times = "would set"
    else:
        try:
            _writable(path, lambda: _set_file_times(path, stamp))
            result.file_times = "set"
        except Exception as exc:  # noqa: BLE001 - reported per file
            result.file_times, failed = "FAILED", True
            notes.append(f"timestamps: {_clean(exc)}")

    result.status = ERROR if failed else OK
    result.note = "; ".join(notes)
    return result


def run(
    root: str,
    options: StampOptions,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> StampReport:
    root = os.path.abspath(root)
    report = StampReport(root=root, dry_run=options.dry_run, metadata_available=metadata_available())
    report_progress = on_progress or (lambda _p: None)

    report_progress(Progress("discover", detail="Finding images..."))
    files = find_files(root, options.recurse)
    with _ComApartment():
        for index, path in enumerate(files, start=1):
            if cancel is not None and cancel.is_set():
                report.cancelled = True
                break
            report.results.append(stamp_file(path, root, options, report.metadata_available))
            report_progress(Progress("stamp", index, len(files), os.path.basename(path)))
    return report


def write_csv(report: StampReport, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["File", "Date", "DateTaken", "FileTimes", "Status", "Note"])
        for r in report.results:
            writer.writerow([r.file, r.date, r.date_taken, r.file_times, r.status, r.note])
