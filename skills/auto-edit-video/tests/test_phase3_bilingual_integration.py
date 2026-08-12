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
import re
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


UNBREAKABLE = (
    "unbreakabletranslationrunwithnospacesorhyphensanywhereinsideitatallwhatsoever"
)
ASCII_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9._+/-]*|\d+(?:\.\d+)?(?:%|[A-Za-z]+)?")


def _reply(self, translate) -> None:
    """Answer one loopback translation request with `translate(item)`."""
    length = int(self.headers.get("Content-Length", "0"))
    request = json.loads(self.rfile.read(length).decode("utf-8"))
    requested = json.loads(request["messages"][-1]["content"].rsplit("\n", 1)[-1])
    content = {
        "items": [
            {
                "caption_instance_id": item["caption_instance_id"],
                "translated_text": translate(item),
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


def _keeping_source_tokens(source: str, translated: str) -> str:
    """Carry the source's numbers and Latin tokens into the answer.

    Not politeness: `_validate_translations` rejects an answer that dropped
    them, so a fake that drops them never reaches the layout question these
    tests are about.
    """
    kept = " ".join(ASCII_TOKEN_RE.findall(source))
    return f"{translated} {kept}".strip()


def _overlong_then_budgeted_translation(self) -> None:
    """A provider that only shortens when it is told how short.

    First answer: the unbreakable run — one line, wider than any frame, and
    nothing to break it on. Second answer, once a character_budget arrives
    with the request: a line that fits. This is SPEC Phase 3 v1 §3 step 3
    end to end, in a real cut with a real compositor.
    """

    def translate(item):
        budget = item.get("character_budget")
        if budget is None:
            return _keeping_source_tokens(item["source"], UNBREAKABLE)
        shortened = _keeping_source_tokens(item["source"], "a short second line")
        assert len(shortened) <= budget, (len(shortened), budget)
        return shortened

    _reply(self, translate)


REQUESTS: list[bool] = []


def _always_overlong_translation(self) -> None:
    """A provider that ignores the budget, recording whether it was given one.

    SPEC §3 allows one retry, and one is a number that only means something
    if it is counted at the wire: an unbounded loop of "still too long, ask
    again" is exactly what a provider that never complies would produce.
    """

    def translate(item):
        REQUESTS.append("character_budget" in item)
        return _keeping_source_tokens(item["source"], UNBREAKABLE)

    _reply(self, translate)


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

    def test_a_budgeted_retry_saves_a_cut_the_first_translation_would_lose(self) -> None:
        """SPEC §3 step 3: measured budget, one retry, and the cut survives."""
        fixture = phase0e.Phase0eKineticIntegrationTests(
            "test_public_cut_delivers_bilingual_motion_sfx_and_finalized_envelope"
        )
        with patch.object(
            phase0e._OllamaHandler, "do_POST", _overlong_then_budgeted_translation
        ):
            fixture.setUp()
            try:
                code, payload, stderr = fixture._run_cut()
                self.assertEqual(
                    code,
                    0,
                    "the same cut that fails closed on an unbreakable translation "
                    "must succeed once the provider is asked again with a budget: "
                    + stderr
                    + json.dumps(payload, ensure_ascii=False),
                )
                placements = json.loads(
                    (fixture.project / "working/overlay_placements.json").read_text(
                        encoding="utf-8"
                    )
                )
                caption_findings = [
                    finding
                    for finding in placements.get("review", {}).get("safe_area", [])
                    if str(finding.get("overlay", "")).startswith("image caption-")
                ]
                self.assertEqual(
                    caption_findings,
                    [],
                    json.dumps(caption_findings, ensure_ascii=False),
                )
                artifact = json.loads(
                    (fixture.project / "working/caption_delivery_v2.json").read_text(
                        encoding="utf-8"
                    )
                )
                receipt = artifact["provider_receipt"]
                self.assertEqual(receipt["shortening_rounds"], 1)
                self.assertTrue(receipt["shortening_character_budgets"])
                for instance_id, budget in receipt[
                    "shortening_character_budgets"
                ].items():
                    self.assertRegex(instance_id, r"^caption-instance-[0-9a-f]{16}$")
                    self.assertGreater(budget, 0)
                shortened = {item["translated_text"] for item in artifact["items"]}
                self.assertNotIn(UNBREAKABLE, " ".join(shortened))
            finally:
                fixture.tearDown()

    def test_a_provider_that_ignores_the_budget_is_never_asked_a_third_time(self) -> None:
        """SPEC §3: at most one shortening round, counted at the wire."""
        fixture = phase0e.Phase0eKineticIntegrationTests(
            "test_public_cut_delivers_bilingual_motion_sfx_and_finalized_envelope"
        )
        REQUESTS.clear()
        with patch.object(
            phase0e._OllamaHandler, "do_POST", _always_overlong_translation
        ):
            fixture.setUp()
            try:
                _code, _payload, _stderr = fixture._run_cut()
                budgeted = [given for given in REQUESTS if given]
                self.assertTrue(REQUESTS, "the provider was never asked at all")
                self.assertTrue(budgeted, "the overlong translation was never retried")
                self.assertEqual(
                    len(budgeted) * 2,
                    len(REQUESTS),
                    "one shortening round per first round, no more: "
                    + repr(REQUESTS),
                )
                artifact = json.loads(
                    (fixture.project / "working/caption_delivery_v2.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    artifact["provider_receipt"]["shortening_rounds"], 1
                )
            finally:
                fixture.tearDown()
