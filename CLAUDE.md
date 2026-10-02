# CLAUDE.md -- rules for any Claude session or subagent working in this repo

Global rules live in `C:\Users\gaura\.claude\CLAUDE.md`; these are the repo-specific ones that
bit us. Read `docs/RESUME.md` first (it is the living checkpoint).

1. **Work only in your own git worktree, never in this main checkout**:
   `git worktree add ..\reclaim-wt-<slug> -b <branch> origin/main`. A stray empty file once appeared in
   the main checkout because a subagent ran `cat > ../../../x` from a nested worktree.
2. **A deletion's check and the deletion never run in the same command; an empty check is a failed
   check.** Run the check alone, read its output, state what it showed, then delete in a separate
   command (applies to your own scratch files and index copies too). Incidents, 2026-10-01: a
   duplicate HF model copy deleted in the same command as a completeness check whose filter printed
   nothing; a second agent deleted its own scratch copy in the same command as the listing.
3. **HARD EXCLUSION (permanent, owner-set 2026-10-02): never touch `fr-en-transformer`,
   `shipdoc-extract` or `intent-router`** (under `C:\Users\gaura\ml-projects\`, incl. worktrees, venvs,
   data, models, caches): no deletions, no cache/data/model removal, no git operations, no tags, no
   worktree changes. Any reclaim cleanup run keeps them on its exclusion list and its report states
   that none appeared among applied candidates.
4. **Verify with `scripts/verify.py`, never bare `pytest`** (`testpaths=["tests"]` silently skips the
   `evals/` safety gates). In a worktree `uv run` would sync a new environment: run its steps through
   `.venv\Scripts\python.exe` with the worktree's `src` first on `sys.path`; the CLIP `.onnx` files must
   be real (a worktree can hold 134-byte Git LFS pointers: copy the real files from the main checkout,
   never stage them).
5. **Never self-merge.** Every PR handoff includes the full table: each required check, mergeStateStatus,
   draft, base, rebased-onto-main yes/no, plus which later PRs need a rebase after their predecessors
   land (PRs touching the same files or choke point). "Green at handoff" is not enough for a train:
   re-verify each PR after its predecessors merge.
6. **Report VERIFIED vs BELIEVED**, with the command and output for every number.
