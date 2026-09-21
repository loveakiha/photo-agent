"""M2 technical quality measurement (pure local, CPU-only, no ML).

Positioning: M2 is a **Measurement** layer, not a Judgment layer. It answers
only "is there a *clear* technical problem (blur / exposure clipping /
noise)". Aesthetic and value questions belong to M3b (VLM) and later
decision stages.

Three raw indicators, computed on a grayscale image downscaled to a fixed
long edge (DEFAULT_LONG_EDGE) so scores are comparable across source sizes:

- sharpness: variance of the Laplacian (higher = sharper; mild blur drops it
  noticeably)
- exposure: clipped-pixel ratio at both ends of the 8-bit histogram (lower =
  better; a dark night scene is *not* flagged — only clipped pixels count)
- noise: median of 5x5 non-overlapping local variances of the Laplacian
  (median ignores strong texture and edges; higher = noisier)

raw values are stored forever (they are facts); the 0-1 dimension scores are
a frozen piecewise-linear clamp against two thresholds per dimension
(frozen after calibration; ``quality.score`` is NOT frozen yet — aggregation
formulas wait for experiment data, so ``quality_score`` stays NULL).
"""
from __future__ import annotations

from pathlib import Path

import cv2
import heif  # registers the HEIF opener (no-op if pillow-heif is missing)
import arw  # registers the ARW opener (no-op if rawpy is missing)
import numpy as np
from PIL import Image, ImageOps

QUALITY_VERSION = "m2-v1"

DEFAULT_LONG_EDGE = 2048

# Frozen calibration thresholds: score = 0 at "low", 1 at "high", linear
# between, clamped outside. Direction: sharpness/exposure = "higher raw is
# better"; noise = "lower raw is better".
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "sharpness": {"low": 15.0, "high": 550.0},
    "exposure": {"low": 0.005, "high": 0.05},
    "noise": {"low": 10.0, "high": 60.0},
}

# Raw indicators per image.
RawResult = dict[str, float]


def _load_pil_bgr(path: Path, long_edge: int) -> np.ndarray | None:
    """Pillow fallback for formats cv2 cannot decode (HEIC/HEIF).

    Uses the registered HEIF opener (via ``import heif``) and applies
    EXIF orientation the same way the M0/M1 modules do (thumbnails.py,
    hashes.py). Returns an HxWx3 BGR uint8 array.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = im.convert("RGB").resize(
                (max(1, round(im.width * long_edge / max(im.width, im.height))),
                 max(1, round(im.height * long_edge / max(im.width, im.height)))),
                Image.Resampling.LANCZOS if long_edge < max(im.width, im.height) else Image.Resampling.BILINEAR,
            )
            return np.array(im)[:, :, ::-1]  # RGB -> BGR
    except (OSError, ValueError):
        return None


def _load_gray(path: str | Path, long_edge: int) -> np.ndarray | None:
    """Read -> EXIF-orient -> downscale to long edge -> 8-bit grayscale."""
    path = Path(path)
    data = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if data is None:
        # cv2 cannot decode this format (e.g. HEIC) -> Pillow fallback.
        data = _load_pil_bgr(path, long_edge)
        if data is None:
            return None
    else:
        h, w = data.shape[:2]
        scale = long_edge / max(h, w)
        if scale < 1.0:
            data = cv2.resize(
                data,
                (max(1, round(w * scale)), max(1, round(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
        elif scale > 1.0:
            data = cv2.resize(
                data,
                (max(1, round(w * scale)), max(1, round(h * scale))),
                interpolation=cv2.INTER_LINEAR,
            )
    return cv2.cvtColor(data, cv2.COLOR_BGR2GRAY)


def compute_quality_raw(path: str | Path, long_edge: int = DEFAULT_LONG_EDGE) -> dict[str, float] | None:
    """Compute the three raw indicators for one image file.

    Returns ``{"sharpness_raw":…, "exposure_raw":…, "noise_raw":…}`` or
    ``None`` when the file cannot be decoded; the caller counts the failure.
    """
    gray = _load_gray(path, long_edge)
    if gray is None:
        return None

    # sharpness: variance of the Laplacian (float64 precision)
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    sharpness_raw = float(lap.var())

    # exposure: fraction of pixels clipped at the dark or bright end
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    clipped = (hist[0] + hist[1] + hist[254] + hist[255]) / gray.size
    exposure_raw = float(clipped)

    # noise: median of 5x5 non-overlapping local variances of the
    # Laplacian. The median makes the estimate dominated by locally flat
    # regions, so strong texture/edges do not inflate the noise score.
    lap_u = cv2.Laplacian(gray, cv2.CV_64F)
    h8, w8 = lap_u.shape[:2]
    gh, gw = (h8 // 5) * 5, (w8 // 5) * 5
    if gh >= 5 and gw >= 5:
        local = lap_u[:gh, :gw].reshape(gh // 5, 5, gw // 5, 5)
        noise_raw = float(np.median(local.var(axis=(1, 3))))
    else:
        noise_raw = float(lap_u.var())

    return {
        "sharpness_raw": sharpness_raw,
        "exposure_raw": exposure_raw,
        "noise_raw": noise_raw,
    }


def clamp_score(raw: float, low: float, high: float, higher_is_better: bool) -> float:
    """Piecewise-linear clamp to [0, 1].

    higher_is_better=True: 0 at ``low``, 1 at ``high`` (high > low).
    higher_is_better=False: 1 at ``low``, 0 at ``high`` (low < high).
    """
    if high == low:
        return 1.0 if (raw >= low) == higher_is_better else 0.0
    span = high - low
    t = (raw - low) / span
    score = t if higher_is_better else 1.0 - t
    return float(min(1.0, max(0.0, score)))


def score_dimensions(raw: dict[str, float], thresholds: dict[str, dict[str, float]]) -> dict[str, float]:
    """Map raw indicators to 0-1 dimension scores using frozen thresholds."""
    better = {"sharpness": True, "exposure": False, "noise": False}
    out: dict[str, float] = {}
    for dim in ("sharpness", "exposure", "noise"):
        t = thresholds[dim]
        out[dim] = clamp_score(raw[f"{dim}_raw"], t["low"], t["high"], better[dim])
    return out


def run_quality(conn, cfg: dict, force: bool = False) -> dict:
    """Compute quality for all photos in the DB (incremental).

    A photo is recomputed when its quality row is missing, its
    algorithm_version differs from QUALITY_VERSION, or its sha256 changed
    since the row was written. ``force=True`` recomputes everything.
    Read-only on disk: opens each file, never writes anything back.
    """
    from database import has_quality, upsert_quality

    q_cfg = cfg.get("quality") or {}
    long_edge = int(q_cfg.get("long_edge", DEFAULT_LONG_EDGE))
    thresholds = q_cfg.get("thresholds") or DEFAULT_THRESHOLDS

    stats: dict[str, int] = {"total": 0, "computed": 0, "cached": 0, "failed": 0}
    photos = conn.execute(
        "SELECT photo_id, abs_path, sha256 FROM photos ORDER BY photo_id"
    ).fetchall()
    for row in photos:
        photo_id, abs_path, sha256 = row["photo_id"], row["abs_path"], row["sha256"]
        stats["total"] += 1
        if not force and has_quality(conn, photo_id, QUALITY_VERSION, sha256):
            stats["cached"] += 1
            continue
        raw = compute_quality_raw(abs_path, long_edge=long_edge)
        if raw is None:
            stats["failed"] += 1
            continue
        scores = score_dimensions(raw, thresholds)
        upsert_quality(
            conn,
            photo_id,
            raw,
            scores,
            quality_score=None,  # frozen once aggregation is decided
            algorithm_version=QUALITY_VERSION,
            sha256=sha256,
        )
        stats["computed"] += 1
    conn.commit()
    return stats
