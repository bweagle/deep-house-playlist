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

    def find_playlist(self, name: str) -> Optional[str]:
        """Return the playlist ID of the first playlist matching name, or None."""
        page_token = None
        while True:
            kwargs = dict(part="snippet", mine=True, maxResults=50)
            if page_token:
                kwargs["pageToken"] = page_token
            response = self.youtube.playlists().list(**kwargs).execute()
            for item in response.get("items", []):
                if item["snippet"]["title"] == name:
                    return item["id"]
            page_token = response.get("nextPageToken")
            if not page_token:
                return None

    def find_or_create_playlist(self, name: str, description: str = "") -> str:
        """Return existing playlist ID by name, or create a new one."""
        existing = self.find_playlist(name)
        if existing:
            print(f"Found existing playlist: '{name}' ({existing})")
            return existing
        return self._create_playlist(name, description)

    def create_unique_playlist(self, name: str, description: str = "") -> str:
        """
        Always create a new playlist. If a playlist named `name` already
        exists, appends .1, .2, … until an unused name is found.
        """
        candidate = name
        n = 1
        while self.find_playlist(candidate) is not None:
            candidate = f"{name}.{n}"
            n += 1
        return self._create_playlist(candidate, description)

    def _create_playlist(self, name: str, description: str = "") -> str:
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

    def clear_playlist(self, playlist_id: str) -> int:
        """Remove all items from a playlist. Returns number of items removed."""
        removed = 0
        page_token = None
        while True:
            kwargs = dict(part="id", playlistId=playlist_id, maxResults=50)
            if page_token:
                kwargs["pageToken"] = page_token
            try:
                response = self.youtube.playlistItems().list(**kwargs).execute()
            except HttpError as e:
                if int(e.status_code) == 404:
                    return 0  # playlist was just created — nothing to clear
                raise
            for item in response.get("items", []):
                for attempt in range(3):
                    try:
                        self.youtube.playlistItems().delete(id=item["id"]).execute()
                        break
                    except HttpError as e:
                        if int(e.status_code) == 409 and attempt < 2:
                            time.sleep(2 ** attempt)
                        else:
                            raise
                removed += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return removed

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_duration_seconds(iso: str) -> int:
        """Convert ISO 8601 duration (PT1H28M10S) to total seconds."""
        import re
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
        if not m:
            return 0
        h, mins, s = (int(x or 0) for x in m.groups())
        return h * 3600 + mins * 60 + s

    @staticmethod
    def _artist_keyword(artist: str) -> str:
        """First significant word of the primary artist name (before any comma)."""
        import re
        primary = artist.split(",")[0].strip()
        words = re.sub(r"[^a-z0-9 ]", "", primary.lower()).split()
        stop = {"the", "a", "an", "dj", "mc", "and", "feat", "ft", "vs", "b2b"}
        sig = [w for w in words if w not in stop and len(w) > 1]
        return sig[0] if sig else ""

    def search_track(self, query: str, title: str = "", artist: str = "", delay: float = 0.5) -> Optional[str]:
        """
        Search YouTube for a song and return the best matching videoId.

        Fetches up to 5 candidates, then batch-checks via videos.list (1 quota unit).
        A result is accepted only when ALL conditions hold:
          1. Duration between 3 and 10 minutes
          2. At least one significant word from `title` in the video title
          3. Artist keyword appears in the channel name (when artist is provided)

        Raises HttpError so callers can detect quota exhaustion (403/429).
        """
        import re
        _3_MIN, _10_MIN = 3 * 60, 10 * 60
        _STOP = {"the", "a", "an", "feat", "ft", "vs", "remix", "mix",
                 "original", "club", "edit", "extended", "radio", "version"}
        title_words = (
            {w for w in re.sub(r"[^a-z0-9 ]", " ", title.lower()).split()
             if w not in _STOP and len(w) > 2}
            if title else set()
        )
        artist_kw = self._artist_keyword(artist) if artist else ""

        try:
            response = self.youtube.search().list(
                part="id",
                q=query,
                type="video",
                videoCategoryId="10",  # Music
                maxResults=5,
            ).execute()
            items = response.get("items", [])
            if not items:
                return None

            video_ids = [item["id"]["videoId"] for item in items]

            # Batch-fetch duration + title + channel (costs 1 quota unit for all 5)
            details = self.youtube.videos().list(
                part="contentDetails,snippet",
                id=",".join(video_ids),
            ).execute()

            for vid in details.get("items", []):
                # 1. Duration check
                secs = self._iso_duration_seconds(
                    vid.get("contentDetails", {}).get("duration", "")
                )
                if not (_3_MIN <= secs <= _10_MIN):
                    continue

                snippet = vid.get("snippet", {})

                # 2. Title word overlap
                if title_words:
                    yt_title = snippet.get("title", "").lower()
                    yt_words = set(re.sub(r"[^a-z0-9 ]", " ", yt_title).split())
                    if not (title_words & yt_words):
                        continue

                # 3. Artist keyword in channel name
                if artist_kw:
                    channel = re.sub(r"[^a-z0-9 ]", " ",
                                     snippet.get("channelTitle", "").lower())
                    if artist_kw not in channel:
                        continue

                return vid["id"]

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
