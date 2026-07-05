"""
Weekly mix publisher.

Resolves Spotify IDs for ranked candidates, searches YouTube for each track,
creates the weekly playlist, and adds all found tracks to it.
Saves the result to the weekly_mixes and mix_tracks tables.
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from modules.db import get_connection
from ytmusic_client import YTMusicClient


def _build_yt_cache(db_path: str) -> dict[tuple, str]:
    """
    Return a (title.lower, artist.lower) → youtube_video_id map.
    Includes both past mix tracks and catalog songs so catalog classics
    are always instant cache hits (no quota spent on searching them).
    """
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT LOWER(title) AS t, LOWER(artist) AS a, youtube_video_id
        FROM mix_tracks WHERE youtube_video_id IS NOT NULL
        UNION
        SELECT LOWER(title) AS t, LOWER(COALESCE(artist, '')) AS a, youtube_video_id
        FROM catalog WHERE youtube_video_id IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return {(r["t"], r["a"]): r["youtube_video_id"] for r in rows}


def _youtube_search(yt: YTMusicClient, candidates: list[dict], db_path: str,
                    target: int = 0) -> list[dict]:
    """
    Attach youtube_video_id to each candidate.
    Checks the mix_tracks cache first — only calls the API for uncached tracks.
    Retries up to 3× on 429 (rate limit) with exponential backoff (5s/10s/20s).
    Stops on 403 (daily quota exhausted) or 401 (auth error).
    If target > 0, stops searching once that many tracks have been found.
    """
    from googleapiclient.errors import HttpError

    cache = _build_yt_cache(db_path)
    cache_hits = 0
    found_count = 0

    for c in candidates:
        if target and found_count >= target:
            break
        key = (c["title"].lower(), c["artist"].lower())
        if key in cache:
            c["youtube_video_id"] = cache[key]
            cache_hits += 1
            found_count += 1
            print(f"  [yt] cached: {c['title']} — {c['artist']}")
            continue

        query = f"{c['title']} {c['artist']}"
        video_id = None
        stop_all = False

        for attempt in range(4):  # initial try + up to 3 retries on 429
            try:
                video_id = yt.search_track(query, title=c["title"], artist=c["artist"], delay=1.0)
                break
            except HttpError as e:
                status_code = int(e.status_code) if hasattr(e, "status_code") else 0
                if status_code == 429 and attempt < 3:
                    wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    print(f"  [yt] rate limited — waiting {wait}s (retry {attempt + 1}/3)…")
                    time.sleep(wait)
                elif status_code == 403:
                    print(f"  [yt] quota exhausted (403) — stopping YouTube search.")
                    stop_all = True
                    break
                elif status_code == 429:
                    print(f"  [yt] rate limited (429) — max retries reached, stopping YouTube search.")
                    stop_all = True
                    break
                elif status_code == 401:
                    print(f"  [yt] auth error (401) — stopping YouTube search.")
                    stop_all = True
                    break
                else:
                    print(f"  [yt] Search error for '{query}': {e}")
                    break

        if stop_all:
            break

        c["youtube_video_id"] = video_id
        if video_id:
            found_count += 1
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


def _publish_soundcloud(
    config: dict,
    ranked_tracks: list[dict],
    week: str,
    target_count: int = 0,
) -> None:
    """
    Search SoundCloud for each track and publish to a dated + pinned playlist.

    ranked_tracks may be a larger pool (e.g. 2× the desired size).
    target_count caps how many SC tracks to collect before stopping the search.
    After collecting, the found tracks are re-sorted harmonically so the
    playlist order reflects SC availability, not the original ranking order.
    """
    from soundcloud_client import SoundCloudClient

    sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
    playlist_template = config.get("soundcloud", {}).get(
        "playlist_name_template",
        config.get("youtube", {}).get("playlist_name_template", "Weekly Deep House Mix {week}"),
    ).format(week=week)
    pinned_name = config.get("soundcloud", {}).get(
        "pinned_playlist_name",
        config.get("youtube", {}).get("pinned_playlist_name"),
    )

    if target_count <= 0:
        target_count = len(ranked_tracks)

    try:
        sc = SoundCloudClient(token_file=sc_token)
    except FileNotFoundError:
        return

    print(f"[soundcloud] Searching up to {len(ranked_tracks)} candidates for {target_count} tracks…")
    found_tracks: list[dict] = []   # full track dicts for re-sorting
    track_ids: list[int] = []
    consecutive_errors = 0

    for t in ranked_tracks:
        if len(track_ids) >= target_count:
            break
        try:
            tid = sc.search_track(t["title"], t["artist"])
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            print(f"  [sc] Search error for '{t['title']}': {e}")
            if consecutive_errors >= 3:
                print("  [sc] Stopping after 3 consecutive errors.")
                break
            continue

        if tid:
            found_tracks.append({**t, "sc_track_id": tid})
            track_ids.append(tid)
            print(f"  [sc] found ({len(track_ids)}/{target_count}): {t['title']} — {t['artist']}")
        else:
            print(f"  [sc] not found: {t['title']} — {t['artist']}")

    print(f"[soundcloud] {len(track_ids)}/{len(ranked_tracks)} candidates searched, {len(track_ids)} found")
    if not track_ids:
        return

    track_ids = [t["sc_track_id"] for t in found_tracks]

    print("[soundcloud] Final tracklist order:")
    for i, t in enumerate(found_tracks, 1):
        key_str = t.get("camelot_key") or t.get("key_camelot") or "?"
        bpm_str = f"{t['bpm']:.0f}" if t.get("bpm") else "?"
        print(f"  {i:2}. [{key_str} {bpm_str}] {t['title']} — {t['artist']}")

    try:
        sc.create_unique_playlist(playlist_template, track_ids)
        if pinned_name:
            print(f"[soundcloud] Updating pinned playlist '{pinned_name}'…")
            sc.find_or_create_playlist(pinned_name, track_ids)
    except Exception as e:
        print(f"[soundcloud] Playlist creation failed: {e}")


def publish_mix(
    config: dict,
    db_path: str,
    run_id: str,
    ranked_tracks: list[dict],
    sp=None,
    dry_run: bool = False,
    soundcloud_only: bool = False,
    sc_target_count: int = 0,
    yt_target_count: int = 0,
    dev: bool = False,
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

    # SoundCloud-only mode: skip YouTube entirely
    if soundcloud_only:
        print("[publisher] SoundCloud-only mode — skipping YouTube.")
        week = datetime.now(timezone.utc).strftime("%Y-W%V")
        sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
        if Path(sc_token).exists():
            _publish_soundcloud(config, ranked_tracks, week, target_count=sc_target_count)
        # Save to mix history so these tracks are excluded from future runs
        if not dev:
            _save_mix(run_id, "soundcloud-only", f"SC {week}", ranked_tracks, db_path)
        return {"playlist_id": None, "found": 0, "total": len(ranked_tracks)}

    # Skip YouTube interaction in CI unless explicitly enabled
    if os.environ.get("CI") and not os.environ.get("YOUTUBE_PUBLISH"):
        print("[publisher] CI environment detected — skipping YouTube publish.")
        return {"playlist_id": None, "found": 0, "total": len(ranked_tracks)}

    # 2. Connect to YouTube
    from googleapiclient.errors import HttpError as YTHttpError

    yt = YTMusicClient(auth_file=auth_file)

    # 3. YouTube search (uses cache from previous mixes to save quota)
    pool_size = len(ranked_tracks)
    print(f"[publisher] Searching YouTube (pool={pool_size}, target={yt_target_count or pool_size})…")
    _youtube_search(yt, ranked_tracks, db_path, target=yt_target_count)

    found = [t for t in ranked_tracks if t.get("youtube_video_id")]
    if yt_target_count:
        found = found[:yt_target_count]
    print(f"[publisher] {len(found)}/{pool_size} tracks found on YouTube")

    if not found:
        print("[publisher] No tracks found — skipping playlist creation.")
        return {"playlist_id": None, "found": 0, "total": len(ranked_tracks)}

    week = datetime.now(timezone.utc).strftime("%Y-W%V")

    if dry_run:
        print("[publisher] Dry-run mode — YouTube playlist not created.")
        sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
        if Path(sc_token).exists():
            _publish_soundcloud(config, ranked_tracks, week, target_count=sc_target_count)
        return {"playlist_id": None, "found": len(found), "total": len(ranked_tracks)}

    try:
        # 4. Create dated archive playlist (always a fresh playlist)
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

        # 7. Persist (skipped in dev mode so tracks aren't marked as recently played)
        if not dev:
            _save_mix(run_id, playlist_id, playlist_name, ranked_tracks, db_path)
        else:
            print("[publisher] dev mode — skipping mix history save.")

    except YTHttpError as e:
        code = int(e.status_code) if hasattr(e, "status_code") else 0
        if code in (403, 429):
            print(
                "\n[publisher] YouTube quota exhausted — playlist was not created.\n"
                "Your daily quota resets at midnight Pacific time. Try again tomorrow."
            )
            return {"playlist_id": None, "found": len(found), "total": len(ranked_tracks), "error": "quota"}
        raise

    result = {
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "found": len(found),
        "added": added,
        "total": len(ranked_tracks),
    }

    # 8. SoundCloud publish (optional)
    sc_token = config.get("soundcloud", {}).get("token_file", "soundcloud_token.json")
    if Path(sc_token).exists():
        _publish_soundcloud(config, ranked_tracks, week, target_count=sc_target_count)

    return result
