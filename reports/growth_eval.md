# Disk growth attribution: eval on this machine's real logs

Produced by `python evals/growth/growth_eval.py` at commit `45c36aaf578961b231d83a2972034104239dd292` (code and fixtures);
machine-readable twin: `reports/growth_eval.json`. Ground truth:
`evals/fixtures/growth/events.json` (5 evaluable of 12 documented events). Detector = `reclaim.growth.detect_anomalies`
with spec defaults (window freshness 6 h / 1 h), replayed unchanged; baseline = alert
when any key's growth over
24 h or 72 h is >= 5 GiB, blamed on the largest grower. No tuning on these events.

**Summary.** As replayed, the detector finds 1/5 evaluable events and the baseline 2/5; read literally (spec 1.5) the detector finds 1/5 and the baseline 0/5. That is 5 events from 30.77 h of fragments: it ranks neither method and says nothing about the spec's success metric (see the honesty section).

**Not an independent hit.** Event `hf-copy-to-d-2026-10-09` was added to the ground truth after the first replay had shown a D: alert, and the alert-to-event match uses a factor-2 size band that was added after a 16 GiB alert was seen matching a 185 GiB copy (both described under "Two events were added after the first replay" below). Counting only the events that existed before that replay, the detector finds 0/4 as replayed and 0/4 read literally, the baseline 2/4 and 0/4. The detector's one detection is therefore not evidence that it works.

| metric | detector (as replayed) | detector (spec-literal) | baseline | baseline (spec-literal) |
|---|---|---|---|---|
| recall (evaluable events detected) | 1/5 | 1/5 | 2/5 | 0/5 |
| top-1 attribution (detected events with a known folder) | 0/0 | 0/0 | 0/2 | 0/0 |
| alert episodes | 5 | 3 | 2 | 1 |
| false-alarm episodes | 4 | 2 | 1 | 1 |
| precision (lower bound) | 0.2 | 0.333 | 0.5 | 0.0 |
| false-alarm episodes per week of data | 21.84 (not meaningful) | 10.92 (not meaningful) | 5.46 (not meaningful) | 5.46 (not meaningful) |

A "false alarm" here means no documented event covers the episode, not a verified non-event: the docs do not list every real write.

## Change log

- Detector fix, window freshness (spec 1.5): before = code `4435381165df666cb67b5c5d5bea8b56e31e13b9` (report `60028742245f969a6aebf0d62d666687711f7e80`), after = `4d497dea65ff4ecdaf0d80a6f9feb7091fabc33e`. Fixtures, abs_floor, share_floor and z thresholds unchanged; the baseline is unchanged (it passes no freshness limits).

| detector variant | recall before -> after | alert episodes | false-alarm episodes | precision (lower bound) |
|---|---|---|---|---|
| detector | 3/5 -> 1/5 | 16 -> 5 | 14 -> 4 | 0.125 -> 0.2 |
| detector_literal | 1/5 -> 1/5 | 10 -> 3 | 9 -> 2 | 0.1 -> 0.333 |

Read the drop in recall with the honesty section: it is the two census-time detections that rested on a 52 h old C: sample.

- Detector fix, volume rule alerts on growth in excess of the volume's own daily baseline (steady growth): before = code `e1c40b0847ac11d6455f8205a0fa6534859e69c9` (report `c768b927cedabc339bbb61be76ca352b0f260086`), after = `53efd8563fd40c38d80d3d3aa847309dcb6995d6`. Fixtures, abs_floor, share_floor, z_min, windows and the key rule unchanged; the baseline is unchanged (it does not call the detector). A first attempt at this fix (`44c8a5d`, volume rate robust z >= z_min) was rejected by a falsifying verifier before merge: it compared a window-average rate with per-interval rate dispersion, so on a noisy volume a real +40 GiB / 24 h surge was suppressed (0 alerts where `e1c40b0` gave 32 on the same series) and bursty steady growth (+2 or +3 GiB once a day) still alarmed on every snapshot (MAD 0, z dominated by eps). The rule now subtracts the median daily-step rate of the previous 14 days times the window length and alerts if the excess is >= abs_floor; no new threshold.

| detector variant | recall before -> after | alert episodes | false-alarm episodes | precision (lower bound) |
|---|---|---|---|---|
| detector | 1/5 -> 1/5 | 5 -> 5 | 4 -> 4 | 0.2 -> 0.2 |
| detector_literal | 1/5 -> 1/5 | 3 -> 3 | 2 -> 2 | 0.333 -> 0.333 |

A "false alarm" here means no documented event covers the episode, not a verified non-event: the docs do not list every real write.

Synthetic controls (seed 42; `e1c40b0` and the rejected `44c8a5d` measured by running the same control functions on the exported code, constants below; warm-up = first 60 snapshots):

| control | `e1c40b0` (before) | `44c8a5d` (rejected) | after |
|---|---|---|---|
| steady 1.0 GiB/day, 84 snapshots: alerting (after warm-up) | 0 (0) | 0 (0) | 0 (0) |
| steady 1.5 GiB/day, 84 snapshots: alerting (after warm-up) | 0 (0) | 0 (0) | 0 (0) |
| steady 2.0 GiB/day, 84 snapshots: alerting (after warm-up) | 66 (24) | 6 (0) | 36 (0) |
| steady 3.0 GiB/day, 84 snapshots: alerting (after warm-up) | 66 (24) | 6 (0) | 36 (0) |
| bursty steady 2.0 GiB once a day, 180 snapshots: alerting (after warm-up) | 162 (120) | 162 (120) | 36 (0) |
| bursty steady 3.0 GiB once a day, 180 snapshots: alerting (after warm-up) | 162 (120) | 162 (120) | 36 (0) |
| 2.0 GiB/day + 20 GiB in one day: alerting from the surge day on | 12 | 11 | 10 |
| random walk sd 3.0 GiB / 4 h + 40 GiB over a day: alerts from the surge start (first alert, h after start) | 32 (4 h) | 0 | 29 (4 h) |
| the same walk without the surge: alerts | 27 | 0 | 24 |

The rule still fires on a surge on top of steady growth and on a noisy volume; it no longer fires on the steady growth underneath. On the noisy walk the 5 GiB floor is far below the walk's own 24 h spread, so noise alone still alerts (row above): this rule cannot tell a surge from noise there, but it does not suppress the surge, which a zero would have meant (the rejected attempt's result).

Alert episodes the pre-fix volume rule raised on the real fixtures: 5 (4 false-alarm, recall 1/5); the rule no longer raises 0 of them. Any dropped alert is DROPPED AND UNVERIFIED, not a win: A "false alarm" here means no documented event covers the episode, not a verified non-event: the docs do not list every real write.

Of the pre-fix rule's 55 alerts on these fixtures, 0 had >= 6 daily intervals of history before their window; the rest fall back to the floor alone.


## Coverage (hours of data per source)

| source | hours | continuous stretches | longest stretch (h) | samples | span (days) |
|---|---|---|---|---|---|
| attrib | 2.67 | 2 | 2.04 | 131 | 2.4 |
| census | 0.0 | 2 | 0.0 | 2 | 3.25 |
| watchdog | 10.64 | 11 | 4.71 | 584 | 15.92 |
| jobs | 20.11 | 3 | 12.98 | 206 | 1.31 |
| all_sources | 30.77 | 15 | 12.98 | 920 | 23.61 |

Stretches end at a hole longer than 1 h. `census` is two snapshots, so 0 h.

## Evaluable events

| event | detector | detector_literal | baseline | baseline_literal |
|---|---|---|---|---|
| hf-copy-to-d-2026-10-09 | detected (volume alert 177.6 GiB, start 29.2 h old, attribution n/a) | detected (volume alert 177.6 GiB, start 29.2 h old, attribution n/a) | missed | missed |
| c-drop-2026-10-09-1522 | missed | missed | missed | missed |
| build-c-drop-2026-10-10 | missed | missed | missed | missed |
| temp-growth-2026-10-09-to-12 | missed | missed | detected (key alert 60.6 GiB, start 78.0 h old, attribution coarse) | missed |
| ml-projects-growth-2026-10-09-to-12 | missed | missed | detected (key alert 60.6 GiB, start 78.0 h old, attribution wrong) | missed |

What the detector's windows saw at each evaluable event's end:

A negative volume growth means used space FELL between the window's start sample and the
event's end (BELIEVED: the pagefile left C: at the 9 Oct 13:02 boot and the HF cache
moved off C: 9 Oct 15:47-18:59, so a rise on top is invisible to the 5 GiB volume rule).

- `hf-copy-to-d-2026-10-09`: {"volume_growth_24h_gib": 210.5, "volume_growth_72h_gib": null}
- `c-drop-2026-10-09-1522`: {"volume_growth_24h_gib": -13.5, "volume_growth_72h_gib": null}
- `build-c-drop-2026-10-10`: {"volume_growth_24h_gib": -106.5, "volume_growth_72h_gib": null}
- `temp-growth-2026-10-09-to-12`: {"volume_growth_24h_gib": null, "volume_growth_72h_gib": 84.6, "key_growth_72h_gib": 59.6, "key_z": null, "key_z_note": "None = fewer than 6 thinned intervals in the 14 days before the window"}
- `ml-projects-growth-2026-10-09-to-12`: {"volume_growth_24h_gib": null, "volume_growth_72h_gib": 84.6, "key_growth_72h_gib": 46.2, "key_z": null, "key_z_note": "None = fewer than 6 thinned intervals in the 14 days before the window"}

## Events not evaluable for detection

- `hf-move-2026-10-09` (shrink, 189.4 GiB, docs/RESUME.md:464): shrink: a move frees space, the growth-only detector has nothing to detect
- `pagefile-left-c-2026-10-09` (shrink, 17.0 GiB, docs/RESUME.md:581): shrink on C: (the matching +16 GiB on D: is not quantified in the docs), and no fixture sample near the 13:02 boot
- `pagefile-to-d-2026-10-09` (growth, 16.0 GiB, docs/RESUME.md:581, docs/RESUME.md:533): no D: sample within 1 h of the 13:02 boot (last before: 10-08 11:42, first after: 10-09 15:21:54). Added after the first replay showed a D: volume alert of exactly 16.0 GiB at 15:21:54; the doc entry is independent of it, but it turns that alert from a false alarm into a match
- `overnight-drop-2026-10-10` (growth, 58.5 GiB, docs/RESUME.md:465, docs/RESUME.md:472): no fixture sample of C: within 1 h of 08:35 (nearest: 10-10 01:38 before the window, 16:47 after it); the 10-09/10 sampler that held the gg-portfolio series no longer exists
- `swing-2026-10-08` (growth, 7.5 GiB, docs/RESUME.md:619, docs/RESUME.md:620): watchdog.csv for 10-08 ends 11:42:20 (the watchdog aborted the build at C: 3.93 GiB), 88 min before the window end
- `uv-growth-c_attrib6-2026-10-13` (growth, 20.2 GiB, docs/RESUME.md:386): the doc itself calls it UNPROVEN and the uv cache is ~30 GB elsewhere in the docs (RESUME.md:659, 904): a truncated first walk, not a ground-truth growth
- `ollama-setup-c_attrib6-2026-10-13` (growth, 4.3 GiB, docs/RESUME.md:386): 4.3 GiB is below the 5 GiB floor and the docs name only processes, never a folder

## The 3-day volume rule on steady growth

Synthetic control (deterministic, no noise, 14 days, 4-hourly snapshots): every volume alert is a false alarm by construction.

| growth (GiB/day) | snapshots | alerting snapshots | alerting after warm-up | first alert (day) |
|---|---|---|---|---|
| 1.0 | 84 | 0 | 0 | None |
| 1.5 | 84 | 0 | 0 | None |
| 2.0 | 84 | 36 | 0 | 3.0 |
| 3.0 | 84 | 36 | 0 | 3.0 |

Arithmetic: a steady rate r crosses the 5 GiB / 72 h floor once 3r >= 5, i.e. r >= 1.67 GiB/day. Before the volume-z fix it then stayed tripped for as long as the growth lasted. Now only the warm-up (the first 60 snapshots, while the volume has < 6 daily intervals before the window and no baseline exists) can alert.

Surge on top of steady growth (the fix must not blind the rule): same series, plus extra growth spread over one day ending 1 day before the end.

| growth (GiB/day) | surge (GiB) | alerting snapshots | alerting after warm-up | alerting from surge day on |
|---|---|---|---|---|
| 2.0 | 20 | 46 | 10 | 10 |

Bursty steady growth (one step a day, flat in between; 30 days, no event):

| step (GiB/day) | snapshots | alerting snapshots | alerting after warm-up |
|---|---|---|---|
| 2.0 | 180 | 36 | 0 |
| 3.0 | 180 | 36 | 0 |

Noisy volume (seed 42, random walk sd 3.0 GiB per 4 h, 20 days): with a 40 GiB surge over day 14 the detector alerts on 29 snapshots from the surge start, the first 4 h after it; the same walk without the surge alerts on 24 (first 24 h after day 14).

Sustained leak (no pass/fail): 10.0 GiB/day after 14 quiet days. The detector alerts on 62 snapshots, from 8 h to 252 h after the leak starts (10.3 days between first and last), then the 14-day history absorbs it as usual. The 80%-full notification (R5) is the backstop for a leak that goes on; it does not fire below its threshold, so a leak that stays under 80% full is silent after that point.

Real data: no continuous stretch reaches 72 h, so steady growth over 3 days cannot be measured on it. Net C: used change per continuous stretch of >= 2 h:

- 2026-10-09T15:21:54: 4.7 h, -189.9 GiB net, -967.6 GiB/day
- 2026-10-15T11:32:49: 2.0 h, 4.9 GiB net, 58.0 GiB/day
- 2026-10-30T17:58:19: 13.0 h, 18.2 GiB net, 33.6 GiB/day
- 2026-10-31T13:49:19: 4.5 h, -5.7 GiB net, -30.4 GiB/day
- 2026-10-31T22:49:51: 2.6 h, 3.9 GiB net, 35.7 GiB/day

Of the 4 as-replayed detector false-alarm episodes, 2 rest on a start sample older than 72 h or a newest sample older than 1 h (gap artifacts); the rest are in the JSON (`methods.detector_literal`). A "false alarm" here means no documented event covers the episode, not a verified non-event: the docs do not list every real write.

## Steady growth and the 3-day volume rule (FIXED)

Steady growth of >= 1.67 GiB/day (5 GiB / 3 days) used to trip the volume rule and keep it tripped for as long as the growth lasted (2.0 and 3.0 GiB/day alerted on 66 of 84 snapshots). Fixed as applied in the change log: the volume rule now subtracts the volume's usual growth (the median of its day-to-day rates over the previous 14 days, times the window length) and alerts only if the excess is >= abs_floor. With fewer than 6 daily intervals of history there is no baseline and the floor alone still alerts, so a fresh install does not go quiet (the remaining control alerts are all in that warm-up). No threshold was changed and the fix was decided on synthetic controls, not tuned on the 5 evaluable events. Limits: a sustained leak is absorbed into the baseline once it fills half of the 14-day history (control below), and on a noisy volume noise alone still crosses the floor. Steady growth that fills a disk is still covered by the 80%-full notification (R5), not by this rule; R5 does not fire below its threshold.

## Honesty section

- **Not a 4-week eval.** The data are 30.77 h of samples in 15 stretches over
  23.61 days; the longest continuous stretch is 12.98 h. Four continuous weeks would be
  672 h. Every per-week figure above is therefore **not meaningful** (it divides a handful of
  episodes by 30.77 h of data).
- **Tiny n.** 5 evaluable events (of 12 documented), of which 2 are the same census
  pair seen twice. Recall, top-1 and precision are counts to read, not rates to quote. The spec's
  success metric (recall >= 0.8, <= 1 false alarm per week) is neither met nor refuted by this data.
- **Precision is a lower bound, false alarms an upper bound.** An episode counts as true only if it
  covers a documented growth event of >= 5 GiB; the docs do not list every real write, so some
  "false alarms" may be real, undocumented growth (D: in particular).
- **The key rule never ran.** It needs >= 6 thinned (30 min) intervals of history before the
  window start and a 24/72 h window. No folder series in the data spans more than about 2 h
  except the two census snapshots (2 points), so every detector detection comes from the
  volume rule;
  key-level z-scores are all `None` (see `event_diagnostics` in the JSON).
- **Gaps make the as-is replay look busy.** The sources are fragments, so a "24 h" window often
  starts from a sample days old (`start_age_h`), and census-time alerts use a C: sample 52 h old.
  Real snapshots every 4 h would not have those holes. The *literal* columns drop alerts whose
  start sample is older than 72 h or whose newest sample is older than 1 h; they are the closer
  reading of the spec, not a tuned variant. The finding that `detect_anomalies` reported growth
  accumulated over a whole gap as growth "in 3 days" is FIXED (see the change log): a window's
  start sample may now be at most window + 6 h old and the newest sample at most 1 h old. The
  detector's "as replayed" column therefore now loses the two census-time detections of the
  9 -> 12 Oct growth: their volume alert rested on a C: sample 52 h old, which is a hole in the
  fragments, not a missed snapshot a real 4-hourly run would have. The loss comes from the data
  being fragmentary and is not evidence about the detector on real snapshots.
- **Two events were added after the first replay** (`pagefile-to-d-2026-10-09`,
  `hf-copy-to-d-2026-10-09`, both D:, both documented in RESUME.md); the first replay showed D:
  alerts and the original event list had only C: events. The alert-to-event matching also gained
  a factor-2 size band after a 16 GiB alert was seen matching a 185 GiB copy. No threshold of the
  detector or baseline was changed; spec defaults throughout.
- **Same-instant events share one alert.** The Temp and ml-projects growths (9 -> 12 Oct) are two
  documented contributors to one volume alert, whose `attributed` field names only the top one,
  so top-1 cannot exceed 1 of 2 there. The census is too shallow to hold the gg-portfolio scratch
  folder itself, so naming `Temp` is scored correct and naming `AppData` coarse.
- **Fixtures are thinned, not 4-hourly snapshots:** 60 s (watchdog, attrib) and 300 s (jobs)
  samples, folder sizes every 1.7-2.5 min in three 30-40 min windows, two census snapshots. The
  `uv` growth inside those windows (0.58 -> 20.8 GiB and similar) is a budget-truncated walk of
  the old sampler and is excluded from the ground truth; the baseline does alert on it.

### What data is needed

At least 4 weeks (672 h) of continuous 4-hourly snapshots from the shipped feature on this machine
(168 snapshots), with growth events of the documented classes inside them and attributed
afterwards by a person: overnight scratch growth by another session, an unexplained build-time C:
drop, a model or cache download, a pagefile move, steady drift. With about 10 events the recall
interval would still be wide; the spec's 0.8 target needs a larger event set
to be tested honestly.

## Fixture SHA-256 (line endings normalised to LF)

- `attrib.csv`: `e2cf6034ee47598e49f66bd85b5906d506503d97b55ea5fc4e7d21ed07e7a8df`
- `census.csv`: `9af4404d540dfabcc3ef8a6ec320aa1db0b4e655d172cee78b2ad8e1dcf309b1`
- `events.json`: `71e2a9d10fe58db3002260cf3d5929c55e2dc68d12476a5de74d841bf182c437`
- `jobs.csv`: `73ea0cb7aaf1098c7643496900992e4c522d1c76510bae7e6f4b5ccaa872ab2d`
- `meta.json`: `ff2d25040604c86252c4dfd76b64a772b325b179ec19d954dd4e3b1234b21863`
- `watchdog.csv`: `083c456a735fa186d0c04a3db6c55466a19793e7a2460e153f19beae22d9758f`
