"""Replace a term in file names, e.g. "Bday" -> "B-day".

Only the name changes. A rename on the same volume leaves the file's contents,
Created / Modified times and every embedded metadata field untouched.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .scanner import IMAGE_EXTENSIONS, SKIP_DIR_NAMES

INVALID_NAME_CHARS = set('\\/:*?"<>|')

READY, RENAMED, CONFLICT, INVALID, ERROR, UNDONE = (
    "will rename", "renamed", "CONFLICT", "INVALID", "ERROR", "undone",
)


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


def plan(root: str, options: RenameOptions) -> list[RenameItem]:
    """Every file whose name contains the term, with its new name and any problem."""
    validate(options)
    root = os.path.abspath(root)
    items: list[RenameItem] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _: None):
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


def apply(items: list[RenameItem]) -> None:
    """Rename every READY item in place, recording the outcome on each."""
    for item in items:
        if item.status != READY:
            continue
        # os.rename overwrites on POSIX, so never rely on it to refuse.
        if os.path.exists(item.new_path) and not _same_file(item.path, item.new_path):
            item.status, item.note = CONFLICT, "a file with the new name appeared"
            continue
        try:
            os.rename(item.path, item.new_path)
            item.status = RENAMED
        except OSError as exc:
            item.status, item.note = ERROR, exc.strerror or str(exc)


def undo(items: list[RenameItem]) -> None:
    """Put every RENAMED item back under its original name."""
    for item in reversed(items):
        if item.status != RENAMED:
            continue
        if os.path.exists(item.path) and not _same_file(item.path, item.new_path):
            item.status, item.note = ERROR, "original name is taken; left as renamed"
            continue
        try:
            os.rename(item.new_path, item.path)
            item.status, item.note = UNDONE, ""
        except OSError as exc:
            item.status, item.note = ERROR, f"undo failed: {exc.strerror or exc}"
