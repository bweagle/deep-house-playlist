"""
One-time setup: authenticate with the YouTube Data API v3.

Steps:
  1. Go to https://console.cloud.google.com
  2. Select your project
  3. APIs & Services → Credentials → find your OAuth client
  4. Make sure Application type is "Desktop app" (not TV/Limited Input)
  5. Download the JSON file for that client (click the download icon)
  6. Run: python3 setup_auth.py --secrets path/to/downloaded_client_secret.json

A browser window will open for sign-in. Credentials are saved to oauth.json
(gitignored — never commit it).
"""

import argparse
import json

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube"]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up YouTube Data API OAuth credentials.")
    parser.add_argument(
        "--secrets",
        required=True,
        help="Path to the client_secret JSON downloaded from Google Cloud Console",
    )
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(args.secrets, SCOPES)
    credentials = flow.run_local_server(port=0)

    # Save credentials to oauth.json
    token_data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes),
    }
    with open("oauth.json", "w") as f:
        json.dump(token_data, f, indent=2)

    print("\nDone! Credentials saved to oauth.json.")
    print("You can now run:")
    print("  python3 main.py --csv songs.csv --playlist 'My Playlist'")
