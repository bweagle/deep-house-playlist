"""
Last.fm Similar Artists source.

Pulls similar artists for the top catalog artists and fetches
their top tracks, surfacing underground/Bandcamp-tier music that
Spotify's algorithm often misses.
"""

from datetime import datetime, timezone

import requests

from modules.db import get_connection

LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"


def _lastfm_get(api_key: str, method: str, **params) -> dict:
    resp = requests.get(
        LASTFM_API_BASE,
        params={"method": method, "api_key": api_key, "format": "json", **params},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_top_catalog_artists(db_path: str, top_n: int = 15) -> list[str]:
    """Return the top N artists by total play count (sum of ratings) from catalog."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT artist, SUM(CAST(COALESCE(NULLIF(rating, ''), '0') AS INTEGER)) as total
        FROM catalog
        WHERE artist != ''
        GROUP BY artist
        ORDER BY total DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()
    conn.close()
    return [r["artist"] for r in rows]


def get_lastfm_recommendations(
    api_key: str,
    db_path: str,
    run_id: str,
    count: int = 15,
) -> list[dict]:
    """
    For each top catalog artist, fetch similar artists and their top tracks.
    Returns a deduplicated candidate list.
    """
    top_artists = get_top_catalog_artists(db_path)
    if not top_artists:
        print("  [lastfm] No catalog artists found.")
        return []

    # Collect all catalog (title, artist) pairs for dedup
    conn = get_connection(db_path)
    existing = {
        (r["title"].lower(), r["artist"].lower())
        for r in conn.execute("SELECT title, artist FROM catalog").fetchall()
    }
    conn.close()

    seen: set[tuple] = set()
    candidates: list[dict] = []

    for artist in top_artists:
        try:
            data = _lastfm_get(api_key, "artist.getSimilar", artist=artist, limit=10)
        except requests.RequestException as e:
            print(f"  [lastfm] getSimilar error for '{artist}': {e}")
            continue

        similar = data.get("similarartists", {}).get("artist", [])

        for similar_artist in similar[:5]:
            name = similar_artist.get("name", "")
            if not name:
                continue

            try:
                top_tracks = _lastfm_get(
                    api_key, "artist.getTopTracks", artist=name, limit=5
                )
            except requests.RequestException:
                continue

            for track in top_tracks.get("toptracks", {}).get("track", []):
                title = track.get("name", "")
                if not title:
                    continue

                key = (title.lower(), name.lower())
                if key in seen or key in existing:
                    continue
                seen.add(key)

                candidates.append(
                    {
                        "run_id": run_id,
                        "source": "lastfm",
                        "title": title,
                        "artist": name,
                        "spotify_id": None,
                        "reason": f"Top track by {name}, similar to {artist} in your catalog",
                    }
                )

                if len(candidates) >= count:
                    break
            if len(candidates) >= count:
                break
        if len(candidates) >= count:
            break

    _save_candidates(candidates, db_path)
    print(f"  [lastfm] {len(candidates)} candidates generated")
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
