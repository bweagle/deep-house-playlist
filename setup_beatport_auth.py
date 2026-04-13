#!/usr/bin/env python3
"""
Set up Beatport OAuth 2.0 authentication.

Opens a browser so you can log in to Beatport, captures the callback,
and saves your access + refresh tokens to beatport_token.json.

Usage:
    python3 setup_beatport_auth.py
    python3 setup_beatport_auth.py --client-id YOUR_CLIENT_ID
"""

import argparse
import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from modules.beatport_client import (
    BEATPORT_AUTH_URL,
    BEATPORT_TOKEN_URL,
    scrape_client_id,
)

REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_FILE = "beatport_token.json"


class _CallbackHandler(BaseHTTPRequestHandler):
    auth_code = None
    error = None

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            body = b"<h2>Beatport auth complete! You can close this tab.</h2>"
        else:
            _CallbackHandler.error = params.get("error", ["unknown"])[0]
            body = b"<h2>Auth error. Check your terminal.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silence request logs


def _run_callback_server(timeout: int = 120) -> str:
    """Start a local server and wait for the OAuth callback. Returns the auth code."""
    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    server.timeout = 1
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.auth_code:
            server.server_close()
            return _CallbackHandler.auth_code
        if _CallbackHandler.error:
            server.server_close()
            raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
    server.server_close()
    raise TimeoutError("Timed out waiting for Beatport OAuth callback.")


def main():
    parser = argparse.ArgumentParser(description="Set up Beatport OAuth")
    parser.add_argument(
        "--client-id",
        default=None,
        help="Beatport OAuth client ID (auto-detected from docs if omitted)",
    )
    parser.add_argument(
        "--out",
        default=TOKEN_FILE,
        help=f"Output token file (default: {TOKEN_FILE})",
    )
    args = parser.parse_args()

    # 1. Get client_id
    client_id = args.client_id
    if not client_id:
        print("Auto-detecting Beatport client ID from docs page…")
        client_id = scrape_client_id()
        if client_id:
            print(f"  Found client ID: {client_id}")
        else:
            client_id = input(
                "Could not auto-detect client ID.\n"
                "Open https://api.beatport.com/v4/docs/ in your browser,\n"
                "click 'Authorize', and copy the client_id shown there.\n"
                "Paste it here: "
            ).strip()

    # 2. Build auth URL with PKCE state
    state = secrets.token_urlsafe(16)
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    auth_url = f"{BEATPORT_AUTH_URL}?{urlencode(auth_params)}"

    print(f"\nOpening Beatport login in your browser…")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    # 3. Wait for callback
    print("Waiting for OAuth callback on http://localhost:8765/callback …")
    try:
        code = _run_callback_server(timeout=120)
    except (TimeoutError, RuntimeError) as e:
        print(f"Error: {e}")
        return 1

    # 4. Exchange code for token
    print("Exchanging authorization code for tokens…")
    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"Token exchange failed: {resp.status_code} {resp.text}")
        return 1

    token_data = resp.json()
    token_data["client_id"] = client_id
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

    # 5. Save
    with open(args.out, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nBeatport auth complete! Token saved to '{args.out}'.")
    print("Add 'beatport_token.json' to your .gitignore if it isn't already.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
