"""Tests for pipeline.run (never calls ffmpeg or an LLM; all of Deps is swapped out).

Verifies stage ordering, the progress callback, and how output paths are assembled.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from meeting_minutes.model.cancel import PipelineCancelled
from meeting_minutes.model.config import Config
from meeting_minutes.model.frames import Frame
from meeting_minutes.model.pipeline import STAGES, Deps, run
from meeting_minutes.model.transcribe import Segment
from meeting_minutes.model.vision import FrameNote


class FakeClient:
    def __init__(self, *, with_preflight: bool = False):
        self.closed = False
        self.preflight_models: list[str] | None = None
        if with_preflight:
            self.preflight = self._preflight  # type: ignore[assignment]

    def _preflight(self, models):
        self.preflight_models = list(models)

    def close(self):
        self.closed = True


@pytest.fixture
def config(tmp_path) -> Config:
    cfg = Config()
    cfg.output.dir = str(tmp_path / "out")  # an absolute path, so used as-is
    return cfg


@pytest.fixture
def video(tmp_path) -> Path:
    p = tmp_path / "会議.mp4"
    p.write_bytes(b"not really a video")
    return p


def _fake_deps(recorder: list[str], client: FakeClient) -> Deps:
    def extract_audio(video_path, out_wav):
        recorder.append("extract_audio")
        return Path(out_wav)

    def transcribe_wav(
        wav_path, tr_config, *, on_progress=None, total_hint=None, cancel_event=None,
        language=None,
    ):
        recorder.append("transcribe_wav")
        segs = [Segment(0.0, 3.0, "こんにちは"), Segment(3.0, 6.0, "本題です")]
        if on_progress:
            for i, s in enumerate(segs, 1):
                on_progress(i, len(segs), s.text)
        return segs

    def save_transcript(segments, out_dir):
        recorder.append("save_transcript")
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        j, t = d / "transcript.json", d / "transcript.txt"
        j.write_text("[]", encoding="utf-8")
        t.write_text("こんにちは\n", encoding="utf-8")
        return j, t

    def extract_frames(video_path, out_dir, fr_config):
        recorder.append("extract_frames")
        d = Path(out_dir) / "frames"
        d.mkdir(parents=True, exist_ok=True)
        return [Frame(timestamp=1.0, path=d / "frame_0001_000001.jpg")]

    def save_frame_index(frames, out_dir):
        recorder.append("save_frame_index")
        p = Path(out_dir) / "frames" / "frames.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[]", encoding="utf-8")
        return p

    def describe_frames(
        frames, client_, out_dir, *, on_progress=None, cancel_event=None, reuse=None,
        language=None,
    ):
        recorder.append("describe_frames")
        assert client_ is client
        if on_progress:
            on_progress(1, 1, "解析")
        return [FrameNote(1.0, "frames/frame_0001_000001.jpg", "スライド: 議題")]

    def save_frame_notes(notes, out_dir):
        recorder.append("save_frame_notes")
        p = Path(out_dir) / "frames" / "frame_notes.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[]", encoding="utf-8")
        return p

    def generate_minutes(
        segments,
        notes,
        client_,
        llm_config,
        meta,
        *,
        on_progress=None,
        cancel_event=None,
        out_dir=None,
        reuse=None,
        context_tokens=None,
        template_path=None,
        auto_structure=False,
        language=None,
    ):
        recorder.append("generate_minutes")
        assert client_ is client
        if on_progress:
            on_progress(1, 1, "生成")
        return "# 議事録: 会議\n"

    def save_minutes(markdown, out_dir):
        recorder.append("save_minutes")
        p = Path(out_dir) / "minutes.md"
        p.write_text(markdown, encoding="utf-8")
        return p

    def save_minutes_docx(markdown, out_dir):
        recorder.append("save_minutes_docx")
        p = Path(out_dir) / "minutes.docx"
        p.write_text("(fake docx)", encoding="utf-8")
        return p

    def load_transcript(out_dir):
        recorder.append("load_transcript")
        return [Segment(0.0, 3.0, "再利用こんにちは"), Segment(3.0, 6.0, "再利用本題")]

    def load_frames(out_dir):
        recorder.append("load_frames")
        return [Frame(timestamp=2.0, path=Path(out_dir) / "frames" / "f.jpg")]

    return Deps(
        extract_audio=extract_audio,
        transcribe_wav=transcribe_wav,
        save_transcript=save_transcript,
        load_transcript=load_transcript,
        extract_frames=extract_frames,
        save_frame_index=save_frame_index,
        load_frames=load_frames,
        describe_frames=describe_frames,
        save_frame_notes=save_frame_notes,
        generate_minutes=generate_minutes,
        save_minutes=save_minutes,
        save_minutes_docx=save_minutes_docx,
        make_client=lambda cfg: client,
        probe_duration=lambda p: 123.0,
    )


def test_pipeline_runs_stages_in_order(config, video):
    recorder: list[str] = []
    client = FakeClient()
    events: list[tuple] = []

    run(
        video,
        config,
        on_progress=lambda *a: events.append(a),
        deps=_fake_deps(recorder, client),
    )

    assert recorder == [
        "extract_audio",
        "transcribe_wav",
        "save_transcript",
        "extract_frames",
        "save_frame_index",
        "describe_frames",
        "save_frame_notes",
        "generate_minutes",
        "save_minutes",
        "save_minutes_docx",
    ]

    # progress stages first appear in their defined order
    seen_order: list[str] = []
    for stage, *_ in events:
        if stage not in seen_order:
            seen_order.append(stage)
    assert seen_order == [*STAGES, "done"]

    # the client is closed at the end
    assert client.closed is True


def test_pipeline_messages_show_model_and_waiting(config, video):
    """(5)(6): each phase's message includes the model name and "waiting for a response"."""
    events: list[tuple] = []
    run(
        video, config,
        on_progress=lambda *a: events.append(a),
        deps=_fake_deps([], FakeClient()),
    )
    by_stage: dict[str, list[str]] = {}
    for stage, _cur, _tot, msg in events:
        by_stage.setdefault(stage, []).append(msg)

    preflight_msg = by_stage["preflight"][0]
    assert config.ai.llm_model in preflight_msg
    assert config.ai.vlm_model in preflight_msg
    assert "待っています" in preflight_msg

    transcribe_start = by_stage["transcribe"][0]
    assert config.transcribe.model in transcribe_start

    vision_start = by_stage["vision"][0]
    assert config.ai.vlm_model in vision_start


def test_pipeline_messages_translated_when_language_en(config, video):
    """When language="en", progress messages come out in English (the
    default, ja, is covered by a separate test)."""
    events: list[tuple] = []
    run(
        video, config,
        on_progress=lambda *a: events.append(a),
        deps=_fake_deps([], FakeClient()),
        language="en",
    )
    msgs = [msg for _stage, _cur, _tot, msg in events]
    joined = "\n".join(msgs)
    assert "Waiting for the LLM server" in joined
    assert "Extracting audio from the video" in joined
    assert any(m.startswith("Done: ") for m in msgs)
    # no Japanese boilerplate phrases are mixed in
    assert "応答を待っています" not in joined
    assert "音声を抽出" not in joined


def test_pipeline_missing_video_raises_english_message(config, tmp_path):
    with pytest.raises(FileNotFoundError) as ei:
        run(tmp_path / "no-such.mp4", config, deps=Deps(), language="en")
    assert "Video file not found" in str(ei.value)


def test_pipeline_result_paths(config, video):
    recorder: list[str] = []
    client = FakeClient()
    result = run(video, config, deps=_fake_deps(recorder, client))

    assert result.out_dir == config.output_root / "会議"
    assert result.minutes_path.is_file()
    assert result.minutes_path.read_text(encoding="utf-8").startswith("# 議事録")
    assert result.transcript_txt.is_file()
    # (1) the audio file and transcript are together in one transcript/ folder
    assert result.transcript_txt.parent.name == "transcript"
    assert result.transcript_json.parent == result.transcript_txt.parent
    assert result.n_segments == 2
    assert result.n_frames == 1
    # the .docx (Word) version is also written to the root of the same folder and appears in the result
    assert result.minutes_docx_path == result.out_dir / "minutes.docx"
    assert result.minutes_docx_path.is_file()


def test_pipeline_writes_real_docx_from_markdown(config, video):
    """Use the real save_minutes_docx conversion so the generated .docx can be reopened with python-docx."""
    import docx

    from meeting_minutes.model.docx_export import save_minutes_docx

    deps = _fake_deps([], FakeClient())
    deps.save_minutes_docx = save_minutes_docx
    result = run(video, config, deps=deps)

    assert result.minutes_docx_path.is_file()
    opened = docx.Document(str(result.minutes_docx_path))
    assert opened.paragraphs[0].style.name == "Heading 1"  # "# 議事録: 会議"


def test_pipeline_docx_failure_is_isolated(config, video):
    """Even if .docx conversion raises, run completes; minutes.md is
    produced and a warning is added to warnings."""
    def boom(markdown, out_dir):
        raise RuntimeError("docx broke")

    deps = _fake_deps([], FakeClient())
    deps.save_minutes_docx = boom
    result = run(video, config, deps=deps)

    assert result.minutes_path.is_file()
    assert result.minutes_docx_path is None
    assert any("docx broke" in w for w in result.warnings)


def test_pipeline_puts_audio_alongside_transcript(config, video):
    recorder: list[str] = []
    audio_paths: list[Path] = []

    deps = _fake_deps(recorder, FakeClient())
    real_extract_audio = deps.extract_audio

    def extract_audio(video_path, out_wav):
        audio_paths.append(Path(out_wav))
        return real_extract_audio(video_path, out_wav)

    deps.extract_audio = extract_audio
    result = run(video, config, deps=deps)

    assert len(audio_paths) == 1
    assert audio_paths[0].parent == result.transcript_txt.parent
    assert audio_paths[0].name == "audio.wav"


def test_pipeline_missing_video_raises(config, tmp_path):
    with pytest.raises(FileNotFoundError):
        run(tmp_path / "no-such.mp4", config, deps=Deps())


def test_pipeline_preflight_called_with_configured_models(config, video):
    client = FakeClient(with_preflight=True)
    run(video, config, deps=_fake_deps([], client))
    assert client.preflight_models == [config.ai.llm_model, config.ai.vlm_model]


def test_pipeline_reuses_existing_transcript_and_frames(config, video):
    # set up the previous run's artifacts
    out_dir = config.output_root / "会議"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript").mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript" / "transcript.json").write_text("[]", encoding="utf-8")
    (out_dir / "frames" / "frames.json").write_text("[]", encoding="utf-8")

    recorder: list[str] = []
    client = FakeClient()
    result = run(video, config, deps=_fake_deps(recorder, client), reuse=True)

    # transcription/frame extraction are skipped; load_* are used instead
    assert "transcribe_wav" not in recorder
    assert "extract_frames" not in recorder
    assert "load_transcript" in recorder
    assert "load_frames" in recorder
    # VLM and minutes generation run as usual
    assert recorder[-3:] == ["generate_minutes", "save_minutes", "save_minutes_docx"]
    assert result.n_segments == 2
    assert result.n_frames == 1


def test_pipeline_fresh_ignores_existing(config, video):
    out_dir = config.output_root / "会議"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript").mkdir(parents=True, exist_ok=True)
    (out_dir / "transcript" / "transcript.json").write_text("[]", encoding="utf-8")
    (out_dir / "frames" / "frames.json").write_text("[]", encoding="utf-8")

    recorder: list[str] = []
    run(video, config, deps=_fake_deps(recorder, FakeClient()), reuse=False)

    assert "transcribe_wav" in recorder
    assert "extract_frames" in recorder
    assert "load_transcript" not in recorder


def test_pipeline_cancel_during_vision_stops_and_closes_client(config, video):
    recorder: list[str] = []
    client = FakeClient()
    cancel_event = threading.Event()

    def describe_frames(
        frames, client_, out_dir, *, on_progress=None, cancel_event=None, reuse=None,
        language=None,
    ):
        recorder.append("describe_frames")
        # simulate the user pressing Stop right after entering the VLM stage
        from meeting_minutes.model.cancel import check_cancel

        cancel_event.set()
        check_cancel(cancel_event)
        return []  # this point is not expected to be reached

    deps = _fake_deps(recorder, client)
    deps.describe_frames = describe_frames

    with pytest.raises(PipelineCancelled):
        run(video, config, deps=deps, cancel_event=cancel_event)

    assert "generate_minutes" not in recorder  # doesn't proceed as far as minutes generation
    assert client.closed is True  # always closed in the finally block


def test_pipeline_cancel_before_start_raises_immediately(config, video):
    recorder: list[str] = []
    client = FakeClient()
    cancel_event = threading.Event()
    cancel_event.set()  # cancellation was already requested before starting

    with pytest.raises(PipelineCancelled):
        run(video, config, deps=_fake_deps(recorder, client), cancel_event=cancel_event)

    assert recorder == []  # nothing is executed
    assert client.closed is True
