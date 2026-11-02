# Reclaim: how to use it (plain version)

Written for the owner. Sections 1-6b were checked against earlier builds (2026-10-08 and 2026-10-09);
section 6c is about the build installed on 2026-10-17 (main `a446853`, installer SHA-256
`65d1501e6ca5e2bd51d52e9bb967b3031cd23b7782c0ef204765ee91978c208a`). Things I could not see on screen are
marked **not seen**. For the 1.4.0 release prep, the button, tab and setting names and the `reclaim relocate` and
`reclaim agent-artifacts` options in sections 4 and 7-10 were re-checked against the code and dashboard page of that
branch (not against an installed 1.4.0 build, which did not exist yet); the measured numbers in sections 2-6c describe
the builds named there, not 1.4.0.

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
proposed), **AI Suggestions** (recommend-only, never deletes by itself), **Quarantine & Restore**, **Move to Another Drive** (section 9), **Settings**.
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
**Snooze for a week** button. Turn it off: Settings -> *Low disk space alert* -> off. **Seen:** you saw the alert on
screen when C: was at 97% (confirmed by you, 2026-10-16). Since the 10-15 build Windows' own notification counter for
Reclaim also moves when a toast is sent, so it can be checked without looking at the screen.

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
- (Superseded: the 80% alert has since been seen on screen by you, see section 5.)

## 6c. What to expect from the build installed on 2026-10-17 (checked on your real profile)
- **The scan index can live on another drive.** It was 11 GB (13.8 GB after the duplicate pass) because the 12.8 million
  entries of your profile are stored. Your install now has, at the end of `config.toml`:
  `[storage]` / `index_dir = 'D:\reclaim-index'`. The index file is `D:\reclaim-index\reclaim_index.sqlite3` (moved and
  checked identical, SHA-256 `d0ea890d...59a25d`); C: free went from 31.8 to 44.7 GB. The quarantine vault stays on C:
  on purpose (moving a file into it must stay an instant rename on the same drive).
  If you ever remove the `[storage]` lines, Reclaim looks for the index under `data\` again and finds none (a warning
  says so); do not do that without moving the file back.
- **Scheduled tasks still work from any folder.** The default `config.toml` is now the one next to `reclaim.exe`, wherever a
  command is started from, so the weekly clean applies your exclusions even if started elsewhere (checked from a different
  folder: would remove 38.1 GB, `%TEMP%\claude` skipped, your three excluded projects not touched).
- **A mistyped `--config` is refused** instead of silently falling back to defaults (for the commands that use the index).
- **The first Overview after a fresh scan is still slow on this build.** On the full 12.8-million-entry index the duplicate
  preparation was still running after 2 hours (120 min on 10-15, lost to a reboot) and used about 6 GB of memory; I stopped a
  second try after 69 minutes. Do not expect Overview to be fast on this build until the next one below is installed.
- **Next build (not installed, waiting for your merge of the weekly release PR):** the scanner stops listing every file inside
  `node_modules`, `.venv`, `venv`, `site-packages`, `.git` and `__pycache__`; each becomes one entry with its total size. On a
  copy of your real index this cut it from **12,795,878 to 2,449,175 entries** and from **12.89 GB to 1.69 GB**, the duplicate
  preparation finished in **49.6 minutes** (2978 s) with the server at about 0.9 GB when I last sampled it (30 minutes in; later not sampled), and the first Overview opened in **0.15 s**
  (then 0.17 s and 0.24 s). That is a measurement on a scratch copy with the source from the release branch, not on an installed build.
  Deleting such a folder is then checked against its total size, file count and newest change instead of file by file; any
  difference refuses the delete, and a file hard-linked into another Python environment still refuses it.
- The disk alert, the weekly clean and the one-click flows are unchanged from 6b.

## 7. If something looks wrong
- Settings and the top bar's **Copy diagnostics** button collect what support needs; nothing leaves your machine.
- Every automatic or one-click run is recorded in `...\Reclaim\data\regenerable_audit.jsonl` (what was cleaned, skipped, and why).
- `reclaim.exe auto-clean` (no flags) is a preview; `reclaim.exe auto-clean --apply` really cleans.
- Uninstall: Windows Settings -> Installed apps -> Reclaim. It asks whether to remove everything; answer Yes to also be asked about
  the data folder. Checked on the real installed build (2026-10-15): a silent uninstall keeps your `config.toml` and data and removes
  both scheduled tasks and the Windows notification identity; the dialog path with Yes/Yes removes the whole folder, data, config,
  tasks and identity. Reinstalling puts the tasks back. An index you moved with `index_dir` (6c) is on another drive and is
  **not** removed by either path; delete that folder by hand.


## 8. Use Reclaim from Claude or Codex (your own AI assistant)
Reclaim can be driven from Claude Desktop, Claude Code or Codex CLI on this machine. The assistant talks to
`reclaim.exe mcp-serve` over a local pipe: nothing goes over the network, and Reclaim itself calls no AI service
for this.

What the assistant can do:
- scan a folder in your profile and list what Reclaim found;
- plan how to free a given amount (`plan_free_space`), using Reclaim's own categories only;
- list the large folders that could move to another drive, with their size and why one cannot
  (`list_relocation_options`, the same list as the **Move to Another Drive** page);
- ask to clean a selection.

What it cannot do:
- name a file or folder to delete;
- approve anything. Every clean shows a card in Reclaim's own window, and nothing happens until you click
  **Approve**. Everything it cleans goes to the vault or the Recycle Bin, so it can be restored.
- move folders to another drive. It can only list the options; you start a move yourself, on the
  **Move to Another Drive** page.

Keep Reclaim's window open while you use it: without the window there is nobody to approve, and a clean is refused.

Set it up (once). The exact text for your machine is on **Settings > Use Reclaim from your AI assistant**, with a copy
button. The formats below are from each client's documentation as I know it; they were not checked against a live install
(BELIEVED):
- **Claude Desktop:** add to `%APPDATA%\Claude\claude_desktop_config.json`:
  ```json
  {"mcpServers": {"reclaim": {"command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Reclaim\\reclaim.exe",
                              "args": ["mcp-serve"]}}}
  ```
  Then restart Claude Desktop.
- **Claude Code:**
  ```
  claude mcp add --scope user reclaim -- "C:\Users\<you>\AppData\Local\Programs\Reclaim\reclaim.exe" mcp-serve
  ```
- **Codex CLI:** add to `%USERPROFILE%\.codex\config.toml`:
  ```toml
  [mcp_servers.reclaim]
  command = 'C:\Users\<you>\AppData\Local\Programs\Reclaim\reclaim.exe'
  args = ["mcp-serve"]
  ```

Reclaim never edits these files for you.

Things to ask: "free 20 GB, keep my projects, rebuildable things only", "what is using my disk?", "plan 50 GB but no
duplicates". A plan says when the number cannot be reached under what you asked (`reaches_target: false` with the
shortfall), rather than quietly including more.

## 9. Move a big folder to another drive (tab: **Move to Another Drive**)
For folders programs still need, so deleting is not an option: model caches, browser builds for tests and the like.
Reclaim copies the folder to your other fixed drive, checks every file (SHA-256), and leaves a link (a junction) at the
old place, so every program still finds it there. New in 1.4.0: it needs a build of 1.4.0 or later.

1. The tab lists the usual large folders and sorts them: those it can move first, then the rest, each with the reason.
   - **Moved with their own steps:** Docker's disk image and WSL. Reclaim shows their size and the steps (Docker's
     settings, `wsl --export`/`--import`), and never moves them itself.
   - **Refused:**
     - a folder holding one of your protected project names;
     - a folder with hardlinks inside (a copy would split them);
     - a folder reached through a link;
     - a name too long to move safely (over 239 characters), a short "8.3" name, and a network path.
2. **Check this move** runs a dry run and shows where it goes, the folder's size and file count, the free space on
   the target, and anything in the way. Nothing changes yet.
3. **Move it** does the move. The original stays next to the link as `<name>.reclaim-moved`, unchanged, so nothing is
   lost if you change your mind.
4. Each move is listed under **Moves**:
   - **Move it back** copies the folder, as it is now, back to its own place and removes the link;
   - **Delete the original copy** frees the space on the first drive. It checks every file of the kept copy against the
     moved one first and deletes nothing if any differs. Before 7 days have passed it warns you first.

From a terminal: `reclaim relocate list`, `plan`, `move`, `status`, `rollback <id>`, `delete-old <id>`. If a move was
interrupted (a crash, a power cut), `reclaim relocate recover <id>` finishes it or puts it back.
`rollback <id> --use-old-copy` puts back the copy from before the move instead of the moved folder; the page does not
offer that.

## 10. Finished AI-agent leftovers (category: **Agent Leftovers**)
Coding agents leave git worktrees, throwaway clones and scratch folders behind. Reclaim can offer the finished ones for
cleanup. New in 1.4.0: it needs a build of 1.4.0 or later.
- **Off until you turn it on,** and always off in safe mode: switch to power mode, then turn **Agent Leftovers** on in
  **Settings**.
- **What is offered:** an item is offered only if it is proven finished. All its work is pushed. Nothing is left
  hidden (untracked files, stashes, git filters that change what a commit stores). No program is using it. It is not one of your
  protected projects. Anything that cannot be proven is kept, and the report says why.
- **You choose each one.** It goes to the vault (**Quarantine & Restore**), never straight to deletion, and comes back
  with **Restore**. For a worktree, git's link to it comes back too.
- **To look without changing anything:** `reclaim agent-artifacts` prints what would be offered and why the rest is
  kept.
