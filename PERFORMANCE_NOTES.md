# Recorded performance notes

These measurements were recorded on a Windows machine with Python 3.11.0 on 2026-11-11. They use synthetic Git reference text and are not release benchmarks, CI thresholds, or guarantees for networked repositories.

| Workload | Recorded result | Scope |
|---|---:|---|
| Parse 10,000 synthetic `ls-remote` tag rows, 7 repetitions | Median 17.994 ms; approximately 555,735 rows/second | Includes parsing and deterministic sorting; excludes network latency and baseline disk writes. |
| Parse, baseline, and compare two 100,000-row inputs; 3,000 expected changes | 5.980 s total; malformed final row rejected at row 100,001 in 0.949 s | Separate stage timings: parse old 1.147 s, parse current 1.164 s, baseline 2.220 s, compare 1.449 s. |
| Memory during the 100,000-row workload | Sampled RSS increase 73.0 MiB; separate `tracemalloc` Python allocation peak 73.9 MiB | RSS began with both generated input strings already in memory; sampling every 5 ms can miss short-lived peaks. `tracemalloc` materially slows execution. |

The inputs were generated locally and no public host was contacted. Network latency, very large real repositories, slow disks, and sustained remote operations were not measured. These focused parser measurements do not represent a broader cross-project or large-repository benchmark.

## 2026-11-16 reproducible parser recheck

Environment: Python 3.11.0, Git 2.54.0.windows.1, Windows build 26200 / DisplayVersion 25H2. Microsoft's release table maps build 26200 to Windows 11 25H2 ([Windows release information](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)). The sandbox did not expose hardware model or RAM through its CIM interface, so these results should not be generalized to other machines. No public host was contacted.

Reproduce from the project root with `python tools/benchmark_parser.py`. The script prints the SHA-256 of the exact `src/tagledger/core.py` measured; for this run it was `809e8003bf75529e183ba27ec4bc06ed26c228408695061feeb614bbfa2c6f27`. Synthetic rows are generated as `f"{i:040x}\trefs/tags/v1.2.{i}\n"` and passed to `parse_ls_remote`. Timings include line splitting, validation, object creation and deterministic sorting; they exclude Git process startup, network, baseline writing, comparison and report rendering.

| Synthetic rows | Repetitions | Median parse time | Median throughput |
|---:|---:|---:|---:|
| 10,000 | 7 | 52.292 ms | 191,235 rows/s |
| 100,000 | 3 | 635.836 ms | 157,273 rows/s |

These reproducible focused parser timings supersede the earlier same-day ad hoc 37.241 ms / 401.330 ms measurements, which used a different harness and were not tied to a source hash. They are not directly comparable to the earlier 100,000-row parse/baseline/compare measurement above. Current guards cap Git stdout at 16 MiB, baseline JSON at 64 MiB and distinct tags at 100,000; the query timeout remains 60 seconds. The 100,000-row run exercises the maximum distinct-tag count for the parser, but no sustained memory, network, end-to-end report, or cross-machine benchmark was run in this recheck.
