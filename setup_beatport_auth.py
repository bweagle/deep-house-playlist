#!/usr/bin/env python3
"""
Set up Beatport authentication.

Tries two methods:
  1. Email + password  (fast, works if you have a Beatport-native password)
  2. Browser login     (works for all accounts: Google, Apple, or email)

Usage:
    python3 setup_beatport_auth.py
    python3 setup_beatport_auth.py --method browser
"""

import argparse
import getpass
import json
import time
import webbrowser
from urllib.parse import parse_qs, urlparse, urlencode

import requests

from modules.beatport_client import (
    BEATPORT_TOKEN_URL,
    BEATPORT_AUTH_URL,
    BEATPORT_CLIENT_ID,
)

TOKEN_FILE = "beatport_token.json"

# Redirect URI registered for this client (found in Beatport's bundled JS)
REDIRECT_URI = "https://api.beatport.com/v4/auth/o/post-message/"


# ---------------------------------------------------------------------------
# Method 1: password grant (no browser)
# ---------------------------------------------------------------------------

def _password_auth() -> dict | None:
    username = input("  Username: ").strip()
    password = getpass.getpass("  Password: ")

    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": BEATPORT_CLIENT_ID,
            "username": username,
            "password": password,
        },
        timeout=15,
    )
    if resp.ok:
        return resp.json()

    err = resp.json().get("error_description", resp.text)
    print(f"  Password login failed: {err}")
    return None


# ---------------------------------------------------------------------------
# Method 2: browser OAuth (handles Google/Apple/email login)
# ---------------------------------------------------------------------------

def _browser_auth() -> dict | None:
    auth_url = (
        f"{BEATPORT_AUTH_URL}?"
        + urlencode({
            "response_type": "code",
            "client_id": BEATPORT_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
        })
    )

    print(f"\nOpening Beatport login in your browser…")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("After you log in, your browser will land on a mostly-blank page.")
    print("The URL in the address bar will look like:")
    print(f"  {REDIRECT_URI}?code=XXXXXXXX&...")
    print("\nCopy the FULL URL from your browser's address bar and paste it here:")
    url = input("  URL: ").strip()

    # Accept either the full redirect URL or just the code itself
    if url.startswith("http"):
        params = parse_qs(urlparse(url).query)
        if "code" not in params:
            print("  No 'code' parameter found in that URL. Make sure you copied the full address bar URL.")
            return None
        code = params["code"][0]
    else:
        # User pasted just the code
        code = url

    print("\nExchanging code for token…")
    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": BEATPORT_CLIENT_ID,
        },
        timeout=15,
    )
    if resp.ok:
        return resp.json()

    print(f"  Token exchange failed: {resp.status_code} {resp.text}")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not BEATPORT_CLIENT_ID:
        print(
            "Error: BEATPORT_CLIENT_ID is not set.\n"
            "Open modules/beatport_client.py and follow the instructions\n"
            "in the comment above BEATPORT_CLIENT_ID to find and set your client_id."
        )
        return 1

    parser = argparse.ArgumentParser(description="Set up Beatport authentication")
    parser.add_argument(
        "--method",
        choices=["auto", "password", "browser"],
        default="browser",
        help="Login method (default: browser)",
    )
    args = parser.parse_args()

    token_data = None

    if args.method == "password":
        print("Beatport authentication setup")
        print("Enter your Beatport account credentials (username, not email):\n")
        token_data = _password_auth()

    if token_data is None and args.method in ("auto", "browser"):
        token_data = _browser_auth()

    if not token_data:
        print("\nAuthentication failed.")
        return 1

    token_data["client_id"] = BEATPORT_CLIENT_ID
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSuccess! Token saved to '{TOKEN_FILE}'.")
    print("\nTo push to GitHub (required for CI to work):")
    print(f"  gh secret set BEATPORT_TOKEN_JSON --repo bweagle/deep-house-playlist --body \"$(cat {TOKEN_FILE})\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
