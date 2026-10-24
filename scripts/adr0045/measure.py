from __future__ import annotations

# ADR-0045's measurements of `idx_files_path_lower`. Run ONLY on a scratch copy of an index:
#
#   python scripts/scratch_index.py <index.sqlite3> -- python scripts/adr0045/measure.py MODE {DB}
#
# MODE:
#   sizes          bytes per table/index (dbstat); read-only.
#   without-rowid  rebuilds `files` as a WITHOUT ROWID copy (`files_wr`) with the same secondary
#                  indexes in the SAME file and compares sizes and query timings. It writes, so
#                  it refuses a path that does not look like a scratch copy.
#   like-cost      times each default detector LIKE pattern with the planner free to use
#                  idx_files_path_lower and with `NOT INDEXED`, and checks both return the same
#                  rows; read-only. Needs `src` on PYTHONPATH (reads the default Config).
import json
import sqlite3
import sys
import time
from pathlib import Path


def _sizes(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    return {name: int(size) for name, size in rows}


def _best(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        conn.execute(sql, params).fetchall()
        best = min(best, time.perf_counter() - start)
    return round(best, 4)


def sizes(db: str) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return {
        "file_bytes": page_size * conn.execute("PRAGMA page_count").fetchone()[0],
        "freelist_bytes": page_size * conn.execute("PRAGMA freelist_count").fetchone()[0],
        "files_rows": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "objects": dict(sorted(_sizes(conn).items(), key=lambda kv: -kv[1])),
    }


def without_rowid(db: str) -> dict[str, object]:
    if "reclaim-scratch-index-" not in Path(db).resolve().as_posix():
        raise SystemExit(f"refusing to write to {db}: not a scripts/scratch_index.py copy")
    conn = sqlite3.connect(db)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
    ddl = conn.execute("SELECT sql FROM sqlite_schema WHERE name = 'files'").fetchone()[0]
    wr_ddl = ddl.replace("CREATE TABLE files", "CREATE TABLE files_wr", 1).rstrip().rstrip(";")
    start = time.perf_counter()
    conn.execute(wr_ddl + " WITHOUT ROWID")
    col_list = ",".join(cols)
    conn.execute(f"INSERT INTO files_wr ({col_list}) SELECT {col_list} FROM files")  # noqa: S608
    for statement in (
        "CREATE INDEX wr_dev_ino ON files_wr(dev, ino)",
        "CREATE INDEX wr_cloud ON files_wr(is_cloud_placeholder)",
        "CREATE INDEX wr_ext ON files_wr(ext)",
        "CREATE INDEX wr_size ON files_wr(size)",
        "CREATE INDEX wr_name ON files_wr(name)",
        "CREATE INDEX wr_path_lower ON files_wr(path_lower COLLATE NOCASE)",
    ):
        conn.execute(statement)
    conn.commit()
    rebuild_seconds = round(time.perf_counter() - start, 1)
    all_sizes = _sizes(conn)
    original = {
        k: v
        for k, v in all_sizes.items()
        if k.startswith(("idx_files", "sqlite_autoindex_files")) or k == "files"
    }
    rebuilt = {k: v for k, v in all_sizes.items() if k.startswith(("files_wr", "wr_"))}
    sample = [r[0] for r in conn.execute("SELECT path FROM files ORDER BY random() LIMIT 2000")]
    timings: dict[str, dict[str, float]] = {}
    for label, table in (("original", "files"), ("without_rowid", "files_wr")):
        start = time.perf_counter()
        for path in sample:
            conn.execute(f"SELECT size FROM {table} WHERE path = ?", (path,)).fetchone()  # noqa: S608
        timings[label] = {
            "point_lookup_x2000": round(time.perf_counter() - start, 4),
            "full_scan_sum": _best(conn, f"SELECT SUM(size) FROM {table}"),  # noqa: S608
            "size_index_range": _best(
                conn,
                f"SELECT path FROM {table} WHERE size > ?",  # noqa: S608
                (100_000_000,),
            ),
        }
    return {
        "rows": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "rebuild_seconds": rebuild_seconds,
        "original_total_bytes": sum(original.values()),
        "without_rowid_total_bytes": sum(rebuilt.values()),
        "original": original,
        "without_rowid": rebuilt,
        "timings_s": timings,
    }


def like_cost(db: str) -> dict[str, object]:
    from reclaim.config import Config

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cats = Config().categories
    patterns: list[tuple[str, int | None]] = [(p, 1) for p in cats.package_caches.paths]
    patterns += [(p, 1) for p in cats.model_caches.paths]
    patterns += [
        (f"{root}/*{ext}", 0)
        for root in cats.model_caches.paths
        for ext in cats.model_caches.model_extensions
    ]
    patterns += [(p, None) for p in cats.temp_and_browser_caches.cache_paths]
    patterns += [(p, 1) for p in cats.temp_and_browser_caches.temp_roots if "*" in p or "?" in p]
    results = []
    for pattern, is_dir in patterns:
        like = pattern.replace("\\", "/").lower().replace("*", "%").replace("?", "_")
        where, params = "path_lower LIKE ?", [like]
        params_t: tuple[object, ...] = (like,)
        if is_dir is not None:
            where += " AND is_dir = ?"
            params_t = (like, is_dir)
        params = list(params_t)
        indexed = f"SELECT path FROM files WHERE {where}"  # noqa: S608
        scanned = f"SELECT path FROM files NOT INDEXED WHERE {where}"  # noqa: S608
        results.append(
            {
                "plan": " | ".join(
                    r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + indexed, params)
                ),
                "indexed_s": _best(conn, indexed, tuple(params)),
                "not_indexed_s": _best(conn, scanned, tuple(params)),
                "same_rows": sorted(r[0] for r in conn.execute(indexed, params))
                == sorted(r[0] for r in conn.execute(scanned, params)),
            }
        )
    eq = "SELECT COUNT(*) FROM files{} WHERE path_lower = ? COLLATE NOCASE AND opaque = 1"
    probe = ("c:/nonexistent/node_modules",)
    return {
        "pattern_count": len(results),
        "total_indexed_s": round(sum(r["indexed_s"] for r in results), 3),
        "total_not_indexed_s": round(sum(r["not_indexed_s"] for r in results), 3),
        "all_same_rows": all(r["same_rows"] for r in results),
        "drop_opaque_eq_indexed_s": _best(conn, eq.format(""), probe),
        "drop_opaque_eq_not_indexed_s": _best(conn, eq.format(" NOT INDEXED"), probe),
        "patterns": results,  # patterns themselves omitted: they contain the user's profile path
    }


if __name__ == "__main__":
    modes = {"sizes": sizes, "without-rowid": without_rowid, "like-cost": like_cost}
    if len(sys.argv) != 3 or sys.argv[1] not in modes:
        raise SystemExit(f"usage: measure.py {{{'|'.join(modes)}}} DB")
    print(json.dumps(modes[sys.argv[1]](sys.argv[2]), indent=1))
