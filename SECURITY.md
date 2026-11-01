# Security policy

Reclaim moves and deletes files on your own machine, so a vulnerability here can cost someone
data. Please report problems privately and give a fix time to ship before discussing them
publicly.

## Supported versions

| Version | Supported |
|---|---|
| 1.4.x | Yes, security and data-loss fixes |
| 1.3.x | Best effort |
| 1.2.x and older | No. 1.1.0 has a known crash-safety gap and is superseded; upgrade |

This is a solo project: "supported" means the maintainer will look at a report and ship a fix in
a new release where one is warranted. There is no backporting commitment beyond the table.

## How to report a vulnerability

Use GitHub's private reporting on this repository: **Security tab -> Report a vulnerability**
(a private security advisory on
[gaurav-gandhi-2411/reclaim](https://github.com/gaurav-gandhi-2411/reclaim/security/advisories/new)).
Only the maintainer sees it.

If that button is not shown (the repository owner has to switch private vulnerability reporting
on, and it may be off), open a public issue titled "Security contact request" with **no details**
(no exploit, no paths, no file names) and ask for a private channel. Do not post the
vulnerability itself in a public issue, a pull request or a discussion.

Please include: the Reclaim version (`reclaim.exe --version`), the mode (safe or power), what you
did, what you expected, what happened, and whether any file was lost or changed. Never attach
your scan index or file contents (see [SUPPORT.md](SUPPORT.md)).

## Scope

Reclaim is a local app with no server side and no account. In scope:

- **The localhost dashboard and API.** It binds to loopback only; mutating calls need a
  per-process CSRF token; `Host` and `Origin` are checked against the bound address. A way around
  any of these, a cross-origin page triggering a clean, or a DNS-rebinding bypass is in scope.
- **The delete, move and vault paths.** Anything that makes Reclaim delete, move or restore a
  file it should not: a bypass of the safety validator or the exclusion list, a protected path
  reached under an alias (UNC, 8.3, junction, substituted drive), a restore that writes outside
  its place or outside the vault, a safe-mode bypass (permanent delete or vault use in safe mode),
  a race between scan and apply, and the relocate (move to another drive) and agent-leftover
  paths.
- **The MCP server (`reclaim.exe mcp-serve`).** It runs over stdio only. An assistant must not be
  able to name a path, delete without the user's click in Reclaim's window, or make an
  irreversible delete. Bypassing the approval card or the selection-hash check is in scope.
- **The installer and uninstaller**, scheduled tasks, and anything that makes Reclaim run elevated
  (it refuses to).
- **Dependencies** with a known vulnerability that is reachable in Reclaim. `pip-audit` runs in CI
  and fails the build on a known advisory.

Out of scope: attacks that already need code execution as the same user (the app runs with that
user's own rights), a malicious local administrator, physical access, the SmartScreen or antivirus
warning on the unsigned installer (documented in the README), and third-party AI clients' own
behaviour (Reclaim does not edit their configuration).

## What to expect

These are goals, not promises; this is one person's side project.

- An acknowledgement within about a week.
- A first assessment (reproduced or not, severity) within about two weeks.
- For a confirmed data-loss or safety-boundary issue, a fix or a mitigation prioritised over other
  work, released as a new version with the issue named in the changelog.
- Credit in the changelog if you want it, and none if you do not.

Reclaim has no bug bounty.
