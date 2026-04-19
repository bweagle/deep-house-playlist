"""
SoundCloud API client for track search and playlist management.

Authentication uses OAuth 2.0 authorization_code flow.
Run `python3 setup_soundcloud_auth.py` once to generate soundcloud_token.json.

To register an app: https://soundcloud.com/you/apps
"""

import json
import time
from pathlib import Path
from typing import Optional

import requests

SOUNDCLOUD_BASE = "https://api.soundcloud.com"
SOUNDCLOUD_AUTH_URL = "https://soundcloud.com/connect"
SOUNDCLOUD_TOKEN_URL = "https://api.soundcloud.com/oauth2/token"


class SoundCloudClient:
    def __init__(self, token_file: str | Path = "soundcloud_token.json"):
        self.token_file = Path(token_file)
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"Token file '{self.token_file}' not found. "
                "Run `python3 setup_soundcloud_auth.py` first."
            )
        with self.token_file.open() as f:
            self._token = json.load(f)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"OAuth {self._token['access_token']}"}

    def _get(self, path: str, **params) -> dict:
        resp = requests.get(
            f"{SOUNDCLOUD_BASE}{path}",
            headers=self._headers(),
            params={k: v for k, v in params.items() if v is not None},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = requests.post(
            f"{SOUNDCLOUD_BASE}{path}",
            headers={**self._headers(), "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            timeout=15,
        )
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} — {resp.text[:400]}", response=resp
            )
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        resp = requests.put(
            f"{SOUNDCLOUD_BASE}{path}",
            headers={**self._headers(), "Content-Type": "application/json; charset=utf-8"},
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Track search
    # ------------------------------------------------------------------

    def search_track(self, title: str, artist: str = "", delay: float = 0.3) -> Optional[int]:
        """
        Search SoundCloud for a track. Returns the track ID or None.
        """
        query = f"{artist} {title}".strip() if artist else title
        try:
            data = self._get("/tracks", q=query, limit=5)
            tracks = data if isinstance(data, list) else data.get("collection", [])
            if tracks:
                return tracks[0]["id"]
        except requests.HTTPError as e:
            raise
        except Exception:
            pass
        finally:
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def find_playlist(self, name: str) -> Optional[int]:
        """Return the playlist ID matching name from the user's playlists, or None."""
        try:
            data = self._get("/me/playlists", limit=200)
            playlists = data if isinstance(data, list) else data.get("collection", [])
            for pl in playlists:
                if pl.get("title") == name:
                    return pl["id"]
        except Exception:
            pass
        return None

    def create_playlist(self, name: str, track_ids: list[int], public: bool = False) -> int:
        """Create a new playlist with the given tracks. Returns the playlist ID."""
        body = {
            "playlist": {
                "title": name,
                "sharing": "public" if public else "private",
                "tracks": [{"id": tid} for tid in track_ids],
            }
        }
        data = self._post("/playlists", body)
        playlist_id = data["id"]
        print(f"  [sc] Created playlist: '{name}' ({playlist_id})")
        return playlist_id

    def update_playlist(self, playlist_id: int, track_ids: list[int]) -> None:
        """Replace all tracks in an existing playlist."""
        body = {
            "playlist": {
                "tracks": [{"id": tid} for tid in track_ids],
            }
        }
        self._put(f"/playlists/{playlist_id}", body)

    def find_or_create_playlist(self, name: str, track_ids: list[int]) -> int:
        """Update existing playlist by name, or create a new one."""
        existing = self.find_playlist(name)
        if existing:
            print(f"  [sc] Updating existing playlist: '{name}' ({existing})")
            self.update_playlist(existing, track_ids)
            return existing
        return self.create_playlist(name, track_ids)

    def create_unique_playlist(self, name: str, track_ids: list[int]) -> int:
        """Always create a new playlist, appending .1, .2 if name already exists."""
        candidate = name
        n = 1
        while self.find_playlist(candidate) is not None:
            candidate = f"{name}.{n}"
            n += 1
        return self.create_playlist(candidate, track_ids)
