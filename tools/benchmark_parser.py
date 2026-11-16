from __future__ import annotations

import hashlib
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tagledger.core import parse_ls_remote  # noqa: E402


def benchmark(row_count: int, repetitions: int) -> tuple[float, float]:
    output = "".join(
        f"{index:040x}\trefs/tags/v1.2.{index}\n" for index in range(row_count)
    )
    samples = []
    for _ in range(repetitions):
        started = time.perf_counter()
        tags = parse_ls_remote(output)
        samples.append(time.perf_counter() - started)
        if len(tags) != row_count:
            raise RuntimeError(f"Expected {row_count} parsed refs, found {len(tags)}.")
    median_seconds = statistics.median(samples)
    throughput = row_count / median_seconds
    return median_seconds, throughput


def main() -> None:
    core_path = PROJECT_ROOT / "src" / "tagledger" / "core.py"
    core_sha256 = hashlib.sha256(core_path.read_bytes()).hexdigest()
    print(f"core.py SHA-256: {core_sha256}")
    print("Synthetic parser-only timings; no Git process or network is used.")
    for row_count, repetitions in ((10_000, 7), (100_000, 3)):
        median_seconds, throughput = benchmark(row_count, repetitions)
        print(
            f"{row_count:,} rows; {repetitions} runs; "
            f"median {median_seconds * 1000:.3f} ms; "
            f"{throughput:,.0f} rows/s"
        )


if __name__ == "__main__":
    main()
