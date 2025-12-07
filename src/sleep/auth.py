import base64
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
REDIRECT_URI = "http://localhost:8080/"
SCOPES = "sleep activity"

CLIENT_ID_FILE = Path.home() / ".config" / "sleep" / "client.json"


def load_client_credentials() -> tuple[str, str]:
    """Load client_id and client_secret from config file."""
    if not CLIENT_ID_FILE.exists():
        raise RuntimeError(
            f"Missing {CLIENT_ID_FILE}. Create it with:\n"
            '{"client_id": "YOUR_ID", "client_secret": "YOUR_SECRET"}'
        )
    creds = json.loads(CLIENT_ID_FILE.read_text())
    return creds["client_id"], creds["client_secret"]


def load_tokens(tokens_file: Path) -> dict:
    return json.loads(tokens_file.read_text())


def run_auth_flow() -> dict:
    """Run the OAuth2 authorization code flow. Returns tokens dict."""
    client_id, client_secret = load_client_credentials()

    auth_url = (
        f"{AUTH_URL}?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
    )

    authorization_code = None

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal authorization_code
            query = parse_qs(urlparse(self.path).query)
            authorization_code = query.get("code", [None])[0]

            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Success! You can close this tab.</h1>")

        def log_message(self, format, *args):
            pass

    print(f"Opening browser for authorization...\n{auth_url}")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.handle_request()

    if not authorization_code:
        raise RuntimeError("No authorization code received")

    return exchange_code_for_tokens(client_id, client_secret, authorization_code)


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange authorization code for access and refresh tokens."""
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code,
        },
    )
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Use refresh token to get new access token."""
    client_id, client_secret = load_client_credentials()
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    response = httpx.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    response.raise_for_status()
    return response.json()



