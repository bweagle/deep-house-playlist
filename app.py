#!/usr/bin/env python3
"""
Weekly Deep House Mix — CLI entry point.

Commands:
  analyze          Load CSV, match Beatport, resolve YouTube IDs, compute taste profile
  recommend        Run all recommendation sources and save candidates
  rank             Score/rank saved candidates and print the tracklist
  publish          Create the YouTube playlist from ranked candidates
  generate-weekly  Full pipeline: recommend → rank → publish
  mixtape          Build a playlist from your catalog (YouTube or SoundCloud)
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


def cmd_mixtape(args: argparse.Namespace) -> None:
    from modules.mixtape import build_mixtape

    config = _load_config(args.config)
    db_path = config["pipeline"]["database"]
    playlist_name = args.name
    soundcloud = getattr(args, "soundcloud", False)
    platform = "soundcloud" if soundcloud else "youtube"

    print(f"\n=== Mixtape: '{playlist_name}' ({platform}) ===\n")

    print(f"[mixtape] Selecting {args.count} tracks from catalog ({platform}-cached songs only)…")
    tracks = build_mixtape(db_path, count=args.count, max_per_artist=args.max_per_artist,
                           platform=platform)

    if not tracks:
        sys.exit(
            f"[mixtape] No {platform}-resolved songs in catalog.\n"
            "Run `python3 app.py analyze` to search for your catalog songs."
        )

    print(f"[mixtape] Selected {len(tracks)} tracks:")
    for t in tracks:
        key_str = t.get("camelot_key") or "?"
        bpm_str = f"{t['bpm']:.0f}" if t.get("bpm") else "?"
        print(f"  {t['rank']:3}. [{key_str} {bpm_str}] {t['title']} — {t['artist']}  (rating={t.get('rating', '?')})")

    if args.dry_run:
        print("\n[mixtape] Dry run — stopping before playlist creation.")
        return

    if soundcloud:
        from soundcloud_client import SoundCloudClient
        sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
        sc = SoundCloudClient(token_file=sc_token)
        track_ids = [t["soundcloud_track_id"] for t in tracks]

        print(f"\n[mixtape] Creating SoundCloud playlist '{playlist_name}'…")
        playlist_id = sc.find_or_create_playlist(playlist_name, track_ids)
        print(f"[mixtape] Done — {len(track_ids)} tracks added to '{playlist_name}' (SC id: {playlist_id})")
    else:
        from ytmusic_client import YTMusicClient
        auth_file = config.get("youtube", {}).get("oauth_file", "oauth.json")
        yt = YTMusicClient(auth_file=auth_file)

        print(f"\n[mixtape] Creating YouTube playlist '{playlist_name}'…")
        playlist_id = yt.find_or_create_playlist(
            name=playlist_name,
            description=f"Personal deep house mixtape — {len(tracks)} tracks ordered harmonically.",
        )
        removed = yt.clear_playlist(playlist_id)
        if removed:
            print(f"[mixtape] Cleared {removed} old tracks from '{playlist_name}'")

        video_ids = [t["youtube_video_id"] for t in tracks]
        added = yt.add_to_playlist(playlist_id, video_ids)
        print(f"[mixtape] Done — {added}/{len(tracks)} tracks added to '{playlist_name}'")


def cmd_show_labels(args: argparse.Namespace) -> None:
    from modules.db import get_connection
    from modules.recommender_beatport import (
        _BLOCKED_LABELS, _MIN_LABEL_RELEASES, _MAX_LABEL_RECENT_RELEASES, _LABEL_ALLOWLIST,
    )

    config = _load_config(args.config)
    db_path = config["pipeline"]["database"]
    conn = get_connection(db_path)

    micro_rows = conn.execute(
        "SELECT label_id, name, track_count, fetched_at FROM label_cache ORDER BY track_count ASC"
    ).fetchall()
    churn_rows = conn.execute(
        "SELECT name, recent_count, fetched_at FROM label_churn_cache ORDER BY recent_count DESC"
    ).fetchall()
    conn.close()

    # --- Micro-label section ---
    blocked, passing, allowlisted = [], [], []
    for row in micro_rows:
        name = row["name"] or ""
        if any(name.lower().startswith(bl) for bl in _BLOCKED_LABELS):
            continue
        if name.lower() in _LABEL_ALLOWLIST:
            allowlisted.append(("micro", row["name"], row["track_count"], row["fetched_at"]))
        elif row["track_count"] < _MIN_LABEL_RELEASES:
            blocked.append(("micro", row["name"], row["track_count"], row["fetched_at"]))
        else:
            passing.append(("micro", row["name"], row["track_count"], row["fetched_at"]))

    # --- Churn section ---
    churn_blocked, churn_passing = [], []
    for row in churn_rows:
        name = row["name"] or ""
        if any(name.lower().startswith(bl) for bl in _BLOCKED_LABELS):
            continue
        if name.lower() in _LABEL_ALLOWLIST:
            allowlisted.append(("churn", row["name"], row["recent_count"], row["fetched_at"]))
        elif row["recent_count"] > _MAX_LABEL_RECENT_RELEASES:
            churn_blocked.append(row)
        else:
            churn_passing.append(row)

    print(f"\n=== Blocked: micro-labels (< {_MIN_LABEL_RELEASES} total releases) ===")
    if blocked:
        for _, name, count, fetched in blocked:
            print(f"  {name:<42} {count:4} releases  (checked {fetched[:10]})")
    else:
        print("  (none)")

    print(f"\n=== Blocked: churn factories (> {_MAX_LABEL_RECENT_RELEASES} releases in 90 days) ===")
    if churn_blocked:
        for row in churn_blocked:
            print(f"  {row['name']:<42} {row['recent_count']:4} recent  (checked {row['fetched_at'][:10]})")
    else:
        print("  (none yet — populate by running the pipeline)")

    print(f"\n=== Passing labels ===")
    all_passing = {name for _, name, _, _ in passing}
    for row in sorted(churn_passing, key=lambda r: r["recent_count"], reverse=True):
        if row["name"] in all_passing:
            print(f"  {row['name']:<42} {row['recent_count']:3}/90d")

    if allowlisted:
        print(f"\n=== Allowlisted (all filters bypassed) ===")
        for _, name, count, _ in allowlisted:
            print(f"  {name}")

    print(f"\nTo rescue a blocked label: add its name (lowercase) to _LABEL_ALLOWLIST")
    print(f"  in modules/recommender_beatport.py")


def cmd_shuffle(args: argparse.Namespace) -> None:
    from modules.shuffler import shuffle_pinned_playlists
    config = _load_config(args.config)
    shuffle_pinned_playlists(config, verbose=True)


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

    # mixtape
    p_mix = sub.add_parser("mixtape", help="Build a playlist from your own catalog, ordered harmonically")
    p_mix.add_argument("--name", default="Deephouse Mixtape",
                       help="Playlist name (default: 'Deephouse Mixtape')")
    p_mix.add_argument("--count", type=int, default=100,
                       help="Number of tracks (default: 100)")
    p_mix.add_argument("--max-per-artist", dest="max_per_artist", type=int, default=3,
                       help="Max tracks per artist (default: 3)")
    p_mix.add_argument("--soundcloud", action="store_true",
                       help="Publish to SoundCloud instead of YouTube Music")
    p_mix.add_argument("--dry-run", action="store_true",
                       help="Print tracklist without creating the playlist")

    # generate-weekly
    p_gen = sub.add_parser("generate-weekly", help="Full pipeline: recommend → rank → publish")
    p_gen.add_argument("--dry-run", action="store_true",
                       help="Run full pipeline but skip playlist creation")
    p_gen.add_argument("--dev", action="store_true",
                       help="Skip recent-exclusion filter")

    # show-labels
    sub.add_parser("show-labels", help="Show Beatport labels seen so far — blocked micro vs. passing")

    # shuffle
    sub.add_parser("shuffle", help="Randomize the pinned playlists (YouTube + SoundCloud)")

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
        "mixtape": cmd_mixtape,
        "show-labels": cmd_show_labels,
        "shuffle": cmd_shuffle,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
