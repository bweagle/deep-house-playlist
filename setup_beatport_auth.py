#!/usr/bin/env python3
"""
Set up Beatport OAuth 2.0 authentication.

Tries three methods in order:
  1. Fetch the OpenAPI spec to auto-detect the client_id, then browser OAuth
  2. Username + password (simpler, no browser needed)
  3. Manual: paste a token JSON you captured from your browser's Network tab

Usage:
    python3 setup_beatport_auth.py
    python3 setup_beatport_auth.py --method password
    python3 setup_beatport_auth.py --client-id KNOWN_ID
"""

import argparse
import json
import re
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

BEATPORT_BASE = "https://api.beatport.com/v4"
BEATPORT_TOKEN_URL = f"{BEATPORT_BASE}/auth/o/token/"
BEATPORT_AUTH_URL = f"{BEATPORT_BASE}/auth/o/authorize/"
REDIRECT_URI = "http://localhost:8765/callback"
TOKEN_FILE = "beatport_token.json"

# Candidate URLs where the OpenAPI spec might live
_SPEC_URLS = [
    f"{BEATPORT_BASE}/schema/",
    f"{BEATPORT_BASE}/openapi.json",
    f"{BEATPORT_BASE}/swagger.json",
    f"{BEATPORT_BASE}/docs/swagger.json",
]


# ---------------------------------------------------------------------------
# Client ID discovery
# ---------------------------------------------------------------------------

def find_client_id() -> str | None:
    """
    Try to find the public OAuth client_id from the Beatport API spec.
    Returns None if all attempts fail.
    """
    # 1. Try spec URLs directly
    for url in _SPEC_URLS:
        try:
            resp = requests.get(url, timeout=10, headers={"Accept": "application/json"})
            if resp.ok and resp.headers.get("content-type", "").startswith("application/"):
                spec = resp.json()
                cid = _extract_client_id_from_spec(spec)
                if cid:
                    print(f"  Found client_id in spec at {url}: {cid}")
                    return cid
        except Exception:
            continue

    # 2. Parse the docs HTML to find the spec URL, then fetch that
    try:
        html = requests.get(f"{BEATPORT_BASE}/docs/", timeout=10).text
        # SwaggerUI embeds the spec URL like: url: "/v4/schema/"
        spec_match = re.search(r'url\s*:\s*["\']([^"\']+schema[^"\']*)["\']', html)
        if not spec_match:
            spec_match = re.search(r'url\s*:\s*["\']([^"\']+openapi[^"\']*)["\']', html)
        if spec_match:
            spec_url = spec_match.group(1)
            if not spec_url.startswith("http"):
                spec_url = f"https://api.beatport.com{spec_url}"
            resp = requests.get(spec_url, timeout=10)
            if resp.ok:
                cid = _extract_client_id_from_spec(resp.json())
                if cid:
                    return cid

        # Also look for initOAuth config in the HTML
        init_match = re.search(
            r'initOAuth\s*\(\s*\{[^}]*["\']?clientId["\']?\s*:\s*["\']([^"\']+)["\']', html
        )
        if init_match:
            return init_match.group(1)

        # Generic clientId in HTML
        for pattern in [
            r'"clientId"\s*:\s*"([^"]{4,64})"',
            r"'clientId'\s*:\s*'([^']{4,64})'",
            r'client_id["\s:=]+([A-Za-z0-9_\-]{4,64})',
        ]:
            m = re.search(pattern, html)
            if m:
                return m.group(1)
    except Exception:
        pass

    return None


def _extract_client_id_from_spec(spec: dict) -> str | None:
    """Pull client_id out of an OpenAPI 3.x security scheme."""
    schemes = spec.get("components", {}).get("securitySchemes", {})
    for scheme in schemes.values():
        if scheme.get("type") == "oauth2":
            for flow in scheme.get("flows", {}).values():
                for key in ("x-clientId", "clientId", "x-client-id", "client_id"):
                    if key in flow:
                        return flow[key]
    return None


# ---------------------------------------------------------------------------
# Method 1: browser OAuth (authorization_code)
# ---------------------------------------------------------------------------

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
            body = b"<h2>Auth failed. Check your terminal.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _browser_oauth(client_id: str) -> dict | None:
    state = secrets.token_urlsafe(16)
    auth_url = (
        f"{BEATPORT_AUTH_URL}?"
        + urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
        })
    )
    print(f"\nOpening Beatport login in your browser…")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("Waiting for callback on http://localhost:8765/callback  (2 min timeout)…")
    server = HTTPServer(("localhost", 8765), _CallbackHandler)
    server.timeout = 1
    deadline = time.time() + 120
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.auth_code:
            server.server_close()
            break
        if _CallbackHandler.error:
            server.server_close()
            print(f"OAuth error: {_CallbackHandler.error}")
            return None
    else:
        server.server_close()
        print("Timed out waiting for callback.")
        return None

    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": _CallbackHandler.auth_code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"Token exchange failed: {resp.status_code}\n{resp.text}")
        return None
    return resp.json()


# ---------------------------------------------------------------------------
# Method 2: username + password (ROPC grant)
# ---------------------------------------------------------------------------

def _password_auth(client_id: str) -> dict | None:
    import getpass
    print("\nEnter your Beatport account credentials:")
    username = input("  Email: ").strip()
    password = getpass.getpass("  Password: ")

    resp = requests.post(
        BEATPORT_TOKEN_URL,
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": client_id,
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"Login failed: {resp.status_code}\n{resp.text}")
        return None
    return resp.json()


# ---------------------------------------------------------------------------
# Method 3: manual paste
# ---------------------------------------------------------------------------

def _manual_paste() -> dict | None:
    print(
        "\nManual token capture:\n"
        "  1. Open https://api.beatport.com/v4/docs/ in your browser\n"
        "  2. Open DevTools → Network tab, then reload the page\n"
        "  3. Log in via the Authorize button if visible\n"
        "  4. Find the POST request to 'token' — copy its full JSON response\n"
        "  5. Paste it below, then press Enter twice:\n"
    )
    lines = []
    while True:
        line = input()
        if not line and lines:
            break
        lines.append(line)
    raw = "\n".join(lines).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Set up Beatport OAuth")
    parser.add_argument("--client-id", default=None)
    parser.add_argument(
        "--method",
        choices=["auto", "browser", "password", "manual"],
        default="auto",
        help="Auth method (default: auto — tries browser then password)",
    )
    parser.add_argument("--out", default=TOKEN_FILE)
    args = parser.parse_args()

    # Step 1: get client_id
    client_id = args.client_id
    if not client_id and args.method != "manual":
        print("Auto-detecting Beatport client ID from API spec…")
        client_id = find_client_id()
        if client_id:
            print(f"  client_id: {client_id}")
        else:
            print(
                "  Could not auto-detect.\n"
                "\n"
                "  To find it manually:\n"
                "    1. Go to https://api.beatport.com/v4/docs/ in your browser\n"
                "    2. Open DevTools → right-click the page → View Page Source\n"
                "    3. Cmd+F (search) for 'clientId' or 'client_id'\n"
                "    4. Copy the value (usually 8-20 characters)\n"
            )
            client_id = input("  Paste client_id here (or press Enter to skip to manual mode): ").strip()
            if not client_id:
                args.method = "manual"

    # Step 2: get token
    token_data = None

    if args.method in ("auto", "browser") and client_id:
        token_data = _browser_oauth(client_id)

    if token_data is None and args.method in ("auto", "password") and client_id:
        print("\nTrying username/password (ROPC) flow…")
        token_data = _password_auth(client_id)

    if token_data is None:
        print("\nFalling back to manual token paste…")
        token_data = _manual_paste()

    if not token_data:
        print("Authentication failed — no token obtained.")
        return 1

    # Step 3: save
    if client_id:
        token_data["client_id"] = client_id
    token_data["expires_at"] = time.time() + token_data.get("expires_in", 3600)

    with open(args.out, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nBeatport auth complete! Token saved to '{args.out}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
