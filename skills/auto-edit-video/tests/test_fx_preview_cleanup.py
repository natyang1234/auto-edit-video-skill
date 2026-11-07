#!/usr/bin/env python3
"""Cancellation ownership and timing-retention contracts for FX preview jobs."""
from __future__ import annotations

import os
import inspect
import io
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fx_preview_jobs as jobs  # noqa: E402


class FxPreviewWorkerCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.project = self.tmp / "project"
        (self.project / "working").mkdir(parents=True)
        (self.project / "source").mkdir()
        self.source = self.project / "source/source.mp4"
        self.source.write_bytes(b"fixture")
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.outside_sentinel = self.outside / "sentinel.txt"
        self.outside_sentinel.write_text("keep", encoding="utf-8")
        self.worker = self.tmp / "owned_worker.py"
        self.worker.write_text(
            "\n".join(
                (
                    "import argparse, fcntl, json, os, time",
                    "from pathlib import Path",
                    "p=argparse.ArgumentParser()",
                    "p.add_argument('--request', required=True)",
                    "p.add_argument('--result', required=True)",
                    "a=p.parse_args()",
                    "data=json.loads(Path(a.request).read_text(encoding='utf-8'))",
                    "owner=data.get('scratch_owner') or Path(data['output']).name.split('.', 1)[0]",
                    "scratch=Path(data['cache_dir'])/f'timeline-project-{owner}-0123456789abcdef-owned-worker'",
                    "scratch.mkdir(parents=True, exist_ok=True)",
                    "lock_fd=os.dup(int(os.environ['FX_PREVIEW_SCRATCH_LEASE_FD']))",
                    "fcntl.flock(lock_fd, fcntl.LOCK_EX|fcntl.LOCK_NB)",
                    "(scratch/'inner.part.mp4').write_bytes(b'partial')",
                    f"(scratch/'outside-link').symlink_to({str(self.outside)!r}, target_is_directory=True)",
                    "Path(data['output']).write_bytes(b'partial')",
                    "time.sleep(60)",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        self.manager = jobs.FxPreviewJobManager(
            self.project,
            context_provider=lambda: {},
            renderer_script=self.worker,
            render_timeout_s=5,
        )
        self.addCleanup(self.manager.close)
        (self.manager.runtime_dir / "foreign.keep").write_text("keep", encoding="utf-8")
        (self.manager.renderer_cache_dir / "foreign.keep").write_text("keep", encoding="utf-8")

    def _start_worker(self, job_id: str) -> threading.Thread:
        now = time.monotonic()
        job = {
            "id": job_id,
            "state": "running",
            "phase": "queued",
            "message": "queued",
            "cache_hit": False,
            "timing_started": now,
            "timing_phase_started": now,
            "phase_timings_s": {},
            "updated_monotonic": time.time(),
            "state_revision": "b" * 64,
            "overlay": {"id": "fx", "type": "fx", "code": "T3", "start": 0, "end": 2},
            "sample_window": {"start_s": 0, "duration_s": 2},
            "preview_options": {"mode": "single", "quality": "draft"},
            "state_snapshot": {},
            "manifest_snapshot": {},
            "timeline_snapshot": None,
            "replace_overlay_id": None,
            "dependencies": {
                "pack_id": "",
                "canvas": {"width": 1080, "height": 1920},
            },
            "dependency_hash": "c" * 64,
            "source_identity": {"path": self.source},
            "cache_key": "d" * 64,
            "process": None,
            "cancel_requested": False,
            "media": None,
            "error": None,
        }
        with self.manager._lock:
            self.manager._jobs[job_id] = job
            self.manager._active_job_id = job_id
        thread = threading.Thread(
            target=self.manager._run_job,
            args=(
                job_id,
                self.manager.cache_dir / f"{job['cache_key']}.mp4",
                self.manager.cache_dir / f"{job['cache_key']}.json",
            ),
        )
        thread.start()
        return thread

    def _worker_scratch(self, job_id: str) -> Path:
        return self.manager.renderer_cache_dir / f"timeline-project-{job_id}-0123456789abcdef-owned-worker"

    def _wait_for_worker_scratch(self, job_id: str) -> None:
        scratch = self._worker_scratch(job_id) / "inner.part.mp4"
        deadline = time.monotonic() + 5
        while not scratch.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(scratch.is_file(), "real worker subprocess did not create its owned scratch")

    def _assert_only_owned_artifacts_removed(self, job_id: str) -> None:
        self.assertFalse(self._worker_scratch(job_id).exists())
        for suffix in ("request.json", "result.json", "part.mp4"):
            self.assertFalse((self.manager.runtime_dir / f"{job_id}.{suffix}").exists())
        self.assertEqual((self.manager.runtime_dir / "foreign.keep").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.manager.renderer_cache_dir / "foreign.keep").read_text(encoding="utf-8"), "keep")
        self.assertEqual(self.outside_sentinel.read_text(encoding="utf-8"), "keep")

    def test_real_worker_cancel_removes_owned_scratch_without_following_symlink(self) -> None:
        job_id = "1" * 32
        thread = self._start_worker(job_id)
        self._wait_for_worker_scratch(job_id)
        cancelled = self.manager.cancel(job_id)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(self.manager.public_job(job_id)["state"], "cancelled")
        self._assert_only_owned_artifacts_removed(job_id)

    def test_cancel_race_never_exposes_or_records_failed_phase(self) -> None:
        job_id = "a" * 32
        thread = self._start_worker(job_id)
        self._wait_for_worker_scratch(job_id)
        original_terminate = self.manager._terminate_process
        observed_states = []

        def terminate_and_yield_to_worker(process):
            terminated = original_terminate(process)
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                with self.manager._lock:
                    state = self.manager._jobs[job_id]["state"]
                observed_states.append(state)
                if state == "failed":
                    break
                time.sleep(0.01)
            return terminated

        with mock.patch.object(
            self.manager, "_terminate_process", side_effect=terminate_and_yield_to_worker
        ):
            cancelled = self.manager.cancel(job_id)
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("failed", observed_states)
        self.assertNotIn("cancelled", observed_states)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertNotIn("failed", cancelled["phase_timings_s"])
        self._assert_only_owned_artifacts_removed(job_id)

    def test_late_cancel_callback_cannot_rewrite_a_terminal_state(self) -> None:
        now = time.monotonic()
        for state in ("ready", "failed"):
            job_id = ("d" if state == "ready" else "e") * 32
            self.manager._jobs[job_id] = {
                "id": job_id,
                "state": state,
                "phase": state,
                "message": state,
                "timing_started": now - 1,
                "timing_phase_started": now,
                "timing_finished": now,
                "phase_timings_s": {state: 0.0},
                "updated_monotonic": time.time(),
                "preview_options": {"mode": "single", "quality": "draft"},
                "sample_window": {"duration_s": 2},
                "overlay": {"code": "T3", "start": 0, "end": 2},
                "error": {"code": "render_failed", "message": "failed"}
                if state == "failed"
                else None,
            }
            before = dict(self.manager._jobs[job_id])
            with self.manager._lock:
                self.manager._finish_cancelled(job_id)
            self.assertEqual(self.manager._jobs[job_id], before)

    def test_cancel_handler_alone_decides_unverified_or_cancelled(self) -> None:
        for suffix, terminate_result in (("f", False), ("0", True)):
            with self.subTest(terminate_result=terminate_result):
                owner = suffix * 32
                now = time.monotonic()
                self.manager._jobs[owner] = {
                    "id": owner,
                    "state": "running",
                    "phase": "rendering",
                    "message": "rendering",
                    "cache_key": suffix * 64,
                    "process": object(),
                    "cancel_requested": False,
                    "timing_started": now,
                    "timing_phase_started": now,
                    "phase_timings_s": {},
                    "updated_monotonic": time.time(),
                    "preview_options": {"mode": "single", "quality": "draft"},
                    "sample_window": {"duration_s": 2},
                    "overlay": {"code": "T3", "start": 0, "end": 2},
                    "error": None,
                }
                self.manager._active_job_id = owner
                terminate_entered = threading.Event()
                release_terminate = threading.Event()
                outcome = {}

                def blocked_terminate(_process):
                    terminate_entered.set()
                    self.assertTrue(release_terminate.wait(timeout=5))
                    return terminate_result

                def issue_cancel():
                    outcome["job"] = self.manager.cancel(owner)

                with mock.patch.object(
                    self.manager, "_terminate_process", side_effect=blocked_terminate
                ):
                    cancel_thread = threading.Thread(target=issue_cancel)
                    cancel_thread.start()
                    self.assertTrue(terminate_entered.wait(timeout=5))
                    with self.manager._lock:
                        pending = self.manager._jobs[owner]
                        self.assertTrue(self.manager._worker_cancel_pending(pending))
                        self.manager._finish_cancelled(owner)
                        self.assertEqual(
                            (pending["state"], pending["phase"]),
                            ("running", "rendering"),
                        )
                    self.manager._consume_progress(
                        owner, io.StringIO('{"phase":"encoding"}\n')
                    )
                    with self.manager._lock:
                        pending = self.manager._jobs[owner]
                        self.assertEqual(
                            (pending["state"], pending["phase"]),
                            ("running", "rendering"),
                        )
                    release_terminate.set()
                    cancel_thread.join(timeout=5)
                    self.assertFalse(cancel_thread.is_alive())

                status = outcome["job"]
                if terminate_result:
                    self.assertEqual(
                        (status["state"], status["phase"]),
                        ("cancelled", "cancelled"),
                    )
                else:
                    self.assertEqual(
                        (status["state"], status["phase"]),
                        ("running", "cancelling"),
                    )
                    self.assertEqual(status["error"]["code"], "termination_unverified")
                    self.manager._consume_progress(
                        owner, io.StringIO('{"phase":"preparing"}\n')
                    )
                    unchanged = self.manager.public_job(owner)
                    self.assertEqual(
                        (unchanged["state"], unchanged["phase"]),
                        ("running", "cancelling"),
                    )
                    with self.manager._lock:
                        self.manager._active_job_id = None
                        self.manager._jobs[owner]["process"] = None

    def test_popen_and_process_registration_are_atomic_with_cancel(self) -> None:
        for suffix, terminate_result in (("6", False), ("7", True)):
            with self.subTest(terminate_result=terminate_result):
                owner = suffix * 32
                now = time.monotonic()
                job = {
                    "id": owner,
                    "state": "running",
                    "phase": "queued",
                    "message": "queued",
                    "cache_hit": False,
                    "timing_started": now,
                    "timing_phase_started": now,
                    "phase_timings_s": {},
                    "updated_monotonic": time.time(),
                    "state_revision": "b" * 64,
                    "overlay": {"id": "fx", "type": "fx", "code": "T3", "start": 0, "end": 2},
                    "sample_window": {"start_s": 0, "duration_s": 2},
                    "preview_options": {"mode": "single", "quality": "draft"},
                    "state_snapshot": {},
                    "manifest_snapshot": {},
                    "timeline_snapshot": None,
                    "replace_overlay_id": None,
                    "dependencies": {"pack_id": "", "canvas": {"width": 1080, "height": 1920}},
                    "dependency_hash": "c" * 64,
                    "source_identity": {"path": self.source},
                    "cache_key": suffix * 64,
                    "process": None,
                    "cancel_requested": False,
                    "media": None,
                    "error": None,
                }
                with self.manager._lock:
                    self.manager._jobs[owner] = job
                    self.manager._active_job_id = owner
                popen_entered = threading.Event()
                release_popen = threading.Event()
                cancel_started = threading.Event()
                cancel_outcome = {}

                class FakeProcess:
                    stderr = None
                    returncode = None

                    @staticmethod
                    def poll():
                        return None

                fake_process = FakeProcess()

                def fake_popen(*_args, **_kwargs):
                    popen_entered.set()
                    self.assertTrue(release_popen.wait(timeout=5))
                    return fake_process

                def issue_cancel():
                    cancel_started.set()
                    cancel_outcome["job"] = self.manager.cancel(owner)

                with (
                    mock.patch.object(jobs.subprocess, "Popen", side_effect=fake_popen),
                    mock.patch.object(
                        self.manager, "_terminate_process", return_value=terminate_result
                    ),
                ):
                    worker = threading.Thread(
                        target=self.manager._run_job,
                        args=(
                            owner,
                            self.manager.cache_dir / f"{job['cache_key']}.mp4",
                            self.manager.cache_dir / f"{job['cache_key']}.json",
                        ),
                    )
                    worker.start()
                    self.assertTrue(popen_entered.wait(timeout=5))
                    cancel_thread = threading.Thread(target=issue_cancel)
                    cancel_thread.start()
                    self.assertTrue(cancel_started.wait(timeout=5))
                    self.assertTrue(
                        cancel_thread.is_alive(),
                        "cancel must wait until Popen is atomically registered",
                    )
                    release_popen.set()
                    cancel_thread.join(timeout=5)
                    worker.join(timeout=5)
                    self.assertFalse(cancel_thread.is_alive())
                    self.assertFalse(worker.is_alive())

                status = cancel_outcome["job"]
                if terminate_result:
                    self.assertEqual(
                        (status["state"], status["phase"]),
                        ("cancelled", "cancelled"),
                    )
                else:
                    self.assertEqual(
                        (status["state"], status["phase"]),
                        ("running", "cancelling"),
                    )
                    self.assertEqual(status["error"]["code"], "termination_unverified")
                    with self.manager._lock:
                        self.manager._active_job_id = None
                        self.manager._jobs[owner]["process"] = None

    def test_post_process_worker_callbacks_never_claim_cancelled(self) -> None:
        source = inspect.getsource(self.manager._run_job)
        post_process = source[source.index("progress.join"):source.rindex("self.prune()")]
        self.assertNotIn("_finish_cancelled", post_process)
        self.assertGreaterEqual(post_process.count("_worker_cancel_pending"), 3)

    def test_retry_after_unverified_cancel_reclaims_only_that_job_shadow(self) -> None:
        owner = "b" * 32
        other_owner = "c" * 32
        now = time.monotonic()
        self.manager._jobs[owner] = {
            "id": owner,
            "state": "running",
            "phase": "rendering",
            "message": "working",
            "cache_key": "d" * 64,
            "process": object(),
            "cancel_requested": False,
            "timing_started": now,
            "timing_phase_started": now,
            "phase_timings_s": {},
            "updated_monotonic": time.time(),
            "preview_options": {"mode": "single", "quality": "draft"},
            "sample_window": {"duration_s": 2},
            "overlay": {"code": "T3", "start": 0, "end": 2},
            "error": None,
        }
        self.manager._active_job_id = owner
        lease_fd = self.manager._try_acquire_scratch_lease(owner, create=True)
        self.assertIsNotNone(lease_fd)
        owned = self.manager.renderer_cache_dir / f"timeline-project-{owner}-0123456789abcdef-retry"
        owned.mkdir()
        (owned / "partial.mp4").write_bytes(b"owned")

        other_lease_fd = self.manager._try_acquire_scratch_lease(other_owner, create=True)
        self.assertIsNotNone(other_lease_fd)
        other = self.manager.renderer_cache_dir / f"timeline-project-{other_owner}-0123456789abcdef-other"
        other.mkdir()
        (other / "partial.mp4").write_bytes(b"other")
        legacy = self.manager.renderer_cache_dir / "timeline-project-legacy-unowned"
        legacy.mkdir()
        (legacy / "partial.mp4").write_bytes(b"legacy")

        try:
            with mock.patch.object(self.manager, "_terminate_process", return_value=False):
                first = self.manager.cancel(owner)
            self.assertEqual(first["state"], "running")
            self.assertEqual(first["phase"], "cancelling")
            self.assertEqual(first["error"]["code"], "termination_unverified")
            self.assertTrue(owned.is_dir())

            os.close(lease_fd)
            lease_fd = None
            with mock.patch.object(self.manager, "_terminate_process", return_value=True):
                second = self.manager.cancel(owner)

            self.assertEqual(second["state"], "cancelled")
            self.assertFalse(owned.exists())
            self.assertFalse(
                (self.manager.renderer_cache_dir / f".timeline-owner-{owner}.lock").exists()
            )
            self.assertTrue(other.is_dir())
            self.assertTrue(legacy.is_dir())
            self.assertEqual(
                (self.manager.renderer_cache_dir / "foreign.keep").read_text(encoding="utf-8"),
                "keep",
            )
        finally:
            if lease_fd is not None:
                os.close(lease_fd)
            os.close(other_lease_fd)

    def test_real_worker_timeout_removes_owned_scratch_and_preserves_foreign_files(self) -> None:
        self.manager.render_timeout_s = 0.2
        job_id = "2" * 32
        thread = self._start_worker(job_id)
        self._wait_for_worker_scratch(job_id)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        status = self.manager.public_job(job_id)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error"]["code"], "render_timeout")
        self._assert_only_owned_artifacts_removed(job_id)

    def test_second_manager_and_generic_maintenance_preserve_busy_owner(self) -> None:
        owner = "5" * 32
        maintenance_fd = self.manager._acquire_renderer_maintenance_lock(exclusive=False)
        self.assertIsNotNone(maintenance_fd)
        lease_fd = self.manager._try_acquire_scratch_lease(owner, create=True)
        self.assertIsNotNone(lease_fd)
        scratch = self.manager.renderer_cache_dir / f"timeline-project-{owner}-0123456789abcdef-live"
        scratch.mkdir()
        live_file = scratch / "active.part.mp4"
        live_file.write_bytes(b"active")
        (scratch / "outside-link").symlink_to(self.outside, target_is_directory=True)
        ordinary = self.manager.renderer_cache_dir / "working/fx/active-single.mov"
        ordinary.parent.mkdir(parents=True)
        ordinary.write_bytes(b"active-single")
        old_time = time.time() - jobs.CACHE_IDLE_TTL_S - 60
        os.utime(ordinary, (old_time, old_time))
        runtime_files = [
            self.manager.runtime_dir / f"{owner}.request.json",
            self.manager.runtime_dir / f"{owner}.part.mp4",
        ]
        for path in runtime_files:
            path.write_bytes(b"active")
        before_mtime = live_file.stat().st_mtime_ns
        ordinary_mtime = ordinary.stat().st_mtime_ns

        second = jobs.FxPreviewJobManager(self.project, context_provider=lambda: {})
        try:
            self.assertTrue(scratch.is_dir())
            self.assertTrue(all(path.is_file() for path in runtime_files))
            second._touch_renderer_cache()
            second.prune()
            self.assertTrue(scratch.is_dir())
            self.assertEqual(live_file.stat().st_mtime_ns, before_mtime)
            self.assertTrue(ordinary.is_file())
            self.assertEqual(ordinary.stat().st_mtime_ns, ordinary_mtime)
            self.assertEqual(self.outside_sentinel.read_text(encoding="utf-8"), "keep")
        finally:
            second.close()

        os.close(maintenance_fd)
        os.close(lease_fd)
        self.manager._cleanup_orphan_partials()
        self.manager._cleanup_renderer_scratch()
        self.assertFalse(scratch.exists())
        self.assertTrue(all(not path.exists() for path in runtime_files))
        self.assertEqual(self.outside_sentinel.read_text(encoding="utf-8"), "keep")

    def test_exclusive_maintenance_guard_blocks_new_worker_cache_entry(self) -> None:
        owner = "9" * 32
        maintenance_fd = self.manager._acquire_renderer_maintenance_lock(exclusive=True)
        self.assertIsNotNone(maintenance_fd)
        thread = self._start_worker(owner)
        time.sleep(0.1)
        self.assertFalse(self._worker_scratch(owner).exists())
        with self.manager._lock:
            self.assertIsNone(self.manager._jobs[owner]["process"])

        os.close(maintenance_fd)
        self._wait_for_worker_scratch(owner)
        self.manager.cancel(owner)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self._assert_only_owned_artifacts_removed(owner)

    def test_startup_preserves_legacy_or_unsafe_unleased_scratch(self) -> None:
        legacy_owner = "6" * 32
        legacy = self.manager.renderer_cache_dir / f"timeline-project-{legacy_owner}-0123456789abcdef-legacy"
        legacy.mkdir()
        legacy_file = legacy / "unknown.part.mp4"
        legacy_file.write_bytes(b"unknown")
        old_time = time.time() - jobs.CACHE_IDLE_TTL_S - 60
        os.utime(legacy_file, (old_time, old_time))
        legacy_mtime = legacy_file.stat().st_mtime_ns
        legacy_runtime = self.manager.runtime_dir / f"{legacy_owner}.part.mp4"
        legacy_runtime.write_bytes(b"unknown")

        unsafe_owner = "7" * 32
        unsafe = self.manager.renderer_cache_dir / f"timeline-project-{unsafe_owner}-0123456789abcdef-unsafe"
        unsafe.mkdir()
        unsafe_lease = self.manager.renderer_cache_dir / f".timeline-owner-{unsafe_owner}.lock"
        unsafe_lease.symlink_to(self.outside_sentinel)

        second = jobs.FxPreviewJobManager(self.project, context_provider=lambda: {})
        try:
            self.assertTrue(legacy.is_dir())
            self.assertTrue(legacy_runtime.is_file())
            self.assertTrue(unsafe.is_dir())
            self.assertTrue(unsafe_lease.is_symlink())
            second._touch_renderer_cache()
            second.prune()
            self.assertTrue(legacy_file.is_file())
            self.assertEqual(legacy_file.stat().st_mtime_ns, legacy_mtime)
            self.assertTrue(
                (second.renderer_cache_dir / jobs.RENDERER_MAINTENANCE_LOCK).is_file()
            )
            self.assertEqual(self.outside_sentinel.read_text(encoding="utf-8"), "keep")
        finally:
            second.close()

    def test_failed_shadow_removal_keeps_lease_for_later_retry(self) -> None:
        owner = "8" * 32
        lease_fd = self.manager._try_acquire_scratch_lease(owner, create=True)
        self.assertIsNotNone(lease_fd)
        os.close(lease_fd)
        lease = self.manager.renderer_cache_dir / f".timeline-owner-{owner}.lock"
        scratch = self.manager.renderer_cache_dir / f"timeline-project-{owner}-0123456789abcdef-retry"
        scratch.mkdir()
        (scratch / "partial.mp4").write_bytes(b"partial")

        with mock.patch.object(self.manager, "_remove_tree_at", side_effect=OSError("fixture")):
            self.manager._cleanup_renderer_scratch(owner)
        self.assertTrue(scratch.is_dir())
        self.assertTrue(lease.is_file())

        self.manager._cleanup_renderer_scratch(owner)
        self.assertFalse(scratch.exists())
        self.assertFalse(lease.exists())

    def test_old_job_finally_does_not_remove_new_job_shadow(self) -> None:
        old_job_id = "3" * 32
        new_job_id = "4" * 32
        old_cleanup_entered = threading.Event()
        release_old_cleanup = threading.Event()
        original_cleanup = self.manager._cleanup_renderer_scratch
        old_thread = None

        def controlled_cleanup(owner: str | None = None) -> None:
            if threading.current_thread() is old_thread:
                old_cleanup_entered.set()
                self.assertTrue(release_old_cleanup.wait(timeout=5))
            if owner is None:
                original_cleanup()
            else:
                original_cleanup(owner)

        self.manager._cleanup_renderer_scratch = controlled_cleanup
        old_thread = self._start_worker(old_job_id)
        self._wait_for_worker_scratch(old_job_id)
        cancelled = self.manager.cancel(old_job_id)
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(old_cleanup_entered.wait(timeout=5))

        new_thread = self._start_worker(new_job_id)
        self._wait_for_worker_scratch(new_job_id)
        release_old_cleanup.set()
        old_thread.join(timeout=5)
        self.assertFalse(old_thread.is_alive())
        self.assertTrue(
            self._worker_scratch(new_job_id).is_dir(),
            "the old job's delayed finally removed the new job's owned shadow",
        )

        self.manager.cancel(new_job_id)
        new_thread.join(timeout=5)
        self.assertFalse(new_thread.is_alive())
        self._assert_only_owned_artifacts_removed(new_job_id)


class FxPreviewTimingLruTests(unittest.TestCase):
    def test_successful_sample_refreshes_group_before_capacity_eviction(self) -> None:
        manager = object.__new__(jobs.FxPreviewJobManager)
        manager._timing_samples = {}
        hot = ("timeline", "standard", "T3", 8.0, 4.0)
        manager._record_timing_sample(hot, 10.0)
        cold = []
        for index in range(99):
            group = ("single", "draft", f"cold-{index}", 2.0, 2.0)
            cold.append(group)
            manager._record_timing_sample(group, 1.0)
        manager._record_timing_sample(hot, 9.0)
        newest = ("single", "draft", "newest", 2.0, 2.0)
        manager._record_timing_sample(newest, 1.0)

        self.assertEqual(len(manager._timing_samples), 100)
        self.assertNotIn(cold[0], manager._timing_samples)
        self.assertEqual(manager._timing_samples[hot], [10.0, 9.0])
        self.assertIn(newest, manager._timing_samples)
        self.assertEqual(next(reversed(manager._timing_samples)), newest)


if __name__ == "__main__":
    unittest.main()
