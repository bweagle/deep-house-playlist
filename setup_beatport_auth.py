#!/usr/bin/env python3
"""
Set up Beatport authentication.

Uses your Beatport account email + password to get an access token.
No browser, no OAuth redirect dance needed.

Usage:
    python3 setup_beatport_auth.py
"""

import getpass
import json
import time

import requests

from modules.beatport_client import BEATPORT_TOKEN_URL, BEATPORT_CLIENT_ID

TOKEN_FILE = "beatport_token.json"


def main():
    print("Beatport authentication setup")
    print("Enter your Beatport account credentials:\n")

    username = input("  Email: ").strip()
    password = getpass.getpass("  Password: ")

    print("\nRequesting token…")
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

    if not resp.ok:
        try:
            err = resp.json()
            msg = err.get("error_description") or err.get("error") or resp.text
        except Exception:
            msg = resp.text
        print(f"\nAuthentication failed: {msg}")
        return 1

    token_data = resp.json()
    token_data["client_id"] = BEATPORT_CLIENT_ID
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nSuccess! Token saved to '{TOKEN_FILE}'.")
    print(f"  Access token expires in {token_data.get('expires_in', 3600) // 60} minutes.")
    print(f"  Refresh token will keep it alive automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
