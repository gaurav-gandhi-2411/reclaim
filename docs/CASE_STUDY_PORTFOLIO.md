# Reclaim — portfolio blurb (150 words)

Reclaim is a Windows disk-cleanup agent: it scans a drive, proposes deletions, and — on
explicit approval — deletes. The engineering problem isn't finding junk; it's that a wrong
delete is unrecoverable, so every design decision optimizes for that constraint first.

Run against a real 3.1M-file disk in daily use, the exact-duplicate estimate was corrected
four times as real data exposed each overclaim: 48GB (naive byte match) → 23GB (hardlink-aware
accounting) → 4.26GB (excluding model caches and live environments) → 3.92GB, after a live
apply broke the project's own Python environment by recycle-binning files from a shared
interpreter install none of the existing checks recognized. Recovered in full from the Recycle
Bin — chosen for exactly this reason — within the hour, then root-caused and closed.

Eleven real bugs, four themes (observability, scalability, selectivity, honesty), all found on
production-scale data no fixture ever reproduced. 33.73GB reclaimed, measured before/after, not
estimated.
