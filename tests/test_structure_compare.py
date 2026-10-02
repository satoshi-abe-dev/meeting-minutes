"""Tests for the developer comparison of structure engines (Issue #192). Fake LLM only."""

from __future__ import annotations

import json

import pytest

from meeting_minutes.model.config import AIConfig, Config
from meeting_minutes.model.structure_compare import run_comparison
from meeting_minutes.model.structure_engine import StructureEngineUnavailable
from meeting_minutes.model.transcribe import Segment, save_transcript

GOOD = (
    "# 議事録: {title}\n\n- 日時: {datetime_hint}\n- 記録時間: {duration_hint}\n\n"
    "## 進捗\n（明示されたことだけ）\n"
)


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []
        self.closed = False

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls.append(user)
        if "議事録の「型」" in user:
            return GOOD
        return "- 要点"  # a chunk summary

    def close(self):
        self.closed = True


def _make_source(tmp_path, n=5, text="進捗の発言"):
    src = tmp_path / "meeting"
    segs = [Segment(start=i * 3.0, end=i * 3.0 + 3.0, text=f"{text}{i}") for i in range(n)]
    save_transcript(segs, src / "transcript")
    return src


def _config(tmp_path, **ai) -> Config:
    cfg = Config(ai=AIConfig(**ai))
    cfg.output.dir = str(tmp_path / "out")
    return cfg


def _runners(seen: dict):
    def single(client, material, **kw):
        seen["single"] = material
        return GOOD, 1, 0

    def crewai(client, material, **kw):
        seen["crewai"] = material
        return None, 6, 3

    return {"single": single, "crewai": crewai}


def test_both_engines_get_exactly_the_same_material(tmp_path):
    seen: dict = {}
    result = run_comparison(
        _make_source(tmp_path), _config(tmp_path), client=FakeClient(), runners=_runners(seen)
    )
    assert seen["single"] == seen["crewai"]
    assert "進捗の発言0" in seen["single"]  # short meeting: the full transcript
    assert (result / "material.txt").read_text(encoding="utf-8") == seen["single"]


def test_results_are_saved_side_by_side(tmp_path):
    result = run_comparison(
        _make_source(tmp_path), _config(tmp_path), client=FakeClient(), runners=_runners({})
    )
    assert result.parent == tmp_path / "out" / "_compare"  # under output/, never the repo
    data = json.loads((result / "comparison.json").read_text(encoding="utf-8"))
    by = {r["engine"]: r for r in data["results"]}
    assert by["single"]["llm_calls"] == 1 and by["single"]["structure"] == GOOD
    assert by["crewai"]["llm_calls"] == 6 and by["crewai"]["tool_calls"] == 3
    assert by["crewai"]["structure"] is None
    assert (result / "structure_single.txt").is_file()
    assert not (result / "structure_crewai.txt").exists()  # failed engine writes no structure
    md = (result / "comparison.md").read_text(encoding="utf-8")
    assert "| single | ok |" in md
    assert "| crewai | failed (would fall back) |" in md
    assert data["material_chars"] == len((result / "material.txt").read_text(encoding="utf-8"))


def test_no_minutes_are_generated(tmp_path):
    client = FakeClient()
    result = run_comparison(
        _make_source(tmp_path), _config(tmp_path), client=client, runners=_runners({})
    )
    assert client.calls == []  # the fake engines make no calls, and the minutes step never ran
    assert not (result / "minutes.md").exists()


def test_the_default_single_engine_counts_its_llm_calls(tmp_path):
    from meeting_minutes.model.structure_compare import DEFAULT_RUNNERS

    client = FakeClient()
    result = run_comparison(
        _make_source(tmp_path), _config(tmp_path), client=client,
        engines=("single",), runners={"single": DEFAULT_RUNNERS["single"]},
    )
    data = json.loads((result / "comparison.json").read_text(encoding="utf-8"))
    assert data["results"][0]["llm_calls"] == 1
    assert len(client.calls) == 1


def test_long_meetings_use_chunk_summaries_as_material(tmp_path):
    cfg = _config(tmp_path, chunk_trigger_chars=2000, chunk_size_chars=1000)
    seen: dict = {}
    run_comparison(
        _make_source(tmp_path, n=300, text="議題について長い発言をする" * 5), cfg,
        client=FakeClient(), runners=_runners(seen),
    )
    assert "### 部分 1" in seen["single"]
    assert "議題について長い発言をする" not in seen["single"]
    assert seen["single"] == seen["crewai"]


def test_an_engine_that_raises_is_recorded_not_fatal(tmp_path):
    def boom(client, material, **kw):
        raise RuntimeError("kaput")

    runners = _runners({})
    runners["crewai"] = boom
    result = run_comparison(
        _make_source(tmp_path), _config(tmp_path), client=FakeClient(), runners=runners
    )
    data = json.loads((result / "comparison.json").read_text(encoding="utf-8"))
    crew = next(r for r in data["results"] if r["engine"] == "crewai")
    assert crew["structure"] is None and "kaput" in crew["notes"][0]


def test_missing_crewai_is_an_error_not_a_failed_engine(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "crewai", None)
    with pytest.raises(StructureEngineUnavailable):
        run_comparison(_make_source(tmp_path), _config(tmp_path), client=FakeClient())


def test_source_without_transcript_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_comparison(tmp_path, _config(tmp_path), client=FakeClient(), runners=_runners({}))


def test_transcript_json_directly_in_the_folder_is_accepted(tmp_path):
    src = tmp_path / "flat"
    save_transcript([Segment(start=0.0, end=1.0, text="直下の発言")], src)
    seen: dict = {}
    run_comparison(src, _config(tmp_path), client=FakeClient(), runners=_runners(seen))
    assert "直下の発言" in seen["single"]
