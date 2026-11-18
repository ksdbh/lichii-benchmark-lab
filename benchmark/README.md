# M4 benchmark scaffold

This directory prepares comparisons of two exact source revisions on the owner's M4. It currently validates inputs and creates a plan. It does not build, launch, control or benchmark an emulator. The `run` command refuses execution until a macOS execution adapter and a complete interval recorder are implemented. A successful planning job is not a benchmark pass.

## Does this need a separate repository?

A new repository is optional. Use a private controller repository, or an existing private repository whose workflow code and access are trusted. Keep this template in the public source repository for review, but do not register the M4 as a runner for that public repository. The private controller holds the reviewed orchestration code; baseline and candidate SHAs identify the source under comparison, not the workflow to execute.

The intended runner is the existing M4. This scaffold provisions no AWS service, virtual machine or cloud GPU. Local power, storage and any separate account costs remain outside the scaffold.

## Future private-controller setup

These are manual setup instructions, not actions performed by this contribution:

1. Copy `m4_benchmark.py`, the reviewed suite configuration and this README into the same `benchmark/` paths in the private controller. Copy `templates/m4-benchmark.yml` to its `.github/workflows/m4-benchmark.yml`.
2. Review and commit the controller files on its default branch. Keep game files, keys, saves, private paths and raw captures outside Git.
3. Keep dispatch and workflow-edit access restricted to the owner or explicitly trusted maintainers. The plan-only workflow uses manual dispatch, the default-branch gate, an explicit enable variable and the reviewed SHA allowlist. An environment with required reviewers is an optional additional control, with [availability depending on the GitHub plan](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments); it is not required here, and no paid upgrade is needed by this scaffold.
4. Separately register an appropriately restricted self-hosted runner for this private controller, with labels `self-hosted`, `macOS`, `ARM64`, `lichii-m4`. Runner registration and credentials are not included here. Avoid assigning that runner to unrelated repositories or jobs while measurements are active.
5. Only after reviewing that setup, set repository variable `M4_BENCHMARK_ENABLED` to the literal string `true`. Dispatch from the default branch with full baseline and candidate commit SHAs.

The template checks repository privacy, the explicit enable variable and default-branch dispatch before scheduling its job. It requests only `contents: read`, pins checkout, and disables persisted checkout credentials. It checks out the controller revision associated with the dispatch; it never checks out either input revision. Inputs enter the shell through quoted environment variables rather than executable workflow expressions.

Concurrency allows one running job in this group and does not cancel the active job. The default pending slot can be replaced by a newer dispatch; submit comparisons deliberately. See [GitHub concurrency behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency). These controls do not replace reviewing code that a future adapter would build or execute on a machine containing private data.

## Planning locally

```sh
python3 -I benchmark/m4_benchmark.py plan \
  --suite benchmark/suite.example.json \
  --baseline "$BASELINE_SHA" --candidate "$CANDIDATE_SHA" \
  --cache warm --out /tmp/m4-plan.json
```

Set both variables to full reviewed commit SHAs. `suite.example.json` is an example configuration, not evidence that its scenes or measurements have run. The workflow stores its plan in `RUNNER_TEMP`; it uploads no artifacts, screenshots, logs or game data. Its summary reports planning status only.

The suite allowlist must explicitly include both revisions. The generated schedule alternates baseline/candidate and candidate/baseline pairs, using fresh profile copies. The plan is validated again when comparing recordings; changing its schedule or limits by hand is rejected. Approval of a source SHA does not verify the identity of a later executable.

`preflight` only checks native arm64 macOS and the base Apple M4 model, and reports system metadata. It does not establish GUI availability, driver identity, thermal stability, paging or readiness to measure. `run` always exits with an error while execution remains unimplemented.

## Measurement contract

A future execution adapter must use isolated copies of the user profile and saves, bound its own process lifetime, and preserve the original app, game and profile. Run one emulator and one measurement at a time. Build and warm-up work must finish before measured intervals; separate clean performance passes from compilation/JIT diagnostic passes. Record source and artifact identities, driver, settings, scene/checkpoint, input sequence, cache condition, interval boundaries and completion status so a comparison can be audited.

`warm` and `cold_profile` describe profile caches. `cold_profile` does not promise a cold Apple system/compiler cache. Never clear system caches or relabel a repeated run as cold without evidence. Lichii’s existing `tools/nx2/cache_bench.py` is a Windows workflow that swaps real profile caches; do not reuse it as the macOS adapter.

The existing debug server's `input_sequence` can provide frame-counted input steps. Its integration still needs a complete frame-interval recorder: do not infer an entire long run from a short retained `frame_times` window, and do not issue simultaneous blocking metric RPCs while an input sequence owns the server. Preserve gaps, timeouts and incomplete intervals as missing evidence, not zero-duration frames or passing results.

Any report must distinguish steady frame pacing, first-use compilation hitches, CPU cache clears and startup/cache restoration. CPU time is not GPU utilization, and zero or unavailable per-dispatch timestamps on MoltenVK do not prove zero GPU work. Comparisons need matched conditions and reversals before a performance change is attributed to code. No gameplay speedup or benchmark result is claimed by this scaffold.

Only explicitly reviewed aggregate reports should leave the private controller. Raw logs, game assets, captures and screenshots remain local; the template has no automatic upload step.

## Recording and comparison format

The offline `record` command converts complete present timestamps into validated recordings. Native timestamp capture and a raw-log importer are not implemented yet. `compare` accepts a JSON array in the exact generated run order, with one recording per planned run. Each recording supplies:

- `schema: 1`, matching `id`, `variant`, `revision`, `cache` and `program_id`.
- `plan_sha256`: SHA-256 of the plan serialized with Python `json.dumps(plan, sort_keys=True, separators=(",", ":"))`, encoded as UTF-8. This binds the recording to the complete plan, including its scenario, measurement limits and schedule.
- `coverage: "all_present_intervals"`, and `intervals_ms` containing exactly `measurement_frames` positive finite intervals. `elapsed_s` must equal their sum divided by 1000, within the validator's tolerance. Measure elapsed time between those same frame boundaries, not from process launch or warm-up start.
- Explicit boolean `scene_verified`, `image_check_passed`, `clean_window` and `completed` fields. The trusted recorder must supply these checks; setting a field does not itself establish the fact.
- Five lowercase SHA-256 values: `machine_fingerprint`, `settings_fingerprint`, `driver_fingerprint`, `checkpoint_fingerprint` and `cache_snapshot_fingerprint`. Each corresponding fingerprint must agree across all recordings. The cache fingerprint identifies the initial copied seed before variant-specific warm-up, not the resulting warmed cache bytes. The future adapter must define reproducible inputs to these hashes before it can produce trustworthy comparisons.

### Converting captured timestamps

Supply a metadata receipt with the fields above except `coverage`, `intervals_ms` and `elapsed_s`; timing fields in the receipt are rejected. The receipt must come from a trusted recorder. Conversion validates its format and plan identity, but cannot establish that the scene or image checks actually occurred.

The timestamp file uses this structure (shown abbreviated):

```json
{
  "schema": 1,
  "clock": "monotonic_ns",
  "boundaries": [
    {"present": 900, "timestamp_ns": 1000000000000000000},
    {"present": 901, "timestamp_ns": 1000000000020000000}
  ]
}
```

Capture exactly `measurement_frames + 1` consecutive boundaries after warm-up. Present counters must be consecutive integers; monotonic nanosecond timestamps must strictly increase. Missing or repeated boundaries are rejected. A long time between consecutive presents remains a long interval, so stalls contribute to elapsed time, FPS and tail latency. Timestamps are subtracted as integers before conversion to milliseconds to preserve precision at large clock origins.

```sh
python3 -I benchmark/m4_benchmark.py record \
  --plan /tmp/m4-plan.json --run-id run-001 \
  --receipt /tmp/m4-receipt.json --timestamps /tmp/m4-presents.json \
  --out /tmp/m4-run-001.json
```

The output contains only the documented recording fields; extra receipt fields are discarded. This command launches nothing and does not collect timestamps itself. A successful conversion means the recording validates, not that performance improved or an image check passed. An explicit failed image check is retained for comparison.

### Comparing runs

Keep recordings private. Assemble the individual recording objects into one JSON array in the plan's run order. To produce the aggregate report:

```sh
python3 -I benchmark/m4_benchmark.py compare \
  --plan /tmp/m4-plan.json --recordings /tmp/m4-recordings.json \
  --out /tmp/m4-comparison.json
```

Output files must not already exist. Reports contain selected aggregate fields, not raw intervals, paths, images or logs. Missing, mismatched, incomplete or noisy evidence yields `inconclusive`; a recorded image-check failure yields `correctness_failure` once the recordings otherwise validate. Valid comparisons can report `regression`, `improvement` or `no_clear_change` using median run FPS, median run p99 and the configured thresholds. These labels are provisional decision rules, not statistical confidence intervals or an assertion that the recorder has been implemented.

`compare` exits 2 for inconclusive/invalid input, 1 for regression or correctness failure, and 0 for improvement or no clear change. Exit 0 alone is not evidence of a speedup. No timing or correctness outcome is supplied by this scaffold itself.

## Scaffold checks

The `.github/workflows/benchmark-harness-check.yml` runs only Python tests on a GitHub-hosted Ubuntu runner. It never selects the M4 runner or executes a game. Local checks are:

```sh
python3 -I -m unittest discover -s benchmark -p test_m4_benchmark.py
actionlint -config-file benchmark/actionlint.yaml \
  .github/workflows/benchmark-harness-check.yml \
  benchmark/templates/m4-benchmark.yml
```

`actionlint.yaml` declares the custom `lichii-m4` runner label. The tests use generated synthetic recordings only; their performance classifications are not measurements of the emulator.
