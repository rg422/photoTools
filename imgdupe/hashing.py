"""Perceptual hashing and image feature extraction.

Two independent 64-bit structural hashes are computed per image:

* ``phash``  - DCT of a 32x32 luma image, low-frequency coefficients thresholded
               at their median. Robust to rescaling, re-compression and mild
               colour/brightness shifts, which is the common case for
               "same picture, different export".
* ``dhash``  - horizontal gradient signature. Catches images whose overall
               frequency content shifts (heavy sharpening, scanner contrast)
               but whose edge layout is unchanged.

Both are computed after trimming uniform borders, so a scan with white margins
still matches the cropped version of the same picture.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageFile as PILImageFile, ImageOps

PILImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 500_000_000

_DCT_SIZE = 32
_DCT_LOW = 8
_ANALYSIS_EDGE = 512


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    m = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    m[0] /= np.sqrt(2)
    return m * np.sqrt(2 / n)


_DCT = _dct_matrix(_DCT_SIZE)

_LAPLACIAN = np.array([[0.0, 1.0, 0.0],
                       [1.0, -4.0, 1.0],
                       [0.0, 1.0, 0.0]])


@dataclass(slots=True)
class ImageSignature:
    """Everything derived from an image's pixels in a single decode pass."""

    phash: int
    dhash: int
    width: int
    height: int
    mode: str
    fmt: str
    sharpness: float
    colorfulness: float
    content_md5: str


def bits_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def file_md5(path: str, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _trim_border(img: Image.Image, tolerance: int = 12) -> Image.Image:
    """Remove a uniform frame (scanner margin, letterboxing) if present."""
    if img.width < 32 or img.height < 32:
        return img
    gray = img.convert("L")
    corners = [
        gray.getpixel((0, 0)),
        gray.getpixel((gray.width - 1, 0)),
        gray.getpixel((0, gray.height - 1)),
        gray.getpixel((gray.width - 1, gray.height - 1)),
    ]
    if max(corners) - min(corners) > tolerance:
        return img
    background = Image.new("L", gray.size, int(sum(corners) / 4))
    diff = ImageChops.difference(gray, background)
    box = diff.point(lambda p: 255 if p > tolerance else 0).getbbox()
    if not box:
        return img
    w, h = box[2] - box[0], box[3] - box[1]
    # Ignore implausible crops: a real border trim keeps most of the picture.
    if w < img.width * 0.4 or h < img.height * 0.4 or (w == img.width and h == img.height):
        return img
    return img.crop(box)


def _phash(gray32: np.ndarray) -> int:
    coeffs = _DCT @ gray32 @ _DCT.T
    low = coeffs[:_DCT_LOW, :_DCT_LOW].flatten()
    median = np.median(low[1:])
    bits = low > median
    return int("".join("1" if b else "0" for b in bits), 2)


def _dhash(gray: Image.Image) -> int:
    small = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def _sharpness(gray: Image.Image) -> float:
    """Variance of the Laplacian at a fixed analysis scale.

    Fixing the scale keeps this a measure of real captured detail rather than a
    second vote for resolution, so an upscaled blurry copy scores low even when
    its pixel count is high.
    """
    if max(gray.size) > _ANALYSIS_EDGE:
        gray = ImageOps.contain(gray, (_ANALYSIS_EDGE, _ANALYSIS_EDGE), Image.Resampling.LANCZOS)
    arr = np.asarray(gray, dtype=np.float32) / 255.0
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    windows = np.lib.stride_tricks.sliding_window_view(arr, (3, 3))
    lap = np.einsum("ijkl,kl->ij", windows, _LAPLACIAN)
    return float(lap.var() * 10_000)


def _colorfulness(img: Image.Image) -> float:
    """Hasler-Susstrunk colourfulness, 0 for greyscale."""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    small = ImageOps.contain(img, (128, 128), Image.Resampling.BILINEAR).convert("RGB")
    arr = np.asarray(small, dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    rg = r - g
    yb = 0.5 * (r + g) - b
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))


def signature(path: str) -> ImageSignature:
    """Decode an image once and derive every pixel-based metric from it."""
    with Image.open(path) as opened:
        fmt = opened.format or ""
        img = ImageOps.exif_transpose(opened)
        img.load()
        width, height = img.size
        mode = img.mode
        colorfulness = _colorfulness(img)
        content = _trim_border(img)
        gray = content.convert("L")

    sharp = _sharpness(gray)
    gray32 = np.asarray(gray.resize((_DCT_SIZE, _DCT_SIZE), Image.Resampling.LANCZOS), dtype=np.float64)
    normalized = gray.resize((64, 64), Image.Resampling.LANCZOS).tobytes()

    return ImageSignature(
        phash=_phash(gray32),
        dhash=_dhash(gray),
        width=width,
        height=height,
        mode=mode,
        fmt=fmt,
        sharpness=sharp,
        colorfulness=colorfulness,
        content_md5=hashlib.md5(normalized).hexdigest(),
    )
