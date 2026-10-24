"""auto_edit.py re-executes editor/studio under an interpreter that has the caption engine."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import auto_edit  # noqa: E402


class InterpreterGateTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop(auto_edit.REEXEC_FLAG, None)

    def tearDown(self) -> None:
        os.environ.pop(auto_edit.REEXEC_FLAG, None)

    def _run(self, argv, *, engine, candidates_ok, exists=True):
        calls = []
        err = io.StringIO()
        out = io.StringIO()
        with (
            mock.patch.object(auto_edit, "_interpreter_has_caption_engine", return_value=engine),
            mock.patch.object(auto_edit, "_probe_interpreter", return_value=candidates_ok),
            mock.patch.object(auto_edit.os.path, "exists", return_value=exists),
            mock.patch.object(auto_edit.os.path, "realpath", side_effect=lambda p: p),
            mock.patch.object(auto_edit.sys, "executable", "/usr/bin/python3"),  # never one of the candidates
            mock.patch.object(auto_edit.os, "execve", side_effect=lambda *a: calls.append(a)),
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(out),
        ):
            auto_edit.maybe_reexec_for_caption_engine(argv)
        return calls, err.getvalue(), out.getvalue()

    def test_json_subcommands_never_reexec(self) -> None:
        calls, err, out = self._run(["auto_edit.py", "plan-overlays", "--manifest", "x"], engine=False, candidates_ok=True)
        self.assertEqual(calls, [])
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_editor_with_engine_present_stays_put(self) -> None:
        calls, err, out = self._run(["auto_edit.py", "editor", "--project-dir", "p"], engine=True, candidates_ok=True)
        self.assertEqual(calls, [])
        self.assertEqual(out, "")

    def test_editor_without_engine_reexecs_once_with_loop_guard(self) -> None:
        calls, err, out = self._run(["auto_edit.py", "editor", "--project-dir", "p", "--port", "1"], engine=False, candidates_ok=True)
        self.assertEqual(len(calls), 1)
        python, argv, env = calls[0]
        self.assertEqual(python, auto_edit.PREFERRED_INTERPRETERS[0])
        self.assertEqual(argv[2:], ["editor", "--project-dir", "p", "--port", "1"])
        self.assertEqual(env[auto_edit.REEXEC_FLAG], "1")
        self.assertIn("重新啟動", err)
        self.assertEqual(out, "")  # stdout untouched

    def test_flag_set_means_no_second_reexec_but_warns(self) -> None:
        os.environ[auto_edit.REEXEC_FLAG] = "1"
        calls, err, out = self._run(["auto_edit.py", "studio"], engine=False, candidates_ok=True)
        self.assertEqual(calls, [])
        self.assertIn("警告", err)
        self.assertEqual(out, "")

    def test_execve_failure_falls_through_to_next_candidate(self) -> None:
        attempts = []

        def failing_then_ok(python, argv, env):
            attempts.append(python)
            if len(attempts) == 1:
                raise OSError("no such interpreter")

        err = io.StringIO()
        with (
            mock.patch.object(auto_edit, "_interpreter_has_caption_engine", return_value=False),
            mock.patch.object(auto_edit, "_probe_interpreter", return_value=True),
            mock.patch.object(auto_edit.os.path, "exists", return_value=True),
            mock.patch.object(auto_edit.os.path, "realpath", side_effect=lambda p: p),
            mock.patch.object(auto_edit.sys, "executable", "/usr/bin/python3"),
            mock.patch.object(auto_edit.os, "execve", side_effect=failing_then_ok),
            contextlib.redirect_stderr(err),
        ):
            auto_edit.maybe_reexec_for_caption_engine(["auto_edit.py", "editor"])
        self.assertEqual(attempts, list(auto_edit.PREFERRED_INTERPRETERS[:2]))

    def test_no_capable_interpreter_warns_on_stderr_only(self) -> None:
        calls, err, out = self._run(["auto_edit.py", "studio"], engine=False, candidates_ok=False)
        self.assertEqual(calls, [])
        self.assertIn("警告", err)
        self.assertEqual(out, "")
        calls, err, out = self._run(["auto_edit.py", "studio"], engine=False, candidates_ok=True, exists=False)
        self.assertEqual(calls, [])
        self.assertIn("警告", err)


if __name__ == "__main__":
    unittest.main()
