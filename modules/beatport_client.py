"""
Beatport API v4 client.

Authentication uses OAuth 2.0 authorization_code flow.
Run `python3 setup_beatport_auth.py` once to generate beatport_token.json.

Token refresh is handled automatically on expiry.

Camelot key convention: Beatport returns "8A" / "8B" notation.
This client normalises to the m/d suffix used throughout this codebase
("8A" → "8m", "8B" → "8d") so all Camelot wheel logic stays consistent.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

BEATPORT_BASE = "https://api.beatport.com/v4"
BEATPORT_AUTH_URL = "https://api.beatport.com/v4/auth/o/authorize/"
BEATPORT_TOKEN_URL = "https://api.beatport.com/v4/auth/o/token/"
BEATPORT_DOCS_URL = "https://api.beatport.com/v4/docs/"

# Extracted from Beatport's bundled React app (API_CLIENT_ID constant)
BEATPORT_CLIENT_ID = "0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd"
# Registered redirect URI (found in bundled JS: redirectUri: '...')
BEATPORT_REDIRECT_URI = "https://api.beatport.com/v4/auth/o/post-message/"

# Beatport genre IDs
GENRE_DEEP_HOUSE = 12
GENRE_MELODIC_HOUSE_TECHNO = 90
GENRE_ORGANIC_HOUSE = 93
GENRE_AFRO_HOUSE = 89


# ---------------------------------------------------------------------------
# Camelot key normalisation
# ---------------------------------------------------------------------------

def beatport_camelot_to_local(camelot: str | None) -> str | None:
    """
    Convert Beatport Camelot notation to the m/d suffix used in this app.
      "8A"  (minor) → "8m"
      "8B"  (major) → "8d"
    Returns None if camelot is empty/None.
    """
    if not camelot:
        return None
    camelot = camelot.strip()
    if camelot.endswith("A"):
        return camelot[:-1] + "m"
    if camelot.endswith("B"):
        return camelot[:-1] + "d"
    return camelot  # already in some other format — pass through


# ---------------------------------------------------------------------------
# Client ID auto-discovery
# ---------------------------------------------------------------------------

def scrape_client_id() -> Optional[str]:
    """
    Try to find the public OAuth client_id from the Beatport docs page.

    The docs page is a React app — the client_id is in the minified JS bundles,
    not the HTML. We download each bundle and search for OAuth patterns.
    Returns None if scraping fails.
    """
    try:
        html_resp = requests.get(BEATPORT_DOCS_URL, timeout=15)
        html = html_resp.text

        # 1. Try OpenAPI spec endpoints directly first (fastest)
        for spec_path in ("/v4/schema/", "/v4/openapi.json", "/v4/swagger.json"):
            try:
                spec_resp = requests.get(
                    f"https://api.beatport.com{spec_path}",
                    timeout=8,
                    headers={"Accept": "application/json"},
                )
                if spec_resp.ok:
                    spec = spec_resp.json()
                    for scheme in spec.get("components", {}).get("securitySchemes", {}).values():
                        for flow in scheme.get("flows", {}).values():
                            for key in ("x-clientId", "clientId", "client_id"):
                                if key in flow:
                                    return flow[key]
            except Exception:
                continue

        # 2. Download JS bundles and search for OAuth client_id patterns
        js_urls = re.findall(r'src="(/static/[^"]+\.js)"', html)
        for js_path in js_urls:
            js_url = f"https://api.beatport.com{js_path}"
            try:
                js = requests.get(js_url, timeout=15).text
                # Look for clientId near OAuth-related strings
                # Minified JS often looks like: clientId:"XXXXXXXX" or "clientId","XXXXX"
                for pattern in [
                    r'clientId["\s]*:["\s]*["\']([A-Za-z0-9_\-]{4,64})["\']',
                    r'"client_id"[,\s]*"([A-Za-z0-9_\-]{4,64})"',
                    r'client_id=([A-Za-z0-9_\-]{4,64})[&"\s]',
                ]:
                    m = re.search(pattern, js)
                    if m:
                        candidate = m.group(1)
                        # Filter out common false positives
                        if candidate not in ("undefined", "null", "true", "false"):
                            return candidate
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# BeatportClient
# ---------------------------------------------------------------------------

class BeatportClient:
    """
    Thin wrapper around the Beatport API v4.
    Handles token loading, refresh, and the most-used endpoints.
    """

    def __init__(self, token_file: str | Path = "beatport_token.json"):
        self.token_file = Path(token_file)
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"Token file '{self.token_file}' not found. "
                "Run `python3 setup_beatport_auth.py` first."
            )
        with self.token_file.open() as f:
            self._token = json.load(f)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _save_token(self) -> None:
        if not os.environ.get("CI"):
            with self.token_file.open("w") as f:
                json.dump(self._token, f, indent=2)

    def _refresh_if_needed(self) -> None:
        expires_at = self._token.get("expires_at", 0)
        if time.time() < expires_at - 60:
            return  # still valid

        refresh_token = self._token.get("refresh_token")
        client_id = self._token.get("client_id")
        if not refresh_token or not client_id:
            raise RuntimeError(
                "Beatport token expired and no refresh token available. "
                "Re-run setup_beatport_auth.py."
            )
        resp = requests.post(
            BEATPORT_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=10,
        )
        resp.raise_for_status()
        new_data = resp.json()
        self._token["access_token"] = new_data["access_token"]
        self._token["expires_at"] = time.time() + new_data.get("expires_in", 3600)
        if "refresh_token" in new_data:
            self._token["refresh_token"] = new_data["refresh_token"]
        self._save_token()

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _get(self, path: str, **params) -> dict:
        self._refresh_if_needed()
        resp = requests.get(
            f"{BEATPORT_BASE}{path}",
            headers={"Authorization": f"Bearer {self._token['access_token']}"},
            params={k: v for k, v in params.items() if v is not None},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Track utilities
    # ------------------------------------------------------------------

    @staticmethod
    def parse_track(raw: dict) -> dict:
        """
        Normalise a raw Beatport track object to the flat dict used by the pipeline.
        """
        key_obj = raw.get("key") or {}
        genre_obj = raw.get("genre") or {}
        sub_genre_obj = raw.get("sub_genre") or {}
        release_obj = raw.get("release") or {}
        label_obj = release_obj.get("label") or {}
        artists = raw.get("artists") or []

        return {
            "beatport_id": raw.get("id"),
            "title": raw.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in artists),
            "artist_ids": [a.get("id") for a in artists if a.get("id")],
            "bpm": raw.get("bpm"),
            "camelot_key": beatport_camelot_to_local(key_obj.get("camelot")),
            "key_name": key_obj.get("name"),
            "genre": genre_obj.get("name", ""),
            "genre_id": genre_obj.get("id"),
            "sub_genre": sub_genre_obj.get("name", ""),
            "label": label_obj.get("name", ""),
            "release_date": raw.get("publish_date") or raw.get("release_date"),
            "duration": raw.get("length_ms"),
        }

    # ------------------------------------------------------------------
    # Catalog search
    # ------------------------------------------------------------------

    def search_track(self, title: str, artist: str = "") -> Optional[dict]:
        """
        Search Beatport for a track. Returns a parsed track dict or None.
        """
        query = f"{artist} {title}".strip() if artist else title
        try:
            data = self._get("/catalog/search/", q=query, type="tracks", per_page=5)
            # v4 search wraps results under the type key
            raw_list = (
                data.get("tracks", {}).get("data", [])
                or data.get("results", [])
            )
            if raw_list:
                return self.parse_track(raw_list[0])
        except requests.RequestException:
            pass
        return None

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------

    def get_genre_top_tracks(self, genre_id: int, per_page: int = 100) -> list[dict]:
        """Top tracks for a genre (replaces the website Top 100 chart)."""
        try:
            data = self._get(f"/catalog/genres/{genre_id}/top/", per_page=per_page)
            return [self.parse_track(t) for t in data.get("results", [])]
        except requests.RequestException as e:
            print(f"  [beatport] Genre {genre_id} top tracks error: {e}")
            return []

    def get_chart_tracks(self, chart_slug: str, per_page: int = 100) -> list[dict]:
        """Tracks from a named editorial chart (e.g. 'the-hype-deep-house')."""
        try:
            data = self._get(
                f"/catalog/charts/{chart_slug}/tracks/", per_page=per_page
            )
            return [self.parse_track(t) for t in data.get("results", [])]
        except requests.RequestException as e:
            print(f"  [beatport] Chart '{chart_slug}' error: {e}")
            return []

    # ------------------------------------------------------------------
    # Artist releases
    # ------------------------------------------------------------------

    def get_artist_recent_tracks(
        self, artist_id: int, days: int = 30, per_page: int = 25
    ) -> list[dict]:
        """New releases from an artist in the last N days."""
        from datetime import datetime, timedelta, timezone
        since = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%d")
        try:
            data = self._get(
                f"/catalog/artists/{artist_id}/tracks/",
                publish_date__gte=since,
                ordering="-publish_date",
                per_page=per_page,
            )
            return [self.parse_track(t) for t in data.get("results", [])]
        except requests.RequestException as e:
            print(f"  [beatport] Artist {artist_id} releases error: {e}")
            return []
