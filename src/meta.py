"""Capture-date and dimension reader. Pillow-backed, honest about provenance.

Replaces a hand-rolled stdlib TIFF/IFD parser. That parser worked on a synthetic fixture
but had never read a real camera file, and because the fixture and the parser shared an
author, a shared misconception about the format would have passed a green suite. Pillow has
read every camera file in the world; this module's job is now the part Pillow does *not* do.

WHAT THIS MODULE OWNS (the sound part of the original design, kept)
  * Provenance. Every date carries a label — 'exif' | 'filename' | 'mtime' — so the census
    can report how much of a library has real capture metadata versus how much is guessed.
    A pipeline that silently substitutes mtime for capture time will happily flatten fifteen
    years into whatever day the files were last copied.
  * Precedence. exif > filename > mtime, deliberately in that order (see `best_date`).
  * Never raising. One corrupt file out of 100,000 must not stop an overnight run.
  * Rejecting the all-zero placeholder date, which real cameras really do write.

`register_heif()` is called once at import so `.heic`/`.heif` open like any other format.
Note that iCloud for Windows stores JPEG-converted copies rather than the original HEICs, so
a library synced that way will contain no HEIC at all — the support is for the phone-transfer
and Android cases.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

try:  # optional: absence degrades to "HEIC unreadable", never to a crash
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover - exercised only on a broken install
    HEIF_OK = False

# Pillow decompression-bomb guard: a 100k-file walk should not be a DoS surface. Raised
# well above any real photo (this is ~1.5 gigapixel) so legitimate panoramas still open.
Image.MAX_IMAGE_PIXELS = 1_500_000_000

_TAG = {name: num for num, name in ExifTags.TAGS.items()}
_DT_ORIGINAL = _TAG["DateTimeOriginal"]        # 0x9003, in the Exif sub-IFD
_DT_DIGITIZED = _TAG["DateTimeDigitized"]      # 0x9004
_DT_BASIC = _TAG["DateTime"]                   # 0x0132, in IFD0 — file mod time, weaker
_MAKE, _MODEL, _ORIENT = _TAG["Make"], _TAG["Model"], _TAG["Orientation"]

EMPTY: dict = {"captured_at": None, "capture_source": None, "make": None,
               "model": None, "orientation": None, "width": None, "height": None}


def read_meta(fh, ext: str = "") -> dict:
    """Best-effort metadata from an open binary file handle.

    Returns a dict with EMPTY's keys, any of which may be None. `ext` is accepted and
    ignored — Pillow sniffs the container, so the extension is not load-bearing here, which
    also means a mislabelled `.jpg` that is really a PNG still reads correctly.
    """
    out = dict(EMPTY)
    try:
        fh.seek(0)
        with Image.open(fh) as img:
            out["width"], out["height"] = img.size
            exif = img.getexif()
            if not exif:
                return out

            out["make"] = _clean(exif.get(_MAKE))
            out["model"] = _clean(exif.get(_MODEL))
            orient = exif.get(_ORIENT)
            out["orientation"] = int(orient) if isinstance(orient, int) else None

            # DateTimeOriginal lives in the Exif sub-IFD, not IFD0. getexif() returns only
            # IFD0, so the sub-IFD has to be requested explicitly — the single easiest
            # thing to get wrong here, and the reason a naive read returns no date.
            sub = {}
            try:
                sub = exif.get_ifd(ExifTags.IFD.Exif) or {}
            except Exception:
                sub = {}

            stamp = (_norm(sub.get(_DT_ORIGINAL))
                     or _norm(sub.get(_DT_DIGITIZED))
                     or _norm(exif.get(_DT_BASIC)))
            if stamp:
                out["captured_at"] = stamp
                out["capture_source"] = "exif"
    except Exception:
        pass  # deliberate: malformed, truncated, or unsupported degrades to EMPTY
    return out


def read_meta_path(path: Path) -> dict:
    """Convenience wrapper. Opens read-only via the contract's sanctioned reader."""
    import contract
    try:
        with contract.open_source(path) as fh:
            return read_meta(fh, path.suffix.lower())
    except OSError:
        return dict(EMPTY)


def _clean(val) -> str | None:
    if not isinstance(val, str):
        return None
    return val.strip().strip("\x00").strip() or None


def _norm(text) -> str | None:
    """'2019:07:04 11:23:07' -> ISO 8601. Rejects the all-zero placeholder."""
    if not isinstance(text, str):
        return None
    text = text.strip().strip("\x00").replace("/", ":")
    if not text or text.startswith("0000"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[:len(datetime(2000, 1, 1).strftime(fmt))], fmt)
        except ValueError:
            continue
        if 1990 <= dt.year <= datetime.now().year + 1:
            return dt.isoformat(timespec="seconds")
    return None


_FNAME_DATE = re.compile(
    r"(?<!\d)(199\d|20[0-5]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def date_from_filename(name: str) -> str | None:
    """Pull YYYYMMDD / YYYY-MM-DD out of IMG_20190704_112307.jpg and friends."""
    m = _FNAME_DATE.search(name)
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if 1990 <= dt.year <= datetime.now().year + 1:
        return dt.isoformat(timespec="seconds")
    return None


def best_date(meta: dict, path: Path, mtime: float) -> tuple[str, str]:
    """Resolve the capture date with an explicit provenance label.

    Precedence is exif > filename > mtime, because mtime is the least trustworthy field in
    any library that has ever been copied between drives — a bulk copy rewrites every mtime
    to the copy date and silently flattens fifteen years into one.
    """
    if meta.get("captured_at"):
        return meta["captured_at"], "exif"
    from_name = date_from_filename(path.name)
    if from_name:
        return from_name, "filename"
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds"), "mtime"
