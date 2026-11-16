#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan private M4 benchmarks and compare complete recordings. Execution is not wired yet."""
import argparse
import hashlib
import json
import math
import platform
import re
import statistics
import subprocess
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SCENARIO = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
FINGERPRINTS = ("machine_fingerprint", "settings_fingerprint", "driver_fingerprint",
                "checkpoint_fingerprint", "cache_snapshot_fingerprint")


class BenchmarkError(ValueError):
    pass


def number(value, name, low=0, high=1e9):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{name} must be a number")
    if not low <= value <= high or not math.isfinite(value):
        raise BenchmarkError(f"{name} is outside its permitted range")
    return value


def integer(value, name, low, high):
    number(value, name, low, high)
    if not isinstance(value, int):
        raise BenchmarkError(f"{name} must be an integer")
    return value


def validate_suite(suite):
    if not isinstance(suite, dict) or type(suite.get("schema")) is not int or suite["schema"] != 1:
        raise BenchmarkError("unsupported suite schema")
    if not isinstance(suite.get("scenario"), str) or not SCENARIO.fullmatch(suite["scenario"]):
        raise BenchmarkError("invalid scenario identifier")
    if not isinstance(suite.get("program_id"), str) or not re.fullmatch(r"[0-9A-F]{16}", suite["program_id"]):
        raise BenchmarkError("invalid program ID")
    approved = suite.get("approved_revisions")
    if not isinstance(approved, list) or not approved or any(
            not isinstance(x, str) or not SHA.fullmatch(x) for x in approved):
        raise BenchmarkError("approved_revisions must contain full lowercase commit SHAs")
    integer(suite.get("repeats_per_variant"), "repeats_per_variant", 2, 10)
    integer(suite.get("measurement_frames"), "measurement_frames", 30, 36000)
    integer(suite.get("warmup_frames"), "warmup_frames", 0, 36000)
    for key in ("min_fps_change_percent", "max_p99_regression_percent", "max_run_variation_percent"):
        number(suite.get(key), key, 0.1, 100)
    return suite


def make_plan(suite, baseline, candidate, cache):
    validate_suite(suite)
    if cache not in ("warm", "cold_profile"):
        raise BenchmarkError("cache must be warm or cold_profile")
    for revision in (baseline, candidate):
        if not isinstance(revision, str) or not SHA.fullmatch(revision):
            raise BenchmarkError("revisions must be full lowercase commit SHAs")
        if revision not in suite["approved_revisions"]:
            raise BenchmarkError("revision is not in the reviewed suite allowlist")
    runs = []
    for repeat in range(suite["repeats_per_variant"]):
        order = ("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")
        for variant in order:
            runs.append({"id": f"run-{len(runs) + 1:03}", "variant": variant,
                         "revision": baseline if variant == "baseline" else candidate,
                         "pair": repeat + 1, "cache": cache,
                         "profile_policy": "fresh_copy",
                         "measurement_frames": suite["measurement_frames"],
                         "warmup_frames": suite["warmup_frames"]})
    return {"schema": 1, "status": "planned_not_executed", "suite": suite,
            "baseline": baseline, "candidate": candidate, "cache": cache, "runs": runs,
            "limits": ["No source checkout, build, emulator launch or runner registration occurs.",
                       "The allowlist records review intent; it does not establish artifact identity.",
                       "cold_profile means an empty isolated emulator cache, not a cold OS/driver cache.",
                       "The execution adapter and complete interval recorder are not implemented."]}


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_plan(plan):
    if not isinstance(plan, dict) or type(plan.get("schema")) is not int or plan["schema"] != 1 or not isinstance(plan.get("suite"), dict):
        raise BenchmarkError("invalid plan")
    expected = make_plan(plan["suite"], plan.get("baseline"), plan.get("candidate"), plan.get("cache"))
    if plan_digest(plan) != plan_digest(expected):
        raise BenchmarkError("plan differs from its generated run schedule")
    return plan


def percentile(values, fraction):
    rank = (len(values) - 1) * fraction
    lower, upper = math.floor(rank), math.ceil(rank)
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def run_metrics(record, expected, plan):
    if not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != 1:
        raise BenchmarkError("invalid recording schema")
    for key in ("id", "variant", "revision", "cache"):
        if record.get(key) != expected[key]:
            raise BenchmarkError(f"recording has mismatched {key}")
    if record.get("program_id") != plan["suite"]["program_id"]:
        raise BenchmarkError("recording has mismatched program ID")
    if record.get("plan_sha256") != plan_digest(plan):
        raise BenchmarkError("recording belongs to a different plan")
    if record.get("coverage") != "all_present_intervals":
        raise BenchmarkError("complete present intervals are required; a latest-120 list is insufficient")
    for key in FINGERPRINTS:
        if not isinstance(record.get(key), str) or not DIGEST.fullmatch(record[key]):
            raise BenchmarkError(f"missing or invalid {key}")
    for key in ("scene_verified", "image_check_passed", "clean_window", "completed"):
        if type(record.get(key)) is not bool:
            raise BenchmarkError(f"{key} must be an explicit boolean")
    if not record["completed"] or not record["clean_window"] or not record["scene_verified"]:
        raise BenchmarkError("recording is incomplete, contaminated, or from an unverified scene")
    intervals = record.get("intervals_ms")
    if not isinstance(intervals, list) or len(intervals) != expected["measurement_frames"]:
        raise BenchmarkError("recording does not cover the planned number of frame intervals")
    for value in intervals:
        number(value, "frame interval", 0.000001, 3_600_000)
    elapsed = number(record.get("elapsed_s"), "elapsed_s", 0.000001, 3600)
    if not math.isclose(math.fsum(intervals) / 1000, elapsed, rel_tol=1e-6, abs_tol=1e-6):
        raise BenchmarkError("frame-boundary elapsed time and complete intervals disagree")
    ordered = sorted(intervals)
    return {"fps": len(intervals) / elapsed, "p50_ms": percentile(ordered, .5),
            "p95_ms": percentile(ordered, .95), "p99_ms": percentile(ordered, .99),
            "max_ms": ordered[-1], "over100_ms": sum(x > 100 for x in intervals)}


def compare(plan, recordings):
    checked_plan(plan)
    result = {"schema": 1, "status": "inconclusive", "scenario": plan["suite"]["scenario"],
              "plan_sha256": plan_digest(plan), "reasons": [], "variants": {}}
    if not isinstance(recordings, list) or len(recordings) != len(plan["runs"]):
        result["reasons"].append("missing or unexpected run recordings")
        return result
    reference = None
    metrics = {"baseline": [], "candidate": []}
    image_failure = False
    for expected, record in zip(plan["runs"], recordings):
        try:
            measured = run_metrics(record, expected, plan)
            fingerprint = tuple(record[key] for key in FINGERPRINTS)
            if reference is not None and fingerprint != reference:
                raise BenchmarkError("machine/settings/driver/checkpoint/cache fingerprints differ")
            reference = fingerprint
            image_failure |= not record["image_check_passed"]
            metrics[expected["variant"]].append(measured)
        except (BenchmarkError, TypeError) as error:
            result["reasons"].append(str(error))
    if result["reasons"]:
        return result
    noisy = False
    for variant, samples in metrics.items():
        fps = statistics.median(x["fps"] for x in samples)
        variation = max(abs(x["fps"] / fps - 1) * 100 for x in samples)
        noisy |= variation > plan["suite"]["max_run_variation_percent"]
        result["variants"][variant] = {
            "runs": len(samples), "median_run_fps": fps,
            "median_run_p99_ms": statistics.median(x["p99_ms"] for x in samples),
            "worst_frame_ms": max(x["max_ms"] for x in samples),
            "frames_over100_ms": sum(x["over100_ms"] for x in samples),
            "max_fps_variation_percent": variation}
    before, after = (result["variants"][key] for key in ("baseline", "candidate"))
    fps_change = (after["median_run_fps"] / before["median_run_fps"] - 1) * 100
    tail_change = (after["median_run_p99_ms"] / before["median_run_p99_ms"] - 1) * 100
    result.update(fps_change_percent=fps_change, p99_change_percent=tail_change)
    if image_failure:
        result.update(status="correctness_failure", reasons=["a recorded image check failed"])
    elif noisy:
        result["reasons"].append("run variation exceeds the configured noise allowance")
    elif plan["baseline"] == plan["candidate"] and (
            abs(fps_change) >= plan["suite"]["min_fps_change_percent"] or
            abs(tail_change) >= plan["suite"]["max_p99_regression_percent"]):
        result["reasons"].append("A/A control differs despite identical source revisions")
    elif fps_change <= -plan["suite"]["min_fps_change_percent"] or tail_change >= plan["suite"]["max_p99_regression_percent"]:
        result["status"] = "regression"
    elif fps_change >= plan["suite"]["min_fps_change_percent"]:
        result["status"] = "improvement"
    else:
        result["status"] = "no_clear_change"
    result["limits"] = ["Configured thresholds are provisional, not statistical confidence intervals.",
                        "Fingerprints and scene/image checks must be produced by a trusted recorder.",
                        "No JIT or GPU cause is inferred from FPS alone."]
    return result


def preflight():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise BenchmarkError("native Apple silicon macOS is required")
    def sysctl(key):
        return subprocess.run(["/usr/sbin/sysctl", "-n", key], capture_output=True,
                              text=True, check=True).stdout.strip()
    cpu = sysctl("machdep.cpu.brand_string")
    if cpu != "Apple M4":
        raise BenchmarkError("this scaffold targets the base Apple M4")
    return {"schema": 1, "status": "preflight_only", "cpu": cpu,
            "memory_bytes": int(sysctl("hw.memsize")), "macos": platform.mac_ver()[0],
            "limits": ["Does not establish GUI readiness, thermal state, paging or GPU access.",
                       "Does not launch an emulator or change power/security settings."]}


def write_result(value, output):
    payload = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if output:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    else:
        print(payload, end="")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan")
    p.add_argument("--suite", type=Path, required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--cache", choices=("warm", "cold_profile"), default="warm")
    p.add_argument("--out", type=Path)
    p = commands.add_parser("compare")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--recordings", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p = commands.add_parser("preflight")
    p.add_argument("--out", type=Path)
    commands.add_parser("run", help="Reserved: execution remains disabled")
    args = parser.parse_args()
    try:
        if args.command == "run":
            raise BenchmarkError("execution disabled: implement and validate the isolated launch/record adapter first")
        if args.command == "plan":
            value = make_plan(json.loads(args.suite.read_text()), args.baseline, args.candidate, args.cache)
        elif args.command == "compare":
            value = compare(json.loads(args.plan.read_text()), json.loads(args.recordings.read_text()))
        else:
            value = preflight()
        write_result(value, args.out)
        if args.command == "compare" and value["status"] in ("regression", "correctness_failure"):
            return 1
        if args.command == "compare" and value["status"] == "inconclusive":
            return 2
        return 0
    except (BenchmarkError, OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(2, f"m4-benchmark: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
