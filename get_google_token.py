#!/usr/bin/env python3
"""One-time Google Health sign-in: opens your browser to Google's consent page,
catches the redirect on a local port, and writes tokens/fitbit.token in the
format Fitbit_Fetch.py reads. Run it once before `docker compose up -d`.

Reads GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET from the environment or ./.env.
Standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

PORT = 8080
SCOPES = " ".join([
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.settings.readonly",
])
TOKEN_FILE = Path("tokens/fitbit.token")


def env(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    for line in Path(".env").read_text().splitlines() if Path(".env").exists() else []:
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} not set — put it in .env (see .env.example)")


def main() -> None:
    client_id, client_secret = env("GOOGLE_CLIENT_ID"), env("GOOGLE_CLIENT_SECRET")

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    consent = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": f"http://localhost:{PORT}",
        "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })

    code_box: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = "You can close this tab and return to the terminal."
            if "code" in qs:
                code_box["code"] = qs["code"][0]
            else:
                body = f"Authorization failed: {qs.get('error', ['unknown'])[0]}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())
            done.set()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("Opening Google's consent page in your browser…")
    print("(If your OAuth consent screen is unverified, click Advanced → continue.)")
    webbrowser.open(consent)
    if not done.wait(timeout=300):
        raise SystemExit("Timed out waiting for the browser redirect.")
    server.shutdown()
    if "code" not in code_box:
        raise SystemExit("Authorization was not completed.")

    form = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "code": code_box["code"], "code_verifier": verifier,
        "grant_type": "authorization_code", "redirect_uri": f"http://localhost:{PORT}",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", form) as r:
        tokens = json.load(r)
    if "refresh_token" not in tokens:
        raise SystemExit("Google did not return a refresh token — revoke access at "
                         "myaccount.google.com/permissions and retry.")

    TOKEN_FILE.parent.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({
        "provider": "google",
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_in": tokens.get("expires_in", 3600),
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
    }))
    print(f"Saved {TOKEN_FILE}. Now run: docker compose up -d")


if __name__ == "__main__":
    main()
