import http.server
import json
import os
import sys
from pathlib import Path

import typer

from sleep.auth import load_tokens, run_auth_flow
from sleep.fitbit import fetch_activities, fetch_sleep_data

app = typer.Typer()

CONFIG_DIR = Path.home() / ".config" / "sleep"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"


def get_sleep_data(days: int) -> tuple[list, dict | None]:
    """Fetch sleep data, return (data, new_tokens_if_refreshed)."""
    if not TOKENS_FILE.exists():
        typer.echo("Not authenticated. Run 'sleep auth' first.", err=True)
        raise typer.Exit(1)
    tokens = load_tokens(TOKENS_FILE)
    return fetch_sleep_data(tokens["access_token"], tokens, days)


def save_tokens_if_refreshed(new_tokens: dict | None):
    if new_tokens:
        TOKENS_FILE.write_text(json.dumps(new_tokens, indent=2))


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
    data, new_tokens = get_sleep_data(days)
    save_tokens_if_refreshed(new_tokens)
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


@app.command()
def sync(days: int = 30):
    """Fetch sleep and activity data, save to data/."""
    if not TOKENS_FILE.exists():
        typer.echo("Not authenticated. Run 'sleep auth' first.", err=True)
        raise typer.Exit(1)

    tokens = load_tokens(TOKENS_FILE)
    DATA_DIR.mkdir(exist_ok=True)

    # Sleep
    sleep_data, new_tokens = fetch_sleep_data(tokens["access_token"], tokens, days)
    if new_tokens:
        tokens = new_tokens
        TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    (DATA_DIR / "sleep.json").write_text(json.dumps(sleep_data, indent=2))
    typer.echo(f"Saved {len(sleep_data)} sleep records")

    # Activities
    activities, new_tokens = fetch_activities(tokens["access_token"], tokens, days)
    save_tokens_if_refreshed(new_tokens)
    (DATA_DIR / "activities.json").write_text(json.dumps(activities, indent=2))
    typer.echo(f"Saved {len(activities)} activities")


@app.command()
def build():
    """Transform sleep data for visualization and write to docs/data.json."""
    sleep_file = DATA_DIR / "sleep.json"
    activities_file = DATA_DIR / "activities.json"

    if not sleep_file.exists():
        typer.echo("No data. Run 'sleep sync' first.", err=True)
        raise typer.Exit(1)

    sleep_raw = json.loads(sleep_file.read_text())
    chart_data = [transform_for_chart(record) for record in sleep_raw if record.get("isMainSleep")]
    chart_data.sort(key=lambda x: x["date"])

    # Merge activity data by date
    if activities_file.exists():
        activities = json.loads(activities_file.read_text())
        activities_by_date = build_activities_by_date(activities)
        for record in chart_data:
            record["activities"] = activities_by_date.get(record["date"], [])

    docs_dir = PROJECT_ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    out_file = docs_dir / "data.json"
    out_file.write_text(json.dumps(chart_data, indent=2))
    typer.echo(f"Wrote {len(chart_data)} records to {out_file}")


def transform_for_chart(record: dict) -> dict:
    """Extract chart-relevant fields from a Fitbit sleep record."""
    summary = record.get("levels", {}).get("summary", {})
    levels = record.get("levels", {})
    segments = levels.get("data", []) + levels.get("shortData", [])
    segments.sort(key=lambda s: s["dateTime"])
    return {
        "date": record["dateOfSleep"],
        "deep": summary.get("deep", {}).get("minutes", 0),
        "light": summary.get("light", {}).get("minutes", 0),
        "rem": summary.get("rem", {}).get("minutes", 0),
        "wake": summary.get("wake", {}).get("minutes", 0),
        "efficiency": record.get("efficiency", 0),
        "startTime": record.get("startTime"),
        "endTime": record.get("endTime"),
        "segments": segments,
    }


def build_activities_by_date(activities: list[dict]) -> dict[str, list[dict]]:
    """Group activities by date, extracting key fields."""
    by_date = {}
    for act in activities:
        start = act.get("startTime", "")
        if not start:
            continue
        # Extract date from ISO timestamp like "2025-11-30T17:21:08.323-05:00"
        activity_date = start[:10]
        entry = {
            "name": act.get("activityName"),
            "duration": act.get("activeDuration", 0) // 1000 // 60,  # ms -> minutes
            "distance": act.get("distance", 0),  # km
            "calories": act.get("calories", 0),
        }
        by_date.setdefault(activity_date, []).append(entry)
    return by_date


@app.command()
def serve(port: int = 8000):
    """Serve docs/ locally for development."""
    os.chdir(DOCS_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with http.server.HTTPServer(("", port), handler) as httpd:
        typer.echo(f"Serving at http://localhost:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    app()

