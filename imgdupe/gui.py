"""Tkinter front end for reviewing and pruning duplicate images."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import deleter, engine
from .grouping import DuplicateGroup
from .quality import Candidate

THUMB_SIZE = (190, 190)
CARD_WIDTH = 210


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
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=8)
        self.master.title("Image Duplicate Finder")
        self.master.geometry("1280x820")
        self.master.minsize(1000, 640)
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)

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

        ttk.Label(bar, text="Folder:").grid(row=0, column=0, padx=(0, 6))
        entry = ttk.Entry(bar, textvariable=self.folder)
        entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(bar, text="Browse...", command=self._choose_folder).grid(row=0, column=2, padx=6)

        ttk.Label(bar, text="Sensitivity:").grid(row=0, column=3, padx=(12, 6))
        combo = ttk.Combobox(
            bar, textvariable=self.sensitivity, state="readonly", width=28,
            values=list(engine.SENSITIVITY_PRESETS),
        )
        combo.grid(row=0, column=4)

        self.scan_button = ttk.Button(bar, text="Scan", command=self._start_scan)
        self.scan_button.grid(row=0, column=5, padx=(12, 0))

        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Label(bar, textvariable=self.status).grid(row=2, column=0, columnspan=6, sticky="w", pady=(4, 8))

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")
        self.after(80, lambda: panes.sashpos(0, 380))

        left = ttk.Frame(panes)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("files", "save", "kind")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Group")
        self.tree.heading("files", text="Files")
        self.tree.heading("save", text="Reclaim")
        self.tree.heading("kind", text="Match")
        self.tree.column("#0", width=175, minwidth=110, anchor="w")
        self.tree.column("files", width=45, minwidth=45, stretch=False, anchor="center")
        self.tree.column("save", width=75, minwidth=75, stretch=False, anchor="e")
        self.tree.column("kind", width=65, minwidth=65, stretch=False, anchor="center")
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
            right, text="Select a group to review it.", wraplength=820, justify="left",
        )
        self.suggestion.grid(row=0, column=0, sticky="ew", padx=8, pady=(0, 6))

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
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Reset to suggestion", command=self._reset_group).pack(side="left")
        self.group_delete_button = ttk.Button(
            actions, text="Delete unkept in this group", command=self._delete_current_group, state="disabled",
        )
        self.group_delete_button.pack(side="left", padx=8)
        panes.add(right, weight=3)

    def _build_footer(self) -> None:
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)

        self.summary = ttk.Label(footer, text="")
        self.summary.grid(row=0, column=0, sticky="w")

        recycle_label = "Send to Recycle Bin" if deleter.recycle_bin_available() else "Recycle Bin unavailable"
        check = ttk.Checkbutton(footer, text=recycle_label, variable=self.use_recycle_bin)
        if not deleter.recycle_bin_available():
            check.state(["disabled"])
        check.grid(row=0, column=1, padx=8)

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
            self.tree.insert(
                "", "end", iid=str(index - 1),
                text=f"{index}. {group.keeper.name}",
                values=(len(group.members), human_bytes(group.reclaimable_bytes),
                        "exact" if group.exact else "similar"),
            )

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
        card = ttk.LabelFrame(self.cards, text=title, padding=6)
        card.grid(row=0, column=column, padx=6, pady=6, sticky="n")

        thumb = self._thumbnail(member.path)
        holder = ttk.Label(card, image=thumb) if thumb else ttk.Label(card, text="(preview failed)", width=24)
        holder.pack()
        holder.bind("<Double-Button-1>", lambda _e, p=member.path: open_file(p))

        ttk.Checkbutton(
            card, text="Keep this file", variable=self.keep_flags[member.path],
            command=self._refresh_summary,
        ).pack(anchor="w", pady=(6, 2))

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
                detail, text=text, wraplength=CARD_WIDTH - 20, justify="left",
                font=("TkDefaultFont", 8) if i > 1 else ("TkDefaultFont", 9, "bold" if i == 0 else "normal"),
                foreground="gray30" if i >= 3 else "",
            ).pack(anchor="w")

        buttons = ttk.Frame(card)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Open", width=7, command=lambda p=member.path: open_file(p)).pack(side="left")
        ttk.Button(buttons, text="Folder", width=8, command=lambda p=member.path: open_in_explorer(p)).pack(side="left", padx=4)

    def _thumbnail(self, path: str) -> ImageTk.PhotoImage | None:
        if path in self.thumbnails:
            return self.thumbnails[path]
        try:
            with Image.open(path) as img:
                img.draft("RGB", THUMB_SIZE)
                img = img.convert("RGB")
                img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001 - a broken preview must not break review
            return None
        self.thumbnails[path] = photo
        return photo

    def _on_mousewheel(self, event: tk.Event) -> None:
        self.canvas.xview_scroll(-1 * (event.delta // 120), "units")

    def _refresh_summary(self) -> None:
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
            self.tree.insert(
                "", "end", iid=str(index - 1),
                text=f"{index}. {group.keeper.name}",
                values=(len(group.members), human_bytes(group.reclaimable_bytes),
                        "exact" if group.exact else "similar"),
            )
        self.current_group = None
        self._render_cards(None)
        self._refresh_summary()


def main() -> None:
    root = tk.Tk()
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - non-Windows or older Windows
        pass
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = DuplicateFinderApp(root)
    app.mainloop()


if __name__ == "__main__":
    main()
