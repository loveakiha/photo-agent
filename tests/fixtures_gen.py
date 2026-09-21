"""Deterministic synthetic fixtures for M2 quality tests.

3 base images (different scenes) x 6 variants = 18 images, generated ON
DEMAND in a temp dir — never stored in the repo, never touching the
test_pics originals (V1 rule: read-only on originals).

Variants each attack ONE dimension so ordering assertions stay clean:

- blur_light / blur_strong      -> sharpness must drop
- overexposed / underexposed    -> exposure clip must rise
- noise_light / noise_strong    -> noise must rise

All transforms use fixed seeds/parameters, so the generated pixels and the
ordering relationships are stable across runs.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

VARIANTS = [
    "blur_light",
    "blur_strong",
    "overexposed",
    "underexposed",
    "noise_light",
    "noise_strong",
]

_W, _H = 1280, 800


def _base_shapes() -> np.ndarray:
    """Mid-brightness synthetic 'photo': gradient + rectangles + circle.

    Contains edges and texture (for noise estimation) but no clipping.
    """
    y, x = np.mgrid[0:_H, 0:_W].astype(np.float64)
    img = 10 + 90 * (x / _W) + 70 * (y / _H)  # gradient 10..170
    img = np.repeat(img[:, :, None], 3, axis=2)
    cv2.rectangle(img, (120, 100), (520, 420), (190, 160, 90), -1)
    cv2.rectangle(img, (700, 480), (1150, 720), (80, 120, 200), -1)
    cv2.circle(img, (950, 200), 90, (160, 90, 60), -1)
    cv2.circle(img, (300, 600), 60, (110, 110, 110), -1)
    return np.clip(img, 0, 255).astype(np.uint8)


def _base_texture() -> np.ndarray:
    """Seeded organic-ish texture: low-res noise upscaled (fixed seed)."""
    rng = np.random.default_rng(42)
    small = rng.integers(30, 225, size=(160, 100, 3), dtype=np.uint8)
    img = cv2.resize(small, (_W, _H), interpolation=cv2.INTER_CUBIC)
    # mild blur so it reads as texture, not grain
    img = cv2.GaussianBlur(img, (3, 3), 1.0)
    return img


def _base_dark() -> np.ndarray:
    """Dark night scene: mean brightness well below mid-gray, no clipping.

    Sanity anchor: the exposure indicator must NOT flag a legitimately
    dark scene (that is an aesthetic call, not a Measurement call).
    """
    y, x = np.mgrid[0:_H, 0:_W].astype(np.float64)
    img = 35 + 15 * np.sin(x / 90.0) + 10 * np.cos(y / 70.0)  # 10..60
    img = np.repeat(img[:, :, None], 3, axis=2)
    cv2.rectangle(img, (200, 150), (420, 300), (210, 200, 170), -1)  # window
    cv2.rectangle(img, (800, 500), (980, 620), (180, 170, 140), -1)
    return np.clip(img, 0, 255).astype(np.uint8)


def _variant(name: str, img: np.ndarray) -> np.ndarray:
    if name == "blur_light":
        return cv2.GaussianBlur(img, (5, 5), 1.5)
    if name == "blur_strong":
        return cv2.GaussianBlur(img, (15, 15), 5.0)
    if name == "overexposed":
        return np.clip(img.astype(np.float64) * 1.6, 0, 255).astype(np.uint8)
    if name == "underexposed":
        return np.clip(img.astype(np.float64) * 0.05, 0, 255).astype(np.uint8)
    if name in ("noise_light", "noise_strong"):
        sigma = 8.0 if name == "noise_light" else 30.0
        rng = np.random.default_rng(7)
        noise = rng.normal(0.0, sigma, img.shape)
        return np.clip(img.astype(np.float64) + noise, 0, 255).astype(np.uint8)
    raise ValueError(name)


def generate_fixtures(out_dir: Path) -> dict[str, Path]:
    """Generate all 18 fixtures into ``out_dir``; returns {name: path}.

    Names: ``{base}_{variant}`` for variants, ``{base}_base`` for the clean
    base image (bases: shapes / texture / dark).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bases = {
        "shapes": _base_shapes(),
        "texture": _base_texture(),
        "dark": _base_dark(),
    }
    out: dict[str, Path] = {}
    for base_name, img in bases.items():
        path = out_dir / f"{base_name}_base.jpg"
        cv2.imwrite(str(path), img)
        out[f"{base_name}_base"] = path
        for variant in VARIANTS:
            vpath = out_dir / f"{base_name}_{variant}.jpg"
            cv2.imwrite(str(vpath), _variant(variant, img))
            out[f"{base_name}_{variant}"] = vpath
    return out
