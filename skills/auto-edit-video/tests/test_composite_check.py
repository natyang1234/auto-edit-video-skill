import sys
import hashlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import composite_check


def _time_map():
    return [
        {"source_start_s": 0.0, "source_end_s": 2.0, "output_start_s": 0.0, "output_end_s": 2.0},
        {"source_start_s": 4.0, "source_end_s": 6.0, "output_start_s": 2.0, "output_end_s": 4.0},
    ]


def test_short_sampling_is_interior_and_bounded():
    samples = composite_check.sample_output_timestamps(2.0)
    assert len(samples) == 5
    assert samples == sorted(set(samples))
    assert all(0.0 < value < 2.0 for value in samples)


def test_long_sampling_is_capped_at_eight():
    samples = composite_check.sample_output_timestamps(4.0)
    assert len(samples) == 8
    assert all(0.0 < value < 4.0 for value in samples)


def test_deleted_source_gap_uses_next_half_open_segment():
    mapping = _time_map()
    assert composite_check.map_output_to_source(mapping, 1.999) == pytest.approx(1.999)
    assert composite_check.map_output_to_source(mapping, 2.0) == pytest.approx(4.0)
    assert composite_check.map_output_to_source(mapping, 3.5) == pytest.approx(5.5)
    assert composite_check.map_output_to_source(mapping, 4.0) is None


def test_time_map_allows_source_gap_but_rejects_output_gap_and_overlap():
    assert composite_check.validate_time_map(_time_map())[-1]["source_start_s"] == 4.0
    with pytest.raises(ValueError):
        composite_check.validate_time_map(
            [
                {"source_start_s": 0, "source_end_s": 1, "output_start_s": 0, "output_end_s": 1},
                {"source_start_s": 2, "source_end_s": 3, "output_start_s": 1.2, "output_end_s": 2.2},
            ]
        )
    with pytest.raises(ValueError):
        composite_check.validate_time_map(
            [
                {"source_start_s": 0, "source_end_s": 2, "output_start_s": 0, "output_end_s": 2},
                {"source_start_s": 1, "source_end_s": 3, "output_start_s": 2, "output_end_s": 4},
            ]
        )


def test_geometry_uses_intersection_over_smaller_and_iou():
    subject = {"x": 10, "y": 10, "width": 100, "height": 100}
    text = {"x": 90, "y": 90, "width": 100, "height": 20}
    assert composite_check.rect_intersection_fraction(subject, text) == pytest.approx(0.2)
    assert composite_check.rect_iou(subject, subject) == pytest.approx(1.0)
    assert composite_check.rect_iou(subject, {"x": 500, "y": 500, "width": 20, "height": 20}) == 0.0
    assert composite_check.rect_intersection_fraction(subject, {"x": 0, "y": 0, "width": 0, "height": 2}) == 0.0


def test_single_effect_is_explicitly_unmeasured_without_private_inputs(tmp_path):
    receipt = composite_check.analyze(
        {
            "composite": tmp_path / "composite.mp4",
            "reference": None,
            "source": tmp_path / "source.mp4",
            "time_map": _time_map(),
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path,
            "source_sha256": "0" * 64,
            "expected_text": True,
            "scope": "single-effect",
        }
    )
    assert receipt["scope"] == "single-effect"
    assert receipt["measurement"] == "sampled-local-frames"
    assert receipt["status"] == "unmeasured"
    assert {item["kind"] for item in receipt["findings"]} >= {"full_timeline_required"}
    assert "composite.mp4" not in str(receipt)
    assert "source.mp4" not in str(receipt)


def test_invalid_request_returns_bounded_unknown_receipt():
    receipt = composite_check.analyze(None)  # type: ignore[arg-type]
    assert receipt["status"] == "unmeasured"
    assert receipt["sample_count"] == 0
    assert receipt["measured_count"] == 0
    assert receipt["frame_size"]["width"] > 0
    assert receipt["frame_size"]["height"] > 0
    assert receipt["findings"] == [{"kind": "analysis_incomplete"}]


def test_parent_verified_source_hash_is_echoed_without_rehashing_source(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    composite = tmp_path / "composite.mp4"
    source.write_bytes(b"ordinary source fixture")
    composite.write_bytes(b"ordinary composite fixture")
    verified_source_sha = "a" * 64
    hash_calls = []
    original_hash_file = composite_check._hash_file

    def spy_hash_file(path):
        hash_calls.append(path)
        return original_hash_file(path)

    monkeypatch.setattr(composite_check, "_hash_file", spy_hash_file)
    receipt = composite_check.analyze(
        {
            "composite": composite,
            "reference": None,
            "source": source,
            "time_map": _time_map(),
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path,
            "source_sha256": verified_source_sha,
            "expected_text": True,
            "scope": "single-effect",
        }
    )

    assert hash_calls == [composite]
    assert receipt["source_sha256"] == verified_source_sha
    assert receipt["media_sha256"] == original_hash_file(composite)
    assert "source.mp4" not in str(receipt)


@pytest.mark.parametrize("source_sha256", [None, "A" * 64, "not-a-sha256"])
def test_invalid_parent_source_hash_cannot_become_a_valid_binding(
    tmp_path, monkeypatch, source_sha256
):
    composite = tmp_path / "composite.mp4"
    composite.write_bytes(b"ordinary composite fixture")
    hash_calls = []
    original_hash_file = composite_check._hash_file

    def spy_hash_file(path):
        hash_calls.append(path)
        return original_hash_file(path)

    monkeypatch.setattr(composite_check, "_hash_file", spy_hash_file)
    receipt = composite_check.analyze(
        {
            "composite": composite,
            "reference": None,
            "source": tmp_path / "source.mp4",
            "time_map": _time_map(),
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path,
            "source_sha256": source_sha256,
            "expected_text": True,
            "scope": "single-effect",
        }
    )

    assert hash_calls == [composite]
    assert receipt["source_sha256"] == "0" * 64
    assert receipt["status"] == "unmeasured"
    assert {item["kind"] for item in receipt["findings"]} >= {
        "analysis_incomplete",
        "full_timeline_required",
    }


def test_expected_text_is_conservative_unless_explicitly_false():
    assert composite_check._expected_text_at(True, 0) is True
    assert composite_check._expected_text_at([False, True], 0) is False
    assert composite_check._expected_text_at([False], 1) is True
    assert composite_check._expected_text_at(None, 0) is True


def test_reference_text_overlap_is_reported_even_when_final_face_is_covered(tmp_path, monkeypatch):
    class _Frame:
        def __init__(self, kind):
            self.kind = kind
            self.shape = (100, 100, 3)

    class _Reader:
        def __init__(self, path, _cv2):
            self.kind = Path(path).stem
            self.width = 100
            self.height = 100

        def read(self, _timestamp):
            return _Frame(self.kind)

        def close(self):
            pass

    class _Detector:
        failed = False

        def __init__(self, _model, _cv2):
            pass

        def detect(self, frame):
            face = {"x": 20, "y": 20, "width": 40, "height": 40}
            if frame.kind in {"source", "reference"}:
                return True, [face]
            return True, []

    class _CV2:
        @staticmethod
        def imwrite(_path, _frame):
            return True

    for name in ("source", "reference", "composite"):
        (tmp_path / f"{name}.mp4").write_bytes(name.encode())
    monkeypatch.setattr(composite_check, "_load_cv2", lambda: _CV2())
    monkeypatch.setattr(composite_check, "_VideoReader", _Reader)
    monkeypatch.setattr(composite_check, "_YuNetProbe", _Detector)
    monkeypatch.setattr(
        composite_check,
        "_vision_ocr_boxes",
        lambda _path, _width, _height: ("ok", [{"x": 25, "y": 25, "width": 20, "height": 20}]),
    )
    import hashlib

    source = tmp_path / "source.mp4"
    receipt = composite_check.analyze(
        {
            "composite": tmp_path / "composite.mp4",
            "reference": tmp_path / "reference.mp4",
            "source": source,
            "time_map": [{"source_start_s": 0, "source_end_s": 1, "output_start_s": 0, "output_end_s": 1}],
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path / "scratch",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "expected_text": True,
            "scope": "full-timeline",
        }
    )
    kinds = {finding["kind"] for finding in receipt["findings"]}
    assert "text_subject_overlap" in kinds
    assert "visibility_uncertain" in kinds
    assert receipt["status"] == "unmeasured"


def _fake_overlap_receipt(tmp_path, monkeypatch, mode):
    class _Frame:
        shape = (100, 100, 3)

    class _Reader:
        width = 100
        height = 100

        def __init__(self, _path, _cv2):
            pass

        def read(self, _timestamp):
            return _Frame()

        def close(self):
            pass

    class _Detector:
        failed = False

        def __init__(self, _model, _cv2):
            pass

        def detect(self, _frame):
            if mode == "capacity":
                return True, [
                    {"x": 5, "y": 20, "width": 15, "height": 20},
                    {"x": 25, "y": 20, "width": 15, "height": 20},
                    {"x": 45, "y": 20, "width": 15, "height": 20},
                    {"x": 65, "y": 20, "width": 15, "height": 20},
                    {"x": 85, "y": 20, "width": 10, "height": 20},
                ]
            if mode == "overflow":
                return True, [
                    {"x": 5, "y": 20, "width": 15, "height": 20},
                    {"x": 30, "y": 20, "width": 15, "height": 20},
                    {"x": 55, "y": 20, "width": 15, "height": 20},
                    {"x": 80, "y": 20, "width": 15, "height": 20},
                ]
            return True, [{"x": 20, "y": 20, "width": 40, "height": 40}]

    class _CV2:
        @staticmethod
        def imwrite(_path, _frame):
            return True

    if mode == "capacity":
        text_boxes = [
            {"x": 5, "y": 20, "width": 10, "height": 10},
            {"x": 25, "y": 20, "width": 10, "height": 10},
            {"x": 45, "y": 20, "width": 10, "height": 10},
            {"x": 65, "y": 20, "width": 10, "height": 10},
            {"x": 85, "y": 20, "width": 8, "height": 10},
        ]
    elif mode == "overflow":
        text_boxes = [
            {"x": 5, "y": 20, "width": 10, "height": 10},
            {"x": 30, "y": 20, "width": 10, "height": 10},
            {"x": 55, "y": 20, "width": 10, "height": 10},
            {"x": 80, "y": 20, "width": 10, "height": 10},
        ]
    else:
        text_boxes = [
            {"x": 0, "y": 20, "width": 30, "height": 30},
            {"x": 10, "y": 25, "width": 35, "height": 25},
            {"x": 25, "y": 25, "width": 30, "height": 30},
            {"x": 40, "y": 30, "width": 40, "height": 20},
        ]
    source = tmp_path / "source.mp4"
    composite = tmp_path / "composite.mp4"
    reference = tmp_path / "reference.mp4"
    source.write_bytes(b"ordinary source fixture")
    composite.write_bytes(b"ordinary composite fixture")
    reference.write_bytes(b"ordinary reference fixture")
    monkeypatch.setattr(composite_check, "_load_cv2", lambda: _CV2())
    monkeypatch.setattr(composite_check, "_VideoReader", _Reader)
    monkeypatch.setattr(composite_check, "_YuNetProbe", _Detector)
    monkeypatch.setattr(
        composite_check,
        "_vision_ocr_boxes",
        lambda _path, _width, _height: ("ok", text_boxes),
    )
    return composite_check.analyze(
        {
            "composite": composite,
            "reference": reference,
            "source": source,
            "time_map": [{
                "source_start_s": 0.0,
                "source_end_s": 4.0,
                "output_start_s": 0.0,
                "output_end_s": 4.0,
            }],
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path / "scratch",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "expected_text": True,
            "scope": "full-timeline",
        }
    )


def test_many_overlap_lines_keep_one_strongest_box_per_face_and_sample(tmp_path, monkeypatch):
    receipt = _fake_overlap_receipt(tmp_path, monkeypatch, "warn")
    assert receipt["status"] == "warn"
    assert receipt["sample_count"] == 8
    assert receipt["measured_count"] == 8
    assert receipt["engines"] == {"faces": "opencv-yunet", "ocr": "macos-vision"}
    assert all(
        sample["source_face_count"] == 1
        and sample["reference_face_count"] == 1
        and sample["visible_face_count"] == 1
        and sample["text_box_count"] == 4
        and sample["measured"]
        for sample in receipt["samples"]
    )
    overlaps = [
        finding
        for finding in receipt["findings"]
        if finding["kind"] == "text_subject_overlap"
    ]
    assert len(overlaps) == 8
    assert all(overlap["text_box"] == {"x": 25.0, "y": 25.0, "width": 30.0, "height": 30.0} for overlap in overlaps)
    assert all(finding["kind"] == "text_subject_overlap" for finding in receipt["findings"])


def test_32_findings_fit_capacity_and_remain_warn(tmp_path, monkeypatch):
    receipt = _fake_overlap_receipt(tmp_path, monkeypatch, "overflow")
    assert receipt["status"] == "warn"
    assert receipt["sample_count"] == 8
    assert receipt["measured_count"] == 8
    assert all(sample["reference_face_count"] == 4 for sample in receipt["samples"])
    assert sum(item["kind"] == "text_subject_overlap" for item in receipt["findings"]) == 32
    assert all(item["kind"] == "text_subject_overlap" for item in receipt["findings"])


def test_33rd_finding_still_marks_real_capacity_overflow(tmp_path, monkeypatch):
    receipt = _fake_overlap_receipt(tmp_path, monkeypatch, "capacity")
    assert receipt["status"] == "unmeasured"
    assert receipt["sample_count"] == 8
    assert receipt["measured_count"] == 8
    assert all(sample["reference_face_count"] == 5 for sample in receipt["samples"])
    assert sum(item["kind"] == "text_subject_overlap" for item in receipt["findings"]) == 31
    assert receipt["findings"][-1] == {"kind": "analysis_incomplete"}


def test_finding_collector_capacity_and_finalize_are_idempotent():
    collector = composite_check._FindingCollector()
    finding = {"kind": "analysis_incomplete"}
    for _ in range(composite_check.MAX_FINDINGS):
        collector.add(finding)
    exact = collector.finalize()
    assert len(exact) == composite_check.MAX_FINDINGS
    assert all(item == finding for item in exact)
    collector.add(finding)
    overflow = collector.finalize()
    assert overflow == collector.finalize()
    assert len(overflow) == composite_check.MAX_FINDINGS
    assert overflow[-1] == finding


def _fake_capability_receipt(
    tmp_path,
    monkeypatch,
    *,
    cv2_available=True,
    detector_failed=False,
    ocr_results=(("empty", []),),
    expected_text=False,
):
    class _Frame:
        shape = (100, 100, 3)

    class _Reader:
        width = 100
        height = 100

        def __init__(self, _path, _cv2):
            pass

        def read(self, _timestamp):
            return _Frame()

        def close(self):
            pass

    class _Detector:
        failed = detector_failed

        def __init__(self, _model, _cv2):
            pass

        def detect(self, _frame):
            if self.failed:
                return False, []
            return True, [{"x": 20, "y": 20, "width": 40, "height": 40}]

    class _CV2:
        @staticmethod
        def imwrite(_path, _frame):
            return True

    source = tmp_path / "source.mp4"
    composite = tmp_path / "composite.mp4"
    reference = tmp_path / "reference.mp4"
    source.write_bytes(b"ordinary source fixture")
    composite.write_bytes(b"ordinary composite fixture")
    reference.write_bytes(b"ordinary reference fixture")
    monkeypatch.setattr(composite_check, "_load_cv2", lambda: _CV2() if cv2_available else None)
    monkeypatch.setattr(composite_check, "_VideoReader", _Reader)
    monkeypatch.setattr(composite_check, "_YuNetProbe", _Detector)
    sequence = list(ocr_results)
    calls = []

    def fake_ocr(_path, _width, _height):
        index = min(len(calls), len(sequence) - 1)
        calls.append(index)
        return sequence[index]

    monkeypatch.setattr(composite_check, "_vision_ocr_boxes", fake_ocr)
    return composite_check.analyze(
        {
            "composite": composite,
            "reference": reference,
            "source": source,
            "time_map": [{
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "output_start_s": 0.0,
                "output_end_s": 1.0,
            }],
            "face_model": tmp_path / "face.onnx",
            "scratch": tmp_path / "scratch",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "expected_text": expected_text,
            "scope": "full-timeline",
        }
    )


def test_engine_receipt_reports_unavailable_capabilities_without_cv2(tmp_path, monkeypatch):
    receipt = _fake_capability_receipt(tmp_path, monkeypatch, cv2_available=False)
    assert receipt["engines"] == {"faces": "unavailable", "ocr": "unavailable"}
    assert receipt["status"] == "unmeasured"


def test_engine_receipt_marks_detector_failure_but_ocr_empty_call_usable(tmp_path, monkeypatch):
    receipt = _fake_capability_receipt(
        tmp_path,
        monkeypatch,
        detector_failed=True,
        ocr_results=(("empty", []),),
        expected_text=False,
    )
    assert receipt["engines"] == {"faces": "unavailable", "ocr": "macos-vision"}
    assert receipt["status"] == "unmeasured"


def test_engine_receipt_keeps_faces_named_when_ocr_never_succeeds(tmp_path, monkeypatch):
    receipt = _fake_capability_receipt(
        tmp_path,
        monkeypatch,
        ocr_results=(("failed", []),),
        expected_text=False,
    )
    assert receipt["engines"] == {"faces": "opencv-yunet", "ocr": "unavailable"}
    assert receipt["status"] == "unmeasured"
    assert any(item["kind"] == "ocr_unavailable" for item in receipt["findings"])


def test_successful_empty_ocr_is_usable_when_text_is_not_expected(tmp_path, monkeypatch):
    receipt = _fake_capability_receipt(
        tmp_path,
        monkeypatch,
        ocr_results=(("empty", []),),
        expected_text=False,
    )
    assert receipt["engines"] == {"faces": "opencv-yunet", "ocr": "macos-vision"}
    assert receipt["status"] == "pass"
    assert receipt["measured_count"] == receipt["sample_count"] == 5


def test_one_successful_ocr_keeps_engine_named_but_failed_samples_unknown(tmp_path, monkeypatch):
    receipt = _fake_capability_receipt(
        tmp_path,
        monkeypatch,
        ocr_results=(
            ("ok", [{"x": 20, "y": 20, "width": 10, "height": 10}]),
            ("failed", []),
        ),
        expected_text=True,
    )
    assert receipt["engines"] == {"faces": "opencv-yunet", "ocr": "macos-vision"}
    assert receipt["status"] == "unmeasured"
    assert 0 < receipt["measured_count"] < receipt["sample_count"]
    assert any(item["kind"] == "ocr_unavailable" for item in receipt["findings"])


def test_vision_ocr_requires_finite_confidence_at_or_above_boundary(monkeypatch, tmp_path):
    class _Vision:
        @staticmethod
        def recognize_text(_path, timeout_s):
            return [
                {"text": "boundary", "confidence": 0.5, "box": {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.1}},
                {"text": "high", "confidence": 0.9, "box": {"x": 0.3, "y": 0.2, "width": 0.2, "height": 0.1}},
                {"text": "low", "confidence": 0.49, "box": {"x": 0.5, "y": 0.2, "width": 0.2, "height": 0.1}},
                {"text": "missing", "box": {"x": 0.7, "y": 0.2, "width": 0.2, "height": 0.1}},
                {"text": "nan", "confidence": float("nan"), "box": {"x": 0.1, "y": 0.4, "width": 0.2, "height": 0.1}},
                {"text": "string", "confidence": "0.9", "box": {"x": 0.3, "y": 0.4, "width": 0.2, "height": 0.1}},
            ]

    monkeypatch.setitem(sys.modules, "vision_ocr", _Vision)
    status, boxes = composite_check._vision_ocr_boxes(tmp_path / "frame.png", 100, 100)
    assert status == "ok"
    assert len(boxes) == 2
    assert [box["x"] for box in boxes] == [10.0, 30.0]


def test_vision_ocr_low_confidence_only_is_successful_empty(monkeypatch, tmp_path):
    class _Vision:
        @staticmethod
        def recognize_text(_path, timeout_s):
            return [{
                "text": "noise",
                "confidence": 0.1,
                "box": {"x": 0.1, "y": 0.2, "width": 0.2, "height": 0.1},
            }]

    monkeypatch.setitem(sys.modules, "vision_ocr", _Vision)
    status, boxes = composite_check._vision_ocr_boxes(tmp_path / "frame.png", 100, 100)
    assert status == "empty"
    assert boxes == []
