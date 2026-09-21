"""Schema v4: preference_samples / creative_candidates reservation.

These tables are structure-only at the M3.0 stage (IDEA.md §22): no
pipeline stage populates them yet. These tests pin the schema + the
database helpers so M3.1+ (Taste Profile, Intent) can build on them
without another migration, and so purging a photo cleans up preference
rows that reference it.
"""
import pytest

import database


@pytest.fixture
def v4_db(tmp_path):
    conn = database.connect(tmp_path / "photo.db")
    yield conn
    conn.close()


def test_migrates_to_v4(v4_db):
    assert database.SCHEMA_VERSION == 4
    version = database._current_version(v4_db)
    assert version == 4


def test_tables_exist(v4_db):
    tables = {
        row["name"]
        for row in v4_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "preference_samples" in tables
    assert "creative_candidates" in tables


def test_preference_sample_upsert_and_redecide(v4_db):
    # Real photo rows (FKs are enforced on these connections).
    a, _ = database.upsert_photo(v4_db, rel_path="a.jpg", abs_path="/a/a.jpg", sha256="ha")
    b, _ = database.upsert_photo(v4_db, rel_path="b.jpg", abs_path="/a/b.jpg", sha256="hb")
    # First decision: A beats B.
    sid = database.upsert_preference_sample(
        v4_db, candidate_a=a, candidate_b=b, winner=a, source="user"
    )
    v4_db.commit()

    # Same (a, b, user) re-decided: same sample_id, updated winner.
    sid2 = database.upsert_preference_sample(
        v4_db, candidate_a=a, candidate_b=b, winner=b, source="vlm",
        confidence=0.9, reason="teacher agrees",
    )
    v4_db.commit()
    assert sid == sid2
    rows = database.list_preference_samples(v4_db)
    assert len(rows) == 1
    assert rows[0]["winner"] == b
    assert rows[0]["source"] == "vlm"
    assert rows[0]["confidence"] == pytest.approx(0.9)


def test_preference_sample_invalid_source(v4_db):
    with pytest.raises(ValueError):
        database.upsert_preference_sample(
            v4_db, candidate_a=1, candidate_b=2, winner=1, source="bad"
        )


def test_preference_sample_filters(v4_db):
    # Six real photos to reference (FKs enforced).
    ids = []
    for i in range(1, 7):
        pid, _ = database.upsert_photo(
            v4_db, rel_path=f"{i}.jpg", abs_path=f"/a/{i}.jpg", sha256=f"h{i}"
        )
        ids.append(pid)
    a1, a2, a3, a4, a5, a6 = ids
    for a, b, w, src in [
        (a1, a2, a1, "user"),
        (a3, a4, None, "vlm"),
        (a5, a6, a5, "model"),
    ]:
        database.upsert_preference_sample(
            v4_db, candidate_a=a, candidate_b=b, winner=w, source=src
        )
    v4_db.commit()
    assert len(database.list_preference_samples(v4_db)) == 3
    assert len(database.list_preference_samples(v4_db, source="vlm")) == 1
    assert len(database.list_preference_samples(v4_db, undecided_only=True)) == 1
    assert len(database.list_preference_samples(v4_db, winner=a5)) == 1


def test_creative_candidate_add_and_filter(v4_db):
    cid1 = database.add_creative_candidate(
        v4_db, prompt="p1", model="flux", aesthetic_score=0.7,
    )
    cid2 = database.add_creative_candidate(
        v4_db, prompt="p2", model="sd", human_selection=1,
    )
    v4_db.commit()
    assert cid1 != cid2
    assert len(database.list_creative_candidates(v4_db)) == 2
    assert len(database.list_creative_candidates(v4_db, model="flux")) == 1
    assert len(database.list_creative_candidates(v4_db, human_selected_only=True)) == 1


def test_purge_removes_preference_rows(v4_db, tmp_path):
    # One real file on disk (survives prune), one phantom path (purged).
    real = tmp_path / "b.jpg"
    real.write_bytes(b"x")
    photo_id, _ = database.upsert_photo(
        v4_db, rel_path="a.jpg", abs_path="/x/a.jpg", sha256="h1"
    )
    other_id, _ = database.upsert_photo(
        v4_db, rel_path="b.jpg", abs_path=str(real), sha256="h2"
    )
    database.upsert_preference_sample(
        v4_db, candidate_a=photo_id, candidate_b=other_id, winner=photo_id
    )
    v4_db.commit()
    assert len(database.list_preference_samples(v4_db)) == 1

    # /x/a.jpg doesn't exist on disk -> prune purges photo + dependent rows.
    pruned = database.prune_missing_files(v4_db)
    assert pruned == 1
    assert database.list_preference_samples(v4_db) == []
