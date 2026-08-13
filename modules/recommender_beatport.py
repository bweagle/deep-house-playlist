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
    GENRE_PROGRESSIVE_HOUSE,
    GENRE_MELODIC_HOUSE_TECHNO,
    GENRE_ORGANIC_HOUSE,
)
from modules.db import get_connection

# Genres to pull charts from, in priority order.
_CHART_GENRES = [
    GENRE_DEEP_HOUSE,
    GENRE_PROGRESSIVE_HOUSE,
    GENRE_MELODIC_HOUSE_TECHNO,
    GENRE_ORGANIC_HOUSE,
]

# Named editorial charts to pull (slug → human label).
# NOTE: Beatport editorial chart slugs need to be verified against the live API
# before re-enabling. All previously known slugs returned 404.
# To find valid slugs: GET /v4/catalog/charts/ with a valid token.
_EDITORIAL_CHARTS: dict[str, str] = {}

# Max tracks per artist from the genre API — prevents one prolific uploader
# from flooding the pool with a numbered series (e.g. "Uzbekistan meet Turkey Reyhan46")
_MAX_PER_ARTIST_GENRE = 1

# Self-distribution aggregators — not real labels.  Tracks released through
# these platforms are almost always bedroom / micro-releases that don't belong
# in a curated mix.  Matching is case-insensitive prefix / full-name.
_BLOCKED_LABELS = {
    "distrokid",
    "tunecore",
    "onerpm",
    "landr",
    "cd baby",
    "cdbaby",
    "amuse",
    "routenote",
    "soundrop",
    "stem disintermedia",
    "stem.ai",
    "online house music",
    "spirit sounds of trance",
}

# Labels with fewer than this many total Beatport releases are treated as
# micro/vanity labels and excluded.  Checked via the Beatport API and cached
# in the label_cache table for 30 days.
_MIN_LABEL_RELEASES = 50


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
    artist_genre_counts: dict[str, int] = {}  # tracks added per artist from genre charts
    candidates: list[dict] = []
    _label_count_cache: dict[int, int] = {}  # label_id → track_count (runtime cache)

    def _has_non_latin(text: str) -> bool:
        return any(ord(c) > 0x024F and c.isalpha() for c in text)

    def _is_micro_label(label_id: int | None, label_name: str) -> bool:
        """Return True if the label has fewer than _MIN_LABEL_RELEASES tracks on Beatport."""
        if not label_id:
            return False  # no ID — can't check, give benefit of the doubt
        if label_id in _label_count_cache:
            return _label_count_cache[label_id] < _MIN_LABEL_RELEASES
        # Check DB cache (30-day TTL)
        db_conn = get_connection(db_path)
        row = db_conn.execute(
            "SELECT track_count, fetched_at FROM label_cache WHERE label_id = ?",
            (label_id,),
        ).fetchone()
        db_conn.close()
        if row:
            fetched = datetime.fromisoformat(row["fetched_at"])
            if (datetime.now(timezone.utc) - fetched).days < 30:
                count = row["track_count"]
                _label_count_cache[label_id] = count
                return count < _MIN_LABEL_RELEASES
        # Fetch from Beatport API
        count = bp.get_label_track_count(label_id)
        if count is None:
            return False  # API error — give benefit of the doubt
        _label_count_cache[label_id] = count
        db_conn = get_connection(db_path)
        db_conn.execute(
            "INSERT OR REPLACE INTO label_cache (label_id, name, track_count, fetched_at) VALUES (?, ?, ?, ?)",
            (label_id, label_name, count, datetime.now(timezone.utc).isoformat()),
        )
        db_conn.commit()
        db_conn.close()
        if count < _MIN_LABEL_RELEASES:
            print(f"  [beatport] Skipping micro-label '{label_name}' ({count} tracks)")
        return count < _MIN_LABEL_RELEASES

    def _add(track: dict, reason: str, artist_cap: int = 0) -> bool:
        artist = track.get("artist", "").strip()
        label = track.get("label", "").strip()
        # Skip tracks where Beatport returned no artist or used the label as the artist
        if not artist or (label and artist.lower() == label.lower()):
            return False
        # Skip tracks with non-Latin script in title or artist (Arabic, Cyrillic, etc.)
        if _has_non_latin(track.get("title", "")) or _has_non_latin(artist):
            return False
        # Skip tracks on self-distribution aggregators (not real labels)
        if label and any(label.lower().startswith(bl) for bl in _BLOCKED_LABELS):
            return False
        # Skip micro/vanity labels (fewer than _MIN_LABEL_RELEASES tracks on Beatport)
        if _is_micro_label(track.get("label_id"), label):
            return False
        key = (track["title"].lower(), artist.lower())
        if key in seen or key in existing:
            return False
        # Per-artist cap: prevent a single prolific uploader from flooding the pool
        if artist_cap and artist_genre_counts.get(artist.lower(), 0) >= artist_cap:
            return False
        seen.add(key)
        if artist_cap:
            artist_genre_counts[artist.lower()] = artist_genre_counts.get(artist.lower(), 0) + 1
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
            "duration_ms": track.get("duration_ms"),
            "reason": reason,
        })
        return True

    # 1. Genre top charts — limited to last 3 months, 1 track per artist max
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    for genre_id in _CHART_GENRES:
        if len(candidates) >= count * 3:
            break
        print(f"  [beatport] Fetching genre {genre_id} top tracks (since {since})…")
        n_before = len(candidates)
        for track in bp.get_genre_top_tracks(genre_id, per_page=50, since_days=90):
            _add(track, f"Beatport genre {genre_id} top chart", artist_cap=_MAX_PER_ARTIST_GENRE)
        print(f"  [beatport] Genre {genre_id}: {len(candidates) - n_before} added")

    # 2. Editorial charts — already curated, no artist cap needed
    for slug, chart_label in _EDITORIAL_CHARTS.items():
        if len(candidates) >= count * 3:
            break
        print(f"  [beatport] Fetching editorial chart: {chart_label}…")
        n_before = len(candidates)
        for track in bp.get_chart_tracks(slug, per_page=50):
            _add(track, f"Beatport editorial: {chart_label}")
        print(f"  [beatport] Editorial '{chart_label}': {len(candidates) - n_before} added")

    # 3. Artist new releases
    artist_ids = get_top_catalog_artist_ids(db_path)
    print(f"  [beatport] Fetching new releases for {len(artist_ids)} top artists…")
    n_before = len(candidates)
    for artist_id in artist_ids[:20]:
        if len(candidates) >= count * 4:
            break
        for track in bp.get_artist_recent_tracks(artist_id, days=30):
            _add(track, f"New release from catalog artist (id={artist_id})")
    print(f"  [beatport] Artist new releases: {len(candidates) - n_before} added")

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
