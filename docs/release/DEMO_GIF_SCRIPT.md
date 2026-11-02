# Demo GIF script, Reclaim 1.4.0

A shot list for one 30 to 45 second GIF. **Nothing has been recorded.** This file only says what
to record and how. The GIF is for the README and the announcement; the owner decides where it goes.

## The one rule: no real personal paths on screen

Record against a **fixture profile**, never the real one. A real profile shows real folder names,
project names and drive contents, and the scan itself would read them.

1. Make a scratch folder outside the repo, for example `%TEMP%\claude\reclaim\<YYYYMMDD>\demo`.
2. Build a fake user profile in it (a user called `sam`, a few old caches, a `node_modules`, a
   throwaway git worktree for Agent Leftovers, one large "model cache" folder for Relocate). The
   existing `scripts/mcp_e2e/make_fixture.py <dir>` writes a small fake profile (`Users\sam`, a
   webapp with `node_modules`, an old temp file) and shows the env-redirect pattern; extend a copy
   of it in the scratch folder rather than editing the repo script. Use files whose names read well
   on screen (`webapp`, `old-build-cache`, `hf-models`), and make sure no path contains your own
   user name, machine name or a real project name.
3. Start the app with **every root redirected into the fixture**: set `USERPROFILE`, `HOME`,
   `LOCALAPPDATA`, `APPDATA`, `TEMP`, `TMP` to folders inside it, and pass `--db`, `--vault-dir`,
   `--manifest`, `--mode-log`, `--first-run-state`, `--log-path` and `--config` (see
   `reclaim serve --help`) into the fixture's `data` folder. Redirecting only `TEMP` is not enough.
4. For Agent Leftovers, the fixture needs a git worktree whose branch is pushed to a local bare
   "remote" inside the fixture; for Relocate, a second fixed drive or a mounted VHD (a small one is
   enough) so the target drive exists. Both live in the scratch folder or the VHD.
5. Check the address bar and every visible path before recording. If a real path shows, stop.
6. Run it from a built or installed 1.4.0 (the dev server works for rehearsal). **Do not** point it
   at `C:\Users\<you>\AppData\Local\Programs\Reclaim` or the real index.

When done, delete the scratch folder (check its contents first, then delete as a separate
command) and unmount the VHD.

## Recording

- Tool: ScreenToGif (free, Windows, records a region and exports GIF directly), or record an MP4
  with the Windows Snipping Tool or OBS and convert with `ffmpeg` (palette pass, below).
- Capture only the browser viewport at **1280 x 720**, light or dark theme (pick one and keep it),
  browser zoom 100%, bookmarks bar and extensions hidden, a clean profile in the browser.
- Frame rate 12 to 15 fps. Export width 960 px. Keep the file under 5 MB for the README (GitHub
  renders larger ones, but they load slowly); if it is bigger, cut dwell time, not resolution.
- No audio, no cursor highlight flourishes. Move the mouse deliberately; pause one second on each
  caption target.
- Optional ffmpeg conversion from an MP4: `ffmpeg -i demo.mp4 -vf "fps=12,scale=960:-1:flags=lanczos,palettegen" pal.png`
  then `ffmpeg -i demo.mp4 -i pal.png -lavfi "fps=12,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse" demo.gif`.
- Rehearse the whole sequence twice before the take; the fixture scan should be warm so no scene
  waits on a spinner longer than a second or two.

## Shot list (total about 40 s)

| # | Scene | On screen | Duration | Caption |
|---|---|---|---|---|
| 1 | Overview | Advanced mode, Overview tab after a scan of the fixture profile: the stat row and the category grid with sizes. Safe mode badge visible in the top bar. | 5 s | "See what is safe to remove, and why." |
| 2 | A safe clean | Simple mode: click **Clean My Computer** (one click, no scan first). The report lists what was cleaned, how much was freed, and what was skipped and why (for example a browser that is open). Keep the fixture caches small so it finishes in a few seconds. | 9 s | "One click. Only caches that rebuild themselves." |
| 3 | Agent Leftovers | Power mode on (show the typed-confirmation field only as a quick cut, or start the fixture already in power mode and caption it). Review Queue filtered to **Agent Leftovers**: a finished worktree and a throwaway clone with the reason each is offered. Select one. | 8 s | "Finished AI-agent leftovers, proven finished, never deleted outright." |
| 4 | Relocate | **Move to Another Drive** tab: the candidate list, click **Check this move** on the model-cache folder (the plan dialog: target, size, free space), then **Move it**, then the entry under **Moves** showing the junction in place. | 9 s | "Move big folders to another drive. Programs still find them." |
| 5 | Restore | **Quarantine & Restore** tab: the batch from scene 3, click **Restore batch**, the success message, then a cut to the file back in its folder in Explorer (fixture path only). | 7 s | "Everything is recoverable." |
| 6 | End card | Reclaim logo and the line "Windows. Free. Local only. Nothing leaves your machine." with the repository name. | 3 s | (none; the card is the caption) |

Cuts between scenes are hard cuts or a 200 ms fade; no zoom or pan effects.

## Checks before it is published

- [ ] Every frame viewed once at full size: no real user name, project name, drive label or file
      name; the fixture user reads as `sam`.
- [ ] Safe mode is shown first. If power mode appears (scene 3), the badge says so and the caption
      does not imply it is the default.
- [ ] The text in each caption is true of 1.4.0 (Agent Leftovers are off by default and always off
      in safe mode; Relocate leaves the original until a confirmed delete).
- [ ] File size under the README budget above; alt text written for the README embed.
- [ ] Recorded from a build whose version reads 1.4.0 (`reclaim.exe --version`), not from a dev
      checkout.
