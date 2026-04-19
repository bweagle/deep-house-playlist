#!/usr/bin/env python3
"""
Set up SoundCloud authentication.

Requires a registered SoundCloud app:
  https://soundcloud.com/you/apps

Usage:
    python3 setup_soundcloud_auth.py
"""

import json
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests

TOKEN_FILE = "soundcloud_token.json"
REDIRECT_URI = "http://localhost"

SOUNDCLOUD_AUTH_URL = "https://secure.soundcloud.com/authorize"
SOUNDCLOUD_TOKEN_URL = "https://api.soundcloud.com/oauth2/token"


def main():
    print("SoundCloud authentication setup")
    print("You will need a registered SoundCloud app.")
    print("Register one at: https://soundcloud.com/you/apps\n")

    client_id = input("  Client ID: ").strip()
    client_secret = input("  Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Client ID and secret are required.")
        return 1

    auth_url = (
        f"{SOUNDCLOUD_AUTH_URL}?"
        + urlencode({
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "non-expiring",
        })
    )

    print(f"\nOpening SoundCloud login in your browser…")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("After you authorize, your browser will redirect to a localhost URL.")
    print("Copy the full URL from the address bar and paste it here:")
    url = input("  URL: ").strip()

    if url.startswith("http"):
        params = parse_qs(urlparse(url).query)
        if "code" not in params:
            print("No 'code' parameter found. Make sure you copied the full redirect URL.")
            return 1
        code = params["code"][0]
    else:
        code = url  # user pasted just the code

    print("\nExchanging code for token…")
    resp = requests.post(
        SOUNDCLOUD_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
        timeout=15,
    )

    if not resp.ok:
        print(f"  Token exchange failed: {resp.status_code} {resp.text}")
        return 1

    token_data = resp.json()
    token_data["client_id"] = client_id
    token_data["client_secret"] = client_secret

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSuccess! Token saved to '{TOKEN_FILE}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
