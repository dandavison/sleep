import json
import sys
from pathlib import Path

import typer

from sleep.auth import load_tokens, refresh_access_token, run_auth_flow
from sleep.fitbit import fetch_sleep_data

app = typer.Typer()

CONFIG_DIR = Path.home() / ".config" / "sleep"
TOKENS_FILE = CONFIG_DIR / "tokens.json"


@app.command()
def auth():
    """Authenticate with Fitbit (OAuth2 flow)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tokens = run_auth_flow()
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    typer.echo("Authentication successful. Tokens saved.")


@app.command()
def dump(days: int = 7):
    """Dump recent sleep data as JSON to stdout."""
    if not TOKENS_FILE.exists():
        typer.echo("Not authenticated. Run 'sleep auth' first.", err=True)
        raise typer.Exit(1)

    tokens = load_tokens(TOKENS_FILE)
    access_token = tokens.get("access_token")

    data, new_tokens = fetch_sleep_data(access_token, tokens, days)
    if new_tokens:
        TOKENS_FILE.write_text(json.dumps(new_tokens, indent=2))

    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    app()

