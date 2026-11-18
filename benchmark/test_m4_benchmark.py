# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("m4_benchmark", HERE / "m4_benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.suite = json.loads((HERE / "suite.example.json").read_text())
        self.suite.update(measurement_frames=60, repeats_per_variant=2)
        self.base, self.candidate = self.suite["approved_revisions"]
        self.plan = bench.make_plan(self.suite, self.base, self.candidate, "warm")

    def recordings(self, baseline_ms=20, candidate_ms=20):
        result = []
        for run in self.plan["runs"]:
            interval = baseline_ms if run["variant"] == "baseline" else candidate_ms
            r = {k: run[k] for k in ("id", "variant", "revision", "cache")}
            r.update(schema=1, program_id=self.suite["program_id"],
                     plan_sha256=bench.plan_digest(self.plan), coverage="all_present_intervals",
                     scene_verified=True, image_check_passed=True, clean_window=True, completed=True,
                     intervals_ms=[interval] * run["measurement_frames"],
                     elapsed_s=interval * run["measurement_frames"] / 1000)
            r.update({key: "a" * 64 for key in bench.FINGERPRINTS})
            result.append(r)
        return result

    def receipt_and_timestamps(self):
        receipt = self.recordings()[0]
        for key in ("coverage", "intervals_ms", "elapsed_s"):
            receipt.pop(key)
        trace = {"schema": 1, "clock": "monotonic_ns", "boundaries": [
            {"present": 900 + n, "timestamp_ns": 10**18 + n * 20_000_000}
            for n in range(self.suite["measurement_frames"] + 1)]}
        return receipt, trace

    def test_alternating_pairs_are_abba(self):
        self.assertEqual([x["variant"] for x in self.plan["runs"]],
                         ["baseline", "candidate", "candidate", "baseline"])
        self.assertTrue(all(x["profile_policy"] == "fresh_copy" for x in self.plan["runs"]))
        self.assertEqual(self.plan["status"], "planned_not_executed")

    def test_short_or_shell_text_revisions_are_refused(self):
        for value in ("main", self.base[:8], self.base + "; echo unsafe", self.base.upper()):
            with self.assertRaises(bench.BenchmarkError):
                bench.make_plan(self.suite, value, self.candidate, "warm")

    def test_full_sha_requires_reviewed_allowlist(self):
        with self.assertRaises(bench.BenchmarkError):
            bench.make_plan(self.suite, "f" * 40, self.candidate, "warm")

    def test_cache_modes_cannot_be_mixed_or_invented(self):
        with self.assertRaises(bench.BenchmarkError):
            bench.make_plan(self.suite, self.base, self.candidate, "system-purge")
        cold = bench.make_plan(self.suite, self.base, self.candidate, "cold_profile")
        self.assertTrue(all(x["cache"] == "cold_profile" for x in cold["runs"]))

    def test_a_a_control_is_allowed(self):
        p = bench.make_plan(self.suite, self.base, self.base, "warm")
        self.assertEqual(p["baseline"], p["candidate"])

    def test_a_a_difference_is_not_an_optimization(self):
        self.plan = bench.make_plan(self.suite, self.base, self.base, "warm")
        r = bench.compare(self.plan, self.recordings(20, 16))
        self.assertEqual(r["status"], "inconclusive")
        self.assertIn("A/A control", r["reasons"][0])

    def test_schema_numbers_are_strict(self):
        for value in (True, 1.0, "1"):
            suite = dict(self.suite, schema=value)
            with self.assertRaises(bench.BenchmarkError):
                bench.make_plan(suite, self.base, self.candidate, "warm")
            p = dict(self.plan, schema=value)
            with self.assertRaises(bench.BenchmarkError):
                bench.checked_plan(p)
            records = self.recordings()
            records[0]["schema"] = value
            self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_invalid_config_values_are_refused(self):
        for key, value in [("program_id", 7), ("measurement_frames", True),
                           ("measurement_frames", 1), ("repeats_per_variant", 1),
                           ("max_run_variation_percent", float("nan")),
                           ("max_run_variation_percent", 10 ** 400)]:
            suite = dict(self.suite, **{key: value})
            with self.assertRaises(bench.BenchmarkError):
                bench.make_plan(suite, self.base, self.candidate, "warm")

    def test_plan_schedule_tampering_is_refused(self):
        p = copy.deepcopy(self.plan)
        p["runs"].reverse()
        with self.assertRaises(bench.BenchmarkError):
            bench.checked_plan(p)

    def test_plan_cannot_replace_integer_pairs_with_booleans(self):
        p = copy.deepcopy(self.plan)
        p["runs"][0]["pair"] = True
        with self.assertRaises(bench.BenchmarkError):
            bench.checked_plan(p)

    def test_improvement_and_no_clear_change(self):
        self.assertEqual(bench.compare(self.plan, self.recordings(20, 16))["status"], "improvement")
        r = bench.compare(self.plan, self.recordings())
        self.assertEqual(r["status"], "no_clear_change")
        self.assertEqual(r["variants"]["baseline"]["median_run_fps"], 50)

    def test_average_regression(self):
        self.assertEqual(bench.compare(self.plan, self.recordings(20, 25))["status"], "regression")

    def test_tail_regression_overrides_better_average(self):
        records = self.recordings(20, 10)
        for r in records:
            if r["variant"] == "candidate":
                r["intervals_ms"][-1] = 100
                r["elapsed_s"] = sum(r["intervals_ms"]) / 1000
        r = bench.compare(self.plan, records)
        self.assertGreater(r["fps_change_percent"], 0)
        self.assertEqual(r["status"], "regression")

    def test_missing_runs_are_inconclusive(self):
        self.assertEqual(bench.compare(self.plan, self.recordings()[:-1])["status"], "inconclusive")

    def test_threshold_or_ring_truncated_samples_are_refused(self):
        records = self.recordings()
        records[0]["coverage"] = "latest_120"
        self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_mismatched_provenance_is_inconclusive(self):
        for key, value in [("revision", "f" * 40), ("id", "duplicate"),
                           ("program_id", "0" * 16), ("plan_sha256", "b" * 64),
                           ("driver_fingerprint", "b" * 64), ("cache", "cold_profile")]:
            records = self.recordings()
            records[0][key] = value
            self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive", key)

    def test_noisy_repetitions_are_inconclusive(self):
        records = self.recordings()
        records[0]["intervals_ms"] = [10] * 60
        records[0]["elapsed_s"] = .6
        self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_scene_or_contamination_failure_is_inconclusive(self):
        for key in ("scene_verified", "clean_window", "completed"):
            records = self.recordings()
            records[0][key] = False
            self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_image_failure_is_not_a_performance_pass(self):
        records = self.recordings(20, 10)
        records[0]["image_check_passed"] = False
        self.assertEqual(bench.compare(self.plan, records)["status"], "correctness_failure")

    def test_strict_flags_and_finite_positive_intervals(self):
        for value in (False, 0, -1, float("nan"), float("inf"), 10 ** 400):
            records = self.recordings()
            records[0]["intervals_ms"][0] = value
            self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")
        records = self.recordings()
        records[0]["clean_window"] = 1
        self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_elapsed_must_include_all_intervals(self):
        records = self.recordings()
        records[0]["elapsed_s"] = .1
        self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_recording_requires_exact_frame_count(self):
        records = self.recordings()
        records[0]["intervals_ms"].pop()
        self.assertEqual(bench.compare(self.plan, records)["status"], "inconclusive")

    def test_public_report_does_not_contain_private_record_fields(self):
        records = self.recordings()
        records[0]["private_log"] = "/private/game/log/data"
        result = json.dumps(bench.compare(self.plan, records))
        self.assertNotIn("/private", result)
        self.assertNotIn("intervals_ms", result)

    def test_markdown_report_summarizes_improvement_without_private_records(self):
        records = self.recordings(20, 16)
        records[0]["private_log"] = "/private/game/log/data"
        report = bench.comparison_markdown(bench.compare(self.plan, records))
        self.assertIn("Status: **`improvement`**", report)
        self.assertIn("| Median run FPS | 50.00 | 62.50 |", report)
        self.assertIn("FPS change: +25.00% (higher is better)", report)
        self.assertIn("p99 change: -20.00% (lower is better)", report)
        self.assertIn(bench.plan_digest(self.plan), report)
        self.assertIn("not statistical confidence intervals", report)
        for private_field in ("/private", "intervals_ms", "machine_fingerprint"):
            self.assertNotIn(private_field, report)

    def test_markdown_missing_evidence_has_no_comparison_table(self):
        report = bench.comparison_markdown(bench.compare(self.plan, []))
        self.assertIn("Status: **`inconclusive`**", report)
        self.assertIn("missing or unexpected run recordings", report)
        self.assertIn("metrics are unavailable", report)
        self.assertNotIn("| Metric |", report)
        self.assertNotIn("FPS change:", report)

    def test_markdown_exposes_image_failure_despite_better_fps(self):
        records = self.recordings(20, 10)
        records[0]["image_check_passed"] = False
        report = bench.comparison_markdown(bench.compare(self.plan, records))
        self.assertIn("Status: **`correctness_failure`**", report)
        self.assertIn("a recorded image check failed", report)
        self.assertIn("FPS change: +100.00%", report)

    def test_markdown_preserves_hitches_and_noise_classification(self):
        records = self.recordings()
        records[0]["intervals_ms"][-1] = 17020
        records[0]["elapsed_s"] = sum(records[0]["intervals_ms"]) / 1000
        report = bench.comparison_markdown(bench.compare(self.plan, records))
        self.assertIn("Status: **`inconclusive`**", report)
        self.assertIn("run variation exceeds", report)
        self.assertIn("| Worst frame (ms) | 17020.00 | 20.00 |", report)
        self.assertIn("| Frames over 100 ms (all runs) | 1 | 0 |", report)

    def test_compare_cli_formats_preserve_outcome_exit_codes(self):
        failure = self.recordings(20, 10)
        failure[0]["image_check_passed"] = False
        cases = [(self.recordings(20, 16), "improvement", 0),
                 (self.recordings(), "no_clear_change", 0),
                 (self.recordings(20, 25), "regression", 1),
                 (failure, "correctness_failure", 1), ([], "inconclusive", 2)]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "plan.json").write_text(json.dumps(self.plan))
            for records, status, exit_code in cases:
                (root / "records.json").write_text(json.dumps(records))
                for fmt in (None, "json", "markdown"):
                    with self.subTest(status=status, format=fmt):
                        args = [sys.executable, "-I", str(HERE / "m4_benchmark.py"), "compare",
                                "--plan", str(root / "plan.json"),
                                "--recordings", str(root / "records.json")]
                        if fmt:
                            args.extend(["--format", fmt])
                        p = subprocess.run(args, capture_output=True, text=True)
                        self.assertEqual(p.returncode, exit_code, p.stderr)
                        if fmt == "markdown":
                            self.assertIn(f"Status: **`{status}`**", p.stdout)
                        else:
                            self.assertEqual(json.loads(p.stdout)["status"], status)

    def test_markdown_cli_writes_once_without_overwriting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "plan.json").write_text(json.dumps(self.plan))
            (root / "records.json").write_text(json.dumps(self.recordings()))
            output = root / "report.md"
            args = [sys.executable, "-I", str(HERE / "m4_benchmark.py"), "compare",
                    "--plan", str(root / "plan.json"), "--recordings", str(root / "records.json"),
                    "--format", "markdown", "--out", str(output)]
            p = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, "")
            original = output.read_text()
            self.assertIn("Status: **`no_clear_change`**", original)
            p = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertEqual(output.read_text(), original)

    def test_recording_uses_boundary_differences_without_losing_precision(self):
        receipt, trace = self.receipt_and_timestamps()
        record = bench.make_recording(self.plan, "run-001", receipt, trace)
        self.assertEqual(record["intervals_ms"], [20] * 60)
        self.assertEqual(record["elapsed_s"], 1.2)
        self.assertEqual(bench.run_metrics(record, self.plan["runs"][0], self.plan)["fps"], 50)

    def test_recording_preserves_a_long_pause(self):
        receipt, trace = self.receipt_and_timestamps()
        for boundary in trace["boundaries"][30:]:
            boundary["timestamp_ns"] += 17_000_000_000
        record = bench.make_recording(self.plan, "run-001", receipt, trace)
        self.assertEqual(record["intervals_ms"][29], 17020)
        self.assertAlmostEqual(record["elapsed_s"], 18.2)

    def test_recording_needs_exact_boundary_count(self):
        for change in (lambda b: b.pop(), lambda b: b.append(dict(b[-1]))):
            receipt, trace = self.receipt_and_timestamps()
            change(trace["boundaries"])
            with self.assertRaises(bench.BenchmarkError):
                bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_rejects_dropped_or_repeated_present_counters(self):
        for counter in (900, 902):
            receipt, trace = self.receipt_and_timestamps()
            trace["boundaries"][1]["present"] = counter
            with self.assertRaises(bench.BenchmarkError):
                bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_rejects_duplicate_or_reversed_timestamps(self):
        for offset in (0, -1):
            receipt, trace = self.receipt_and_timestamps()
            trace["boundaries"][1]["timestamp_ns"] = trace["boundaries"][0]["timestamp_ns"] + offset
            with self.assertRaises(bench.BenchmarkError):
                bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_requires_bounded_integer_clock_and_counters(self):
        for key in ("present", "timestamp_ns"):
            for value in (True, 1.0, "1", -1, 10**400):
                receipt, trace = self.receipt_and_timestamps()
                trace["boundaries"][0][key] = value
                with self.assertRaises(bench.BenchmarkError):
                    bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_rejects_ambiguous_clock_and_schema(self):
        for key, value in (("schema", True), ("schema", 1.0), ("clock", "wall_time_ms")):
            receipt, trace = self.receipt_and_timestamps()
            trace[key] = value
            with self.assertRaises(bench.BenchmarkError):
                bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_validates_observed_provenance_and_flags(self):
        for key, value in (("revision", "f" * 40), ("completed", False), ("clean_window", 1)):
            receipt, trace = self.receipt_and_timestamps()
            receipt[key] = value
            with self.assertRaises(bench.BenchmarkError):
                bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_cannot_supply_timing_in_metadata_receipt(self):
        receipt, trace = self.receipt_and_timestamps()
        receipt["elapsed_s"] = 0.1
        with self.assertRaises(bench.BenchmarkError):
            bench.make_recording(self.plan, "run-001", receipt, trace)

    def test_recording_drops_private_extras_and_preserves_image_failure(self):
        receipt, trace = self.receipt_and_timestamps()
        receipt.update(private_log="/private/log", image_check_passed=False)
        original = copy.deepcopy((receipt, trace))
        record = bench.make_recording(self.plan, "run-001", receipt, trace)
        self.assertNotIn("private_log", record)
        self.assertFalse(record["image_check_passed"])
        self.assertEqual((receipt, trace), original)

    def test_recording_unknown_run_is_rejected(self):
        receipt, trace = self.receipt_and_timestamps()
        with self.assertRaises(bench.BenchmarkError):
            bench.make_recording(self.plan, "run-999", receipt, trace)

    def test_record_cli_produces_a_valid_offline_recording(self):
        receipt, trace = self.receipt_and_timestamps()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name, value in (("plan", self.plan), ("receipt", receipt), ("timestamps", trace)):
                (root / (name + ".json")).write_text(json.dumps(value))
            p = subprocess.run([sys.executable, "-I", str(HERE / "m4_benchmark.py"), "record",
                                "--plan", str(root / "plan.json"), "--run-id", "run-001",
                                "--receipt", str(root / "receipt.json"),
                                "--timestamps", str(root / "timestamps.json")],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            record = json.loads(p.stdout)
            self.assertEqual(record["coverage"], "all_present_intervals")
            self.assertEqual(len(record["intervals_ms"]), 60)

    def test_preflight_refuses_other_platforms(self):
        with patch.object(bench.platform, "system", return_value="Linux"):
            with self.assertRaises(bench.BenchmarkError):
                bench.preflight()

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "report.json"
            p.write_text("existing")
            with self.assertRaises(FileExistsError):
                bench.write_result({}, p)
            self.assertEqual(p.read_text(), "existing")

    def test_run_command_fails_closed(self):
        p = subprocess.run([sys.executable, "-I", str(HERE / "m4_benchmark.py"), "run"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("execution disabled", p.stderr)

    def test_cli_plan_has_no_side_effects_besides_requested_report(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "plan.json"
            p = subprocess.run([sys.executable, "-I", str(HERE / "m4_benchmark.py"), "plan",
                                "--suite", str(HERE / "suite.example.json"), "--baseline", self.base,
                                "--candidate", self.candidate, "--out", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(out.read_text())["status"], "planned_not_executed")
            self.assertEqual(list(Path(d).iterdir()), [out])


if __name__ == "__main__":
    unittest.main()
