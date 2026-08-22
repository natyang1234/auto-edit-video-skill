from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from capcut_draft import (  # noqa: E402
    CapcutDraftError,
    Cue,
    build_draft,
    parse_srt,
    remap_cues,
    to_us,
    write_draft,
)

PROBE = {"duration_us": 60_000_000, "width": 1920, "height": 1080, "fps": 30.0}
SOURCE = Path("/tmp/fake-source.mp4")


class ParseSrtTest(unittest.TestCase):
    def test_basic_two_cues(self) -> None:
        srt = (
            "1\n00:00:01,000 --> 00:00:02,500\n第一句\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n第二句 上下行\n合併\n"
        )
        cues = parse_srt(srt)
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 1.0)
        self.assertAlmostEqual(cues[0].end, 2.5)
        self.assertEqual(cues[1].text, "第二句 上下行 合併")

    def test_malformed_blocks_skipped(self) -> None:
        srt = "garbage\n\n1\n00:00:01,000 --> 00:00:02,000\nok\n\nnot a cue\nstill not"
        cues = parse_srt(srt)
        self.assertEqual([c.text for c in cues], ["ok"])

    def test_zero_length_cue_dropped(self) -> None:
        srt = "1\n00:00:01,000 --> 00:00:01,000\nempty\n"
        self.assertEqual(parse_srt(srt), [])


class RemapCuesTest(unittest.TestCase):
    def test_cue_inside_single_segment(self) -> None:
        cues = [Cue(11.0, 12.0, "hi")]
        out = remap_cues(cues, [(10.0, 20.0)])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].start, 1.0)
        self.assertAlmostEqual(out[0].end, 2.0)

    def test_cue_spanning_deleted_gap_splits(self) -> None:
        # 片段 0-5 與 8-13，cue 4-10 橫跨被刪的 5-8 區間
        cues = [Cue(4.0, 10.0, "span")]
        out = remap_cues(cues, [(0.0, 5.0), (8.0, 13.0)])
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0].start, 4.0)
        self.assertAlmostEqual(out[0].end, 5.0)
        self.assertAlmostEqual(out[1].start, 5.0)
        self.assertAlmostEqual(out[1].end, 7.0)

    def test_sliver_dropped(self) -> None:
        cues = [Cue(4.95, 5.5, "sliver")]
        out = remap_cues(cues, [(0.0, 5.0), (8.0, 13.0)])
        self.assertEqual(len(out), 0)

    def test_cue_entirely_in_deleted_region_dropped(self) -> None:
        cues = [Cue(6.0, 7.0, "gone")]
        self.assertEqual(remap_cues(cues, [(0.0, 5.0), (8.0, 13.0)]), [])


class BuildDraftTest(unittest.TestCase):
    def test_video_track_contiguous_and_sources_match(self) -> None:
        segments = [(2.0, 5.0), (10.0, 14.5), (20.0, 21.0)]
        draft = build_draft(SOURCE, PROBE, segments, [], draft_name="t")
        video_track = next(t for t in draft["tracks"] if t["type"] == "video")
        cursor = 0
        for seg, (start_s, end_s) in zip(video_track["segments"], segments):
            self.assertEqual(seg["target_timerange"]["start"], cursor)
            self.assertEqual(seg["source_timerange"]["start"], to_us(start_s))
            self.assertEqual(seg["source_timerange"]["duration"], to_us(end_s - start_s))
            self.assertEqual(seg["target_timerange"]["duration"], seg["source_timerange"]["duration"])
            cursor += seg["target_timerange"]["duration"]
        self.assertEqual(draft["duration"], cursor)

    def test_material_refs_resolve(self) -> None:
        draft = build_draft(SOURCE, PROBE, [(0.0, 3.0)], [Cue(1.0, 2.0, "字")], draft_name="t")
        materials = draft["materials"]
        video_ids = {m["id"] for m in materials["videos"]}
        speed_ids = {m["id"] for m in materials["speeds"]}
        text_ids = {m["id"] for m in materials["texts"]}
        for track in draft["tracks"]:
            for seg in track["segments"]:
                if track["type"] == "video":
                    self.assertIn(seg["material_id"], video_ids)
                    for ref in seg["extra_material_refs"]:
                        self.assertIn(ref, speed_ids)
                elif track["type"] == "text":
                    self.assertIn(seg["material_id"], text_ids)

    def test_caption_clamped_to_timeline_end(self) -> None:
        draft = build_draft(SOURCE, PROBE, [(0.0, 3.0)], [Cue(2.5, 9.0, "尾")], draft_name="t")
        text_track = next(t for t in draft["tracks"] if t["type"] == "text")
        seg = text_track["segments"][0]
        self.assertEqual(seg["target_timerange"]["start"] + seg["target_timerange"]["duration"], to_us(3.0))

    def test_canvas_explicit_and_fallback(self) -> None:
        explicit = build_draft(SOURCE, PROBE, [(0.0, 1.0)], [], canvas=(1080, 1920), draft_name="t")
        self.assertEqual(explicit["canvas_config"]["ratio"], "9:16")
        fallback = build_draft(SOURCE, PROBE, [(0.0, 1.0)], [], draft_name="t")
        self.assertEqual(fallback["canvas_config"]["width"], 1920)
        self.assertEqual(fallback["canvas_config"]["ratio"], "16:9")

    def test_skeleton_version_preserved(self) -> None:
        draft = build_draft(SOURCE, PROBE, [(0.0, 1.0)], [], draft_name="t")
        self.assertEqual(draft["version"], 360000)
        self.assertTrue(draft["id"])
        self.assertEqual(draft["platform"]["device_id"], "")

    def test_text_material_content_is_valid_json(self) -> None:
        draft = build_draft(SOURCE, PROBE, [(0.0, 3.0)], [Cue(0.5, 1.5, "測試字幕")], draft_name="t")
        material = draft["materials"]["texts"][0]
        content = json.loads(material["content"])
        self.assertEqual(content["text"], "測試字幕")
        self.assertEqual(content["styles"][0]["range"], [0, 4])
        self.assertEqual(material["type"], "subtitle")

    def test_invalid_segments_raise(self) -> None:
        with self.assertRaises(CapcutDraftError):
            build_draft(SOURCE, PROBE, [], [], draft_name="t")
        with self.assertRaises(CapcutDraftError):
            build_draft(SOURCE, PROBE, [(5.0, 5.0)], [], draft_name="t")
        with self.assertRaises(CapcutDraftError):
            build_draft(SOURCE, PROBE, [(-1.0, 2.0)], [], draft_name="t")


class WriteDraftTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.draft = build_draft(SOURCE, PROBE, [(0.0, 2.0)], [], draft_name="t")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_writes_mirrors_byte_identical(self) -> None:
        target = write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)
        info = (target / "draft_info.json").read_bytes()
        self.assertEqual(info, (target / "template-2.tmp").read_bytes())
        self.assertEqual(info, (target / "draft_content.json").read_bytes())
        meta = json.loads((target / "draft_meta_info.json").read_text())
        self.assertEqual(meta["draft_name"], "proj")
        self.assertEqual(meta["draft_fold_path"], str(target))
        written = json.loads(info)
        self.assertEqual(written["name"], "proj")
        self.assertEqual(written["path"], str(target))
        self.assertTrue((target / "Resources").is_dir())

    def test_refuses_existing_without_force(self) -> None:
        write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)
        with self.assertRaises(CapcutDraftError):
            write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)

    def test_force_backs_up_existing(self) -> None:
        first = write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)
        marker = first / "marker.txt"
        marker.write_text("keep me")
        write_draft(self.draft, "proj", draft_root=self.root, force=True, copy_media=False)
        trash = self.root / ".pre-export-backups"
        backups = list(trash.iterdir())
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "marker.txt").is_file())
        # 備份必須藏在點資料夾裡，否則 CapCut 會把它列成草稿
        visible = [p for p in self.root.iterdir() if not p.name.startswith(".")]
        self.assertEqual({p.name for p in visible}, {"proj"})

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(CapcutDraftError):
            write_draft(self.draft, "proj", draft_root=self.root / "nope", copy_media=False)


    def test_copy_media_places_source_inside_draft(self) -> None:
        real_source = self.root / "clip.mp4"
        real_source.write_bytes(b"fake-bytes")
        draft = build_draft(real_source, PROBE, [(0.0, 2.0)], [], draft_name="t")
        target = write_draft(draft, "proj-media", draft_root=self.root)
        written = json.loads((target / "draft_info.json").read_text())
        material_path = Path(written["materials"]["videos"][0]["path"])
        self.assertTrue(str(material_path).startswith(str(target)))
        self.assertTrue(material_path.is_file())
        self.assertEqual(material_path.read_bytes(), b"fake-bytes")
        # 原始 draft dict 不得被就地改動
        self.assertEqual(draft["materials"]["videos"][0]["path"], str(real_source))

    def test_copy_media_missing_source_raises(self) -> None:
        with self.assertRaises(CapcutDraftError):
            write_draft(self.draft, "proj-x", draft_root=self.root)

    def test_running_capcut_blocks_write_inside_capcut_tree(self) -> None:
        with patch("capcut_draft._root_needs_running_check", return_value=True):
            with patch("capcut_draft.capcut_is_running", return_value=True):
                with self.assertRaises(CapcutDraftError):
                    write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)

    def test_running_capcut_ignored_outside_capcut_tree(self) -> None:
        with patch("capcut_draft.capcut_is_running", return_value=True):
            target = write_draft(self.draft, "proj", draft_root=self.root, copy_media=False)
        self.assertTrue(target.is_dir())

    def test_draft_name_traversal_rejected(self) -> None:
        for bad in ("", ".", "..", "../evil", "a/b", ".hidden", "/abs"):
            with self.assertRaises(CapcutDraftError, msg=f"name {bad!r} should be rejected"):
                write_draft(self.draft, bad, draft_root=self.root, copy_media=False)
        # 確認沒有任何東西被寫到 root 外或 root 內
        self.assertEqual([p.name for p in self.root.iterdir()], [])

    def test_srt_short_millisecond_fields_are_fractions(self) -> None:
        cues = parse_srt("1\n00:00:01,5 --> 00:00:02,50\nok\n")
        self.assertEqual(len(cues), 1)
        self.assertAlmostEqual(cues[0].start, 1.5)
        self.assertAlmostEqual(cues[0].end, 2.5)

    def test_srt_invalid_minute_second_skipped(self) -> None:
        self.assertEqual(parse_srt("1\n00:61:01,000 --> 00:61:02,000\nbad\n"), [])


if __name__ == "__main__":
    unittest.main()
