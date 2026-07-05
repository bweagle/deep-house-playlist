"""
Weekly pipeline orchestrator.

Ties together all phases:
  Phase 1  — Catalog analysis (analyze command, run once / on demand)
  Phase 2  — Recommendations from all sources
  Phase 3  — Filter, score, and rank candidates
  Phase 4  — Publish to YouTube

Called by `app.py generate-weekly` or GitHub Actions.
"""

import uuid
from datetime import datetime, timezone

from modules.db import get_connection, init_db
from modules.catalog_analyzer import load_taste_profile, run_analyze
from modules.recommender import run_recommenders
from modules.recommender_catalog import get_catalog_classics
from modules.filter_ranker import rank_and_filter, _spread_artists
from modules.publisher import publish_mix


def _start_run(db_path: str) -> str:
    run_id = str(uuid.uuid4())[:8]
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO run_log (run_id, started_at, status) VALUES (?, ?, ?)",
        (run_id, datetime.now(timezone.utc).isoformat(), "running"),
    )
    conn.commit()
    conn.close()
    return run_id


def _finish_run(
    db_path: str,
    run_id: str,
    status: str,
    tracks_generated: int = 0,
    tracks_published: int = 0,
    errors: str | None = None,
) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """
        UPDATE run_log
        SET completed_at = ?, status = ?,
            tracks_generated = ?, tracks_published = ?, errors = ?
        WHERE run_id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            status,
            tracks_generated,
            tracks_published,
            errors,
            run_id,
        ),
    )
    conn.commit()
    conn.close()


def run_weekly(config: dict, dry_run: bool = False, dev: bool = False, verbose: bool = True) -> dict:
    """
    Full weekly pipeline.

    config keys used:
      config["pipeline"]["database"]
      config["pipeline"]["final_playlist_size"]   (default 20)
      config["pipeline"]["recommendations_per_source"]  (default 15)
    """
    db_path = config["pipeline"]["database"]
    final_count = config["pipeline"].get("final_playlist_size", 20)
    per_source = config["pipeline"].get("recommendations_per_source", 15)

    init_db(db_path)
    run_id = _start_run(db_path)

    if verbose:
        print(f"\n=== Weekly Deep House Mix — run {run_id} ===")

    try:
        # Phase 1: Load taste profile (must exist — run `analyze` first)
        if verbose:
            print("\n[Phase 1] Loading taste profile…")
        taste_profile = load_taste_profile(db_path)
        if verbose:
            keys = taste_profile.get("top_keys", [])
            print(f"  Taste profile loaded (top keys: {', '.join(keys)})")

        # Phase 2: Recommendations
        if verbose:
            print("\n[Phase 2] Generating recommendations…")
        candidates = run_recommenders(
            config, db_path, run_id, taste_profile, per_source=per_source
        )

        if not candidates:
            _finish_run(db_path, run_id, "failed", errors="No candidates generated")
            print("  No candidates — pipeline aborted.")
            return {"run_id": run_id, "status": "failed"}

        # Phase 3: Rank
        if verbose:
            print(f"\n[Phase 3] Ranking candidates…")
            if dev:
                print("  [dev] Skipping recent-exclusion filter. Results will NOT be saved to mix history.")
        exclude_weeks = 0 if dev else config["pipeline"].get("exclude_recent_weeks", 4)
        n_classics = config["pipeline"].get("catalog_classics_count", 2)
        min_rating = config["pipeline"].get("min_rating_for_taste_profile", 3)

        # Catalog classics — guaranteed slots, already have YouTube IDs
        classics = get_catalog_classics(
            db_path, run_id, taste_profile,
            count=n_classics,
            exclude_recent_weeks=exclude_weeks,
            min_rating=min_rating,
        )
        n_regular = final_count - len(classics)

        # Rank a 2× pool for the regular slots
        ranked = rank_and_filter(
            candidates, taste_profile, db_path, n_regular * 2,
            exclude_recent_weeks=exclude_weeks,
            min_bpm=config["pipeline"].get("min_bpm", 0),
            max_bpm=config["pipeline"].get("max_bpm", 0),
        )

        # Combine: classics first (instant cache hits), then regular pool
        # Re-spread artists across the combined list
        combined = _spread_artists(classics + ranked)
        for i, t in enumerate(combined, 1):
            t["rank"] = i

        if verbose:
            print(f"  Pool: {len(classics)} catalog classic(s) + {len(ranked)} ranked (targeting {final_count}):")
            for t in combined:
                key_str = t.get("camelot_key") or t.get("key_camelot") or "?"
                print(f"    {t['rank']:2}. [{t['source']:<8}] {t['title']} — {t['artist']}  "
                      f"({key_str}, score={t.get('score', 0):.2f})")

        # Phase 4: Publish
        if verbose:
            print(f"\n[Phase 4] Publishing{' (dry run)' if dry_run else ''}…")

        # Build Spotify client for ID resolution (best-effort)
        sp = None
        try:
            from modules.recommender import build_spotify_client
            if config.get("spotify", {}).get("client_id"):
                sp = build_spotify_client(config)
        except Exception:
            pass

        result = publish_mix(
            config=config,
            db_path=db_path,
            run_id=run_id,
            ranked_tracks=combined,
            sp=sp,
            dry_run=dry_run,
            dev=dev,
            yt_target_count=final_count,
        )

        published = result.get("added", 0) if not dry_run else 0
        _finish_run(
            db_path,
            run_id,
            status="completed",
            tracks_generated=len(combined),
            tracks_published=published,
        )

        if verbose:
            if dry_run:
                print(f"\nDry run complete — {len(ranked)} tracks ranked, playlist not created.")
            else:
                print(
                    f"\nDone! Playlist '{result.get('playlist_name')}' created with "
                    f"{result.get('added', 0)}/{result.get('total', 0)} tracks."
                )

        return {"run_id": run_id, "status": "completed", **result}

    except Exception as e:
        _finish_run(db_path, run_id, "failed", errors=str(e))
        raise
