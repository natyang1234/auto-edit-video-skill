"""Phase 3: a real bilingual public cut must reject unsafe captions.

A translation that is merely wide narrows its wrap and, if that still
leaves it too tall, moves up to clear the platform's own reserved margin
(see `constrain_caption_wrap_to_safe_area` / `clamp_captions_into_safe_area`
in render_editor_timeline.py) — both are legitimate fixes, not rejections.
What neither can save is one unbroken run with no space or hyphen to wrap
at: no line width holds it and no shift moves it inside the frame, so it is
the one shape of translation that must still make the cut fail closed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch


TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

import test_phase0e_kinetic_integration as phase0e  # noqa: E402


def _long_translation(self) -> None:
    """Loopback provider response that is valid text with no break point:
    one run of letters, no space or hyphen anywhere in it, long enough that
    no line width holds it and no amount of shrinking brings it inside the
    frame."""
    length = int(self.headers.get("Content-Length", "0"))
    request = json.loads(self.rfile.read(length).decode("utf-8"))
    requested = json.loads(request["messages"][-1]["content"].rsplit("\n", 1)[-1])
    translated = "unbreakabletranslationrunwithnospacesorhyphensanywhereinsideitatallwhatsoever"
    content = {
        "items": [
            {
                "caption_instance_id": item["caption_instance_id"],
                "translated_text": translated,
            }
            for item in requested
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


class Phase3BilingualPublicCutRedTests(phase0e.unittest.TestCase):
    """Reuse one Phase 0e fixture without inheriting its complete test suite."""

    def test_unbreakable_english_caption_must_block_unsafe_instagram_public_cut(self) -> None:
        fixture = phase0e.Phase0eKineticIntegrationTests(
            "test_public_cut_delivers_bilingual_motion_sfx_and_finalized_envelope"
        )
        with patch.object(phase0e._OllamaHandler, "do_POST", _long_translation):
            fixture.setUp()
            try:
                code, payload, stderr = fixture._run_cut()
                if code == 0:
                    placements = json.loads(
                        (fixture.project / "working/overlay_placements.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    review = placements["review"]
                    self.assertTrue(
                        review["safe_area"],
                        stderr + json.dumps(review, ensure_ascii=False),
                    )
                    self.assertTrue(
                        any(
                            finding["overlay"].startswith("image caption-")
                            for finding in review["safe_area"]
                        ),
                        json.dumps(review["safe_area"], ensure_ascii=False),
                    )
                self.assertNotEqual(
                    code,
                    0,
                    "a public kinetic-explainer cut accepted a translated caption "
                    "inside Instagram's reserved safe area: "
                    + stderr
                    + json.dumps(payload, ensure_ascii=False),
                )
            finally:
                fixture.tearDown()
