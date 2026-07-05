"""
Catalog classics recommendation source.

Pulls a small number of highly-rated songs from the user's own catalog
to include in the weekly mix.  Songs are scored by BPM/key fit to the
current taste profile so they blend naturally with the new picks.

Selection is drawn randomly from the top-scoring pool so the same two
tracks don't repeat back-to-back across weeks.  The standard
exclude_recent_weeks filter prevents any track from appearing more often
than once every N weeks.
"""

import random
from datetime import datetime, timezone

from modules.db import get_connection
from modules.filter_ranker import _get_recent_titles, bpm_score, camelot_score


def get_catalog_classics(
    db_path: str,
    run_id: str,
    taste_profile: dict,
    count: int = 2,
    exclude_recent_weeks: int = 4,
    min_rating: int = 3,
) -> list[dict]:
    """
    Return up to `count` catalog songs that:
      - have a YouTube video ID already resolved
      - are rated >= min_rating
      - have not appeared in a mix within the last exclude_recent_weeks weeks

    Scoring: 40% rating + 30% BPM fit + 30% Camelot key fit.
    Final selection is a random draw from the top-scoring pool (top third
    of eligible tracks, min pool size of count*3) for variety week to week.
    """
    recent = _get_recent_titles(db_path, exclude_recent_weeks)

    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT title, artist, bpm, key_camelot, rating, genre, label,
               youtube_video_id
        FROM catalog
        WHERE youtube_video_id IS NOT NULL
          AND CAST(COALESCE(NULLIF(TRIM(rating), ''), '0') AS INTEGER) >= ?
        """,
        (min_rating,),
    ).fetchall()
    conn.close()

    avg_bpm = taste_profile.get("avg_bpm")
    bpm_std = taste_profile.get("bpm_std")
    top_keys = taste_profile.get("top_keys") or []

    eligible = []
    for r in rows:
        title_key = (r["title"].lower(), (r["artist"] or "").lower())
        if title_key in recent:
            continue

        try:
            rating_val = int(r["rating"].strip())
        except (AttributeError, ValueError):
            rating_val = 0

        b = bpm_score(r["bpm"], avg_bpm, bpm_std)
        k = camelot_score(r["key_camelot"], top_keys)
        score = 0.4 * (rating_val / 4.0) + 0.3 * b + 0.3 * k

        eligible.append({
            "run_id": run_id,
            "source": "catalog",
            "title": r["title"],
            "artist": r["artist"] or "",
            "bpm": r["bpm"],
            "camelot_key": r["key_camelot"],
            "key_camelot": r["key_camelot"],
            "genre": r["genre"] or "",
            "label": r["label"] or "",
            "score": round(score, 4),
        })

    if not eligible:
        return []

    eligible.sort(key=lambda x: x["score"], reverse=True)

    # Draw randomly from the top-scoring pool so selection varies week to week
    pool_size = max(count * 3, len(eligible) // 3 + 1)
    pool_size = min(pool_size, len(eligible))
    pool = eligible[:pool_size]
    selected = random.sample(pool, min(count, len(pool)))

    _save_candidates(selected, db_path)
    return selected


def _save_candidates(candidates: list[dict], db_path: str) -> None:
    if not candidates:
        return
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        conn.execute(
            """
            INSERT INTO recommendations
                (run_id, source, title, artist, spotify_id, beatport_id,
                 bpm, camelot_key, genre, sub_genre, label, release_date,
                 reason, duration_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                c["run_id"], c["source"], c["title"], c["artist"],
                None, None,
                c.get("bpm"), c.get("camelot_key"),
                c.get("genre"), None,
                c.get("label"), None,
                "catalog classic", None, now,
            ),
        )
    conn.commit()
    conn.close()
