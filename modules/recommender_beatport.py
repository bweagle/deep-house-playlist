"""
Beatport Recommendations source.

Pulls from Deep House, Melodic House & Techno, Organic House, and Afro House
genre charts (tracks released in the last 3 months), plus recent releases
from the top catalog artists.

Genre charts pulled:
  - Deep House (ID 12)
  - Melodic House & Techno (ID 90)
  - Organic House / Downtempo (ID 93)

Editorial charts:
  - the-hype-deep-house
  - best-new-tracks-deep-house
"""

from datetime import datetime, timedelta, timezone

from modules.beatport_client import (
    BeatportClient,
    GENRE_DEEP_HOUSE,
    GENRE_MELODIC_HOUSE_TECHNO,
    GENRE_ORGANIC_HOUSE,
)
from modules.db import get_connection

# Genres to pull charts from, in priority order
_CHART_GENRES = [
    GENRE_DEEP_HOUSE,
    GENRE_MELODIC_HOUSE_TECHNO,
    GENRE_ORGANIC_HOUSE,
]

# Named editorial charts to pull (slug → human label)
_EDITORIAL_CHARTS = {
    "the-hype-deep-house": "Deep House Hype",
    "best-new-tracks-deep-house": "Best New Tracks Deep House",
}


def get_top_catalog_artist_ids(db_path: str, top_n: int = 20) -> list[int]:
    """Return Beatport artist IDs for the top N catalog artists by play count."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT beatport_artist_ids,
               SUM(CAST(COALESCE(NULLIF(rating, ''), '0') AS INTEGER)) as total
        FROM catalog
        WHERE beatport_artist_ids IS NOT NULL AND beatport_artist_ids != ''
        GROUP BY beatport_artist_ids
        ORDER BY total DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    conn.close()

    ids: list[int] = []
    import json
    for row in rows:
        try:
            artist_ids = json.loads(row["beatport_artist_ids"])
            ids.extend(int(i) for i in artist_ids if i)
        except (json.JSONDecodeError, TypeError):
            pass
    # Deduplicate while preserving order
    seen: set[int] = set()
    return [i for i in ids if not (i in seen or seen.add(i))]  # type: ignore[func-returns-value]


def get_beatport_recommendations(
    bp: BeatportClient,
    db_path: str,
    run_id: str,
    count: int = 15,
) -> list[dict]:
    """
    Fetch candidates from Beatport charts and artist new releases.
    Returns a deduplicated candidate list.
    """
    conn = get_connection(db_path)
    existing = {
        (r["title"].lower(), r["artist"].lower())
        for r in conn.execute("SELECT title, artist FROM catalog").fetchall()
    }
    conn.close()

    seen: set[tuple] = set()
    candidates: list[dict] = []

    def _add(track: dict, reason: str) -> bool:
        key = (track["title"].lower(), track["artist"].lower())
        if key in seen or key in existing:
            return False
        seen.add(key)
        candidates.append({
            "run_id": run_id,
            "source": "beatport",
            "title": track["title"],
            "artist": track["artist"],
            "spotify_id": None,
            "beatport_id": track.get("beatport_id"),
            "bpm": track.get("bpm"),
            "camelot_key": track.get("camelot_key"),
            "genre": track.get("genre", ""),
            "sub_genre": track.get("sub_genre", ""),
            "label": track.get("label", ""),
            "release_date": track.get("release_date"),
            "reason": reason,
        })
        return True

    # 1. Genre top charts — limited to last 3 months
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    for genre_id in _CHART_GENRES:
        if len(candidates) >= count * 3:
            break
        print(f"  [beatport] Fetching genre {genre_id} top tracks (since {since})…")
        for track in bp.get_genre_top_tracks(genre_id, per_page=100, since_days=90):
            _add(track, f"Beatport genre {genre_id} top chart")

    # 2. Editorial charts
    for slug, label in _EDITORIAL_CHARTS.items():
        if len(candidates) >= count * 3:
            break
        print(f"  [beatport] Fetching editorial chart: {label}…")
        for track in bp.get_chart_tracks(slug, per_page=100):
            _add(track, f"Beatport editorial: {label}")

    # 3. Artist new releases
    artist_ids = get_top_catalog_artist_ids(db_path)
    print(f"  [beatport] Fetching new releases for {len(artist_ids)} top artists…")
    for artist_id in artist_ids[:20]:
        if len(candidates) >= count * 4:
            break
        for track in bp.get_artist_recent_tracks(artist_id, days=30):
            _add(track, f"New release from catalog artist (id={artist_id})")

    # Trim to count
    candidates = candidates[:count]
    _save_candidates(candidates, db_path)
    print(f"  [beatport] {len(candidates)} candidates generated")
    return candidates


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
                c.get("spotify_id"), c.get("beatport_id"),
                c.get("bpm"), c.get("camelot_key"),
                c.get("genre"), c.get("sub_genre"),
                c.get("label"), c.get("release_date"),
                c.get("reason"), c.get("duration_ms"), now,
            ),
        )
    conn.commit()
    conn.close()
