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
    # Token management
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        """Exchange the refresh_token for a new access_token and save it."""
        resp = requests.post(
            SOUNDCLOUD_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._token["client_id"],
                "client_secret": self._token.get("client_secret", ""),
                "refresh_token": self._token["refresh_token"],
            },
            timeout=15,
        )
        if not resp.ok:
            raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")
        new = resp.json()
        self._token.update(new)
        with self.token_file.open("w") as f:
            json.dump(self._token, f, indent=2)

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
        if resp.status_code == 401:
            self._refresh()
            resp = requests.get(
                f"{SOUNDCLOUD_BASE}{path}",
                headers=self._headers(),
                params={k: v for k, v in params.items() if v is not None},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json; charset=utf-8"}
        resp = requests.post(f"{SOUNDCLOUD_BASE}{path}", headers=headers, data=raw, timeout=15)
        if resp.status_code == 401:
            self._refresh()
            headers = {**self._headers(), "Content-Type": "application/json; charset=utf-8"}
            resp = requests.post(f"{SOUNDCLOUD_BASE}{path}", headers=headers, data=raw, timeout=15)
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} — {resp.text[:400]}", response=resp
            )
        return resp.json()

    def _put(self, path: str, body: dict) -> dict:
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json; charset=utf-8"}
        resp = requests.put(f"{SOUNDCLOUD_BASE}{path}", headers=headers, data=raw, timeout=15)
        if resp.status_code == 401:
            self._refresh()
            headers = {**self._headers(), "Content-Type": "application/json; charset=utf-8"}
            resp = requests.put(f"{SOUNDCLOUD_BASE}{path}", headers=headers, data=raw, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Track search
    # ------------------------------------------------------------------

    @staticmethod
    def _artist_keyword(artist: str) -> str:
        """Return the first significant word of the primary artist name (before any comma)."""
        import re
        primary = artist.split(",")[0].strip()
        words = re.sub(r"[^a-z0-9 ]", "", primary.lower()).split()
        stop = {"the", "a", "an", "dj", "mc", "and", "feat", "ft", "vs", "b2b"}
        sig = [w for w in words if w not in stop and len(w) > 1]
        return sig[0] if sig else ""

    @staticmethod
    def _tokenize(s: str) -> set[str]:
        """Lowercase, strip non-alphanumeric, split into words."""
        import re
        return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())

    _TITLE_STOP = frozenset({
        "the", "a", "an", "feat", "ft", "vs", "remix", "mix",
        "original", "club", "edit", "extended", "radio", "version",
    })

    def search_track(self, title: str, artist: str = "", delay: float = 0.3) -> Optional[int]:
        """
        Search SoundCloud for a track. Returns the track ID or None.

        A result is accepted only when BOTH conditions hold:
          1. Artist keyword appears as a whole word in the uploader's username/full_name
             (substring match is intentionally avoided — "ame" must not match "amepure").
          2. At least one significant word from the search title appears in the SC track title
             (prevents accepting a different track by the same artist, e.g. "Labyrinth"
             when we searched for "Luminous").
        """
        query = f"{artist} {title}".strip() if artist else title
        artist_kw = self._artist_keyword(artist) if artist else ""
        _10_MIN_MS = 10 * 60 * 1000

        # Significant words from the requested title (used for title overlap check)
        title_sig = self._tokenize(title) - self._TITLE_STOP

        try:
            data = self._get("/tracks", q=query, limit=10, **{"duration[to]": _10_MIN_MS})
            tracks = data if isinstance(data, list) else data.get("collection", [])
            playable = [t for t in tracks if t.get("access") == "playable"]

            if artist_kw:
                for t in playable:
                    # 1. Artist keyword must appear as a substring in the uploader's
                    #    username/full_name (whole-word was too strict for run-together
                    #    SC usernames like "janblomavist" or "townshiprebellion")
                    uploader = self._tokenize(
                        (t.get("user", {}).get("username") or "") + " " +
                        (t.get("user", {}).get("full_name") or "")
                    )
                    uploader_str = " ".join(uploader)
                    if artist_kw not in uploader_str:
                        continue

                    # 2. At least one significant title word must appear in the SC title
                    #    (prevents accepting a different track by the same artist, e.g.
                    #    "Labyrinth" when we searched for "Luminous")
                    sc_title_words = self._tokenize(t.get("title") or "")
                    if title_sig and not (title_sig & sc_title_words):
                        continue

                    return t["id"]
            else:
                if playable:
                    return playable[0]["id"]

        except requests.HTTPError:
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
                "tracks": [{"urn": f"soundcloud:tracks:{tid}"} for tid in track_ids],
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
                "tracks": [{"urn": f"soundcloud:tracks:{tid}"} for tid in track_ids],
            }
        }
        self._put(f"/playlists/{playlist_id}", body)

    def delete_playlist(self, playlist_id: int) -> None:
        """Delete a playlist by ID."""
        resp = requests.delete(
            f"{SOUNDCLOUD_BASE}/playlists/{playlist_id}",
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code == 401:
            self._refresh()
            resp = requests.delete(
                f"{SOUNDCLOUD_BASE}/playlists/{playlist_id}",
                headers=self._headers(),
                timeout=15,
            )
        if resp.status_code not in (200, 204, 404):
            raise requests.HTTPError(
                f"Delete playlist {playlist_id} failed: {resp.status_code} {resp.text[:200]}",
                response=resp,
            )

    def find_or_create_playlist(self, name: str, track_ids: list[int]) -> int:
        """Delete existing playlist by name and recreate it fresh (PUT accumulates tracks)."""
        existing = self.find_playlist(name)
        if existing:
            print(f"  [sc] Replacing pinned playlist '{name}'…")
            self.delete_playlist(existing)
        return self.create_playlist(name, track_ids)

    def create_unique_playlist(self, name: str, track_ids: list[int]) -> int:
        """Always create a new playlist, appending .1, .2 if name already exists."""
        candidate = name
        n = 1
        while self.find_playlist(candidate) is not None:
            candidate = f"{name}.{n}"
            n += 1
        return self.create_playlist(candidate, track_ids)
