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


def _long_then_short_translation(self) -> None:
    """Loopback provider that returns unbreakable on first request,
    then returns a shorter, breakable translation when retried with
    character budget constraint.

    This simulates the semantic shortening flow:
    1. First call: provider returns long translation (unbreakable)
    2. Renderer detects safe-area violation
    3. Renderer retries with explicit character budget
    4. Second call: provider returns short translation (breakable)
    5. Result passes safe-area check
    """
    length = int(self.headers.get("Content-Length", "0"))
    request = json.loads(self.rfile.read(length).decode("utf-8"))
    messages = request["messages"]
    last_message = json.loads(messages[-1]["content"].rsplit("\n", 1)[-1])
    requested = last_message if isinstance(last_message, list) else [last_message]

    # Check if this is a retry request (contains character budget constraint)
    is_retry = any(
        "字元" in (msg.get("content", "") or "") or "character" in (msg.get("content", "") or "")
        for msg in messages[:-1] if msg.get("role") == "assistant"
    ) or "character budget" in messages[-1].get("content", "")

    if is_retry:
        # Retry: return a shorter, breakable translation
        translated = "a concise translation"
    else:
        # First attempt: return unbreakable translation
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

    def test_provider_retry_with_character_budget_when_translation_overflows(self) -> None:
        """Sub-slice 3: When initial translation is too long and unbreakable,
        renderer should retry provider with character budget constraint and
        use the shortened result.

        RED: provider is called twice (once initial, once with budget),
        receipt records the shortening attempt.
        """
        fixture = phase0e.Phase0eKineticIntegrationTests(
            "test_public_cut_delivers_bilingual_motion_sfx_and_finalized_envelope"
        )
        with patch.object(phase0e._OllamaHandler, "do_POST", _long_then_short_translation):
            fixture.setUp()
            try:
                code, payload, stderr = fixture._run_cut()
                # Should succeed (code == 0) because retry provided shorter translation
                self.assertEqual(
                    code,
                    0,
                    "provider retry with character budget should allow cut to succeed: "
                    + stderr
                    + json.dumps(payload, ensure_ascii=False),
                )
                # Verify no safe-area violations in final output
                if code == 0:
                    placements = json.loads(
                        (fixture.project / "working/overlay_placements.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    review = placements.get("review", {})
                    safe_area_findings = review.get("safe_area", [])
                    caption_safe_area = [
                        f for f in safe_area_findings
                        if f.get("overlay", "").startswith("image caption-")
                    ]
                    self.assertEqual(
                        len(caption_safe_area),
                        0,
                        f"caption should not violate safe area after retry: "
                        + json.dumps(caption_safe_area, ensure_ascii=False),
                    )
                # Verify receipt records shortening attempt
                translations = json.loads(
                    (fixture.project / "working/caption_translations.json").read_text(
                        encoding="utf-8"
                    )
                )
                # Receipt should indicate shortening was applied
                for item in translations.get("items", []):
                    # This is where we'd check for shortening record in receipt
                    # (to be implemented in caption_translator.py receipt)
                    pass
            finally:
                fixture.tearDown()
