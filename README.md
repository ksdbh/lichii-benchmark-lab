# Lichii Benchmark Lab

A standalone toolkit for planning repeatable Apple M4 emulator comparisons and validating complete frame recordings.

**Current state: scaffold.** Planning, read-only host preflight, offline timestamp conversion, comparison and synthetic-data tests work. Emulator execution is deliberately disabled. No runner is registered, no cloud resources are created, and no gameplay performance result is claimed.

The project contains original benchmark tooling developed under the `ksdbh` account. It was first published in [the Lichii fork’s scaffold contribution](https://github.com/ksdbh/Lichii/commit/f8dd2a2c05ffa3fe933f650ae3d6df3c73b42a27). This independent repository contains the harness only; it does not import the emulator’s source or history.

## Quick checks

Python 3.10 or newer, standard library only:

```sh
python3 -I -m unittest discover -s benchmark -p test_m4_benchmark.py
python3 -I benchmark/m4_benchmark.py --help
python3 -I benchmark/m4_benchmark.py preflight
```

`preflight` requires a native Apple M4 running macOS. Tests and plan generation also run on Linux CI. The `run` command returns an error until an isolated execution adapter and complete interval recorder are implemented.

## What is included

- Reviewed full-commit allowlists and alternating baseline/candidate schedules.
- Strict recording provenance and complete-interval validation.
- Offline conversion of consecutive present timestamps, preserving stalls and rejecting missing boundaries.
- Provisional FPS and tail-latency comparisons, including inconclusive and correctness-failure results.
- Concise Markdown comparison reports alongside the default JSON output.
- A/A control checks that do not label identical-code discrepancies as optimizations.
- A dormant private-controller workflow template, plus hosted-only unit-test CI.

See [the setup guide and recording contract](benchmark/README.md). The template stays outside `.github/workflows` so this public repository cannot schedule native benchmark jobs. Use a separate private controller, or an existing trusted private repository, when runner setup is authorized. Keep all game files, keys, saves, raw recordings and captures outside this repository.

## License

GPL-3.0-or-later. See [LICENSE.txt](LICENSE.txt).
