"""Public agent-first tracer for the integrated Phase 0e kinetic route."""
from __future__ import annotations

import contextlib
import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import auto_edit  # noqa: E402
import caption_delivery  # noqa: E402
import contract_registry  # noqa: E402
import delivery_envelope  # noqa: E402
import render_editor_timeline as renderer  # noqa: E402


class _OllamaHandler(http.server.BaseHTTPRequestHandler):
    """Deterministic loopback translation provider; no external call occurs."""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        prompt = request["messages"][-1]["content"]
        requested = json.loads(prompt.rsplit("\n", 1)[-1])
        translated = [
            "First, organize the materials into a complete method.",
            "Second pass: organize the same materials into a repeatable method.",
            "The key is reaching an 87% completion rate for a steadier result.",
            "Finally, remember the full process so you can repeat it every time.",
        ]
        content = {
            "items": [
                {
                    "caption_instance_id": item["caption_instance_id"],
                    "translated_text": translated[index],
                }
                for index, item in enumerate(requested)
            ]
        }
        payload = json.dumps(
            {"message": {"content": json.dumps(content, ensure_ascii=False)}}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args) -> None:
        return


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "needs local ffmpeg",
)
class Phase0eKineticIntegrationTests(unittest.TestCase):
    """Keep acquisition deterministic, but execute the real delivery route."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="phase0e-kinetic-")
        self.root = Path(self._tmp.name)
        self.input = self.root / "input.mp4"
        self.project = self.root / "project"
        self.out = self.root / "out"
        self._make_source()
        with contextlib.redirect_stdout(io.StringIO()):
            code = auto_edit.cmd_init(
                auto_edit._args_for(
                    "init",
                    "--input",
                    str(self.input),
                    "--project-dir",
                    str(self.project),
                    "--source-language",
                    "zh-TW",
                    "--source-has-burned-in",
                    "no",
                    "--platform",
                    "instagram-reels",
                )
            )
        self.assertEqual(code, 0)
        self.manifest_path = self.project / "project.json"
        self._import_transcript_source()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        source_pointer = json.loads(
            (self.project / caption_delivery.SOURCE_CURRENT_REL).read_text("utf-8")
        )
        evidence_map = {
            "schema_version": 1,
            "source_sha256": manifest["source"]["sha256"],
            "transcript_revision": source_pointer["revision"],
            "items": [
                    {
                        "id": "evidence-a1b2c3d4",
                        "kind": "quote",
                        "literal": "第一步先整理資料，這是完整方法。",
                        "start": 0.5,
                        "end": 4.0,
                        "confidence": 0.99,
                        "review_status": "approved",
                    },
                    {
                        "id": "evidence-87abcdef",
                        "kind": "number",
                        "literal": "87%",
                        "start": 9.0,
                        "end": 9.5,
                        "confidence": 0.99,
                        "review_status": "approved",
                    },
                ],
        }
        evidence_map["revision"] = contract_registry.canonical_hash(evidence_map)
        self._write_json(
            self.project / "working/evidence_map.json",
            evidence_map,
        )
        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _OllamaHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._tmp.cleanup()

    def _make_source(self) -> None:
        result = subprocess.run(
            [
                renderer.ffmpeg_path(),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=360x640:r=30:d=16",
                "-f",
                "lavfi",
                "-i",
                "sine=f=160:r=48000:d=16",
                "-filter:a",
                "volume=0.025",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(self.input),
            ],
            text=True,
            capture_output=True,
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _import_transcript_source(self) -> None:
        whisper = self.root / "whisper.json"
        segments = [
            (0.5, 4.0, "第一步先整理資料，這是完整方法。"),
            (4.3, 7.5, "第一步先整理資料，這是完整方法。"),
            (7.8, 11.3, "關鍵是完成率達到 87%，所以結果更穩定。"),
            (11.6, 15.0, "最後記住完整流程，因此每次都能重複成功。"),
        ]
        payload = {
            "text": "".join(text for _start, _end, text in segments),
            "language": "zh",
            "segments": [
                {
                    "start": start,
                    "end": end,
                    "text": text,
                    "words": [
                        {
                            "word": text,
                            "start": start,
                            "end": end,
                            "probability": 0.99,
                        }
                    ],
                }
                for start, end, text in segments
            ],
        }
        self._write_json(whisper, payload)
        with contextlib.redirect_stdout(io.StringIO()):
            code = auto_edit.cmd_import_whisper(
                auto_edit._args_for(
                    "import-whisper",
                    "--manifest",
                    str(self.manifest_path),
                    "--whisper-json",
                    str(whisper),
                    "--model",
                    "base",
                )
            )
        self.assertEqual(code, 0)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _cut_args(self):
        return auto_edit.build_parser().parse_args(
            [
                "cut",
                "--input",
                str(self.input),
                "--project-dir",
                str(self.project),
                "--out",
                str(self.out),
                "--director",
                "kinetic-explainer",
                "--clips",
                "1",
                "--quality",
                "final",
                "--keep-pauses",
            ]
        )

    def _run_cut(
        self, *, run_side_effect=None, render_side_effect=None
    ) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        patches = (
            patch.object(auto_edit, "cmd_analyze_video", return_value=0),
            patch.object(auto_edit, "cmd_transcribe_local", return_value=0),
            patch.object(auto_edit, "cmd_build_evidence_index", return_value=0),
            patch.object(auto_edit, "saved_terms", return_value=([], [])),
            patch.object(
                auto_edit,
                "apply_editorial_selection",
                side_effect=lambda plan, *_args: plan,
            ),
        )
        environment = {
            "OLLAMA_HOST": f"http://127.0.0.1:{self.server.server_port}"
        }
        render_patch = (
            patch.object(renderer, "render_project", side_effect=render_side_effect)
            if render_side_effect is not None
            else contextlib.nullcontext()
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patch.dict(os.environ, environment),
            render_patch,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            if run_side_effect is None:
                code = auto_edit.cmd_cut(self._cut_args())
            else:
                with patch.object(subprocess, "run", side_effect=run_side_effect):
                    code = auto_edit.cmd_cut(self._cut_args())
        raw_stdout = stdout.getvalue()
        payload = json.loads(raw_stdout) if raw_stdout.strip() else {"_raw_stdout": raw_stdout}
        return code, payload, stderr.getvalue()

    def _assert_no_staging_residue(self) -> None:
        staging = self.project / "working/delivery_envelopes/.staging"
        if not staging.exists():
            return
        self.assertEqual(
            [item for item in staging.iterdir() if item.name != ".locks"], []
        )

    def test_public_cut_delivers_bilingual_motion_sfx_and_finalized_envelope(self) -> None:
        code, payload, stderr = self._run_cut()
        self.assertEqual(code, 0, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["clips"]), 1)
        clip = payload["clips"][0]
        output = Path(clip["file"])
        self.assertTrue(output.is_file())
        self.assertEqual(clip["output_sha256"], self._sha256(output))
        self.assertGreater(float(auto_edit.probe_media(output)["duration_s"]), 15.0)

        envelope_path = Path(clip["delivery_envelope"])
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        delivery_envelope.validate_envelope(
            self.project, envelope, expected_state="finalized"
        )
        self.assertEqual(clip["delivery_envelope_sha256"], self._sha256(envelope_path))
        self.assertEqual(envelope["profile"]["id"], "kinetic-explainer")
        artifacts = envelope["artifacts"]
        for name in (
            "output",
            "qa_report",
            "contact_sheet",
            "visual_evidence",
            "motion_evidence",
            "caption_v2",
            "audio_event_plan",
            "audio_catalog",
            "sfx_stem",
        ):
            self.assertIsInstance(artifacts[name], dict, name)
            path = (
                Path(artifacts[name]["path"])
                if name == "output"
                else self.project / artifacts[name]["path"]
            )
            self.assertTrue(path.is_file(), name)
            self.assertEqual(artifacts[name]["sha256"], self._sha256(path), name)

        captions = json.loads(
            (self.project / artifacts["caption_v2"]["path"]).read_text("utf-8")
        )
        self.assertTrue(captions["required"])
        self.assertEqual(captions["target_language"], "en")
        self.assertGreaterEqual(len(captions["items"]), 2)
        self.assertEqual(
            len({item["caption_source_id"] for item in captions["items"]}),
            len(captions["items"]),
        )
        self.assertEqual(
            len({item["caption_instance_id"] for item in captions["items"]}),
            len(captions["items"]),
        )
        self.assertTrue(
            all(str(item["translated_text"]).strip() for item in captions["items"])
        )

        report = json.loads(
            (self.project / artifacts["qa_report"]["path"]).read_text("utf-8")
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["sfx_delivery"]["status"], "pass")
        visual = json.loads(
            (self.project / artifacts["visual_evidence"]["path"]).read_text("utf-8")
        )
        items = visual.get("items", [])
        self.assertTrue(any(item.get("kind") == "title" for item in items))
        self.assertTrue(
            any(item.get("kind") not in {"title", "caption"} for item in items)
        )
        self.assertTrue(
            any(
                isinstance(item.get("motion"), dict)
                and item["motion"].get("faithful") is True
                for item in items
            )
        )
        plan = json.loads(
            (self.project / artifacts["audio_event_plan"]["path"]).read_text("utf-8")
        )
        self.assertEqual(len(plan["events"]), 1)
        resolved = json.loads(
            (self.project / "working/resolved_director_profile.json").read_text("utf-8")
        )
        self.assertEqual(resolved["profile_id"], "kinetic-explainer")
        self._assert_no_staging_residue()

    def test_stale_caption_timeline_before_renderer_fails_without_publication(self) -> None:
        real_render = renderer.render_project
        changed = False

        def stale_then_render(project_dir, output, quality, *args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                path = self.project / caption_delivery.CAPTION_REL
                artifact = json.loads(path.read_text(encoding="utf-8"))
                artifact["timeline_revision"] = "f" * 64
                for item in artifact["items"]:
                    item["timeline_revision"] = "f" * 64
                self._write_json(path, artifact)
            return real_render(project_dir, output, quality, *args, **kwargs)

        code, payload, stderr = self._run_cut(render_side_effect=stale_then_render)
        self.assertTrue(changed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])
        self.assertEqual(list(self.out.glob("*.mp4")) if self.out.exists() else [], [])
        envelopes = self.project / "working/delivery_envelopes"
        if envelopes.exists():
            self.assertEqual(
                [item for item in envelopes.glob("*.json") if item.is_file()], []
            )
        self._assert_no_staging_residue()

    def _run_cut_with_finalized_mutation(self, target: str) -> tuple[int, dict, str, bool]:
        real_revalidate = delivery_envelope.revalidate_finalized_delivery
        changed = False
        revalidate_calls = 0
        trigger_call = {"output": 1, "qa": 2, "envelope": 3}[target]

        def atomic_replace(path: Path, payload: bytes) -> None:
            temporary = path.with_name(f".{path.name}.race-{target}")
            temporary.write_bytes(payload)
            os.replace(temporary, path)

        def mutate_then_revalidate(snapshot):
            nonlocal changed, revalidate_calls
            revalidate_calls += 1
            if not changed and revalidate_calls == trigger_call:
                changed = True
                if target == "output":
                    atomic_replace(snapshot.expected_output, b"changed-after-render")
                elif target == "qa":
                    qa_item = snapshot.envelope["artifacts"]["qa_report"]
                    qa_path = snapshot.project_dir / qa_item["path"]
                    atomic_replace(qa_path, b'{"status":"fail"}\n')
                else:
                    atomic_replace(snapshot.envelope_path, b"{}\n")
            return real_revalidate(snapshot)

        with patch.object(
            delivery_envelope,
            "revalidate_finalized_delivery",
            side_effect=mutate_then_revalidate,
        ):
            code, payload, stderr = self._run_cut()
        return code, payload, stderr, changed

    def _assert_finalized_mutation_has_no_public_success(self, target: str) -> None:
        code, payload, stderr, changed = self._run_cut_with_finalized_mutation(target)
        self.assertTrue(changed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])

    def test_output_single_hook_race_has_no_public_success(self) -> None:
        self._assert_finalized_mutation_has_no_public_success("output")
        self.assertEqual(list(self.out.glob("*.mp4")) if self.out.exists() else [], [])
        envelope_root = self.project / delivery_envelope.DELIVERY_REL
        if envelope_root.exists():
            self.assertEqual(
                [item for item in envelope_root.glob("*.json") if item.is_file()],
                [],
            )
        self._assert_no_staging_residue()
        self.assertEqual(delivery_envelope._ACTIVE_STAGING_LEASES, {})

    def test_pre_transfer_revalidation_failure_restores_exact_prior_publication(
        self,
    ) -> None:
        prior_output = b"prior-output-before-pre-transfer-failure"
        prior_envelope = b"prior-envelope-before-pre-transfer-failure\n"
        real_render = renderer.render_project
        real_verified = auto_edit.verified_final_delivery
        real_revalidate = delivery_envelope.revalidate_finalized_delivery
        observed = {}
        changed = False
        armed = False

        def seed_prior_then_render(project_dir, output, quality, *args, **kwargs):
            if "output" not in observed:
                state = json.loads(
                    (self.project / "working/editor_state.json").read_text("utf-8")
                )
                render_id = renderer.direct_final_render_id(state, output)
                envelope = delivery_envelope.finalized_path(self.project, render_id)
                output.parent.mkdir(parents=True, exist_ok=True)
                envelope.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(prior_output)
                envelope.write_bytes(prior_envelope)
                observed.update({"output": output, "envelope": envelope})
            authority = real_render(project_dir, output, quality, *args, **kwargs)
            return authority

        def verify_then_arm(*args, **kwargs):
            nonlocal armed
            snapshot = real_verified(*args, **kwargs)
            armed = True
            return snapshot

        def replace_then_revalidate(snapshot):
            nonlocal changed
            if armed and not changed:
                changed = True
                replacement = snapshot.expected_output.with_name(
                    f".{snapshot.expected_output.name}.pre-transfer-race"
                )
                replacement.write_bytes(b"externally-replaced-current-publication")
                os.replace(replacement, snapshot.expected_output)
            return real_revalidate(snapshot)

        with (
            patch.object(
                auto_edit,
                "verified_final_delivery",
                side_effect=verify_then_arm,
            ),
            patch.object(
                delivery_envelope,
                "revalidate_finalized_delivery",
                side_effect=replace_then_revalidate,
            ),
        ):
            code, payload, stderr = self._run_cut(
                render_side_effect=seed_prior_then_render
            )

        self.assertTrue(changed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])
        self.assertEqual(observed["output"].read_bytes(), prior_output)
        self.assertEqual(observed["envelope"].read_bytes(), prior_envelope)
        self._assert_no_staging_residue()
        self.assertEqual(delivery_envelope._ACTIVE_STAGING_LEASES, {})

    def test_pre_transfer_revalidation_failure_removes_new_publication(self) -> None:
        real_render = renderer.render_project
        real_verified = auto_edit.verified_final_delivery
        real_revalidate = delivery_envelope.revalidate_finalized_delivery
        observed = {}
        armed = False
        changed = False

        def render_then_observe(project_dir, output, quality, *args, **kwargs):
            authority = real_render(project_dir, output, quality, *args, **kwargs)
            observed["authority"] = authority
            return authority

        def verify_then_arm(*args, **kwargs):
            nonlocal armed
            snapshot = real_verified(*args, **kwargs)
            armed = True
            return snapshot

        def replace_then_revalidate(snapshot):
            nonlocal changed
            if armed and not changed:
                changed = True
                observed["snapshot"] = snapshot
                replacement = snapshot.expected_output.with_name(
                    f".{snapshot.expected_output.name}.pre-transfer-race"
                )
                replacement.write_bytes(b"externally-replaced-current-publication")
                os.replace(replacement, snapshot.expected_output)
            return real_revalidate(snapshot)

        with (
            patch.object(
                auto_edit,
                "verified_final_delivery",
                side_effect=verify_then_arm,
            ),
            patch.object(
                delivery_envelope,
                "revalidate_finalized_delivery",
                side_effect=replace_then_revalidate,
            ),
        ):
            code, payload, stderr = self._run_cut(render_side_effect=render_then_observe)

        self.assertTrue(changed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])
        snapshot = observed["snapshot"]
        self.assertFalse(snapshot.expected_output.exists())
        self.assertFalse(snapshot.envelope_path.exists())
        self._assert_no_staging_residue()
        self.assertNotIn(
            delivery_envelope._lease_key(self.project, snapshot.render_id),
            delivery_envelope._ACTIVE_STAGING_LEASES,
        )

    def test_qa_single_hook_race_has_no_public_success(self) -> None:
        self._assert_finalized_mutation_has_no_public_success("qa")

    def test_envelope_single_hook_race_has_no_public_success(self) -> None:
        self._assert_finalized_mutation_has_no_public_success("envelope")

    def test_success_handoff_rejects_atomic_output_replace(self) -> None:
        changed = False
        observed = {}

        def mutate_before_write(_payload, snapshots):
            nonlocal changed
            self.assertEqual(len(snapshots), 1)
            changed = True
            observed["snapshot"] = snapshots[0]
            output = snapshots[0].expected_output
            replacement = output.with_name(f".{output.name}.handoff-race")
            replacement.write_bytes(b"changed-at-success-handoff")
            os.replace(replacement, output)

        with patch.object(
            auto_edit,
            "_FINAL_DELIVERY_PRE_WRITE_HOOK",
            new=mutate_before_write,
        ):
            code, payload, stderr = self._run_cut()

        self.assertTrue(changed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])
        snapshot = observed["snapshot"]
        self.assertFalse(snapshot.expected_output.exists())
        self.assertFalse(snapshot.envelope_path.exists())
        for name, item in snapshot.envelope["artifacts"].items():
            if item is None or name in {"output", "caption_v2", "motion_evidence"}:
                continue
            self.assertFalse((snapshot.project_dir / item["path"]).exists(), name)
        self._assert_no_staging_residue()

    def test_handoff_failure_restores_prior_output_and_envelope(self) -> None:
        prior_output = b"prior-public-output-sentinel"
        prior_envelope = b"prior-public-envelope-sentinel\n"
        real_render = renderer.render_project
        observed = {}

        def seed_prior_then_render(project_dir, output, quality, *args, **kwargs):
            if "output" not in observed:
                state = json.loads(
                    (self.project / "working/editor_state.json").read_text("utf-8")
                )
                render_id = renderer.direct_final_render_id(state, output)
                envelope = delivery_envelope.finalized_path(self.project, render_id)
                output.parent.mkdir(parents=True, exist_ok=True)
                envelope.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(prior_output)
                envelope.write_bytes(prior_envelope)
                observed.update({"output": output, "envelope": envelope})
            return real_render(project_dir, output, quality, *args, **kwargs)

        def mutate_envelope_before_write(_payload, snapshots):
            self.assertEqual(len(snapshots), 1)
            envelope = snapshots[0].envelope_path
            replacement = envelope.with_name(f".{envelope.name}.handoff-race")
            replacement.write_bytes(b"{}\n")
            os.replace(replacement, envelope)

        with patch.object(
            auto_edit,
            "_FINAL_DELIVERY_PRE_WRITE_HOOK",
            new=mutate_envelope_before_write,
        ):
            code, payload, stderr = self._run_cut(
                render_side_effect=seed_prior_then_render
            )

        self.assertIn("output", observed, stderr)
        self.assertEqual(code, 2, stderr + json.dumps(payload, ensure_ascii=False))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["clips"], [])
        self.assertEqual(observed["output"].read_bytes(), prior_output)
        self.assertEqual(observed["envelope"].read_bytes(), prior_envelope)
        self._assert_no_staging_residue()


if __name__ == "__main__":
    unittest.main()
