from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

# Gold-set labeling tool launcher (reclaim-ai-features-spec.md / the explicit autonomy-
# boundary instruction). Run this yourself, point it at a real directory of your own photos,
# label a few hundred candidate near-dup clusters, and Feature 1a Track A's operating point
# (ADR-0012, currently provisional) can graduate to a real, gold-set-derived one in a future
# ADR. This script has NOT been run against a real gold set as part of this build — it
# delivers the tool, not labels; see PLAN.md's checkpoint for why that's a deliberate stop.
#
# Same loopback-only bind discipline as `reclaim serve` — see cli.py's `_loopback_host` for
# the identical reasoning (this tool never deletes anything, but there's no reason to hold
# it to a lesser standard than the main dashboard).

_ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "::1"})
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8421  # one above reclaim serve's default 8420 — never run both at once anyway
_DEFAULT_LABELS_PATH = Path("data/ai_labels/gold_labels.jsonl")


def _loopback_host(value: str) -> str:
    if value not in _ALLOWED_BIND_HOSTS:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an allowed bind address — this tool must never be reachable "
            f"from the network. Allowed: {', '.join(sorted(_ALLOWED_BIND_HOSTS))}."
        )
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai_label_tool",
        description=(
            "Local, privacy-safe gold-set labeling tool for Reclaim's applied-AI layer. "
            "Nothing scanned or labeled ever leaves this machine."
        ),
    )
    parser.add_argument(
        "path", type=Path, help="Directory to scan for candidate near-duplicate image clusters."
    )
    parser.add_argument(
        "--max-hamming-distance",
        type=int,
        default=15,
        help="Looser than ADR-0012's CI gate (10) on purpose — shows borderline candidates "
        "for you to reject, not just the ones the current threshold already accepts (default: 15).",
    )
    parser.add_argument("--host", type=_loopback_host, default=_DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument(
        "--labels-out",
        type=Path,
        default=_DEFAULT_LABELS_PATH,
        help=f"Where to append labels (default: {_DEFAULT_LABELS_PATH}, gitignored — never "
        "committed, since it records real paths from your own disk).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to config.toml for SafetyValidator (default: config.toml, built-in "
        "defaults if missing) — candidate paths are filtered through the same safety rules "
        "the deterministic engine and Feature 1a both use.",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't automatically open a browser tab."
    )
    args = parser.parse_args(argv)

    if not args.path.is_dir():
        print(f"ai_label_tool: not a directory: {args.path}")
        return 1

    # Imports deferred: this script's own argument parsing/validation should work even if the
    # `ai` extra isn't installed yet, so the error message on a missing dependency comes from
    # reclaim.ai._optional.require (actionable), not a top-of-file ImportError traceback.
    import threading

    import uvicorn

    from reclaim.ai.labeling import discover_label_candidates
    from reclaim.ai.labeling_app import create_labeling_app
    from reclaim.config import load_config
    from reclaim.safety import SafetyValidator

    config_path: Path = args.config
    config = load_config(config_path if config_path.exists() else None)
    safety = SafetyValidator(config)

    print(f"ai_label_tool: scanning {args.path} for candidate clusters...")
    candidates = discover_label_candidates(
        args.path, safety=safety, max_hamming_distance=args.max_hamming_distance
    )
    print(
        f"ai_label_tool: {len(candidates)} candidate cluster(s) found "
        f"({sum(len(c.members) for c in candidates)} images total)"
    )
    if not candidates:
        print("ai_label_tool: nothing to label — no candidate clusters found.")
        return 0

    app = create_labeling_app(
        candidates, label_store_path=args.labels_out, host=args.host, port=args.port
    )
    url = f"http://{args.host}:{args.port}"
    print(f"ai_label_tool: {url} (Ctrl+C to stop) — labels append to {args.labels_out}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
