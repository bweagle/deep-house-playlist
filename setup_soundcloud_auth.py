#!/usr/bin/env python3
"""
Set up SoundCloud authentication using OAuth 2.0 with PKCE.

Requires a registered SoundCloud app:
  https://soundcloud.com/you/apps

Usage:
    python3 setup_soundcloud_auth.py
"""

import base64
import hashlib
import json
import os
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

TOKEN_FILE = "soundcloud_token.json"
REDIRECT_URI = "http://localhost:8080"

SOUNDCLOUD_AUTH_URL = "https://secure.soundcloud.com/authorize"
SOUNDCLOUD_TOKEN_URL = "https://api.soundcloud.com/oauth2/token"


def _pkce_pair() -> tuple[str, str]:
    """Generate a (code_verifier, code_challenge) pair for PKCE."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def main():
    print("SoundCloud authentication setup")
    print("You will need a registered SoundCloud app.")
    print("Register one at: https://soundcloud.com/you/apps\n")

    client_id = input("  Client ID: ").strip()
    if not client_id:
        print("Client ID is required.")
        return 1

    code_verifier, code_challenge = _pkce_pair()
    state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

    auth_url = (
        f"{SOUNDCLOUD_AUTH_URL}?"
        + urlencode({
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
    )

    print(f"\nOpening SoundCloud login in your browser…")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("After you authorize, your browser will redirect to a localhost URL.")
    print("The page will appear blank (nothing runs on localhost) — that's normal.")
    print("Copy the full URL from the address bar and paste it here:")
    url = input("  URL: ").strip()

    if url.startswith("http"):
        params = parse_qs(urlparse(url).query)
        if "code" not in params:
            print("No 'code' parameter found. Make sure you copied the full redirect URL.")
            return 1
        code = params["code"][0]
        returned_state = params.get("state", [None])[0]
        if returned_state and returned_state != state:
            print("State mismatch — possible CSRF. Please try again.")
            return 1
    else:
        code = url  # user pasted just the code

    print("\nExchanging code for token…")
    resp = requests.post(
        SOUNDCLOUD_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )

    if not resp.ok:
        print(f"  Token exchange failed: {resp.status_code} {resp.text}")
        return 1

    token_data = resp.json()
    token_data["client_id"] = client_id

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSuccess! Token saved to '{TOKEN_FILE}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
