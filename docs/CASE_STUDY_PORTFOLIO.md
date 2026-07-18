# Reclaim — portfolio blurb (150 words)

Reclaim is a Windows disk-cleanup agent: it scans a drive, proposes deletions, and — on
explicit approval — deletes. The engineering problem isn't finding junk; it's that a wrong
delete is unrecoverable, so every design decision optimizes for that constraint first.

Run against a real 3.1M-file disk in daily use, the exact-duplicate estimate was corrected four
times as real data exposed each overclaim: 48GB → 23GB (hardlink-aware accounting) → 4.26GB
(excluding model caches and live environments) → 3.92GB, after a live apply broke the project's
own Python environment by deleting files from three shared interpreter installs none of the
checks recognized. Recovered from the Recycle Bin — chosen for exactly this reason — then
root-caused twice: the installs missed, then why marker-based detection could never be complete.

Eleven real bugs, four themes (observability, scalability, selectivity, honesty), all found on
production-scale data no fixture reproduced — every safety mechanism defeated once by a shape
its tests never modeled. 33.73GB reclaimed, measured before/after. Honest, not triumphant.
