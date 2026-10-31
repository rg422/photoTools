"""Replace a term in file names, e.g. "Bday" -> "B-day".

Only the name changes. A rename on the same volume leaves the file's contents,
Created / Modified times and every embedded metadata field untouched.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass

from .engine import Progress, ProgressCallback
from .scanner import IMAGE_EXTENSIONS, SKIP_DIR_NAMES

INVALID_NAME_CHARS = set('\\/:*?"<>|')

READY, RENAMED, CONFLICT, INVALID, ERROR, UNDONE, CANCELLED = (
    "will rename", "renamed", "CONFLICT", "INVALID", "ERROR", "undone", "not renamed",
)

# Seconds between progress reports, so a huge tree does not flood the UI.
PROGRESS_INTERVAL = 0.1


@dataclass(slots=True)
class RenameOptions:
    find: str
    replace: str
    match_case: bool = True
    whole_word: bool = False
    recurse: bool = False
    images_only: bool = True
    include_extension: bool = False


@dataclass(slots=True)
class RenameItem:
    path: str
    file: str
    new_name: str
    status: str = READY
    note: str = ""

    @property
    def new_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), self.new_name)


def validate(options: RenameOptions) -> None:
    """Raise ``ValueError`` for options that can never produce a valid rename."""
    if not options.find:
        raise ValueError("Enter the text to find.")
    bad = sorted(INVALID_NAME_CHARS & set(options.replace))
    if bad:
        raise ValueError(f"File names cannot contain {' '.join(bad)}")


def _pattern(options: RenameOptions) -> re.Pattern[str]:
    body = re.escape(options.find)
    if options.whole_word:
        # Letters and digits bound a word; "_", "-", " " and "." separate words.
        body = rf"(?<![^\W_]){body}(?![^\W_])"
    return re.compile(body, 0 if options.match_case else re.IGNORECASE)


def new_name_for(name: str, options: RenameOptions) -> str:
    stem, ext = (name, "") if options.include_extension else os.path.splitext(name)
    return _pattern(options).sub(lambda _m: options.replace, stem) + ext


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _check_name(name: str) -> str:
    stem = os.path.splitext(name)[0]
    if not stem.strip(" ."):
        return "new name would be empty"
    if name != name.rstrip(" ."):
        return "Windows does not allow names ending in a space or dot"
    return ""


def plural(count: int, noun: str, many: str = "") -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {many or noun + 's'}"


class _Throttle:
    """Calls ``report`` at most every PROGRESS_INTERVAL seconds, unless forced."""

    def __init__(self, report: ProgressCallback | None) -> None:
        self.report = report
        self.last = 0.0

    def __call__(self, progress: Progress, force: bool = False) -> None:
        now = time.monotonic()
        if self.report and (force or now - self.last >= PROGRESS_INTERVAL):
            self.last = now
            self.report(progress)


def plan(
    root: str,
    options: RenameOptions,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> list[RenameItem]:
    """Every file whose name contains the term, with its new name and any problem.

    If ``cancel`` is set part-way, the matches found so far are returned.
    """
    validate(options)
    root = os.path.abspath(root)
    report = _Throttle(on_progress)
    items: list[RenameItem] = []
    folders = checked = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
        if cancel is not None and cancel.is_set():
            break
        folders += 1
        checked += len(filenames)
        report(Progress("search", detail=f"Searching {os.path.relpath(dirpath, root)} - "
                                         f"{plural(folders, 'folder')}, {plural(checked, 'file')} checked, "
                                         f"{plural(len(items), 'match', 'matches')}"))
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith("$"))
        for filename in sorted(filenames, key=str.lower):
            if options.images_only and os.path.splitext(filename)[1].lower() not in IMAGE_EXTENSIONS:
                continue
            new_name = new_name_for(filename, options)
            if new_name == filename:
                continue
            path = os.path.join(dirpath, filename)
            items.append(RenameItem(path=path, file=os.path.relpath(path, root), new_name=new_name))
        if not options.recurse:
            break

    claimed: set[str] = set()
    for item in items:
        target = os.path.normcase(item.new_path)
        problem = _check_name(item.new_name)
        if problem:
            item.status, item.note = INVALID, problem
        elif target in claimed:
            item.status, item.note = CONFLICT, "another file in this list gets the same name"
        elif os.path.exists(item.new_path) and not _same_file(item.path, item.new_path):
            item.status, item.note = CONFLICT, "a file with the new name already exists"
        else:
            claimed.add(target)
    return items


def apply(
    items: list[RenameItem],
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Rename every READY item in place, recording the outcome on each.

    If ``cancel`` is set part-way, the remaining items are marked CANCELLED.
    """
    report = _Throttle(on_progress)
    ready = [i for i in items if i.status == READY]
    for done, item in enumerate(ready, start=1):
        if cancel is not None and cancel.is_set():
            for rest in ready[done - 1:]:
                rest.status, rest.note = CANCELLED, "cancelled before renaming"
            break
        report(Progress("rename", done, len(ready), os.path.basename(item.path)), force=done == len(ready))
        # os.rename overwrites on POSIX, so never rely on it to refuse.
        if os.path.exists(item.new_path) and not _same_file(item.path, item.new_path):
            item.status, item.note = CONFLICT, "a file with the new name appeared"
            continue
        try:
            os.rename(item.path, item.new_path)
            item.status = RENAMED
        except OSError as exc:
            item.status, item.note = ERROR, exc.strerror or str(exc)


def undo(items: list[RenameItem], on_progress: ProgressCallback | None = None) -> None:
    """Put every RENAMED item back under its original name."""
    report = _Throttle(on_progress)
    renamed = [i for i in reversed(items) if i.status == RENAMED]
    for done, item in enumerate(renamed, start=1):
        report(Progress("undo", done, len(renamed), item.new_name), force=done == len(renamed))
        if os.path.exists(item.path) and not _same_file(item.path, item.new_path):
            item.status, item.note = ERROR, "original name is taken; left as renamed"
            continue
        try:
            os.rename(item.new_path, item.path)
            item.status, item.note = UNDONE, ""
        except OSError as exc:
            item.status, item.note = ERROR, f"undo failed: {exc.strerror or exc}"
