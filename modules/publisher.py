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


def _resolve_spotify_ids(
    sp: spotipy.Spotify | None,
    candidates: list[dict],
    db_path: str,
) -> list[dict]:
    """
    For candidates without a spotify_id, attempt to search Spotify.
    Updates the candidates list in-place and stores hits in the DB.
    """
    if sp is None:
        return candidates

    conn = get_connection(db_path)
    now = datetime.now(timezone.utc).isoformat()

    for c in candidates:
        if c.get("spotify_id"):
            continue
        query = f"track:{c['title']} artist:{c['artist']}"
        try:
            results = sp.search(q=query, type="track", limit=1, market="US")
            items = results.get("tracks", {}).get("items", [])
            if items:
                c["spotify_id"] = items[0]["id"]
                # Cache in catalog if the track is there
                conn.execute(
                    "UPDATE catalog SET spotify_id = ?, fetched_at = ? "
                    "WHERE LOWER(title) = LOWER(?) AND LOWER(artist) = LOWER(?)",
                    (c["spotify_id"], now, c["title"], c["artist"]),
                )
        except spotipy.exceptions.SpotifyException:
            pass

    conn.commit()
    conn.close()
    return candidates


def _youtube_search(yt: YTMusicClient, candidates: list[dict]) -> list[dict]:
    """
    Search YouTube for each candidate and attach `youtube_video_id`.
    Stops immediately on quota exhaustion (403/429) or auth errors (401).
    Candidates without a hit are retained (video_id stays None).
    """
    from googleapiclient.errors import HttpError

    for c in candidates:
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

    # 3. YouTube search
    print(f"[publisher] Searching YouTube for {len(ranked_tracks)} tracks…")
    _youtube_search(yt, ranked_tracks)

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

        playlist_id = yt.find_or_create_playlist(
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
