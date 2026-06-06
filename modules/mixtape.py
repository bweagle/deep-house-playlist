"""
Mixtape generator — builds a playlist from the user's own catalog.

Songs are scored by rating (4 = best), an artist cap is applied,
then selected by score (highest-rated first).
"""

from modules.db import get_connection
from modules.filter_ranker import _apply_artist_cap


def _rating_score(rating: str | None) -> float:
    """Convert catalog rating to 0–1 score.  4→1.0, 3→0.75, 2→0.5, 1→0.25, X/blank→0.1"""
    if not rating:
        return 0.1
    r = rating.strip().lower()
    if r in ("x", ""):
        return 0.1
    try:
        return int(r) / 4.0
    except ValueError:
        return 0.1


def build_mixtape(
    db_path: str,
    count: int = 100,
    max_per_artist: int = 3,
    platform: str = "youtube",
) -> list[dict]:
    """
    Select `count` songs from the catalog ordered for a DJ set.

    1. Score every catalog track by rating.
    2. Apply a per-artist cap so no artist dominates.
    3. Harmonically sort using the Camelot wheel.
    4. Spread artists so no two adjacent tracks share the same artist.

    `platform` controls which cached IDs are required:
      "youtube"    — only songs with youtube_video_id
      "soundcloud" — only songs with soundcloud_track_id
    """
    conn = get_connection(db_path)
    if platform == "soundcloud":
        query = (
            "SELECT title, artist, bpm, key_camelot, rating, genre, label, "
            "youtube_video_id, soundcloud_track_id FROM catalog "
            "WHERE soundcloud_track_id IS NOT NULL"
        )
    else:
        query = (
            "SELECT title, artist, bpm, key_camelot, rating, genre, label, "
            "youtube_video_id, soundcloud_track_id FROM catalog "
            "WHERE youtube_video_id IS NOT NULL"
        )
    rows = conn.execute(query).fetchall()
    conn.close()

    candidates = [
        {
            "title": r["title"],
            "artist": r["artist"] or "",
            "bpm": r["bpm"],
            "camelot_key": r["key_camelot"],
            "rating": r["rating"],
            "genre": r["genre"] or "",
            "label": r["label"] or "",
            "youtube_video_id": r["youtube_video_id"],
            "soundcloud_track_id": r["soundcloud_track_id"],
            "score": _rating_score(r["rating"]),
        }
        for r in rows
    ]

    # Highest-rated first
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # Cap per artist, take exactly `count` tracks
    selected = _apply_artist_cap(candidates, count, max_per_artist)

    for i, t in enumerate(selected, 1):
        t["rank"] = i
    return selected
