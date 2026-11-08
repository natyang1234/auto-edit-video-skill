"""Backend-to-isolated-renderer P6 contract integration.

Run this module in a fresh process with EDIT_STYLE_PROFILES_SKILL_DIR pointing
at the P6 isolated clone.  It must never import or patch the formal shared
skill at ~/.claude/skills/edit-style-profiles.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fx_overlay  # noqa: E402
import fx_preview_contract as contract  # noqa: E402


def manual_path() -> dict:
    return {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 4.0, "x": 0.25, "y": 0.3},
            {"source_at": 5.0, "x": 0.5, "y": 0.35},
            {"source_at": 6.0, "x": 0.75, "y": 0.4},
        ],
    }


def isolated_compose():
    configured = os.environ.get("EDIT_STYLE_PROFILES_SKILL_DIR")
    if not configured:
        pytest.skip("requires a fresh process pinned to the isolated P6 renderer clone")
    configured_path = Path(configured).resolve()
    formal = (Path.home() / ".claude/skills/edit-style-profiles").resolve()
    assert configured_path != formal
    assert fx_overlay.skill_dir().resolve() == configured_path
    compose = fx_overlay._compose()
    assert configured_path / "tools/compose.py" == Path(compose.__file__).resolve()
    return compose


def make_one_frame_source(
    tmp_path: Path, *, name: str, fps: str, expected_duration: float
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("requires local ffmpeg and ffprobe")

    source = tmp_path / name
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=32x32:r={fps}",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=r_frame_rate,time_base,duration:format=duration",
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(probe.stdout)
    assert float(metadata["format"]["duration"]) == pytest.approx(
        expected_duration
    )
    assert float(metadata["streams"][0]["duration"]) == pytest.approx(
        expected_duration
    )
    return source


def test_isolated_compose_exposes_the_agreed_optional_keyword() -> None:
    compose = isolated_compose()
    parameter = inspect.signature(compose.render_clip).parameters.get("spotlight_path")
    assert parameter is not None
    assert parameter.default is None


@pytest.mark.parametrize("code,status", [("T3", None), ("P5", "tracked")])
def test_legacy_renderer_still_accepts_nonmanual_effects(
    tmp_path: Path, code: str, status: str | None
) -> None:
    project = tmp_path / "project"
    work = project / "working/fx"
    work.mkdir(parents=True)
    source = project / "source.mp4"
    source.write_bytes(b"source")
    person = work / "person.webm"
    mask = work / "mask.mp4"
    person.write_bytes(b"person")
    mask.write_bytes(b"mask")
    seen_kwargs: dict[str, object] = {}
    preflight_probe_called = False

    def forbidden_preflight_probe(*args, **kwargs):
        nonlocal preflight_probe_called
        preflight_probe_called = True
        raise AssertionError("nonmanual effects must not gain a main preflight probe")

    def legacy_render_clip(
        *args,
        log=None,
        fonts=None,
        font_files=None,
        spotlight_mask=None,
        strict_source=False,
        strict_fit="cover",
        material=None,
        subject=None,
        text_fit=True,
        face_model=None,
    ):
        seen_kwargs.update(
            {
                "subject": subject,
                "spotlight_mask": spotlight_mask,
                "strict_fit": strict_fit,
            }
        )
        output = Path(args[12]) / f"legacy-{code}.mov"
        output.write_bytes(b"rendered")
        track = None if status is None else {"version": "spotlight-track-v2", "status": status, "points": []}
        return {
            "ok": True,
            "out": str(output),
            "cached": False,
            "seconds": 0.01,
            "spotlight": track,
            "text_layout": {},
        }

    compose = SimpleNamespace(
        render_clip=legacy_render_clip,
        probe=forbidden_preflight_probe,
    )
    overlay = {
        "id": "legacy",
        "type": "fx",
        "code": code,
        "start": 1.0,
        "end": 2.0,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1.0},
        "subject": {"x": 0.4, "y": 0.4} if code == "P5" else None,
    }
    effect = {"code": code, "needs_person": code == "P5", "block": "fixture"}
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "_compose", return_value=compose
    ), patch.object(
        fx_overlay, "catalog", return_value={"effects": [effect]}
    ), patch.object(
        fx_overlay, "_person_assets", return_value=(person, mask)
    ), patch.object(
        fx_overlay, "families_for", return_value=[]
    ), patch.object(
        fx_overlay, "font_files_for", return_value={}
    ):
        rendered = fx_overlay.render_for_timeline(
            project,
            overlay,
            source,
            None,
            canvas=(1080, 1920),
            fit="cover",
        )
    assert rendered is not None
    assert seen_kwargs["strict_fit"] == "cover"
    assert preflight_probe_called is False


def test_manual_path_fails_clearly_before_calling_legacy_renderer(
    tmp_path: Path,
) -> None:
    called = False

    def legacy_render_clip(*args, subject=None):
        nonlocal called
        called = True
        raise AssertionError("legacy renderer must not receive a manual path")

    compose = SimpleNamespace(render_clip=legacy_render_clip)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.mp4"
    source.write_bytes(b"source")
    overlay = {
        "id": "manual",
        "type": "fx",
        "code": "P5",
        "start": 4.0,
        "end": 6.0,
        "spotlight_path": manual_path(),
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "_compose", return_value=compose
    ):
        with pytest.raises(fx_overlay.ManualSpotlightUnsupportedError, match="不支援手動路徑"):
            fx_overlay.render_for_timeline(
                project,
                overlay,
                source,
                None,
                canvas=(1080, 1920),
                fit="cover",
            )
    assert called is False


def test_submillisecond_raw_end_mismatch_logs_fallback_instead_of_escaping(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated_compose()
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.mp4"
    source.write_bytes(b"source")
    overlay = {
        "id": "manual-boundary-mismatch",
        "type": "fx",
        "code": "P5",
        "start": 1.0006,
        "end": 3.0004,
        "spotlight_path": {
            "version": 1,
            "interpolation": "smoothstep",
            "keyframes": [
                {"source_at": 1.001, "x": 0.25, "y": 0.3},
                {"source_at": 2.0, "x": 0.5, "y": 0.35},
                {"source_at": 3.0, "x": 0.75, "y": 0.4},
            ],
        },
    }

    rendered = fx_overlay.render_for_timeline(
        project,
        overlay,
        source,
        None,
        canvas=(1080, 1920),
        fit="cover",
    )

    assert rendered is None
    logged = json.loads(capsys.readouterr().err)
    assert logged == {
        "fx_fallback": {
            "layer": "manual-boundary-mismatch",
            "code": "P5",
            "reason": "spotlight_path_invalid",
        }
    }


def assert_manual_source_clipping_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    source: Path,
    end: float,
    expected_error: str,
) -> None:
    compose = isolated_compose()
    progress: list[str] = []
    render_called = False
    real_render_clip = compose.render_clip

    def forbidden_render_clip(*args, spotlight_path=None, **kwargs):
        nonlocal render_called
        render_called = True
        return real_render_clip(*args, spotlight_path=spotlight_path, **kwargs)

    project = tmp_path / "project"
    work = project / "working/fx"
    work.mkdir(parents=True)
    person = work / "person.webm"
    mask = work / "mask.mp4"
    person.write_bytes(b"person-fixture")
    mask.write_bytes(b"mask-fixture")
    start = 0.001
    overlay = {
        "id": "manual-source-clipping",
        "type": "fx",
        "code": "P5",
        "start": start,
        "end": end,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1.0},
        "spotlight_path": {
            "version": 1,
            "interpolation": "smoothstep",
            "keyframes": [
                {"source_at": start, "x": 0.25, "y": 0.3},
                {"source_at": end, "x": 0.75, "y": 0.4},
            ],
        },
    }
    catalog = {
        "effects": [
            {
                "code": "P5",
                "needs_person": True,
                "block": "fx-spotlight",
            }
        ]
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ), patch.object(
        fx_overlay, "_person_assets", return_value=(person, mask)
    ) as person_assets_spy, patch.object(
        fx_overlay, "families_for", return_value=[]
    ), patch.object(
        fx_overlay, "font_files_for", return_value={}
    ), patch.object(
        compose, "probe", wraps=compose.probe
    ) as probe_spy, patch.object(
        compose,
        "_spotlight_track",
        side_effect=AssertionError("source clipping must fail before spotlight tracking"),
    ), patch.object(
        compose.cs,
        "load_theme",
        side_effect=AssertionError("source clipping must fail before theme/render work"),
    ), patch.object(
        compose, "render_clip", new=forbidden_render_clip
    ):
        rendered = fx_overlay.render_for_timeline(
            project,
            overlay,
            source,
            None,
            canvas=(1080, 1920),
            fit="cover",
            progress=progress.append,
        )

    assert rendered is None
    assert probe_spy.call_count == 1
    person_assets_spy.assert_not_called()
    assert progress == []
    assert render_called is False
    logged = json.loads(capsys.readouterr().err)
    assert logged["fx_fallback"]["layer"] == "manual-source-clipping"
    assert logged["fx_fallback"]["code"] == "P5"
    assert expected_error in logged["fx_fallback"]["error"]


def test_manual_window_past_source_end_becomes_main_fallback_before_shared_render_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_one_frame_source(
        tmp_path,
        name="one-frame-0635.mp4",
        fps="2000/127",
        expected_duration=0.0635,
    )
    assert_manual_source_clipping_fails_closed(
        tmp_path,
        capsys,
        source=source,
        end=0.065,
        expected_error="手動聚光燈時窗超出原片長",
    )


def test_manual_submillisecond_clip_becomes_main_fallback_before_shared_render_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = make_one_frame_source(
        tmp_path,
        name="one-frame-1095.mp4",
        fps="2000/219",
        expected_duration=0.1095,
    )
    assert_manual_source_clipping_fails_closed(
        tmp_path,
        capsys,
        source=source,
        end=0.110,
        expected_error="手動聚光燈時窗會因原片長裁短",
    )


def test_effective_render_window_is_identical_for_real_main_and_shared_validators() -> None:
    compose = isolated_compose()
    start = 1.0006
    raw_end = 3.0004
    duration = round(raw_end - start, 3)
    effective_end = round(start + duration, 3)
    path = {
        "version": 1,
        "interpolation": "smoothstep",
        "keyframes": [
            {"source_at": 1.001, "x": 0.25, "y": 0.3},
            {"source_at": 2.0, "x": 0.5, "y": 0.35},
            {"source_at": 3.001, "x": 0.75, "y": 0.4},
        ],
    }

    main_path = contract.normalize_spotlight_path(
        path, start=start, end=effective_end
    )
    shared_path = compose.st.canonicalize_manual_path(
        main_path, start, effective_end
    )
    shared_track = compose.st.manual_track(
        shared_path, 1920, 1080, start, duration, "cover"
    )

    assert effective_end == 3.001
    assert shared_path == main_path
    assert shared_track["status"] == "manual"
    assert shared_track["apply_start"] == 1.001
    assert shared_track["apply_end"] == effective_end
    assert shared_track["path_sha256"] == compose.st.manual_path_sha256(main_path)


@pytest.mark.parametrize("quality", ["draft", "final"])
def test_preview_and_final_forward_identical_full_apply_source_path(
    tmp_path: Path, quality: str
) -> None:
    compose = isolated_compose()
    project = tmp_path / "project"
    work = project / "working/fx"
    work.mkdir(parents=True)
    source = project / "source.mp4"
    source.write_bytes(b"source-fixture")
    person = work / "person.webm"
    mask = work / "mask.mp4"
    person.write_bytes(b"person")
    mask.write_bytes(b"mask")
    overlay = {
        "id": "fx-manual",
        "type": "fx",
        "code": "P5",
        "start": 4.0,
        "end": 6.0,
        "values": {},
        "placement": "",
        "scheme": "",
        "font_ids": {},
        "style": {"opacity": 1.0},
        "spotlight_path": manual_path(),
        "text_fit": True,
    }
    calls: list[dict] = []

    def fake_render_clip(*args, spotlight_path=None, **kwargs):
        assert args[1] == 4.0
        assert args[2] == 2.0
        assert spotlight_path == manual_path()
        compose.probe(args[0])  # the real shared renderer probes after main preflight
        calls.append(json.loads(json.dumps(spotlight_path)))
        output = Path(args[12]) / f"manual-{quality}.mov"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"rendered")
        digest = contract.spotlight_path_sha256(
            spotlight_path, start=args[1], end=args[1] + args[2]
        )
        return {
            "ok": True,
            "out": str(output),
            "cached": False,
            "seconds": 0.01,
            "spotlight": {
                "version": "spotlight-track-v3-manual",
                "mode": "manual",
                "status": "manual",
                "interpolation": "smoothstep",
                "keyframe_count": 3,
                "path_sha256": digest,
                "points": [],
            },
            "text_layout": {"version": 2, "status": "pass", "findings": []},
        }

    catalog = {
        "effects": [
            {
                "code": "P5",
                "needs_person": True,
                "block": "fx-spotlight",
            }
        ]
    }
    with patch.object(fx_overlay, "available", return_value=True), patch.object(
        fx_overlay, "catalog", return_value=catalog
    ), patch.object(
        fx_overlay, "_person_assets", return_value=(person, mask)
    ), patch.object(
        fx_overlay, "families_for", return_value=[]
    ), patch.object(
        fx_overlay, "font_files_for", return_value={}
    ), patch.object(
        compose, "render_clip", new=fake_render_clip
    ), patch.object(
        compose, "probe", return_value={"duration": 10.0}
    ) as probe_spy:
        rendered = fx_overlay.render_for_timeline(
            project,
            overlay,
            source,
            None,
            quality=quality,
            canvas=(1080, 1920),
            fit="cover",
        )

    assert calls == [manual_path()]
    assert probe_spy.call_count == 2
    assert rendered is not None
    assert rendered["spotlight"]["status"] == "manual"
    assert rendered["spotlight"]["path_sha256"] == contract.spotlight_path_sha256(
        manual_path(), start=4.0, end=6.0
    )
