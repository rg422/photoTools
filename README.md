# Image Duplicate Finder

Finds near-duplicate images across a folder tree — rescans of the same picture,
re-exports at different sizes, and lossy copies of a lossless master — then ranks
each group by quality, suggests which file to keep, and removes the rest only
after you approve.

![Reviewing a duplicate group](docs/screenshot.png)

## How it works

**Matching.** Every image is reduced to two independent 64-bit perceptual hashes:

- a **pHash** built from the DCT of a 32×32 luma image, thresholded at the median
  of its low-frequency coefficients — stable across rescaling, re-compression and
  moderate brightness or colour shifts;
- a **dHash** horizontal gradient signature, which survives the heavy contrast and
  sharpening that scanners apply.

Uniform borders are trimmed before hashing, so a scan with white margins still
matches the cropped version of the same picture. Comparing every image against
every other is quadratic, so each hash is split into 16 four-bit bands and
indexed; by the pigeonhole principle two hashes within 15 bits of each other must
share a band, and only images sharing a band are ever compared in full. Matching
pairs are merged into groups with union-find.

**Ranking.** Group members are scored 0–100 on four components:

| Component | Weight | Measure |
|---|---|---|
| Resolution | 42% | megapixels, log-scaled, saturating at 24 MP |
| Sharpness | 30% | variance of the Laplacian at a fixed 512 px analysis scale |
| Encoding | 20% | lossless format, or bits per pixel for lossy ones |
| Colour | 8% | Hasler–Süsstrunk colourfulness |

Sharpness is measured at a fixed scale deliberately, so it reports real captured
detail rather than voting for resolution a second time — an upscaled blurry copy
scores low even though its pixel count is high.

**Removal.** Nothing is ever hard-deleted. Files go to the Recycle Bin, or — if
`Send2Trash` is unavailable or you untick the option — into a timestamped
`.imgdupe_quarantine` folder under the scan root that mirrors the original
directory layout. A group where you have not ticked anything to keep is always
skipped, so a whole group can never be wiped out by accident.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Running

```bash
.venv\Scripts\python.exe run.py
```

Pick a top-level folder, choose a sensitivity, and press **Scan**. Every
subfolder is searched.

- **Strict** (4 bits) — near-identical files only.
- **Balanced** (8 bits) — the default; catches resizes, re-compression and scans.
- **Loose** (14 bits) — heavier edits, at the cost of more false positives.

Select a group to review it. Members are shown best-quality-first with the
suggested keeper ticked and the reason for the suggestion spelled out. Tick every
file you want to keep; unticked files are the ones that get removed. Double-click
a thumbnail, or use **Open** / **Folder**, to inspect a file before deciding.

Then either **Delete unkept in this group** or **Delete all unkept files**, both
of which show a confirmation listing what is about to move and where.

Signatures are cached in `%LOCALAPPDATA%\imgdupe\signatures.db`, keyed by path,
size and modification time, so re-scanning a library is near instant.

## Tests

```bash
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite generates a synthetic library with known duplicates — a lossless
master, a downscaled lossy copy, a bordered "scan", a byte-identical pair and an
unrelated photo — and asserts that grouping, keeper selection and the cache all
behave.

## Layout

| File | Purpose |
|---|---|
| `imgdupe/scanner.py` | recursive image discovery |
| `imgdupe/hashing.py` | perceptual hashes, border trim, sharpness, colourfulness |
| `imgdupe/quality.py` | quality scoring and keeper rationale |
| `imgdupe/grouping.py` | banded candidate lookup and union-find clustering |
| `imgdupe/cache.py` | SQLite signature cache |
| `imgdupe/engine.py` | scan orchestration, threading, progress |
| `imgdupe/deleter.py` | recycle-bin and quarantine removal |
| `imgdupe/gui.py` | Tkinter interface |
