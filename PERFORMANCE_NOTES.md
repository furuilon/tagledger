# Recorded performance notes

These measurements were recorded on 2026-11-11 in the local Windows workspace with Python 3.11. They use synthetic Git reference text and are not release benchmarks, CI thresholds, or guarantees for networked repositories.

| Workload | Recorded result | Scope |
|---|---:|---|
| Parse 10,000 synthetic `ls-remote` tag rows, 7 repetitions | Median 17.994 ms; approximately 555,735 rows/second | Includes parsing and deterministic sorting; excludes network latency and baseline disk writes. |
| Parse, baseline, and compare two 100,000-row inputs; 3,000 expected changes | 5.980 s total; malformed final row rejected at row 100,001 in 0.949 s | Separate stage timings: parse old 1.147 s, parse current 1.164 s, baseline 2.220 s, compare 1.449 s. |
| Memory during the 100,000-row workload | Sampled RSS increase 73.0 MiB; separate `tracemalloc` Python allocation peak 73.9 MiB | RSS began with both generated input strings already in memory; sampling every 5 ms can miss short-lived peaks. `tracemalloc` materially slows execution. |

The inputs were generated locally and no public host was contacted. Network latency, very large real repositories, slow disks, and sustained remote operations were not measured. See the Hidden Gems portfolio research notes for the broader cross-project benchmark context; those workspace-level records are not included in this standalone project.
