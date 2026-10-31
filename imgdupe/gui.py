"""Tkinter front end for reviewing and pruning duplicate images."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import __version__, datestamp, deleter, engine, renamer
from .grouping import DuplicateGroup
from .quality import Candidate

THUMB_SIZE = (280, 280)
CARD_WIDTH = 300

# Pixel sizes in this module are written for a 100% (96 DPI) display; main()
# sets SCALE from the real DPI so they grow with Windows display scaling.
SCALE = 1.0


def px(value):
    """Scale a pixel size, or a tuple of them, to the display's DPI."""
    if isinstance(value, tuple):
        return tuple(px(v) for v in value)
    return round(value * SCALE)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def open_in_explorer(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def open_file(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class DuplicateFinderApp(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=px(8))

        self.folder = tk.StringVar()
        self.sensitivity = tk.StringVar(value=engine.DEFAULT_SENSITIVITY)
        self.status = tk.StringVar(value="Choose a folder and press Scan.")
        self.use_recycle_bin = tk.BooleanVar(value=deleter.recycle_bin_available())

        self.groups: list[DuplicateGroup] = []
        self.keep_flags: dict[str, tk.BooleanVar] = {}
        self.thumbnails: dict[str, ImageTk.PhotoImage] = {}
        self.current_group: DuplicateGroup | None = None

        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self.after(100, self._drain_events)

    # ------------------------------------------------------------------ setup

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(bar, text="Folder:").grid(row=0, column=0, padx=px((0, 6)))
        entry = ttk.Entry(bar, textvariable=self.folder)
        entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="Browse...", command=self._choose_folder).grid(row=0, column=2, padx=px(6))

        ttk.Label(bar, text="Sensitivity:").grid(row=0, column=3, padx=px((12, 6)))
        combo = ttk.Combobox(
            bar, textvariable=self.sensitivity, state="readonly", width=28,
            values=list(engine.SENSITIVITY_PRESETS),
        )
        combo.grid(row=0, column=4)

        self.scan_button = ttk.Button(bar, text="Scan", command=self._start_scan)
        self.scan_button.grid(row=0, column=5, padx=px((12, 0)))

        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=6, sticky="ew", pady=px((8, 0)))
        ttk.Label(bar, textvariable=self.status).grid(row=2, column=0, columnspan=6, sticky="w", pady=px((4, 8)))

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")

        # Place the divider once the panes have a real width. Setting it on a
        # timer could run before layout, when it clamps to 0 and hides the list.
        def place_sash(event: tk.Event) -> None:
            if event.width > px(470) * 2:
                panes.sashpos(0, px(470))
                panes.unbind("<Configure>")

        panes.bind("<Configure>", place_sash)

        left = ttk.Frame(panes)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("files", "delete", "keep", "save", "kind")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Group")
        self.tree.heading("files", text="Files")
        self.tree.heading("delete", text="Delete")
        self.tree.heading("keep", text="Keep")
        self.tree.heading("save", text="Reclaim")
        self.tree.heading("kind", text="Match")
        self.tree.column("#0", width=px(150), minwidth=px(100), anchor="w")
        for key, width in (("files", 45), ("delete", 55), ("keep", 45)):
            self.tree.column(key, width=px(width), minwidth=px(width), stretch=False, anchor="center")
        self.tree.column("save", width=px(75), minwidth=px(75), stretch=False, anchor="e")
        self.tree.column("kind", width=px(60), minwidth=px(60), stretch=False, anchor="center")
        # Groups where nothing will be removed are greyed out.
        self.tree.tag_configure("keep_all", foreground="gray50")
        # Nothing ticked: the group is skipped on delete, so flag it.
        self.tree.tag_configure("none_kept", foreground="#b00020")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_group_selected)
        panes.add(left, weight=1)

        right = ttk.Frame(panes)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self.suggestion = ttk.Label(
            right, text="Select a group to review it.", wraplength=px(820), justify="left",
        )
        self.suggestion.grid(row=0, column=0, sticky="ew", padx=px(8), pady=px((0, 6)))

        canvas_wrap = ttk.Frame(right)
        canvas_wrap.grid(row=1, column=0, sticky="nsew")
        canvas_wrap.rowconfigure(0, weight=1)
        canvas_wrap.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(canvas_wrap, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        hbar = ttk.Scrollbar(canvas_wrap, orient="horizontal", command=self.canvas.xview)
        hbar.grid(row=1, column=0, sticky="ew")
        vbar = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        self.cards = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
        self.cards.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

        actions = ttk.Frame(right)
        actions.grid(row=2, column=0, sticky="ew", pady=px((8, 0)))
        ttk.Button(actions, text="Reset to suggestion", command=self._reset_group).pack(side="left")
        ttk.Button(
            actions, text="Keep all - skip group", command=self._keep_all_in_group,
        ).pack(side="left", padx=px((8, 0)))
        self.group_delete_button = ttk.Button(
            actions, text="Delete unkept in this group", command=self._delete_current_group, state="disabled",
        )
        self.group_delete_button.pack(side="left", padx=px(8))
        panes.add(right, weight=3)

    def _build_footer(self) -> None:
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", pady=px((8, 0)))
        footer.columnconfigure(0, weight=1)

        self.summary = ttk.Label(footer, text="")
        self.summary.grid(row=0, column=0, sticky="w")

        recycle_label = "Send to Recycle Bin" if deleter.recycle_bin_available() else "Recycle Bin unavailable"
        check = ttk.Checkbutton(footer, text=recycle_label, variable=self.use_recycle_bin)
        if not deleter.recycle_bin_available():
            check.state(["disabled"])
        check.grid(row=0, column=1, padx=px(8))

        self.delete_all_button = ttk.Button(
            footer, text="Delete all unkept files...", command=self._delete_everything, state="disabled",
        )
        self.delete_all_button.grid(row=0, column=2)

    # ------------------------------------------------------------------ scan

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the folder to search")
        if chosen:
            self.folder.set(chosen)

    def _start_scan(self) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self.status.set("Cancelling...")
            return

        root = self.folder.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Pick a folder", "Choose an existing folder to search.")
            return

        self._clear_results()
        self._cancel = threading.Event()
        threshold = engine.SENSITIVITY_PRESETS[self.sensitivity.get()]
        self.scan_button.configure(text="Cancel")
        self.progress.configure(value=0, maximum=100)

        def work() -> None:
            try:
                result = engine.scan(
                    root, threshold,
                    on_progress=lambda p: self._events.put(("progress", p)),
                    cancel=self._cancel,
                )
                self._events.put(("result", result))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self._events.put(("error", f"{type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._apply_progress(payload)
                elif kind == "result":
                    self._apply_result(payload)
                elif kind == "error":
                    self.scan_button.configure(text="Scan")
                    self.status.set("Scan failed.")
                    messagebox.showerror("Scan failed", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _apply_progress(self, progress: engine.Progress) -> None:
        if progress.total:
            self.progress.configure(maximum=progress.total, value=progress.current)
            self.status.set(f"{progress.current}/{progress.total} - {progress.detail}")
        else:
            self.status.set(progress.detail)

    def _apply_result(self, result: engine.ScanResult) -> None:
        self.scan_button.configure(text="Scan")
        if result.cancelled:
            self.status.set("Scan cancelled.")
            return

        self.groups = result.groups
        for index, group in enumerate(self.groups, start=1):
            for member in group.members:
                self.keep_flags[member.path] = tk.BooleanVar(value=member is group.keeper)
            self.tree.insert("", "end", iid=str(index - 1), text=f"{index}. {group.keeper.name}")

        failures = f", {len(result.failed)} unreadable" if result.failed else ""
        self.status.set(
            f"Scanned {plural(result.scanned, 'image')}{failures}. "
            f"{plural(len(self.groups), 'duplicate group')}, "
            f"{human_bytes(result.reclaimable_bytes)} reclaimable."
        )
        self.progress.configure(value=self.progress["maximum"])
        self._refresh_summary()
        if self.groups:
            self.tree.selection_set("0")
            self.tree.focus("0")

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.groups = []
        self.keep_flags.clear()
        self.thumbnails.clear()
        self.current_group = None
        self._render_cards(None)
        self._refresh_summary()

    # --------------------------------------------------------------- rendering

    def _on_group_selected(self, _event: object) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(self.groups):
            self.current_group = self.groups[index]
            self._render_cards(self.current_group)

    def _render_cards(self, group: DuplicateGroup | None) -> None:
        for child in self.cards.winfo_children():
            child.destroy()

        if group is None:
            self.suggestion.configure(text="Select a group to review it.")
            self.group_delete_button.configure(state="disabled")
            return

        reasons = "; ".join(group.keeper_reasons)
        self.suggestion.configure(
            text=f"Suggested keeper: {group.keeper.name}  -  {reasons}.\n"
                 f"Images are ordered best quality first. Tick every image you want to keep; "
                 f"unticked images are the ones that get removed."
        )
        self.group_delete_button.configure(state="normal")

        best = group.keeper.quality.score
        for column, member in enumerate(group.members):
            self._build_card(column, member, is_suggested=member is group.keeper, best_score=best)
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)

    def _build_card(self, column: int, member: Candidate, is_suggested: bool, best_score: float) -> None:
        title = "SUGGESTED KEEP" if is_suggested else f"{member.quality.score / best_score * 100:.0f}% of best"
        card = ttk.LabelFrame(self.cards, text=title, padding=px(6))
        card.grid(row=0, column=column, padx=px(6), pady=px(6), sticky="n")

        thumb = self._thumbnail(member.path)
        holder = ttk.Label(card, image=thumb) if thumb else ttk.Label(card, text="(preview failed)", width=24)
        holder.pack()
        holder.bind("<Double-Button-1>", lambda _e, p=member.path: open_file(p))

        ttk.Checkbutton(
            card, text="Keep this file", variable=self.keep_flags[member.path],
            command=self._refresh_summary, style="Keep.TCheckbutton",
        ).pack(anchor="w", pady=px((8, 4)))

        detail = ttk.Frame(card)
        detail.pack(fill="x")
        lines = [
            member.name,
            f"score {member.quality.score:.0f}/100",
            f"{member.dimensions}  -  {human_bytes(member.size)}",
            " - ".join(member.quality.notes[1:]) or member.sig.fmt,
            os.path.dirname(member.path),
        ]
        for i, text in enumerate(lines):
            ttk.Label(
                detail, text=text, wraplength=px(CARD_WIDTH - 20), justify="left",
                font=("TkDefaultFont", 8) if i > 1 else ("TkDefaultFont", 9, "bold" if i == 0 else "normal"),
                foreground="gray30" if i >= 3 else "",
            ).pack(anchor="w")

        buttons = ttk.Frame(card)
        buttons.pack(fill="x", pady=px((6, 0)))
        ttk.Button(buttons, text="Open", width=7, command=lambda p=member.path: open_file(p)).pack(side="left")
        ttk.Button(buttons, text="Folder", width=8, command=lambda p=member.path: open_in_explorer(p)).pack(side="left", padx=px(4))

    def _thumbnail(self, path: str) -> ImageTk.PhotoImage | None:
        if path in self.thumbnails:
            return self.thumbnails[path]
        try:
            with Image.open(path) as img:
                img.draft("RGB", px(THUMB_SIZE))
                img = img.convert("RGB")
                img.thumbnail(px(THUMB_SIZE), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001 - a broken preview must not break review
            return None
        self.thumbnails[path] = photo
        return photo

    def _on_mousewheel(self, event: tk.Event) -> None:
        self.canvas.xview_scroll(-1 * (event.delta // 120), "units")

    def _refresh_group_rows(self) -> None:
        """Update every group's Delete / Keep / Reclaim figures from the tick boxes."""
        for index, group in enumerate(self.groups):
            doomed = self._files_to_delete(only=group)
            kept = sum(1 for m in group.members if self.keep_flags[m.path].get())
            tags = ("none_kept",) if not kept else ("keep_all",) if not doomed else ()
            self.tree.item(
                str(index), tags=tags,
                values=(len(group.members), len(doomed), kept,
                        human_bytes(sum(size for _p, size in doomed)),
                        "exact" if group.exact else "similar"),
            )

    def _refresh_summary(self) -> None:
        self._refresh_group_rows()
        doomed = self._files_to_delete()
        total = sum(size for _p, size in doomed)
        self.summary.configure(
            text=f"{plural(len(self.groups), 'group')}  -  "
                 f"{plural(len(doomed), 'file')} marked for removal  -  {human_bytes(total)}"
        )
        self.delete_all_button.configure(state="normal" if doomed else "disabled")

    # ---------------------------------------------------------------- deleting

    def _files_to_delete(self, only: DuplicateGroup | None = None) -> list[tuple[str, int]]:
        doomed: list[tuple[str, int]] = []
        for group in ([only] if only else self.groups):
            kept = [m for m in group.members if self.keep_flags[m.path].get()]
            if not kept:
                continue  # never let a whole group be wiped out
            doomed.extend((m.path, m.size) for m in group.members if not self.keep_flags[m.path].get())
        return doomed

    def _keep_all_in_group(self) -> None:
        """Tick every file in the current group, then move on to the next group."""
        if not self.current_group:
            return
        for member in self.current_group.members:
            self.keep_flags[member.path].set(True)
        self._refresh_summary()
        index = self.groups.index(self.current_group)
        if index + 1 < len(self.groups):
            following = str(index + 1)
            self.tree.selection_set(following)
            self.tree.focus(following)
            self.tree.see(following)

    def _reset_group(self) -> None:
        if not self.current_group:
            return
        for member in self.current_group.members:
            self.keep_flags[member.path].set(member is self.current_group.keeper)
        self._refresh_summary()

    def _delete_current_group(self) -> None:
        if not self.current_group:
            return
        kept = [m for m in self.current_group.members if self.keep_flags[m.path].get()]
        if not kept:
            messagebox.showwarning("Nothing kept", "Tick at least one image to keep in this group.")
            return
        self._confirm_and_delete(self._files_to_delete(only=self.current_group))

    def _delete_everything(self) -> None:
        skipped = sum(
            1 for g in self.groups if not any(self.keep_flags[m.path].get() for m in g.members)
        )
        if skipped:
            messagebox.showinfo(
                "Groups skipped",
                f"{skipped} group(s) have no image ticked to keep and will be left untouched.",
            )
        self._confirm_and_delete(self._files_to_delete())

    def _confirm_and_delete(self, doomed: list[tuple[str, int]]) -> None:
        if not doomed:
            messagebox.showinfo("Nothing to do", "No files are marked for removal.")
            return

        destination = "the Recycle Bin" if (self.use_recycle_bin.get() and deleter.recycle_bin_available()) \
            else f"a quarantine folder inside {self.folder.get()}"
        total = sum(size for _p, size in doomed)
        preview = "\n".join(os.path.basename(p) for p, _s in doomed[:12])
        if len(doomed) > 12:
            preview += f"\n...and {len(doomed) - 12} more"

        if not messagebox.askyesno(
            "Confirm removal",
            f"Move {plural(len(doomed), 'file')} ({human_bytes(total)}) to {destination}?\n\n{preview}",
            icon="warning",
        ):
            return

        outcome = deleter.remove(
            [p for p, _s in doomed], self.folder.get(), use_recycle_bin=self.use_recycle_bin.get()
        )
        self._drop_removed(set(outcome.removed))

        message = (f"Moved {plural(len(outcome.removed), 'file')} to {outcome.destination}, "
                   f"freeing {human_bytes(outcome.freed_bytes)}.")
        if outcome.errors:
            message += f"\n\n{plural(len(outcome.errors), 'file')} could not be removed:\n"
            message += "\n".join(f"{os.path.basename(p)}: {e}" for p, e in outcome.errors[:6])
            messagebox.showwarning("Finished with errors", message)
        else:
            messagebox.showinfo("Done", message)

    def _drop_removed(self, removed: set[str]) -> None:
        if not removed:
            return
        remaining: list[DuplicateGroup] = []
        for group in self.groups:
            group.members = [m for m in group.members if m.path not in removed]
            if len(group.members) > 1:
                remaining.append(group)
        for path in removed:
            self.keep_flags.pop(path, None)
            self.thumbnails.pop(path, None)

        self.groups = remaining
        self.tree.delete(*self.tree.get_children())
        for index, group in enumerate(self.groups, start=1):
            self.tree.insert("", "end", iid=str(index - 1), text=f"{index}. {group.keeper.name}")
        self.current_group = None
        self._render_cards(None)
        self._refresh_summary()


class DateStamperApp(ttk.Frame):
    """Writes the YYYYMMDD date from each file name into Date taken and file times."""

    COLUMNS = (
        ("date", "Date", 140, "w"),
        ("taken", "Date taken", 90, "center"),
        ("times", "File times", 90, "center"),
        ("status", "Status", 90, "center"),
        ("note", "Note", 280, "w"),
    )

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=px(8))

        self.folder = tk.StringVar()
        self.recurse = tk.BooleanVar(value=False)
        self.use_file_time = tk.BooleanVar(value=False)
        self.time_text = tk.StringVar(value="12:00")
        self.write_metadata = tk.BooleanVar(value=datestamp.metadata_available())
        self.problems_only = tk.BooleanVar(value=False)
        self.status = tk.StringVar(
            value="Choose a folder and press Preview. Nothing changes until you press Apply."
        )

        self.report: datestamp.StampReport | None = None
        self._shown: list[datestamp.StampResult] = []
        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._running_dry = True

        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self.after(100, self._drain_events)

    # ------------------------------------------------------------------ setup

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(bar, text="Folder:").grid(row=0, column=0, padx=px((0, 6)))
        ttk.Entry(bar, textvariable=self.folder).grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="Browse...", command=self._choose_folder).grid(row=0, column=2, padx=px(6))
        ttk.Checkbutton(bar, text="Include subfolders", variable=self.recurse).grid(row=0, column=3, padx=px((6, 0)))

        options = ttk.Frame(bar)
        options.grid(row=1, column=0, columnspan=4, sticky="w", pady=px((8, 0)))
        ttk.Label(options, text="When the name has no time:").pack(side="left")
        ttk.Radiobutton(
            options, text="use", variable=self.use_file_time, value=False, command=self._sync_time_entry,
        ).pack(side="left", padx=px((8, 2)))
        self.time_entry = ttk.Entry(options, textvariable=self.time_text, width=8)
        self.time_entry.pack(side="left")
        ttk.Radiobutton(
            options, text="keep the file's current time of day", variable=self.use_file_time, value=True,
            command=self._sync_time_entry,
        ).pack(side="left", padx=px((12, 0)))

        metadata = ttk.Checkbutton(
            options, text='Write EXIF "Date taken"' if datestamp.metadata_available()
            else 'EXIF "Date taken" needs Windows', variable=self.write_metadata,
        )
        if not datestamp.metadata_available():
            metadata.state(["disabled"])
        metadata.pack(side="left", padx=px((24, 0)))

        buttons = ttk.Frame(bar)
        buttons.grid(row=0, column=4, rowspan=2, padx=px((12, 0)), sticky="e")
        self.preview_button = ttk.Button(buttons, text="Preview", command=lambda: self._start(dry_run=True))
        self.preview_button.pack(fill="x")
        self.apply_button = ttk.Button(buttons, text="Apply dates...", command=lambda: self._start(dry_run=False))
        self.apply_button.pack(fill="x", pady=px((4, 0)))

        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=2, column=0, columnspan=5, sticky="ew", pady=px((8, 0)))
        ttk.Label(bar, textvariable=self.status).grid(row=3, column=0, columnspan=5, sticky="w", pady=px((4, 8)))

    def _build_body(self) -> None:
        wrap = ttk.Frame(self)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in self.COLUMNS], show="tree headings", selectmode="browse",
        )
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=px(360), minwidth=px(160), anchor="w")
        for key, title, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=px(width), minwidth=px(60), anchor=anchor, stretch=key in ("note",))
        self.tree.tag_configure(datestamp.UNREADABLE, background="#fff4cc")
        self.tree.tag_configure(datestamp.ERROR, background="#ffd6d6")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-Button-1>", self._open_selected)

    def _build_footer(self) -> None:
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", pady=px((8, 0)))
        footer.columnconfigure(0, weight=1)

        self.summary = ttk.Label(
            footer, text="Files are never renamed and the picture data is never re-encoded.",
        )
        self.summary.grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            footer, text="Only files needing attention", variable=self.problems_only, command=self._fill_tree,
        ).grid(row=0, column=1, padx=px(8))
        self.csv_button = ttk.Button(footer, text="Export CSV...", command=self._export_csv, state="disabled")
        self.csv_button.grid(row=0, column=2)

    def _sync_time_entry(self) -> None:
        self.time_entry.state(["disabled"] if self.use_file_time.get() else ["!disabled"])

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the folder of photos to date")
        if chosen:
            self.folder.set(chosen)

    # ------------------------------------------------------------------- run

    def _stamp_options(self, dry_run: bool) -> datestamp.StampOptions | None:
        try:
            time_of_day = datestamp.parse_time_of_day(self.time_text.get())
        except ValueError as exc:
            if not self.use_file_time.get():
                messagebox.showwarning("Check the time", str(exc))
                return None
            time_of_day = datestamp.DEFAULT_TIME
        return datestamp.StampOptions(
            recurse=self.recurse.get(),
            time_of_day=time_of_day,
            use_file_time=self.use_file_time.get(),
            write_metadata=self.write_metadata.get(),
            dry_run=dry_run,
        )

    def _start(self, dry_run: bool) -> None:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self.status.set("Cancelling...")
            return

        root = self.folder.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Pick a folder", "Choose an existing folder of photos.")
            return
        options = self._stamp_options(dry_run)
        if options is None:
            return
        if not dry_run and not self._confirm_apply(root, options):
            return

        self._cancel = threading.Event()
        self._running_dry = dry_run
        self.progress.configure(value=0, maximum=100)
        (self.preview_button if dry_run else self.apply_button).configure(text="Cancel")
        (self.apply_button if dry_run else self.preview_button).state(["disabled"])

        def work() -> None:
            try:
                report = datestamp.run(
                    root, options,
                    on_progress=lambda p: self._events.put(("progress", p)),
                    cancel=self._cancel,
                )
                self._events.put(("result", report))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self._events.put(("error", f"{type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _confirm_apply(self, root: str, options: datestamp.StampOptions) -> bool:
        files = datestamp.find_files(root, options.recurse)
        dated = 0
        for path in files:
            try:
                datestamp.date_from_name(os.path.basename(path))
                dated += 1
            except datestamp.UnreadableName:
                pass
        if not dated:
            messagebox.showinfo("Nothing to do", "No image in this folder starts with a YYYYMMDD date.")
            return False

        what = 'Date taken and Created/Modified times' if options.write_metadata else 'Created/Modified times'
        skipped = len(files) - dated
        message = f"Write the date from the file name into the {what} of {plural(dated, 'image')} in\n{root}?"
        if skipped:
            message += f"\n\n{plural(skipped, 'file')} without a usable date will be left untouched."
        message += "\n\nThis changes the files in place. Run Preview first if you have not checked it."
        return messagebox.askyesno("Apply dates", message, icon="warning")

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    if payload.total:
                        self.progress.configure(maximum=payload.total, value=payload.current)
                        self.status.set(f"{payload.current}/{payload.total} - {payload.detail}")
                    else:
                        self.status.set(payload.detail)
                elif kind == "result":
                    self._finish()
                    self._apply_report(payload)
                elif kind == "error":
                    self._finish()
                    self.status.set("Failed.")
                    messagebox.showerror("Date stamping failed", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self) -> None:
        self.preview_button.configure(text="Preview")
        self.apply_button.configure(text="Apply dates...")
        self.preview_button.state(["!disabled"])
        self.apply_button.state(["!disabled"])

    def _apply_report(self, report: datestamp.StampReport) -> None:
        self.report = report
        self.progress.configure(value=self.progress["maximum"])
        self._fill_tree()
        self.csv_button.configure(state="normal" if report.results else "disabled")

        ok, errors, unknown = (report.count(s) for s in (datestamp.OK, datestamp.ERROR, datestamp.UNREADABLE))
        verb = "would be dated" if report.dry_run else "dated"
        parts = [f"{plural(len(report.results), 'image')} examined", f"{ok} {verb}"]
        if errors:
            parts.append(f"{plural(errors, 'error')} while writing")
        if unknown:
            parts.append(f"{unknown} not interpretable")
        self.summary.configure(text="  -  ".join(parts))

        if report.cancelled:
            self.status.set("Cancelled. Files already processed are listed below.")
        elif not report.results:
            self.status.set("No image files found in that folder.")
        elif report.dry_run:
            self.status.set("Preview only - nothing was changed. Press Apply dates... to write them.")
        else:
            self.status.set("Done. Highlighted rows were left untouched or failed; see the Note column.")

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if not self.report:
            return
        rows = self.report.problems if self.problems_only.get() else self.report.results
        for index, r in enumerate(rows):
            self.tree.insert(
                "", "end", iid=str(index), text=r.file,
                values=(r.date, r.date_taken, r.file_times, r.status, r.note),
                tags=(r.status,),
            )
        self._shown = rows

    def _open_selected(self, _event: object) -> None:
        selection = self.tree.selection()
        if selection:
            open_file(self._shown[int(selection[0])].path)

    def _export_csv(self) -> None:
        if not self.report:
            return
        target = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".csv", initialfile="photo-dates.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not target:
            return
        try:
            datestamp.write_csv(self.report, target)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc))
            return
        self.status.set(f"Report saved to {target}")


class RenameApp(ttk.Frame):
    """Replaces a term in file names. Only the name changes - never times or metadata."""

    PROBLEMS = (renamer.CONFLICT, renamer.INVALID, renamer.ERROR)

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=px(8))

        self.folder = tk.StringVar()
        self.find_text = tk.StringVar()
        self.replace_text = tk.StringVar()
        self.recurse = tk.BooleanVar(value=False)
        self.match_case = tk.BooleanVar(value=True)
        self.whole_word = tk.BooleanVar(value=False)
        self.images_only = tk.BooleanVar(value=True)
        self.status = tk.StringVar(
            value='Enter a term to find, e.g. "Bday", and what to replace it with, then press Preview.'
        )

        self.items: list[renamer.RenameItem] = []
        self.last_applied: list[renamer.RenameItem] = []

        self._events: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None

        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self.after(100, self._drain_events)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(bar, text="Folder:").grid(row=0, column=0, padx=px((0, 6)), sticky="w")
        ttk.Entry(bar, textvariable=self.folder).grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="Browse...", command=self._choose_folder).grid(row=0, column=2, padx=px(6))
        ttk.Checkbutton(bar, text="Include subfolders", variable=self.recurse).grid(row=0, column=3, padx=px((6, 0)))

        terms = ttk.Frame(bar)
        terms.grid(row=1, column=0, columnspan=4, sticky="w", pady=px((8, 0)))
        ttk.Label(terms, text="Find:").pack(side="left")
        find_entry = ttk.Entry(terms, textvariable=self.find_text, width=22)
        find_entry.pack(side="left", padx=px((6, 12)))
        ttk.Label(terms, text="Replace with:").pack(side="left")
        replace_entry = ttk.Entry(terms, textvariable=self.replace_text, width=22)
        replace_entry.pack(side="left", padx=px((6, 18)))
        for entry in (find_entry, replace_entry):
            entry.bind("<Return>", lambda _e: self._preview())
        ttk.Checkbutton(terms, text="Match case", variable=self.match_case).pack(side="left")
        ttk.Checkbutton(terms, text="Whole word only", variable=self.whole_word).pack(side="left", padx=px((12, 0)))
        ttk.Checkbutton(terms, text="Only image files", variable=self.images_only).pack(side="left", padx=px((12, 0)))

        buttons = ttk.Frame(bar)
        buttons.grid(row=0, column=4, rowspan=2, padx=px((12, 0)), sticky="e")
        self.preview_button = ttk.Button(buttons, text="Preview", command=self._preview)
        self.preview_button.pack(fill="x")
        self.rename_button = ttk.Button(buttons, text="Rename...", command=self._rename)
        self.rename_button.pack(fill="x", pady=px((4, 0)))

        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=2, column=0, columnspan=5, sticky="ew", pady=px((8, 0)))
        ttk.Label(bar, textvariable=self.status).grid(row=3, column=0, columnspan=5, sticky="w", pady=px((4, 8)))

    def _build_body(self) -> None:
        wrap = ttk.Frame(self)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(wrap, columns=("new", "status", "note"), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Current name")
        self.tree.heading("new", text="New name")
        self.tree.heading("status", text="Status")
        self.tree.heading("note", text="Note")
        self.tree.column("#0", width=px(380), minwidth=px(160), anchor="w")
        self.tree.column("new", width=px(320), minwidth=px(160), anchor="w")
        self.tree.column("status", width=px(100), minwidth=px(80), stretch=False, anchor="center")
        self.tree.column("note", width=px(260), minwidth=px(100), anchor="w")
        for status in self.PROBLEMS:
            self.tree.tag_configure(status, background="#ffd6d6")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

    def _build_footer(self) -> None:
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", pady=px((8, 0)))
        footer.columnconfigure(0, weight=1)
        self.summary = ttk.Label(
            footer, text="Only the name changes: Created/Modified dates and photo metadata are left exactly as they are.",
        )
        self.summary.grid(row=0, column=0, sticky="w")
        self.undo_button = ttk.Button(footer, text="Undo last rename", command=self._undo, state="disabled")
        self.undo_button.grid(row=0, column=1)

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the folder of files to rename")
        if chosen:
            self.folder.set(chosen)

    # ------------------------------------------------------------------- run

    # Searching and renaming run on a worker thread so a large tree of
    # subfolders keeps the window responsive and shows progress.

    def _busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    # A Preview that runs longer than this also gets a "finished" pop-up, since
    # the user has likely looked away; quick ones just update the status line.
    SLOW_SEARCH_SECONDS = 2.0

    def _run(self, kind: str, job, then) -> None:
        """Run ``job(report, cancel)`` on a worker, then ``then(result)`` on the UI thread."""
        self._cancel = threading.Event()
        self._then = then
        self._started = time.monotonic()
        self.preview_button.configure(text="Cancel")
        self.rename_button.state(["disabled"])
        self.undo_button.state(["disabled"])
        if kind == "search":
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(12)
        else:
            self.progress.configure(mode="determinate", value=0, maximum=100)

        def work() -> None:
            try:
                result = job(lambda p: self._events.put(("progress", p)), self._cancel)
                self._events.put(("done", result))
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self._events.put(("error", f"{type(exc).__name__}: {exc}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    if payload.total:
                        self.progress.configure(maximum=payload.total, value=payload.current)
                        self.status.set(f"{payload.current}/{payload.total} - {payload.detail}")
                    else:
                        self.status.set(payload.detail)
                elif kind == "done":
                    self._idle()
                    self._then(payload)
                elif kind == "error":
                    self._idle()
                    self.status.set("Failed.")
                    messagebox.showerror("Rename failed", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _idle(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=self.progress["maximum"])
        self.preview_button.configure(text="Preview")
        self.rename_button.state(["!disabled"])
        has_undo = any(i.status == renamer.RENAMED for i in self.last_applied)
        self.undo_button.state(["!disabled"] if has_undo else ["disabled"])

    def _search(self, then) -> None:
        """Find matching files in the background, then call ``then(items)``."""
        if self._busy():
            self._cancel.set()
            self.status.set("Cancelling...")
            return
        root = self.folder.get().strip()
        if not root or not os.path.isdir(root):
            messagebox.showwarning("Pick a folder", "Choose an existing folder.")
            return
        options = renamer.RenameOptions(
            find=self.find_text.get(),
            replace=self.replace_text.get(),
            match_case=self.match_case.get(),
            whole_word=self.whole_word.get(),
            recurse=self.recurse.get(),
            images_only=self.images_only.get(),
        )
        try:
            renamer.validate(options)
        except ValueError as exc:
            messagebox.showwarning("Check the terms", str(exc))
            return
        self.status.set("Searching...")
        self._run(
            "search", lambda report, cancel: renamer.plan(root, options, report, cancel),
            lambda items: self._searched(items, then),
        )

    def _searched(self, items: list[renamer.RenameItem], then) -> None:
        self._show(items)
        if self._cancel.is_set():
            self.status.set(f"Search cancelled - showing the {renamer.plural(len(items), 'match', 'matches')} found so far. "
                            "Nothing was renamed.")
            return
        then(items)

    def _preview(self) -> None:
        self._search(self._previewed)

    def _previewed(self, items: list[renamer.RenameItem]) -> None:
        ready = sum(1 for i in items if i.status == renamer.READY)
        if not items:
            message = f'No file name contains "{self.find_text.get()}".'
        else:
            message = f"Preview only - nothing was changed. {plural(ready, 'file')} will be renamed."
        self.status.set(message)
        if time.monotonic() - self._started >= self.SLOW_SEARCH_SECONDS:
            messagebox.showinfo("Preview complete", message)

    def _rename(self) -> None:
        if not self._busy():
            self._search(self._confirm_rename)

    def _confirm_rename(self, items: list[renamer.RenameItem]) -> None:
        ready = [i for i in items if i.status == renamer.READY]
        if not ready:
            self.status.set("Nothing to rename.")
            messagebox.showinfo("Nothing to do", "No file can be renamed with these terms.")
            return

        sample = "\n".join(f"{os.path.basename(i.path)}  ->  {i.new_name}" for i in ready[:10])
        if len(ready) > 10:
            sample += f"\n...and {len(ready) - 10} more"
        blocked = len(items) - len(ready)
        if blocked:
            sample += f"\n\n{plural(blocked, 'file')} with a problem will be left alone."
        if not messagebox.askyesno("Rename files", f"Rename {plural(len(ready), 'file')}?\n\n{sample}"):
            self.status.set("Nothing was renamed.")
            return

        self.status.set("Renaming...")
        self._run(
            "rename", lambda report, cancel: renamer.apply(items, report, cancel),
            lambda _r: self._renamed(items),
        )

    def _renamed(self, items: list[renamer.RenameItem]) -> None:
        self.last_applied = items
        self._show(items)
        self._idle()  # refresh Undo now that last_applied is set
        done = sum(1 for i in items if i.status == renamer.RENAMED)
        failed = sum(1 for i in items if i.status in self.PROBLEMS)
        skipped = sum(1 for i in items if i.status == renamer.CANCELLED)
        message = f"Renamed {plural(done, 'file')}."
        if skipped:
            message += f" Cancelled - {plural(skipped, 'file')} not renamed."
        if failed:
            message += f" {failed} could not be renamed; see the Note column."
        self.status.set(message)
        if failed:
            messagebox.showwarning("Rename finished with problems", message)
        else:
            messagebox.showinfo("Rename complete", message)

    def _undo(self) -> None:
        renamed = [i for i in self.last_applied if i.status == renamer.RENAMED]
        if self._busy() or not renamed or not messagebox.askyesno(
            "Undo rename", f"Put {plural(len(renamed), 'file')} back to their original names?",
        ):
            return
        self.status.set("Restoring original names...")
        self._run(
            "undo", lambda report, _cancel: renamer.undo(self.last_applied, report),
            lambda _r: self._undone(),
        )

    def _undone(self) -> None:
        self._show(self.last_applied)
        self._idle()
        undone = sum(1 for i in self.last_applied if i.status == renamer.UNDONE)
        self.status.set(f"Restored {plural(undone, 'original name')}.")
        messagebox.showinfo("Undo complete", self.status.get())

    def _show(self, items: list[renamer.RenameItem]) -> None:
        self.items = items
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(items):
            self.tree.insert(
                "", "end", iid=str(index), text=item.file,
                values=(item.new_name, item.status, item.note), tags=(item.status,),
            )
        problems = sum(1 for i in items if i.status in self.PROBLEMS)
        self.summary.configure(
            text=f"{plural(len(items), 'matching file')}" + (f"  -  {problems} with a problem" if problems else "")
        )


def _tick_box_image(size: int, checked: bool, hover: bool) -> Image.Image:
    """A tick box drawn at 4x and downsampled, with a gap on the right before the label."""
    from PIL import ImageDraw

    k = 4
    box = size * k
    img = Image.new("RGBA", ((size + px(8)) * k, box), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    blue = (0, 95, 184, 255) if not hover else (0, 120, 215, 255)
    inset, radius = k * 2, box // 6
    if checked:
        draw.rounded_rectangle((inset, inset, box - inset, box - inset), radius, fill=blue)
        draw.line(
            [(box * 0.25, box * 0.52), (box * 0.43, box * 0.70), (box * 0.76, box * 0.32)],
            fill=(255, 255, 255, 255), width=max(k * 2, box // 9), joint="curve",
        )
    else:
        edge = blue if hover else (90, 90, 90, 255)
        draw.rounded_rectangle((inset, inset, box - inset, box - inset), radius,
                               fill=(255, 255, 255, 255), outline=edge, width=max(k, box // 14))
    return img.resize((img.width // k, size), Image.Resampling.LANCZOS)


def add_keep_checkbox_style(root: tk.Tk, style: ttk.Style, font: tkfont.Font) -> None:
    """``Keep.TCheckbutton``: a large tick box, since the theme's is fixed at a few pixels."""
    size = px(26)
    images = {
        (checked, hover): ImageTk.PhotoImage(_tick_box_image(size, checked, hover), master=root)
        for checked in (False, True) for hover in (False, True)
    }
    root._keep_tick_images = images  # Tk does not hold a reference itself
    style.element_create(
        "Keep.indicator", "image", images[(False, False)],
        ("selected", "active", images[(True, True)]),
        ("selected", images[(True, False)]),
        ("active", images[(False, True)]),
    )
    style.layout("Keep.TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Keep.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])
    style.configure("Keep.TCheckbutton", font=font)


def work_area(root: tk.Tk) -> tuple[int, int, int, int]:
    """The desktop area not covered by the taskbar, as (left, top, right, bottom)."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 - non-Windows
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight() - px(60)


def main() -> None:
    global SCALE
    # Must happen before Tk starts: Tk reads the DPI once, at creation. Opting in
    # afterwards leaves Tk sized for 96 DPI on a scaled display, so the whole
    # window renders too small.
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - non-Windows or older Windows
        pass
    root = tk.Tk()
    SCALE = max(1.0, root.winfo_fpixels("1i") / 96)

    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    style.configure("Treeview", rowheight=linespace + px(6))
    # Larger, easier-to-hit buttons and tabs.
    button_font = tkfont.nametofont("TkDefaultFont").copy()
    button_font.configure(size=11)
    style.configure("TButton", font=button_font, padding=(px(12), px(6)))
    style.configure("TNotebook.Tab", font=button_font, padding=(px(14), px(6)))
    add_keep_checkbox_style(root, style, button_font)

    root.title(f"Photo Tools {__version__}")
    left, top, right, bottom = work_area(root)
    # Leave room for the title bar and border, which the geometry excludes.
    width = min(px(1280), right - left - px(16))
    height = min(px(820), bottom - top - px(48))
    x = left + (right - left - width) // 2
    y = top + max(0, (bottom - top - height - px(40)) // 2)
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.minsize(min(px(1000), width), min(px(640), height))
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    tabs = ttk.Notebook(root)
    tabs.grid(row=0, column=0, sticky="nsew")
    tabs.add(DuplicateFinderApp(tabs), text="Find duplicates")
    tabs.add(DateStamperApp(tabs), text="Date from filename")
    tabs.add(RenameApp(tabs), text="Rename files")
    root.mainloop()


if __name__ == "__main__":
    main()
