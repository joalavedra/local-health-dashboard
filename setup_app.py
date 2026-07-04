#!/usr/bin/env python3
"""Setup & sync page for the health stack (http://localhost:8000).

One place to: connect a Google account (OAuth consent in the browser, PKCE),
see whether the token and data are healthy, and trigger a re-sync or a 28-day
backfill (picked up by the fetcher within ~30 s via tokens/sync-request.json).

Standard library only; runs in the same image as the fetcher.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("SETUP_PORT") or 8000)
CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID") or ""
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET") or ""
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE_PATH") or "tokens/fitbit.token")
INFLUX = os.environ.get("INFLUX_URL") or "http://localhost:8086"
GRAFANA_URL = os.environ.get("GRAFANA_URL") or "http://localhost:3000"
REDIRECT = f"http://localhost:{PORT}/callback"
SCOPES = " ".join([
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.settings.readonly",
])

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
pkce: dict[str, str] = {}


def token_status() -> tuple[str, str]:
    """(state, detail) where state is one of connected / expired / missing."""
    if not TOKEN_FILE.exists():
        return "missing", "No Google account connected yet."
    try:
        saved = json.loads(TOKEN_FILE.read_text())
        form = urllib.parse.urlencode({
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token", "refresh_token": saved.get("refresh_token", ""),
        }).encode()
        urllib.request.urlopen("https://oauth2.googleapis.com/token", form, timeout=10)
        return "connected", f"Token healthy (saved {saved.get('saved_at_utc', 'unknown')[:16]})."
    except Exception:
        return "expired", ("Refresh token rejected — reconnect below. If this keeps happening "
                           "every 7 days, publish your OAuth consent screen to production.")


def last_data_point() -> str:
    try:
        q = urllib.parse.urlencode({"db": "FitbitHealthStats",
                                    "q": 'SELECT last("value") FROM "HeartRate_Intraday"'})
        with urllib.request.urlopen(f"{INFLUX}/query?{q}", timeout=10) as r:
            series = json.load(r)["results"][0].get("series")
        if not series:
            return "no data yet"
        ts = series[0]["values"][0][0]
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 3600
        return f"{ts[:16]} UTC ({age_h / 24:.1f} days ago)" if age_h > 48 else f"{ts[:16]} UTC ({age_h:.1f} h ago)"
    except Exception as e:
        return f"InfluxDB unreachable ({e})"


def page(message: str = "") -> bytes:
    state, detail = token_status()
    badge = {"connected": ("#2e7d32", "Connected"), "expired": ("#c62828", "Token expired"),
             "missing": ("#616161", "Not connected")}[state]
    connect_label = "Connect Google Health" if state == "missing" else "Reconnect Google Health"
    pending = (TOKEN_FILE.parent / "sync-request.json").exists()
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Health stack — setup</title>
<style>
 body {{ background:#111217; color:#d8d9da; font:15px -apple-system,system-ui,sans-serif;
        max-width:640px; margin:8vh auto; padding:0 20px; }}
 .card {{ background:#181b1f; border:1px solid #2c3235; border-radius:10px; padding:24px; margin-bottom:16px; }}
 .badge {{ display:inline-block; padding:3px 10px; border-radius:999px; color:#fff;
          background:{badge[0]}; font-size:13px; }}
 a.btn, button {{ display:inline-block; background:#3d71d9; color:#fff; border:0; border-radius:6px;
          padding:10px 16px; font-size:15px; text-decoration:none; cursor:pointer; margin:4px 8px 4px 0; }}
 button.secondary {{ background:#2c3235; }}
 .muted {{ color:#8e8e8e; font-size:13px; }}
 h1 {{ font-size:20px; }}
</style></head><body>
<h1>Health stack — setup &amp; sync</h1>
{f'<div class="card" style="border-color:#2e7d32">{html.escape(message)}</div>' if message else ''}
<div class="card">
  <p><span class="badge">{badge[1]}</span></p>
  <p>{html.escape(detail)}</p>
  <p class="muted">Last data point in InfluxDB: {html.escape(last_data_point())}</p>
  <a class="btn" href="/connect">{connect_label}</a>
</div>
<div class="card">
  <p>Ask the fetcher for fresh data (picked up within ~30&nbsp;s; watch progress in
     <code>docker compose logs -f fitbit-fetch-data</code>):</p>
  <form method="post" action="/sync" style="display:inline"><input type="hidden" name="days" value="2">
    <button {"disabled" if pending else ""}>Sync last 2 days</button></form>
  <form method="post" action="/sync" style="display:inline"><input type="hidden" name="days" value="28">
    <button class="secondary" {"disabled" if pending else ""}>Backfill 28 days</button></form>
  {'<p class="muted">A sync request is already queued.</p>' if pending else ''}
  <p class="muted">Longer backfills: see “Historical backfill” in the README.</p>
</div>
<div class="card"><a class="btn secondary" style="background:#2c3235" href="{GRAFANA_URL}">Open the dashboard →</a></div>
</body></html>"""
    return body.encode()


class Handler(BaseHTTPRequestHandler):
    def _respond(self, code: int, body: bytes = b"", location: str | None = None) -> None:
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self._respond(200, page(qs.get("msg", [""])[0]))
        elif url.path == "/connect":
            if not CLIENT_ID or not CLIENT_SECRET:
                self._respond(200, page("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set — "
                                        "fill in .env and restart the stack."))
                return
            verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
            pkce["verifier"] = verifier
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            self._respond(302, location="https://accounts.google.com/o/oauth2/v2/auth?"
                          + urllib.parse.urlencode({
                              "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
                              "response_type": "code", "scope": SCOPES,
                              "access_type": "offline", "prompt": "consent",
                              "code_challenge": challenge, "code_challenge_method": "S256"}))
        elif url.path == "/callback":
            code = qs.get("code", [None])[0]
            if not code or "verifier" not in pkce:
                self._respond(302, location="/?msg=" + urllib.parse.quote(
                    "Authorization was not completed — try again."))
                return
            form = urllib.parse.urlencode({
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "code": code,
                "code_verifier": pkce.pop("verifier"), "grant_type": "authorization_code",
                "redirect_uri": REDIRECT}).encode()
            try:
                with urllib.request.urlopen("https://oauth2.googleapis.com/token", form,
                                            timeout=15) as r:
                    tokens = json.load(r)
                TOKEN_FILE.parent.mkdir(exist_ok=True)
                TOKEN_FILE.write_text(json.dumps({
                    "provider": "google", "access_token": tokens["access_token"],
                    "refresh_token": tokens["refresh_token"],
                    "expires_in": tokens.get("expires_in", 3600),
                    "saved_at_utc": datetime.now(timezone.utc).isoformat()}))
                (TOKEN_FILE.parent / "sync-request.json").write_text(json.dumps({"days": 28}))
                self._respond(302, location="/?msg=" + urllib.parse.quote(
                    "Connected! A 28-day backfill is queued — data lands over the next minutes."))
            except KeyError:
                self._respond(302, location="/?msg=" + urllib.parse.quote(
                    "Google returned no refresh token — revoke access at "
                    "myaccount.google.com/permissions and reconnect."))
            except Exception as e:
                self._respond(302, location="/?msg=" + urllib.parse.quote(f"Token exchange failed: {e}"))
        else:
            self._respond(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        if urllib.parse.urlparse(self.path).path == "/sync":
            length = int(self.headers.get("Content-Length") or 0)
            fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
            days = max(1, min(int(fields.get("days", ["2"])[0] or 2), 28))
            TOKEN_FILE.parent.mkdir(exist_ok=True)
            (TOKEN_FILE.parent / "sync-request.json").write_text(json.dumps({"days": days}))
            self._respond(302, location="/?msg=" + urllib.parse.quote(
                f"Sync request queued ({days} days). The fetcher picks it up within ~30 s."))
        else:
            self._respond(404, b"not found")

    def log_message(self, fmt: str, *args: object) -> None:
        logging.info("%s " + fmt, self.address_string(), *args)


if __name__ == "__main__":
    logging.info("Setup page on http://localhost:%s (token file: %s)", PORT, TOKEN_FILE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
