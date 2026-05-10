#!/usr/bin/env python3
"""
Re-authenticate with Beatport and push the fresh token to GitHub.

Run this whenever the weekly CI run fails with Beatport 400 errors
(typically every ~7 days when the refresh token expires).

Usage:
    python3 reauth_beatport.py
"""

import json
import subprocess
import sys
import time

from setup_beatport_auth import _browser_auth, TOKEN_FILE
from modules.beatport_client import BEATPORT_CLIENT_ID

GITHUB_REPO = "bweagle/deep-house-playlist"
SECRET_NAME = "BEATPORT_TOKEN_JSON"


def main():
    print("=== Beatport re-auth ===\n")

    token_data = _browser_auth()
    if not token_data:
        print("\nAuthentication failed.")
        return 1

    token_data["client_id"] = BEATPORT_CLIENT_ID
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    print(f"\nToken saved to '{TOKEN_FILE}'.")

    print(f"Pushing to GitHub secret {SECRET_NAME}…")
    result = subprocess.run(
        ["gh", "secret", "set", SECRET_NAME,
         "--repo", GITHUB_REPO,
         "--body", json.dumps(token_data)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  Failed to update secret: {result.stderr.strip()}")
        print(f"  Update it manually: cat {TOKEN_FILE} | gh secret set {SECRET_NAME} --repo {GITHUB_REPO}")
        return 1

    print(f"  GitHub secret updated.")
    print("\nDone! You can now re-run the GitHub Action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
