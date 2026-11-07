# traceDB design

Goal: RSI swarm gets data fast, never pulls more than it asked for, and the store always knows where everything is.

## Layout (any S3 bucket/prefix or local dir)
    packs/<sha256>.pack      immutable, ~32 MiB, 1 MiB independently-compressed blocks (zlib-1 / zstd if present / raw)
    segments/<sha256>.seg    immutable catalog delta (zlib JSONL: path, sha, size, pack, off, block lens, meta)
    HEAD                     JSON list of segment ids; the only mutable object, updated by compare-and-swap

## Write ordering (crash-safe; borrowed from all-git)
packs (create-only PUT) -> segment (create-only PUT) -> HEAD CAS. A crash anywhere before the CAS leaves invisible orphans.
Writers never block each other: CAS loser re-reads HEAD and appends its segment. Retry of identical content is a no-op.
`compact` merges segments (also auto at 64). Many small files -> few big objects (27,646 files -> 7 packs, 30% of raw bytes).

## Read path
1. Local SQLite catalog (rebuilt from segments; `sync` = 1 HEAD GET + only unseen segments). Point lookup ~7 us p50.
2. `plan` computes raw bytes / wire bytes / requests with zero fetches.
3. `read_many` coalesces ranges (gap <= 256 KiB), fetches in parallel (16), caches ranges on disk, decompresses only needed blocks.
4. **Budget**: every read is capped (default 8 MiB, `TRACEDB_MAX_BYTES`); over-budget raises `BudgetExceeded` with the plan.
   Range/head/tail reads touch only the 1 MiB blocks needed.

## What we took from all-git, and what we dropped
Took: immutable content-addressed packs + separate index, create-only/CAS ordering, ranged reads with coalescing,
content-keyed cache (safe after GC), compaction from day one, "derive, don't invent" ontology.
Dropped: sharded manifest/2PC, locks/merge/zones (we have one logical writer per segment), delta packs, hand-rolled SigV4.
Not trusted: most of all-git's latency tables (loopback/moto, single runs on loaded boxes; its SlowDown test contradicts itself).
So every performance claim here must be measured by `bench/` on real data, and S3 numbers on real S3.

## Measured (local backend, real inference-experiment data, M-series Mac; `bench/bench_real.py`)
27,646 files / 670 MiB -> 7 packs / 199 MiB in 6.6 s. Cold catalog rebuild 0.30 s; no-change sync 0.23 ms; locate p50 7 us, p99 14 us;
prefix ls (762 hits) 1.5 ms; 1000 small files cold-cache pull 72 ms. **These exclude network.** S3 latency is unmeasured so far.

## Measured on real S3 (us-east-1, from a laptop, so RTT ~70 ms; `bench/results/s3-*.txt`)
Conditional create-only PUT and HEAD CAS behave (stale etag refused); 6 concurrent writers lost nothing; verify ok.
Ingest 199 MiB: 16 s. Cold catalog sync 0.9 s (2 MB segment, network-bound); no-change sync = one HEAD GET ~70 ms (`TRACEDB_SYNC_TTL` skips it).
Whole-directory pull (what ontolagni points at): **1 request**, 70-370 ms for 0.1-14 MiB raw.
Random small files: tuned by sweep (workers x merge gap). 1000 random small files: 1399 ms / 6.5x amplification (16 workers, 256K gap)
-> **774 ms / 1.8x** (64 workers, 64K gap, now the defaults). 100 random: 334 ms. Single file after pre-warm: 68 ms (one RTT).
Pre-warming the connection pool at first sync removed ~100 ms of serial TLS handshakes from the first read.

## Known gaps / next
- Benchmark from inside AWS (same region): laptop RTT dominates the numbers above.
- Appending to a growing jsonl re-stores the whole file (dedup is whole-file sha). Tail-append segments would fix it.
- GC of orphaned packs/segments, cache eviction (cache dir is unbounded today).
- Path overwrite = latest segment wins; no history API yet.
- In-process catalog only; a daemon/mmap'd shared catalog would help many agents on one box.
