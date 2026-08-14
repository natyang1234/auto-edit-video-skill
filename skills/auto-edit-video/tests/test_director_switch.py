"""Switching the Studio director must swap the whole editing language.

The evidence for these tests is a copy of a real Studio project (imported with
``teacher-punch``): its manifest, transcript and reviewed plan artifacts are
verbatim, only the staged media is replaced by a placeholder file because the
tests never decode it.  Switching directors is allowed to regenerate derived
cards, animations and copy; it is never allowed to move the approved cut or to
rewrite one transcript-grounded caption.
"""

from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
FIXTURE_PROJECT = Path(__file__).resolve().parent / "fixtures/nat_studio_project"
sys.path.insert(0, str(SCRIPTS_DIR))

import director_resolver  # noqa: E402
import editor_server  # noqa: E402
from editor_server import EditorServer  # noqa: E402


TEACHER_KICKERS = {"30 秒重點課", "先看概念", "核心規則", "記憶技巧", "一秒複習"}
DERIVED_TYPES = {"title", "card", "animation"}


def caption_fingerprint(state: dict) -> list[tuple[str, float, float]]:
    return [
        (str(overlay["text"]), float(overlay["start"]), float(overlay["end"]))
        for overlay in state["overlays"]
        if overlay["type"] == "caption"
    ]


def derived_overlays(state: dict) -> list[dict]:
    return [overlay for overlay in state["overlays"] if overlay["type"] in DERIVED_TYPES]


class DirectorSwitchTests(unittest.TestCase):
    """A director switch is a re-edit of derived content, not a restyle."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="auto-edit-director-switch-")
        self.project = Path(self._tmp.name) / "project"
        shutil.copytree(FIXTURE_PROJECT, self.project)
        for name in ("source", "assets", "renders", "qa"):
            (self.project / name).mkdir(parents=True, exist_ok=True)
        (self.project / "source/original.mov").write_bytes(b"0123456789abcdef")
        manifest = self.read_json("project.json")
        manifest["project_dir"] = str(self.project)
        self.write_json("project.json", manifest)
        # The imported project selected its highlight from the real transcript
        # window; keep it so the designed cards exist to be regenerated.
        plan = self.read_json("working/highlight_plan.json")
        plan["items"] = [
            {
                "id": "highlight-0001",
                "start": 0.0,
                "end": 11.34,
                "title": "我總不能過太久再說一遍",
                "review_status": "pending",
                "score": 0.8,
            }
        ]
        self.write_json("working/highlight_plan.json", plan)
        (self.project / "working/editor_state.json").unlink()
        self.server = EditorServer(("127.0.0.1", 0), self.project)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host, self.port = self.server.server_address

    def read_json(self, relative: str) -> dict:
        return json.loads((self.project / relative).read_text(encoding="utf-8"))

    def write_json(self, relative: str, payload: object) -> None:
        (self.project / relative).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        headers = {"Content-Type": "application/json"}
        if method.upper() not in {"GET", "HEAD"}:
            headers["X-Auto-Edit-CSRF"] = self.server.csrf_token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        status = response.status
        connection.close()
        return status, payload

    def json_request(self, method: str, path: str, payload: object) -> tuple[int, dict]:
        status, body = self.request(
            method, path, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        return status, json.loads(body.decode("utf-8"))

    def bootstrap(self) -> dict:
        status, body = self.request("GET", "/api/project")
        self.assertEqual(status, 200, body)
        return json.loads(body.decode("utf-8"))

    def switch(self, director: str, revision: str) -> tuple[int, dict]:
        return self.json_request(
            "POST",
            "/api/director",
            {"director": director, "expected_revision": revision},
        )

    def test_imported_project_starts_in_the_teaching_editing_language(self) -> None:
        payload = self.bootstrap()
        state = payload["state"]
        self.assertEqual(state["director_style"], "teacher-punch")
        cards = derived_overlays(state)
        self.assertEqual(len(cards), 5)
        self.assertEqual({card["kicker"] for card in cards}, TEACHER_KICKERS)

    def test_switching_to_minimal_regenerates_cards_copy_and_motion(self) -> None:
        before = self.bootstrap()["state"]
        before_captions = caption_fingerprint(before)
        before_segments = json.dumps(before["segments"], ensure_ascii=False, sort_keys=True)

        status, payload = self.switch("minimal", before["revision"])
        self.assertEqual(status, 200, payload)
        after = payload["state"]

        self.assertEqual(after["director_style"], "minimal")
        cards = derived_overlays(after)
        self.assertEqual(len(cards), 5)
        kickers = {card["kicker"] for card in cards}
        self.assertFalse(
            kickers & TEACHER_KICKERS,
            f"teaching card copy survived the director switch: {sorted(kickers)}",
        )
        # ``minimal`` is a low-motion profile: no card may keep a punchy entry.
        self.assertEqual(
            {card["style"]["animation"] for card in cards},
            {"fade"},
        )
        self.assertEqual(
            after["caption_defaults"]["font_size"],
            payload["director_presets"]["minimal"]["caption"]["font_size"]
            if "director_presets" in payload
            else editor_server.DIRECTOR_PRESETS["minimal"]["caption"]["font_size"],
        )

        # The approved cut and the transcript-grounded captions are untouched.
        self.assertEqual(caption_fingerprint(after), before_captions)
        self.assertEqual(
            json.dumps(after["segments"], ensure_ascii=False, sort_keys=True),
            before_segments,
        )
        self.assertEqual(
            [item["id"] for item in after["highlights"]],
            [item["id"] for item in before["highlights"]],
        )

        persisted = self.read_json("working/editor_state.json")
        self.assertEqual(persisted["director_style"], "minimal")
        self.assertEqual(persisted["revision"], after["revision"])
        self.assertNotEqual(after["revision"], before["revision"])
        # Derived plan artifacts follow the regenerated cards.
        plan = self.read_json("working/highlight_visual_plan.json")
        self.assertEqual(
            {item["kicker"] for item in plan["items"]},
            kickers,
        )
        # The selection goes through the canonical resolver, not a state field.
        selection = self.read_json("working/director_selection_request.json")
        resolved = self.read_json("working/resolved_director_profile.json")
        self.assertEqual(selection["profile_id"], "minimal")
        self.assertEqual(resolved["profile_id"], "minimal")
        self.assertEqual(
            selection["resolved_profile_hash"],
            director_resolver.resolve_director_profile("minimal")["resolved_hash"],
        )

    def test_switch_round_trip_restores_the_teaching_cards(self) -> None:
        original = self.bootstrap()["state"]
        original_cards = derived_overlays(original)

        status, payload = self.switch("minimal", original["revision"])
        self.assertEqual(status, 200, payload)
        status, payload = self.switch("teacher-punch", payload["state"]["revision"])
        self.assertEqual(status, 200, payload)
        restored = payload["state"]

        self.assertEqual(restored["director_style"], "teacher-punch")
        self.assertEqual(derived_overlays(restored), original_cards)
        self.assertEqual(caption_fingerprint(restored), caption_fingerprint(original))
        self.assertEqual(
            self.read_json("working/director_selection_request.json")["profile_id"],
            "teacher-punch",
        )

    def test_unavailable_director_is_refused_without_touching_the_project(self) -> None:
        before = self.bootstrap()["state"]
        before_state_bytes = (self.project / "working/editor_state.json").read_bytes()
        before_selection = (self.project / "working/director_selection_request.json").read_bytes()
        capabilities = director_resolver.IMPLEMENTED_CAPABILITIES - {"caption-delivery-v2"}
        with patch.object(director_resolver, "IMPLEMENTED_CAPABILITIES", capabilities):
            status, payload = self.switch("kinetic-explainer", before["revision"])
        self.assertEqual(status, 422, payload)
        self.assertIn("caption-delivery-v2", payload.get("missing_capabilities", []))
        self.assertEqual(
            (self.project / "working/editor_state.json").read_bytes(), before_state_bytes
        )
        self.assertEqual(
            (self.project / "working/director_selection_request.json").read_bytes(),
            before_selection,
        )
        self.assertEqual(self.bootstrap()["state"]["revision"], before["revision"])

    def test_unknown_director_is_refused(self) -> None:
        before = self.bootstrap()["state"]
        status, payload = self.switch("no-such-director", before["revision"])
        self.assertEqual(status, 422, payload)
        self.assertEqual(self.bootstrap()["state"]["director_style"], "teacher-punch")

    def test_stale_revision_is_refused_so_nothing_regenerates_behind_an_edit(self) -> None:
        before = self.bootstrap()["state"]
        status, payload = self.switch("minimal", "0" * 64)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["current_revision"], before["revision"])
        self.assertEqual(self.bootstrap()["state"]["director_style"], "teacher-punch")

    def test_no_switch_request_changes_nothing(self) -> None:
        """The cancel path in the panel: never call the endpoint, never mutate."""
        self.bootstrap()
        before = (self.project / "working/editor_state.json").read_bytes()
        self.bootstrap()
        self.assertEqual((self.project / "working/editor_state.json").read_bytes(), before)

    def test_switch_invalidates_approvals_bound_to_the_old_derived_content(self) -> None:
        before = self.bootstrap()
        status, payload = self.switch("minimal", before["state"]["revision"])
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(
            payload["approval_revisions"]["timeline"],
            before["approval_revisions"]["timeline"],
        )


class DirectorSwitchPanelTests(unittest.TestCase):
    """The panel confirms, calls the endpoint, and reverts on cancel."""

    def setUp(self) -> None:
        self.script = (SKILL_DIR / "editor/app.js").read_text(encoding="utf-8")

    def test_director_change_confirms_before_regenerating(self) -> None:
        handler = self.script.split('elements["director-select"].addEventListener("change"', 1)
        self.assertEqual(len(handler), 2, "director change handler missing")
        body = handler[1].split("\n  });", 1)[0]
        self.assertIn("confirm(", body)
        self.assertIn('request("/api/director"', body)
        self.assertLess(body.index("confirm("), body.index('request("/api/director"'))

    def test_cancelling_reverts_the_select_and_skips_the_request(self) -> None:
        body = self.script.split('elements["director-select"].addEventListener("change"', 1)[1]
        body = body.split("\n  });", 1)[0]
        cancel = body.index("confirm(")
        revert = body.index('elements["director-select"].value = state.director_style', cancel)
        self.assertLess(revert, body.index('request("/api/director"'))
