from __future__ import annotations

# ruff: noqa: F811
# The ninth hostile review's (B9) API probe, moved here from `test_relocate_review_b9.py`
# because the routes live on this branch. It passed on the reviewed head. Every root is under
# `tmp_path` (the `env` fixture).
from test_relocate import (  # type: ignore[import-not-found]
    Env,
    _make_tree,
    env,  # noqa: F401 -- the fixture
)

import reclaim.relocate as relocate
from reclaim.api.relocate_routes import _old_copy_kept
from reclaim.relocate import delete_old_copy, rollback


def test_b9_old_copy_kept_after_a_rollback_that_kept_the_old_copy(env: Env) -> None:
    """The page shows the old copy and offers delete-old for a ROLLED_BACK move; the engine
    then deletes it, and afterwards there is no old copy to show."""
    _make_tree(env.source)
    env.move()
    rollback(env.journal_path())
    rows = relocate.journals(env.journals)
    assert _old_copy_kept(rows[0], rows) is True
    delete_old_copy(env.journal_path(), policy=env.policy, confirmed=True)
    rows = relocate.journals(env.journals)
    assert _old_copy_kept(rows[0], rows) is False
