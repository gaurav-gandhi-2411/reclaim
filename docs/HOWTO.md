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
notification "Disk space is running low" when C: is **80% used or more** (it is at about 98% today, so it will fire), with a
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

## 6b. Known issues in the build installed on 2026-10-08 (fixes are in the merge batch)
- **Do not open the Review Queue right after a big scan** (and avoid "Scan my files" for now). The Review Queue starts its own
  duplicate-detection pass, which on your profile (1.6 million candidate files) takes about 30 minutes, fights the background
  preparation for the database, and while it runs the disk can fill with a very large temporary file (it reached 13 GB). The one click
  ("Clean My Computer") and the weekly clean are NOT affected: they do not use the index.
- The first scan of your profile took about 44 minutes and grew the index from 4.9 to 7.2 GB.
- Preview numbers for **uv** and **npm** caches overstate what is freed (the real command frees far less).
- On first launch you see a one-time "Before you start" screen; click **I understand, continue**.
- An empty peach bar with a "Dismiss" button may appear under the header; it is cosmetic.

## 7. If something looks wrong
- Settings and the top bar's **Copy diagnostics** button collect what support needs; nothing leaves your machine.
- Every automatic or one-click run is recorded in `...\Reclaim\data\regenerable_audit.jsonl` (what was cleaned, skipped, and why).
- `reclaim.exe auto-clean` (no flags) is a preview; `reclaim.exe auto-clean --apply` really cleans.
- Uninstall: Windows Settings -> Installed apps -> Reclaim. This also removes the two scheduled tasks (**not seen**: I did not
  uninstall; the uninstall script compiled and its ownership guard is unit-tested).
