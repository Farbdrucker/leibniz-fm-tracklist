#!/usr/bin/env python3
"""One-time interactive Spotify OAuth login.

Run this locally (never inside a container) to obtain a refresh token:

    SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... python scripts/spotify_login.py

Opens a browser for the Spotify consent screen, then prints the refresh
token to copy into your deployment's SPOTIFY_REFRESH_TOKEN secret (e.g. the
.env file used by docker-compose.yml).
"""

from __future__ import annotations

import base64
import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser

import requests

ACCOUNTS = os.environ.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")
REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"


def main() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        sys.exit("Please set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")

    state = secrets.token_urlsafe(16)
    params = {"client_id": client_id, "response_type": "code",
              "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state}
    auth_url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode(params)

    result: dict[str, str] = {}
    parsed = urllib.parse.urlparse(REDIRECT_URI)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in result and result.get("state") == state
            self.wfile.write(
                ("<h2>Done - you can close this window.</h2>" if ok
                 else "<h2>Failed. Go back to the terminal.</h2>").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    print("Opening a browser. If it doesn't open, visit this URL:\n", auth_url)
    webbrowser.open(auth_url)
    server.handle_request()
    server.server_close()

    if "code" not in result:
        sys.exit(f"No code received: {result}")
    if result.get("state") != state:
        sys.exit("State mismatch - aborting.")

    raw = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        f"{ACCOUNTS}/api/token",
        data={"grant_type": "authorization_code", "code": result["code"],
              "redirect_uri": REDIRECT_URI},
        headers={"Authorization": "Basic " + raw}, timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed: {resp.status_code} {resp.text[:300]}")

    refresh_token = resp.json()["refresh_token"]
    print("\nRefresh token obtained. Add this to your .env file:\n")
    print(f"SPOTIFY_REFRESH_TOKEN={refresh_token}\n")


if __name__ == "__main__":
    main()
