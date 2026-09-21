"""M2 quality: fixture ordering, clamp behavior, incremental DB run."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from database import connect, upsert_photo
from fixtures_gen import generate_fixtures
from quality import (
    DEFAULT_THRESHOLDS,
    QUALITY_VERSION,
    clamp_score,
    compute_quality_raw,
    run_quality,
    score_dimensions,
)

BASES = ["shapes", "texture", "dark"]


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    return generate_fixtures(tmp_path_factory.mktemp("m2_fixtures"))


def _raw(path: Path) -> dict:
    result = compute_quality_raw(path)
    assert result is not None
    return result


class TestRawIndicators:
    def test_returns_three_finite_values(self, fixtures):
        for name, path in fixtures.items():
            raw = _raw(path)
            for key in ("sharpness_raw", "exposure_raw", "noise_raw"):
                assert raw[key] >= 0.0
                assert raw[key] == raw[key]  # not NaN

    def test_sharpness_ordering(self, fixtures):
        """base > blur_light > blur_strong, per base image."""
        for base in BASES:
            r = {v: _raw(fixtures[f"{base}_{v}"])["sharpness_raw"] for v in (
                "base", "blur_light", "blur_strong"
            )}
            assert r["base"] > r["blur_light"] > r["blur_strong"]

    def test_exposure_ordering(self, fixtures):
        """base clips less than both exposure variants, per base image."""
        for base in BASES:
            r = {v: _raw(fixtures[f"{base}_{v}"])["exposure_raw"] for v in (
                "base", "overexposed", "underexposed"
            )}
            assert r["base"] < r["overexposed"]
            assert r["base"] < r["underexposed"]

    def test_noise_ordering(self, fixtures):
        """base < noise_light < noise_strong, per base image."""
        for base in BASES:
            r = {v: _raw(fixtures[f"{base}_{v}"])["noise_raw"] for v in (
                "base", "noise_light", "noise_strong"
            )}
            assert r["base"] < r["noise_light"] < r["noise_strong"]

    def test_dark_scene_not_flagged_as_exposure_problem(self, fixtures):
        """A legitimately dark scene has almost no clipped pixels — the
        exposure indicator must not flag it (aesthetic calls are M3b's job)."""
        assert _raw(fixtures["dark_base"])["exposure_raw"] < 0.01


class TestClampAndScore:
    def test_higher_is_better_direction(self):
        low, high = 100.0, 1000.0
        assert clamp_score(50.0, low, high, True) == 0.0
        assert clamp_score(550.0, low, high, True) == pytest.approx(0.5)
        assert clamp_score(2000.0, low, high, True) == 1.0

    def test_lower_is_better_direction(self):
        low, high = 1.0, 25.0
        assert clamp_score(13.0, low, high, False) == pytest.approx(0.5)
        assert clamp_score(0.5, low, high, False) == 1.0
        assert clamp_score(40.0, low, high, False) == 0.0

    def test_scores_in_unit_interval(self, fixtures):
        for path in fixtures.values():
            dims = score_dimensions(_raw(path), DEFAULT_THRESHOLDS)
            for dim, value in dims.items():
                assert 0.0 <= value <= 1.0


class TestRunQuality:
    def _make_photo(self, conn, tmp_dir: Path, name: str, path: Path) -> int:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        photo_id, action = upsert_photo(
            conn,
            rel_path=name,
            abs_path=str(path),
            sha256=sha,
        )
        assert action == "inserted"
        return photo_id

    def test_incremental_and_invalidation(self, fixtures, tmp_path):
        db_path = tmp_path / "photo.db"
        conn = connect(db_path)
        cfg = {"quality": {"long_edge": 2048, "thresholds": DEFAULT_THRESHOLDS}}
        photo_id = self._make_photo(
            conn, tmp_path, "shapes_base.jpg", fixtures["shapes_base"]
        )
        try:
            first = run_quality(conn, cfg)
            assert first["computed"] == 1 and first["cached"] == 0

            second = run_quality(conn, cfg)
            assert second["computed"] == 0 and second["cached"] == 1

            # content changed -> new sha256 -> recompute required
            src = fixtures["shapes_blur_strong"]
            conn.execute(
                "UPDATE photos SET sha256 = ?, last_scanned = last_scanned "
                "WHERE photo_id = ?",
                (hashlib.sha256(src.read_bytes()).hexdigest(), photo_id),
            )
            # also point abs_path at the changed file, as a rescan would
            conn.execute(
                "UPDATE photos SET abs_path = ? WHERE photo_id = ?",
                (str(src), photo_id),
            )
            conn.commit()
            third = run_quality(conn, cfg)
            assert third["computed"] == 1 and third["cached"] == 0

            row = conn.execute(
                "SELECT quality_score, algorithm_version FROM quality WHERE photo_id=?",
                (photo_id,),
            ).fetchone()
            assert row["quality_score"] is None  # aggregation not frozen yet
            assert row["algorithm_version"] == QUALITY_VERSION
        finally:
            conn.close()
