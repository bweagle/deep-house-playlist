"""
Spotify Recommendations source.

Uses Spotify's /recommendations endpoint with seeds drawn from your
highest-rated catalog tracks. Runs multiple calls with rotated seeds
so different tracks surface each week.
"""

import random
from datetime import datetime, timezone

import spotipy

from modules.db import get_connection


def get_spotify_recommendations(
    sp: spotipy.Spotify,
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int = 15,
    seed_calls: int = 3,
) -> list[dict]:
    """
    Generate candidate tracks from Spotify's recommendation engine.

    Seeds are randomly sampled from catalog tracks that have Spotify IDs
    and a numeric rating. Multiple seed calls are made to ensure diversity.
    Returns a deduplicated list of candidate dicts.
    """
    conn = get_connection(db_path)

    # Pull rated catalog tracks that have Spotify IDs
    rows = conn.execute(
        """
        SELECT spotify_id, title, artist, CAST(rating AS INTEGER) as rating
        FROM catalog
        WHERE spotify_id IS NOT NULL
          AND CAST(rating AS INTEGER) >= 1
        ORDER BY rating DESC
        """,
    ).fetchall()
    conn.close()

    if not rows:
        print("  [spotify] No Spotify-matched catalog tracks found.")
        return []

    # Build audio feature targets from taste profile
    feature_targets = {}
    if taste_profile.get("avg_energy"):
        feature_targets["target_energy"] = taste_profile["avg_energy"]
    if taste_profile.get("avg_danceability"):
        feature_targets["target_danceability"] = taste_profile["avg_danceability"]
    if taste_profile.get("avg_valence"):
        feature_targets["target_valence"] = taste_profile["avg_valence"]
    if taste_profile.get("avg_bpm"):
        feature_targets["target_tempo"] = taste_profile["avg_bpm"]
        feature_targets["min_tempo"] = taste_profile["avg_bpm"] - 10
        feature_targets["max_tempo"] = taste_profile["avg_bpm"] + 10

    seen: set[str] = set()
    candidates: list[dict] = []
    spotify_ids = [r["spotify_id"] for r in rows]

    for _ in range(seed_calls):
        seeds = random.sample(spotify_ids, min(5, len(spotify_ids)))
        try:
            result = sp.recommendations(
                seed_tracks=seeds,
                limit=count,
                **feature_targets,
            )
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 403:
                print(f"  [spotify] Recommendations unavailable (HTTP 403): {e.msg}")
                print("  Skipping Spotify recommendations — check your app's subscription status.")
                break
            print(f"  [spotify] Recommendations error: {e}")
            continue

        for track in result.get("tracks", []):
            sid = track["id"]
            if sid in seen:
                continue
            seen.add(sid)

            artists = ", ".join(a["name"] for a in track.get("artists", []))
            candidates.append(
                {
                    "run_id": run_id,
                    "source": "spotify",
                    "title": track["name"],
                    "artist": artists,
                    "spotify_id": sid,
                    "reason": f"Spotify recommendation seeded from your rated tracks",
                }
            )

    _save_candidates(candidates, db_path)
    print(f"  [spotify] {len(candidates)} candidates generated")
    return candidates


def _save_candidates(candidates: list[dict], db_path: str) -> None:
    if not candidates:
        return
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        conn.execute(
            """
            INSERT INTO recommendations (run_id, source, title, artist, spotify_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (c["run_id"], c["source"], c["title"], c["artist"],
             c.get("spotify_id"), c.get("reason"), now),
        )
    conn.commit()
    conn.close()
