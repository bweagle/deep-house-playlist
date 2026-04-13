"""
Build a YouTube Music playlist from the Deep House backlog CSV.

Usage examples:

  # Preview what would be added (no changes made)
  python3 main.py --csv songs.csv --playlist "Deep House Mix" --dry-run

  # Add all rated songs (ratings 1–4) to a playlist
  python3 main.py --csv songs.csv --playlist "Deep House Mix"

  # Only top-rated songs (rating >= 2)
  python3 main.py --csv songs.csv --playlist "Deep House Best" --min-rating 2

  # Include X/backlog songs too
  python3 main.py --csv songs.csv --playlist "Deep House All" --include-backlog

Progress is saved to progress.json automatically. Re-running the same command
will resume where it left off (useful when hitting the daily API quota of ~100
searches). Quota resets at midnight Pacific Time.
"""

import argparse
import json
import sys
from pathlib import Path

from googleapiclient.errors import HttpError

from songs import filter_songs, load_songs
from ytmusic_client import YTMusicClient

PROGRESS_FILE = Path("progress.json")


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with PROGRESS_FILE.open() as f:
            return json.load(f)
    return {"playlist_id": None, "found": {}, "not_found": []}


def save_progress(progress: dict) -> None:
    with PROGRESS_FILE.open("w") as f:
        json.dump(progress, f, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a YouTube Music playlist from a Deep House CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--csv", required=True, help="Path to the songs CSV file")
    parser.add_argument("--playlist", required=True, help="YouTube Music playlist name")
    parser.add_argument(
        "--auth",
        default="oauth.json",
        help="Path to credentials file (default: oauth.json)",
    )
    parser.add_argument(
        "--min-rating",
        type=int,
        choices=[1, 2, 3, 4],
        default=None,
        help="Only include songs with numeric rating >= this value",
    )
    parser.add_argument(
        "--include-backlog",
        action="store_true",
        help="Also include songs marked X (backlog)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added without modifying any playlists",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait between search requests (default: 0.5)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # --- Load and filter songs ---
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    all_songs = load_songs(csv_path)
    songs = filter_songs(
        all_songs,
        min_rating=args.min_rating,
        include_backlog=args.include_backlog,
    )

    if not songs:
        print("No songs matched the filter criteria.")
        sys.exit(0)

    print(f"Loaded {len(all_songs)} total songs, {len(songs)} matched filters.")
    print()

    if args.dry_run:
        print(f"[DRY RUN] Would search and add to playlist: '{args.playlist}'")
        print("-" * 60)
        for song in songs:
            print(f"  {song}")
        print(f"\nTotal: {len(songs)} songs")
        return

    # --- Connect to YouTube ---
    try:
        client = YTMusicClient(args.auth)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # --- Load or create progress ---
    progress = load_progress()

    if progress["playlist_id"]:
        print(f"Resuming — using existing playlist: {progress['playlist_id']}")
        playlist_id = progress["playlist_id"]
    else:
        playlist_id = client.find_or_create_playlist(
            args.playlist,
            description="Generated from Deep House backlog spreadsheet.",
        )
        progress["playlist_id"] = playlist_id
        save_progress(progress)

    already_searched = set(progress["found"].keys()) | set(progress["not_found"])
    remaining = [s for s in songs if s.search_query not in already_searched]

    print(f"Already searched: {len(already_searched)}, remaining: {len(remaining)}")
    print()

    # --- Search for remaining tracks ---
    quota_hit = False
    for i, song in enumerate(remaining, 1):
        print(f"[{len(already_searched) + i}/{len(songs)}] Searching: {song.title} — {song.artist}")
        try:
            video_id = client.search_track(song.search_query, delay=args.delay)
        except HttpError as e:
            if e.status_code == 403:
                print(f"\nQuota exceeded. Progress saved — re-run tomorrow to continue.")
                print("(YouTube API quota resets at midnight Pacific Time.)")
                save_progress(progress)
                quota_hit = True
                break
            raise

        if video_id:
            progress["found"][song.search_query] = video_id
            print(f"        Found: https://music.youtube.com/watch?v={video_id}")
        else:
            progress["not_found"].append(song.search_query)
            print("        Not found.")

        save_progress(progress)

    if quota_hit:
        sys.exit(0)

    # --- Add all found tracks to playlist ---
    video_ids = list(progress["found"].values())
    print()
    added = client.add_to_playlist(playlist_id, video_ids)
    print(f"Added {added} tracks to '{args.playlist}'.")

    if progress["not_found"]:
        print(f"\nCould not find {len(progress['not_found'])} track(s) on YouTube Music:")
        for title in progress["not_found"]:
            print(f"  - {title}")

    # Clear progress on successful completion
    PROGRESS_FILE.unlink(missing_ok=True)
    print("\nDone! Progress file cleared.")


if __name__ == "__main__":
    main()
