"""A waived desktop test must not become a successful measurement."""
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docker_matrix", ROOT / "tools/docker_matrix.py")
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class DesktopScopeTests(unittest.TestCase):
    def test_low_space_prevents_waiting_for_more_installation(self):
        usage = namedtuple("Usage", "total used free")
        with patch.object(matrix.shutil, "disk_usage", return_value=usage(100, 99, 1)), \
                patch.object(matrix.subprocess, "run") as run:
            with self.assertRaises(matrix.DiskSpaceError):
                matrix.wait_container("test-only", ROOT, 60)
            run.assert_not_called()

    def test_wait_checks_space_again_after_poll_timeout(self):
        usage = namedtuple("Usage", "total used free")
        enough = usage(100 * 1024 ** 3, 50 * 1024 ** 3, 50 * 1024 ** 3)
        low = usage(100 * 1024 ** 3, 99 * 1024 ** 3, 1 * 1024 ** 3)
        with patch.object(matrix.shutil, "disk_usage", side_effect=[enough, enough, low]), \
                patch.object(matrix.subprocess, "run", side_effect=matrix.subprocess.TimeoutExpired("docker", 10)) as run:
            with self.assertRaises(matrix.DiskSpaceError):
                matrix.wait_container("test-only", ROOT, 60)
            self.assertEqual(run.call_count, 1)

    def test_trixie_native_routes_are_not_inherited_from_archive_fallbacks(self):
        cases = matrix.matrix()
        selected = [(case["software"], case["method"]) for case in cases
                    if case["target"] == "debian13" and not matrix.scope_reason(case, cases)]
        self.assertCountEqual(selected, [("fastfetch", "official"), ("fastfetch", "source"), ("jdk", "official")])

    def test_debian13_references_recipe_without_claiming_measurement(self):
        common = {"software": "go", "kind": "online", "method": "official", "version": "1",
                  "entry": {"id": "go", "version": "1", "sha256": "a"}}
        reference = dict(common, target="debian12", image="debian:12", case_id="go12")
        inherited = dict(common, target="debian13", image="debian:13", case_id="go13")
        unique = dict(inherited, case_id="go13-unique", entry={"id": "go", "version": "2", "sha256": "b"})
        cases = [reference, inherited, unique]
        self.assertEqual(matrix.debian12_reference(inherited, cases), reference)
        self.assertIsNone(matrix.scope_reason(unique, cases))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            folder = out / reference["case_id"]
            folder.mkdir()
            for state in ("failed", "passed"):
                (folder / "result.json").write_text(json.dumps(dict(reference, status=state)))
                results = matrix.summarize(out, cases)
                self.assertEqual(results[1]["status"], "skipped")
                self.assertEqual(results[1]["reference_case_id"], "go12")
                self.assertEqual(results[1]["support_inferred"], state == "passed")
                self.assertEqual(results[2]["status"], "not-run")

    def test_only_non_focal_desktop_cases_are_waived(self):
        cases = matrix.matrix()
        for case in cases:
            waived = bool(matrix.desktop_scope_reason(case))
            if case["target"] == "ubuntu20":
                self.assertFalse(waived)
            if case["software"] in ("nodejs22-nvm", "vim9", "go", "jdk", "fast", "select", "tui", "music"):
                self.assertFalse(waived)
            if case["target"] != "ubuntu20" and case["software"] in ("ghostty", "visual-studio-code", "fast-desktop", "select-network"):
                self.assertTrue(waived)

    def test_existing_evidence_is_preserved_without_promoting_failure(self):
        cases = [{"case_id": "desktop-" + state, "target": "ubuntu22", "software": "visual-studio-code", "kind": "online"}
                 for state in ("passed", "failed", "missing")]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for case, state in zip(cases, ("passed", "failed")):
                folder = out / case["case_id"]
                folder.mkdir()
                (folder / "result.json").write_text(json.dumps(dict(case, status=state, reason="original")))
            results = matrix.summarize(out, cases)
            self.assertEqual([r["status"] for r in results], ["passed", "skipped", "skipped"])
            self.assertEqual(results[1]["previous_status"], "failed")
            self.assertEqual(json.loads((out / "desktop-failed/result.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
