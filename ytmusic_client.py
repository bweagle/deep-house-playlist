"""
YouTube Data API v3 client for playlist management.

Authentication: run `python3 setup_auth.py` once to generate oauth.json.

Note on API quota: The YouTube Data API v3 has a daily quota of 10,000 units.
  - Each search costs 100 units
  - Each playlist item insert costs 50 units
  So ~66 searches or ~200 inserts per day on the free tier.
  If you have many songs, run with --min-rating to process in batches.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class YTMusicClient:
    def __init__(self, auth_file: str | Path = "oauth.json"):
        auth_path = Path(auth_file)
        if not auth_path.exists():
            raise FileNotFoundError(
                f"Auth file '{auth_path}' not found. "
                "Run `python3 setup_auth.py` first."
            )
        with auth_path.open() as f:
            data = json.load(f)

        self.credentials = Credentials(
            token=data["token"],
            refresh_token=data["refresh_token"],
            token_uri=data["token_uri"],
            client_id=data["client_id"],
            client_secret=data["client_secret"],
            scopes=data["scopes"],
        )

        # Refresh token if expired
        if self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())
            # Don't write back to disk in CI — the file is ephemeral there
            if not os.environ.get("CI"):
                self._save_credentials(auth_path, data)

        self.youtube = build("youtube", "v3", credentials=self.credentials)

    def _save_credentials(self, auth_path: Path, existing: dict) -> None:
        existing["token"] = self.credentials.token
        with auth_path.open("w") as f:
            json.dump(existing, f, indent=2)

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def find_or_create_playlist(self, name: str, description: str = "") -> str:
        """Create a new playlist and return its ID."""
        response = self.youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": name, "description": description},
                "status": {"privacyStatus": "private"},
            },
        ).execute()
        playlist_id = response["id"]
        print(f"Created new playlist: '{name}' ({playlist_id})")
        return playlist_id

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_track(self, query: str, delay: float = 0.5) -> Optional[str]:
        """Search YouTube for a song and return the best videoId."""
        try:
            response = self.youtube.search().list(
                part="id",
                q=query,
                type="video",
                videoCategoryId="10",  # Music
                maxResults=1,
            ).execute()
            items = response.get("items", [])
            if items:
                return items[0]["id"]["videoId"]
        except HttpError as e:
            print(f"  Search error for '{query}': {e}")
        finally:
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------
    # Add tracks
    # ------------------------------------------------------------------

    def add_to_playlist(self, playlist_id: str, video_ids: list[str]) -> int:
        """Add video_ids to playlist. Returns number of tracks added."""
        added = 0
        for video_id in video_ids:
            try:
                self.youtube.playlistItems().insert(
                    part="snippet",
                    body={
                        "snippet": {
                            "playlistId": playlist_id,
                            "resourceId": {
                                "kind": "youtube#video",
                                "videoId": video_id,
                            },
                        }
                    },
                ).execute()
                added += 1
            except HttpError as e:
                print(f"  Failed to add {video_id}: {e}")
        return added
