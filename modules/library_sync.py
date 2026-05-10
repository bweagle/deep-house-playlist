"""
Library sync — resolves YouTube video IDs for catalog songs and caches them.

Run once (or periodically) to populate youtube_video_id on the catalog table.
The mixtape command then pulls directly from this cache — no live API calls.
"""

from datetime import datetime, timezone

from modules.db import get_connection


def sync_youtube(
    db_path: str,
    auth_file: str = "oauth.json",
    re_search_days: int = 90,
    verbose: bool = True,
) -> tuple[int, int, int]:
    """
    Search YouTube for catalog songs that haven't been resolved yet (or whose
    search is older than `re_search_days`).

    Returns (found, not_found, skipped_already_cached).
    """
    from googleapiclient.errors import HttpError
    from ytmusic_client import YTMusicClient

    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT id, title, artist FROM catalog
        WHERE youtube_searched_at IS NULL
           OR youtube_searched_at < datetime('now', ?)
        ORDER BY
            CAST(COALESCE(NULLIF(rating, ''), NULLIF(rating, 'X'), '0') AS INTEGER) DESC
        """,
        (f"-{re_search_days} days",),
    ).fetchall()
    conn.close()

    already_cached = conn = None
    conn = get_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
    already_cached = total - len(rows)
    conn.close()

    if not rows:
        if verbose:
            print(f"[library-sync] All {total} catalog songs already resolved — nothing to do.")
        return 0, 0, total

    if verbose:
        print(f"[library-sync] {len(rows)} songs to search ({already_cached} already cached)…")

    yt = YTMusicClient(auth_file=auth_file)
    now = datetime.now(timezone.utc).isoformat()
    found = not_found = 0

    conn = get_connection(db_path)
    try:
        for i, row in enumerate(rows, 1):
            try:
                video_id = yt.search_track(
                    f"{row['title']} {row['artist']}",
                    title=row["title"],
                    artist=row["artist"],
                    delay=0.3,
                )
            except HttpError as e:
                status = int(e.status_code) if hasattr(e, "status_code") else 0
                if status in (401, 403, 429):
                    reason = "quota exhausted" if status in (403, 429) else "auth error"
                    print(f"\n[library-sync] Stopping: {reason} (HTTP {status}). "
                          f"Resumed progress saved ({found} found so far).")
                    break
                print(f"  [yt] Error for '{row['title']}': {e}")
                video_id = None

            conn.execute(
                "UPDATE catalog SET youtube_video_id = ?, youtube_searched_at = ? WHERE id = ?",
                (video_id, now, row["id"]),
            )
            conn.commit()

            status_str = "found" if video_id else "not found"
            if verbose:
                print(f"  [{i}/{len(rows)}] {status_str}: {row['title']} — {row['artist']}")

            if video_id:
                found += 1
            else:
                not_found += 1
    finally:
        conn.close()

    if verbose:
        print(f"\n[library-sync] Done: {found} found, {not_found} not found, "
              f"{already_cached} previously cached.")

    return found, not_found, already_cached
