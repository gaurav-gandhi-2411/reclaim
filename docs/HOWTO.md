# Reclaim: how to use it (plain version)

Written for the owner. Everything here was checked against the build installed on 2026-10-08
(`integration/2026-10-07`, installer SHA-256 `55c34c7f3dfc...feb5`). Things I could not see on screen are
marked **not seen**.

## 1. Start it
Start Menu -> **Reclaim**. Your browser opens the dashboard at `http://127.0.0.1:<port>` (it only listens on your own
machine). First time, a short screen explains "safe mode"; read it and continue.

## 2. The one click: **Clean My Computer**
The big button on the first screen (Simple mode).
- **What it cleans, and only this:** the caches that other programs rebuild by themselves: package-manager download
  caches (pip, npm, conda, yarn, uv), temp files nobody has touched for 7+ days, old crash dumps, and the caches of
  browsers that are **closed** right now.
- **What it never touches:** your documents, Downloads, the Recycle Bin, Reclaim's own quarantine vault, anything a running
  program has open, and every folder belonging to the projects on your **exclusion list** (see section 6).
- **What you see afterwards:** how much was freed, how much C: is now used, and a list of what was skipped and why
  (for example "browser is running", "tool not installed", "in use right now").
- **Undo:** these are rebuildable caches and are removed with each tool's own cleaning command, so there is nothing to
  restore; the tools download again when needed. (This is different from the review flow below.)
- **Honest limit:** the uv cache is huge (about 30 GB) but its safe command ("prune") only returns the unused part, so
  expect a couple of GB from it, not 30. The preview number for uv overstates this (known issue, in the gaps list).

## 3. Looking for more: **Scan my files for more to review**
Second button. It scans your user folder and builds an index (the first scan of a big profile takes a while; a live
progress bar shows it). Afterwards Reclaim quietly prepares duplicate detection in the background at low priority.
- Nothing is deleted by scanning.
- What it finds goes to the **Review Queue** (Advanced mode). You pick items, confirm once, and in safe mode every
  delete **goes to the Recycle Bin** (empty it to actually free the space). You can restore anything from the
  **Quarantine & Restore** tab or from Windows' Recycle Bin.
- The third button, **Scan the whole drive (advanced)**, does the same for the entire C: drive and takes much longer.

## 4. Advanced mode (button: **Switch to Advanced**, top right)
Tabs: **Overview** (totals + Quick Clean), **Storage Treemap**, **Review Queue** (every candidate with the reason it was
proposed), **AI Suggestions** (recommend-only, never deletes by itself), **Quarantine & Restore**, **Settings**.
"Safe mode" (top bar) is on by default: everything goes to the Recycle Bin and nothing is applied automatically.
"Power mode" needs you to type a confirmation phrase and unlocks permanent delete for rebuildable caches; you can go back
to safe mode any time without a phrase.

## 5. Automatic things (both are already switched on for your account)
**Weekly clean** (Settings -> *Weekly cache clean*): runs the same safe list as the one click, **Sundays 10:00**, and
again about **3 minutes after you sign in** if something (for example uv) was busy last time. It needs no clicks, shows a
Windows notification only when it freed or skipped something ("Freed X, C: now Y% used"), and does nothing on a sign-in
when there is nothing left to retry. It runs as you, without administrator rights, and only while you are signed in.
Turn it off: Settings -> *Weekly cache clean* -> off (this removes the scheduled task), or Task Scheduler -> *Reclaim Weekly
Auto-Clean (gaura)* -> Disable. Updating Reclaim re-creates the task only if the setting is on.

**80% disk alert** (Settings -> *Low disk space alert*): a background task checks C: a few times a day and shows a Windows
notification "Disk space is running low" when C: is **80% used or more** (C: is at 78% today after the Hugging Face cache moved to D:, so it will not fire until you pass 80%), with a
**Snooze for a week** button. Turn it off: Settings -> *Low disk space alert* -> off. **Not seen:** I could not confirm on
screen that the toast actually appears (the Windows counter I used as a proxy does not track this notification identity), so
please tell me if you never see one.

## 6. Protecting your projects
`C:\Users\gaura\AppData\Local\Programs\Reclaim\config.toml` has
```
[exclusions]
project_names = ["fr-en-transformer", "shipdoc-extract", "intent-router"]
```
Any cleanup path whose name contains one of these (case-insensitive) is skipped by every cleaning route: one click, weekly
clean, and the review flow. Verified: a preview run lists `%TEMP%\claude` and the `hub_roundtrip_*` folders as excluded because of these
names. Limit: a folder that does not contain the name (for example a generic `pytest-1234` temp folder) cannot be recognised
as theirs; for those use `[safety] deny = ["*\\some\\path\\*"]`. The optional "pytest temp" cleaning is **off** and stays off
unless you set `[regenerable] pytest_temp = true`.

## 6b. What to expect from the build installed on 2026-10-09 (checked on your real profile)
- **Scanning is much faster and safe now.** "Scan my files" on your profile took **12.6 minutes** (6.7 million entries), and the
  background preparation that follows took **31.9 minutes** (checked 20:21-20:53). During it the database's temporary file
  stayed under **10 MB** (it reached 13 GB in the 10-08 build), C: free did not move, and no "database is locked" error appeared.
- **While the preparation runs** Overview shows "Indexing your files... The page is not stuck" (screenshots
  `docs/assets/409-install-20261009-0*.png`); there is no red error. Behind the scenes the server answers "not warm yet" (HTTP 409), which
  the page turns into that message.
- **The first time you open Overview after that it can take about 25 seconds** (measured 24.1 s, then 0.25 s); later opens are instant.
- The Review Queue loaded without starting a second duplicate pass (one `dedup.start` in the log). Its screenshot was taken while the list was
  still loading; I did not watch it finish.
- The one click ("Clean My Computer") took **2 minutes** and freed **639 MB** (pip 69 MB, npm 146 MB, Chrome cache 423 MB), C: free 224.04 -> 224.65 GB.
  Edge was running so its cache was skipped; the uv cache had nothing left to prune. Your three excluded projects were not touched (0 excluded paths applied).
  Note: your pip cache now lives on D: (a link at the old place), so its 69 MB is not C: space.
- Preview numbers for **uv** and **npm** caches may still overstate what is freed.
- On first launch you see a one-time "Before you start" screen; click **I understand, continue**.
- An empty peach bar with a "Dismiss" button may appear under the header; it is cosmetic.
- **I could not see the 80% alert on screen.** I fired it with a test threshold (it reported "would notify") and Windows' counter did not move, as before.
  Tell me if you never see a notification.

## 7. If something looks wrong
- Settings and the top bar's **Copy diagnostics** button collect what support needs; nothing leaves your machine.
- Every automatic or one-click run is recorded in `...\Reclaim\data\regenerable_audit.jsonl` (what was cleaned, skipped, and why).
- `reclaim.exe auto-clean` (no flags) is a preview; `reclaim.exe auto-clean --apply` really cleans.
- Uninstall: Windows Settings -> Installed apps -> Reclaim. This also removes the two scheduled tasks (**not seen**: I did not
  uninstall; the uninstall script compiled and its ownership guard is unit-tested).
