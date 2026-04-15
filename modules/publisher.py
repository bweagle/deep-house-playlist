"""
Weekly mix publisher.

Resolves Spotify IDs for ranked candidates, searches YouTube for each track,
creates the weekly playlist, and adds all found tracks to it.
Saves the result to the weekly_mixes and mix_tracks tables.
"""

import os
import time
from datetime import datetime, timezone

from modules.db import get_connection
from ytmusic_client import YTMusicClient


def _build_yt_cache(db_path: str) -> dict[tuple, str]:
    """Return a (title.lower, artist.lower) → youtube_video_id map from past mixes."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT LOWER(title) AS t, LOWER(artist) AS a, youtube_video_id
        FROM mix_tracks
        WHERE youtube_video_id IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return {(r["t"], r["a"]): r["youtube_video_id"] for r in rows}


def _youtube_search(yt: YTMusicClient, candidates: list[dict], db_path: str) -> list[dict]:
    """
    Attach youtube_video_id to each candidate.
    Checks the mix_tracks cache first — only calls the API for uncached tracks.
    Stops immediately on quota exhaustion (403/429) or auth errors (401).
    """
    from googleapiclient.errors import HttpError

    cache = _build_yt_cache(db_path)
    cache_hits = 0

    for c in candidates:
        key = (c["title"].lower(), c["artist"].lower())
        if key in cache:
            c["youtube_video_id"] = cache[key]
            cache_hits += 1
            print(f"  [yt] cached: {c['title']} — {c['artist']}")
            continue

        query = f"{c['title']} {c['artist']}"
        try:
            video_id = yt.search_track(query, delay=0.5)
        except HttpError as e:
            status_code = int(e.status_code) if hasattr(e, "status_code") else 0
            if status_code in (401, 403, 429):
                reason = "quota exhausted" if status_code in (403, 429) else "auth error"
                print(f"  [yt] {reason} (HTTP {status_code}) — stopping YouTube search.")
                break
            print(f"  [yt] Search error for '{query}': {e}")
            video_id = None
        c["youtube_video_id"] = video_id
        status = "found" if video_id else "not found"
        print(f"  [yt] {status}: {c['title']} — {c['artist']}")

    if cache_hits:
        print(f"  [yt] {cache_hits} tracks served from cache (no API calls)")
    return candidates


def _save_mix(
    run_id: str,
    playlist_id: str,
    playlist_name: str,
    tracks: list[dict],
    db_path: str,
) -> None:
    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()

    track_count = sum(1 for t in tracks if t.get("youtube_video_id"))

    conn.execute(
        """
        INSERT OR REPLACE INTO weekly_mixes
            (run_id, playlist_id, playlist_name, track_count, published_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, playlist_id, playlist_name, track_count, now, now),
    )

    for t in tracks:
        conn.execute(
            """
            INSERT INTO mix_tracks
                (run_id, position, title, artist, beatport_id,
                 youtube_video_id, camelot_key, bpm, genre, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                t.get("rank", 0),
                t["title"],
                t["artist"],
                t.get("beatport_id"),
                t.get("youtube_video_id"),
                t.get("camelot_key") or t.get("key_camelot"),
                t.get("bpm"),
                t.get("genre"),
                t.get("label"),
            ),
        )

    conn.commit()
    conn.close()


def publish_mix(
    config: dict,
    db_path: str,
    run_id: str,
    ranked_tracks: list[dict],
    sp=None,
    dry_run: bool = False,
) -> dict:
    """
    Full publish flow:
      1. Search YouTube for each track
      2. Create the weekly playlist
      3. Add found tracks to the playlist
      4. Persist mix record to DB

    Returns a summary dict with playlist_id and track counts.
    """
    auth_file = config.get("youtube", {}).get("oauth_file", "oauth.json")

    # Skip YouTube interaction in CI unless explicitly enabled
    if os.environ.get("CI") and not os.environ.get("YOUTUBE_PUBLISH"):
        print("[publisher] CI environment detected — skipping YouTube publish.")
        return {"playlist_id": None, "found": 0, "total": len(ranked_tracks)}

    # 2. Connect to YouTube
    from googleapiclient.errors import HttpError as YTHttpError

    yt = YTMusicClient(auth_file=auth_file)

    # 3. YouTube search (uses cache from previous mixes to save quota)
    print(f"[publisher] Searching YouTube for {len(ranked_tracks)} tracks…")
    _youtube_search(yt, ranked_tracks, db_path)

    found = [t for t in ranked_tracks if t.get("youtube_video_id")]
    print(f"[publisher] {len(found)}/{len(ranked_tracks)} tracks found on YouTube")

    if dry_run:
        print("[publisher] Dry-run mode — playlist not created.")
        return {"playlist_id": None, "found": len(found), "total": len(ranked_tracks)}

    try:
        # 4. Create dated archive playlist (always a fresh playlist)
        week = datetime.now(timezone.utc).strftime("%Y-W%V")
        playlist_name = config.get("youtube", {}).get(
            "playlist_name_template", "Weekly Deep House Mix {week}"
        ).format(week=week)

        playlist_id = yt.create_unique_playlist(
            name=playlist_name,
            description=(
                f"Auto-generated weekly deep house mix — {week}. "
                "Curated by catalog analyzer + AI recommendations."
            ),
        )

        # 5. Add tracks to dated playlist
        video_ids = [t["youtube_video_id"] for t in found]
        added = yt.add_to_playlist(playlist_id, video_ids)
        print(f"[publisher] {added} tracks added to playlist '{playlist_name}'")

        # 6. Update pinned "current" playlist (find or create, then replace contents)
        pinned_name = config.get("youtube", {}).get("pinned_playlist_name")
        if pinned_name:
            print(f"[publisher] Updating pinned playlist '{pinned_name}'…")
            pinned_id = yt.find_or_create_playlist(
                name=pinned_name,
                description="Current weekly deep house mix — updated every week.",
            )
            removed = yt.clear_playlist(pinned_id)
            if removed:
                print(f"[publisher] Cleared {removed} old tracks from '{pinned_name}'")
            yt.add_to_playlist(pinned_id, video_ids)
            print(f"[publisher] '{pinned_name}' updated with this week's {len(video_ids)} tracks")

        # 7. Persist
        _save_mix(run_id, playlist_id, playlist_name, ranked_tracks, db_path)

    except YTHttpError as e:
        code = int(e.status_code) if hasattr(e, "status_code") else 0
        if code in (403, 429):
            print(
                "\n[publisher] YouTube quota exhausted — playlist was not created.\n"
                "Your daily quota resets at midnight Pacific time. Try again tomorrow."
            )
            return {"playlist_id": None, "found": len(found), "total": len(ranked_tracks), "error": "quota"}
        raise

    return {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "found": len(found),
        "added": added,
        "total": len(ranked_tracks),
    }
