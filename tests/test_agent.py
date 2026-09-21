"""M3.0 tests: Tool API (facts layer) + agent loop guardrails.

No network: the LLM backend is mocked by monkeypatching
``agent._post_json``; the tools are exercised against a real in-test
SQLite database built with the same schema the production CLI uses.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import agent
from agent import run_task
from database import connect, upsert_photo
from tools import call_tool, tool_definitions


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def make_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "photo.db")
    return conn


def insert_photo(conn, name: str, taken_at: str | None = None, fmt: str = "jpeg",
                  rel: str | None = None, phash: str | None = None,
                  dhash: str | None = None) -> int:
    rel = rel or name
    sha = hashlib.sha256(name.encode()).hexdigest()
    photo_id, _ = upsert_photo(conn, rel_path=rel, abs_path=str(Path("tmp") / rel), sha256=sha)  # type: ignore[assignment]
    conn.execute(
        "UPDATE photos SET taken_at=?, format=?, width=100, height=100 WHERE photo_id=?",
        (taken_at, fmt, photo_id),
    )
    if phash is not None and dhash is not None:
        conn.execute(
            "INSERT INTO photo_hashes (photo_id, phash, dhash) VALUES (?,?,?)",
            (photo_id, phash, dhash),
        )
    conn.commit()
    return photo_id


def make_group(conn, kind: str, rep_id: int, member_ids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO groups (kind, size, rep_photo_id) VALUES (?,?,?)",
        (kind, len(member_ids), rep_id),
    )
    group_id = cur.lastrowid
    for mid in member_ids:
        conn.execute(
            "INSERT INTO group_members (group_id, photo_id, sim_to_rep) VALUES (?,?,?)",
            (group_id, mid, 0.95),
        )
    conn.commit()
    return group_id


def make_quality(conn, photo_id: int, sharp=0.9, expo=1.0, noise=0.8) -> None:
    conn.execute(
        """INSERT INTO quality (photo_id, sharpness, exposure, noise,
           sharpness_raw, exposure_raw, noise_raw, algorithm_version)
           VALUES (?,?,?,?,?,?,?,?)""",
        (photo_id, sharp, expo, noise, sharp * 500, expo * 0.01, noise * 10, "m2-v1"),
    )
    conn.commit()


@pytest.fixture
def db(tmp_path: Path):
    conn = make_db(tmp_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# search_photos
# ---------------------------------------------------------------------------


class TestSearchPhotos:
    def test_year_and_format_filters(self, db):
        insert_photo(db, "a.jpg", taken_at="2025-06-01T10:00:00", fmt="jpeg")
        insert_photo(db, "b.heic", taken_at="2024-06-01T10:00:00", fmt="heif")
        out = call_tool(db, "search_photos", {"year": 2025})
        assert out["count"] == 1
        assert out["items"][0]["rel_path"] == "a.jpg"
        out = call_tool(db, "search_photos", {"format": "HEIF"})
        assert out["count"] == 1
        assert out["items"][0]["rel_path"] == "b.heic"

    def test_date_range_inclusive(self, db):
        insert_photo(db, "a.jpg", taken_at="2025-06-01T00:00:00")
        insert_photo(db, "b.jpg", taken_at="2025-06-02T00:00:00")
        insert_photo(db, "c.jpg", taken_at="2025-06-03T00:00:00")
        out = call_tool(db, "search_photos", {"date_from": "2025-06-01", "date_to": "2025-06-02"})
        assert sorted(i["rel_path"] for i in out["items"]) == ["a.jpg", "b.jpg"]

    def test_quality_floor_requires_quality_rows(self, db):
        id_ok = insert_photo(db, "good.jpg", taken_at="2025-01-01T00:00:00")
        id_bad = insert_photo(db, "bad.jpg", taken_at="2025-01-02T00:00:00")
        make_quality(db, id_ok, sharp=0.9)
        make_quality(db, id_bad, sharp=0.2)
        out = call_tool(db, "search_photos", {"min_sharpness": 0.5})
        assert out["count"] == 1
        assert out["items"][0]["rel_path"] == "good.jpg"

    def test_search_includes_quality_inline(self, db):
        pid = insert_photo(db, "q.jpg", taken_at="2025-01-01T00:00:00")
        make_quality(db, pid, sharp=0.42)
        out = call_tool(db, "search_photos", {"include_quality": True})
        assert out["count"] == 1
        assert out["items"][0]["quality"]["sharpness"] == pytest.approx(0.42)

    def test_path_contains_and_max_results(self, db):
        for i in range(5):
            insert_photo(db, f"beach/photo_{i}.jpg")
        insert_photo(db, "mountain/photo_0.jpg")
        out = call_tool(db, "search_photos", {"path_contains": "beach", "max_results": 3})
        assert out["count"] == 3
        assert out["total_matched"] == 5
        assert out["truncated"] is True

    def test_in_group_filter(self, db):
        a = insert_photo(db, "a.jpg")
        b = insert_photo(db, "b.jpg")
        c = insert_photo(db, "c.jpg")
        make_group(db, "exact", a, [a, b])
        out = call_tool(db, "search_photos", {"in_group": "exact"})
        assert sorted(i["rel_path"] for i in out["items"]) == ["a.jpg", "b.jpg"]
        assert "c.jpg" not in [i["rel_path"] for i in out["items"]]
        bad = call_tool(db, "search_photos", {"in_group": "fuzzy"})
        assert "error" in bad

    def test_max_results_hard_cap(self, db):
        for i in range(250):
            insert_photo(db, f"p{i:03d}.jpg")
        out = call_tool(db, "search_photos", {"max_results": 10**9})
        assert out["count"] <= 200


# ---------------------------------------------------------------------------
# single-photo tools
# ---------------------------------------------------------------------------


class TestSinglePhoto:
    def test_get_photo_full(self, db):
        pid = insert_photo(db, "x.jpg", taken_at="2025-05-05T08:00:00")
        make_quality(db, pid, sharp=0.7)
        out = call_tool(db, "get_photo", {"rel_path": "x.jpg"})
        assert out["metadata"]["taken_at"] == "2025-05-05T08:00:00"
        assert out["quality"]["sharpness"] == pytest.approx(0.7)
        assert out["groups"] == []

    def test_get_photo_not_found(self, db):
        out = call_tool(db, "get_photo", {"rel_path": "nope.jpg"})
        assert "error" in out

    def test_get_metadata_and_quality(self, db):
        pid = insert_photo(db, "y.jpg", taken_at="2024-01-01T00:00:00")
        assert call_tool(db, "get_metadata", {"rel_path": "y.jpg"})["metadata"]["taken_at"].startswith("2024")
        # no quality rows yet -> explicit error, not a crash
        bad = call_tool(db, "get_quality", {"rel_path": "y.jpg"})
        assert "error" in bad
        make_quality(db, pid)
        out = call_tool(db, "get_quality", {"rel_path": "y.jpg"})
        assert out["quality"]["exposure"] == pytest.approx(1.0)

    def test_unknown_tool_is_data_not_crash(self, db):
        out = call_tool(db, "delete_all_photos", {})
        assert "error" in out


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------


class TestGroups:
    def test_duplicate_groups_with_quality_and_delta(self, db):
        a = insert_photo(db, "a.jpg", phash="0" * 64, dhash="0" * 64)
        b = insert_photo(db, "b.jpg", phash="0" * 63 + "1", dhash="0" * 64)
        make_group(db, "near", a, [a, b])
        make_quality(db, a, sharp=0.9)
        make_quality(db, b, sharp=0.3)
        out = call_tool(db, "get_duplicate_groups", {"kind": "near"})
        assert out["count"] == 1
        g = out["groups"][0]
        assert g["kind"] == "near" and g["size"] == 2
        assert g["representative"] == "a.jpg"
        by_path = {m["rel_path"]: m for m in g["members"]}
        assert by_path["a.jpg"]["is_representative"] is True
        assert by_path["b.jpg"]["hash_delta_to_rep"]["phash"] == 1
        assert by_path["b.jpg"]["quality"]["sharpness"] == pytest.approx(0.3)

    def test_duplicate_groups_min_size_and_kind(self, db):
        a = insert_photo(db, "a.jpg")
        b = insert_photo(db, "b.jpg")
        make_group(db, "exact", a, [a, b])
        assert call_tool(db, "get_duplicate_groups", {"kind": "exact"})["count"] == 1
        assert call_tool(db, "get_duplicate_groups", {"kind": "near"})["count"] == 0
        assert call_tool(db, "get_duplicate_groups", {"min_size": 3})["count"] == 0

    def test_similar_photos_excludes_self(self, db):
        a = insert_photo(db, "a.jpg", phash="0" * 64, dhash="0" * 64)
        b = insert_photo(db, "b.jpg", phash="0" * 64, dhash="1" + "0" * 63)
        make_group(db, "near", a, [a, b])
        out = call_tool(db, "get_similar_photos", {"rel_path": "a.jpg"})
        assert out["in_near_group"] is True
        assert [s["rel_path"] for s in out["similar"]] == ["b.jpg"]
        assert call_tool(db, "get_similar_photos", {"rel_path": "c.jpg"})["error"]


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------


class TestMiscTools:
    def test_generate_report_writes_file(self, db, tmp_path):
        out = call_tool(db, "generate_report", {}, extra={"reports_dir": str(tmp_path / "reports")})
        assert "error" not in out
        assert Path(out["report_path"]).exists()


# ---------------------------------------------------------------------------
# agent loop guardrails (mocked backend)
# ---------------------------------------------------------------------------

BACKEND = {"base_url": "http://fake:8080", "model": "fake-model"}


def assistant_reply(content: str | None, tool_calls: list[tuple[str, dict]] | None = None) -> dict:
    msg: dict = {"role": "assistant"}
    if tool_calls:
        msg["content"] = None
        msg["tool_calls"] = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for i, (name, args) in enumerate(tool_calls)
        ]
    else:
        msg["content"] = content
    return {"choices": [{"message": msg}]}


class TestAgentLoop:
    def test_tool_call_then_answer(self, db, monkeypatch):
        insert_photo(db, "a.jpg", taken_at="2025-01-01T00:00:00")
        sequence = iter(
            [
                assistant_reply(None, [("search_photos", {"year": 2025})]),
                assistant_reply("找到 1 张：a.jpg"),
            ]
        )
        monkeypatch.setattr(agent, "_post_json", lambda *a, **k: next(sequence))
        result = run_task(db, BACKEND, "2025 年的照片", max_steps=5, max_seconds=10)
        assert result.final_text == "找到 1 张：a.jpg"
        assert [c["name"] for c in result.tool_calls] == ["search_photos"]

    def test_step_limit_stops_loop(self, db, monkeypatch):
        # model keeps calling tools forever -> must stop at max_steps
        monkeypatch.setattr(
            agent,
            "_post_json",
            lambda *a, **k: assistant_reply(None, [("search_photos", {"year": 2025})]),
        )
        result = run_task(db, BACKEND, "loop", max_steps=3, max_tool_calls=20, max_seconds=30)
        assert result.llm_rounds == 3
        assert len(result.tool_calls) == 3
        assert any("上限" in n for n in result.notes)
        assert result.final_text

    def test_tool_call_budget(self, db, monkeypatch):
        monkeypatch.setattr(
            agent,
            "_post_json",
            lambda *a, **k: assistant_reply(None, [("search_photos", {"year": 2025})]),
        )
        result = run_task(db, BACKEND, "loop", max_steps=12, max_tool_calls=2, max_seconds=30)
        assert len(result.tool_calls) == 2
        assert any("上限" in n for n in result.notes)

    def test_duplicate_call_short_circuited(self, db, monkeypatch):
        insert_photo(db, "a.jpg")

        def fake(url, payload, timeout):
            tool_msgs = [m for m in payload["messages"] if m.get("role") == "tool"]
            if len(tool_msgs) >= 2:
                # second identical call was short-circuited
                return assistant_reply("ok")
            return assistant_reply(None, [("search_photos", {})])

        monkeypatch.setattr(agent, "_post_json", fake)
        result = run_task(db, BACKEND, "x", max_steps=5, max_seconds=10)
        assert any("短路" in n for n in result.notes)
        assert result.final_text == "ok"

    def test_tool_result_truncated(self, db, monkeypatch):
        for i in range(60):
            insert_photo(db, f"p{i:03d}.jpg")
        captured: list[dict] = []
        # distinct args each round so no duplicate short-circuit interferes
        calls = iter([
            ("search_photos", {"max_results": 100}),   # round 0: large result -> truncated
            ("search_photos", {"max_results": 1}),     # round 1: after tool msg
        ])

        def fake(url, payload, timeout):
            captured.append(list(payload["messages"]))  # snapshot, not alias
            if payload["messages"][-1].get("role") == "tool":
                return assistant_reply("done")
            name, args = next(calls)
            return assistant_reply(None, [(name, args)])

        monkeypatch.setattr(agent, "_post_json", fake)
        # 300-char budget forces truncation of the first (large) search result
        run_task(db, BACKEND, "x", max_steps=5, max_result_chars=300, max_seconds=10)
        # the round-1 payload carries round-0's tool result
        tool_msgs = [m for m in captured[1] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "截断" in tool_msgs[0]["content"]
        assert len(tool_msgs[0]["content"]) <= 300 + 100

    def test_backend_error_raises_agenterror(self, db, monkeypatch):
        def fake(*a, **k):
            raise agent.AgentError("boom")

        monkeypatch.setattr(agent, "_post_json", fake)
        with pytest.raises(agent.AgentError):
            run_task(db, BACKEND, "x", max_steps=3, max_seconds=5)

    def test_unconfigured_backend(self, db):
        with pytest.raises(agent.AgentError, match="未配置"):
            run_task(db, {"base_url": None, "model": None}, "x")

    def test_history_passed_through(self, db, monkeypatch):
        captured: list[dict] = []

        def fake(url, payload, timeout):
            captured.append(payload["messages"])
            return assistant_reply("hi")

        monkeypatch.setattr(agent, "_post_json", fake)
        history = [
            {"role": "user", "content": "2025 的海边照片"},
            {"role": "assistant", "content": "找到 3 张"},
        ]
        run_task(db, BACKEND, "它们拍于哪天？", history=history, max_steps=3, max_seconds=5)
        joined = " ".join(m["content"] for m in captured[0])
        assert "海边照片" in joined and "它们拍于哪天" in joined

    def test_tool_definitions_match_registry(self):
        defs = tool_definitions()
        assert len(defs) == 7
        names = {d["function"]["name"] for d in defs}
        assert names == {
            "search_photos", "get_photo", "get_metadata", "get_quality",
            "get_duplicate_groups", "get_similar_photos", "generate_report",
        }
