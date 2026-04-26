#!/usr/bin/env python3
"""
Weekly Deep House Mix — CLI entry point.

Commands:
  analyze          Load CSV, match to Spotify, compute taste profile
  recommend        Run all recommendation sources and save candidates
  rank             Score/rank saved candidates and print the tracklist
  publish          Create the YouTube playlist from ranked candidates
  generate-weekly  Full pipeline: recommend → rank → publish
"""

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # loads .env from project root if present


def _load_config(path: str) -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        sys.exit(
            f"Config file '{cfg_path}' not found.\n"
            "Copy config.example.yaml to config.yaml and fill in your credentials."
        )
    with cfg_path.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_analyze(args: argparse.Namespace) -> None:
    from modules.catalog_analyzer import run_analyze
    config = _load_config(args.config)
    run_analyze(config, verbose=True, limit=args.limit)


def cmd_recommend(args: argparse.Namespace) -> None:
    import uuid
    from modules.db import init_db
    from modules.catalog_analyzer import load_taste_profile
    from modules.recommender import run_recommenders

    config = _load_config(args.config)
    db_path = config["pipeline"]["database"]
    per_source = config["pipeline"].get("recommendations_per_source", 15)

    init_db(db_path)
    taste_profile = load_taste_profile(db_path)
    run_id = args.run_id or str(uuid.uuid4())[:8]
    print(f"Run ID: {run_id}")
    candidates = run_recommenders(config, db_path, run_id, taste_profile, per_source)
    print(f"\n{len(candidates)} candidates saved to database.")


def cmd_rank(args: argparse.Namespace) -> None:
    from modules.db import get_connection
    from modules.catalog_analyzer import load_taste_profile
    from modules.filter_ranker import rank_and_filter

    config = _load_config(args.config)
    db_path = config["pipeline"]["database"]
    final_count = config["pipeline"].get("final_playlist_size", 20)
    exclude_recent_weeks = config["pipeline"].get("exclude_recent_weeks", 4)

    taste_profile = load_taste_profile(db_path)

    # Pull the most recent run's candidates
    conn = get_connection(db_path)
    if args.run_id:
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ?", (args.run_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM recommendations
            WHERE run_id = (SELECT run_id FROM recommendations ORDER BY created_at DESC LIMIT 1)
            """
        ).fetchall()
    conn.close()

    if not rows:
        sys.exit("No recommendations found. Run `recommend` first.")

    dev = getattr(args, "dev", False)
    if dev:
        print("[dev] Skipping recent-exclusion filter.")
    candidates = [dict(r) for r in rows]
    ranked = rank_and_filter(candidates, taste_profile, db_path, final_count,
                             exclude_recent_weeks=0 if dev else exclude_recent_weeks,
                             min_bpm=config["pipeline"].get("min_bpm", 0),
                             max_bpm=config["pipeline"].get("max_bpm", 0))

    print(f"\nRanked tracklist ({len(ranked)} tracks):")
    for t in ranked:
        key_str = t.get("camelot_key") or t.get("key_camelot") or "?"
        print(
            f"  {t['rank']:2}. [{t['source']:<8}] {t['title']} — {t['artist']}  "
            f"(key={key_str}, score={t.get('score', 0):.2f})"
        )


def cmd_publish(args: argparse.Namespace) -> None:
    from modules.db import get_connection
    from modules.catalog_analyzer import load_taste_profile
    from modules.filter_ranker import rank_and_filter
    from modules.publisher import publish_mix

    config = _load_config(args.config)
    db_path = config["pipeline"]["database"]
    final_count = config["pipeline"].get("final_playlist_size", 20)

    taste_profile = load_taste_profile(db_path)

    conn = get_connection(db_path)
    if args.run_id:
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ?", (args.run_id,)
        ).fetchall()
        run_id = args.run_id
    else:
        row = conn.execute(
            "SELECT run_id FROM recommendations ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            sys.exit("No recommendations found. Run `recommend` first.")
        run_id = row["run_id"]
        rows = conn.execute(
            "SELECT * FROM recommendations WHERE run_id = ?", (run_id,)
        ).fetchall()
    conn.close()

    soundcloud_only = getattr(args, "soundcloud_only", False)
    dev = getattr(args, "dev", False)
    candidates = [dict(r) for r in rows]

    # dev mode and soundcloud-only both skip the recent-exclusion filter
    if dev or soundcloud_only:
        exclude_weeks = 0
        if dev:
            print("[dev] Skipping recent-exclusion filter.")
    else:
        exclude_weeks = config["pipeline"].get("exclude_recent_weeks", 4)

    # For SoundCloud-only: rank a 2× pool so we can still hit 20 despite lower SC availability
    sc_candidate_pool = final_count * 2 if soundcloud_only else final_count
    ranked = rank_and_filter(candidates, taste_profile, db_path, sc_candidate_pool,
                             exclude_recent_weeks=exclude_weeks,
                             min_bpm=config["pipeline"].get("min_bpm", 0),
                             max_bpm=config["pipeline"].get("max_bpm", 0))

    result = publish_mix(
        config=config,
        db_path=db_path,
        run_id=run_id,
        ranked_tracks=ranked,
        dry_run=args.dry_run,
        soundcloud_only=soundcloud_only,
        sc_target_count=final_count,
    )
    if result.get("error") != "quota":
        print(result)


def cmd_generate_weekly(args: argparse.Namespace) -> None:
    from modules.scheduler import run_weekly
    config = _load_config(args.config)
    run_weekly(config, dry_run=args.dry_run, dev=getattr(args, "dev", False), verbose=True)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="Weekly Deep House Mix pipeline",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config YAML (default: config.yaml)"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = sub.add_parser("analyze", help="Load catalog CSV, match Beatport, compute taste profile")
    p_analyze.add_argument("--limit", type=int, default=None,
                           help="Only process the first N songs (useful for testing)")

    # recommend
    p_rec = sub.add_parser("recommend", help="Run all recommendation sources")
    p_rec.add_argument("--run-id", dest="run_id", default=None,
                       help="Override run ID (default: auto-generated)")

    # rank
    p_rank = sub.add_parser("rank", help="Score and rank saved candidates")
    p_rank.add_argument("--run-id", dest="run_id", default=None,
                        help="Run ID to rank (default: most recent)")
    p_rank.add_argument("--dev", action="store_true",
                        help="Skip recent-exclusion filter (show all candidates)")

    # publish
    p_pub = sub.add_parser("publish", help="Create YouTube playlist from ranked candidates")
    p_pub.add_argument("--run-id", dest="run_id", default=None,
                       help="Run ID to publish (default: most recent)")
    p_pub.add_argument("--dry-run", action="store_true",
                       help="Search YouTube but do not create playlist")
    p_pub.add_argument("--soundcloud-only", dest="soundcloud_only", action="store_true",
                       help="Skip YouTube entirely, publish to SoundCloud only")
    p_pub.add_argument("--dev", action="store_true",
                       help="Skip recent-exclusion filter")

    # generate-weekly
    p_gen = sub.add_parser("generate-weekly", help="Full pipeline: recommend → rank → publish")
    p_gen.add_argument("--dry-run", action="store_true",
                       help="Run full pipeline but skip playlist creation")
    p_gen.add_argument("--dev", action="store_true",
                       help="Skip recent-exclusion filter")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "analyze": cmd_analyze,
        "recommend": cmd_recommend,
        "rank": cmd_rank,
        "publish": cmd_publish,
        "generate-weekly": cmd_generate_weekly,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
