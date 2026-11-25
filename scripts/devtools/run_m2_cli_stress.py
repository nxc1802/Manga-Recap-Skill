"""
tests/adversarial/run_m2_cli_stress.py
Empirical CLI Stress & Adversarial Telemetry Runner for Milestone M2 (PR-02 & PR-03).
Runs high-throughput CLI invocations across diverse boundary cases, measures latency,
verifies 100% stdout purity, and generates empirical challenge statistics.
"""

import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

# Ensure project root in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.cli.exit_codes import ExitCode
from src.cli.response import JsonEnvelope
from src.core.storage import init_chapter_layout, init_project_layout

PYTHON_BIN = sys.executable


def run_cli_proc(args: list[str], env: dict[str, str]) -> tuple[subprocess.CompletedProcess, float]:
    cmd = [PYTHON_BIN, "-m", "src.cli.main"] + args
    t0 = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    elapsed = time.perf_counter() - t0
    return res, elapsed


def main():
    print("=" * 80)
    print("M2 CLI EMPIRICAL ADVERSARIAL STRESS HARNESS")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy()
        env["MANGA_RECAP_DATA_DIR"] = td

        # Initialize mock project
        init_project_layout("stress_proj", "Stress Project")
        init_chapter_layout("stress_proj", "1")

        test_scenarios = [
            ("Version flag (no json)", ["--version"], 0),
            ("Status chapter (valid)", ["status", "-p", "stress_proj", "-c", "1", "--json"], 0),
            ("Inspect chapter (valid)", ["inspect", "-p", "stress_proj", "-c", "1", "--json"], 0),
            ("TTS benchmark (valid)", ["tts_benchmark", "--voices", "Trúc Ly", "--json"], 0),
            ("Inspect missing project (IO_ERROR)", ["inspect", "-p", "nonexistent_proj", "--json"], 3),
            ("Status missing project (IO_ERROR)", ["status", "-p", "nonexistent_proj", "--json"], 3),
            ("Validate missing project (IO_ERROR)", ["validate", "-p", "nonexistent_proj", "--json"], 3),
            ("Missing required project arg (VALIDATION_ERROR)", ["inspect", "--json"], 2),
            ("Crawl missing url and import-dir (VALIDATION_ERROR)", ["crawl", "-p", "stress_proj", "-c", "1", "--json"], 2),
            ("Fuzz invalid flag (DUPLICATE JSON BUG)", ["status", "-p", "stress_proj", "--fuzz-bogus-arg", "--json"], 2),
            ("Missing subcommand (DUPLICATE JSON BUG)", ["--json"], 2),
        ]

        results = []

        print(f"\nRunning 10 iterations per scenario across {len(test_scenarios)} test cases...")

        for name, args, expected_code in test_scenarios:
            latencies = []
            codes = []
            parse_successes = 0
            parse_errors = []

            for _ in range(10):
                proc, elapsed = run_cli_proc(args, env)
                latencies.append(elapsed)
                codes.append(proc.returncode)

                if "--json" in args:
                    try:
                        envelope = json.loads(proc.stdout)
                        JsonEnvelope.model_validate(envelope)
                        parse_successes += 1
                    except Exception as e:
                        parse_errors.append(str(e))

            avg_lat = statistics.mean(latencies) * 1000
            p95_lat = statistics.quantiles(latencies, n=20)[18] * 1000 if len(latencies) >= 20 else max(latencies) * 1000
            all_codes_match = all(c == expected_code for c in codes)

            results.append({
                "scenario": name,
                "expected_exit_code": expected_code,
                "actual_exit_codes": list(set(codes)),
                "exit_code_pass": all_codes_match,
                "json_tested": "--json" in args,
                "json_parse_rate": f"{parse_successes}/10",
                "parse_errors": list(set(parse_errors)),
                "mean_latency_ms": round(avg_lat, 2),
                "p95_latency_ms": round(p95_lat, 2),
            })

            status_icon = "✅" if all_codes_match and (parse_successes == 10 or "--json" not in args) else "❌"
            print(f"[{status_icon}] {name:50s} -> ReturnCode: {codes[0]} | JSON: {parse_successes}/10 | Mean: {avg_lat:.1f}ms")

        print("\n" + "=" * 80)
        print("SUMMARY TELEMETRY TABLE")
        print("=" * 80)
        print(f"{'Scenario':<45} | {'Exp':<4} | {'Act':<4} | {'JSON Pass':<10} | {'Mean (ms)':<10}")
        print("-" * 80)
        for r in results:
            print(f"{r['scenario']:<45} | {r['expected_exit_code']:<4} | {r['actual_exit_codes'][0]:<4} | {r['json_parse_rate']:<10} | {r['mean_latency_ms']:<10.2f}")

        # Dump results JSON
        out_path = Path(__file__).resolve().parent / "m2_stress_telemetry.json"
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nTelemetry saved to: {out_path}")


if __name__ == "__main__":
    main()
