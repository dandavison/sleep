import http.server
import json
import os
import sys
from pathlib import Path

import typer
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build

from sleep.auth import load_tokens, run_auth_flow
from sleep.fitbit import fetch_activities, fetch_sleep_data

app = typer.Typer()

CONFIG_DIR = Path.home() / ".config" / "sleep"
TOKENS_FILE = CONFIG_DIR / "tokens.json"
GOOGLE_CREDS_FILE = CONFIG_DIR / "google-credentials.json"
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DOCS_DIR = PROJECT_ROOT / "docs"

SPREADSHEET_ID = "1hC-UoXQNH-Ra_Qqny3mN0Xc24uDNGAbzKJotCGbYt3k"


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
    """Fetch sleep, activity, and sheet data, save to data/."""
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

    # Google Sheet (subjective data)
    if GOOGLE_CREDS_FILE.exists():
        sheet_data = fetch_sheet_data()
        (DATA_DIR / "subjective.json").write_text(json.dumps(sheet_data, indent=2))
        typer.echo(f"Saved {len(sheet_data)} subjective records")
    else:
        typer.echo("Skipping sheet (no Google credentials)")


@app.command()
def runs():
    """Dump inferred runs as JSON to stdout."""
    activities_file = DATA_DIR / "activities.json"
    if not activities_file.exists():
        typer.echo("No data. Run 'sleep sync' first.", err=True)
        raise typer.Exit(1)

    activities = json.loads(activities_file.read_text())
    runs_list = extract_runs(activities)
    json.dump(runs_list, sys.stdout, indent=2)
    sys.stdout.write("\n")


@app.command()
def sheet():
    """Dump Google Sheet as JSON to stdout."""
    if not GOOGLE_CREDS_FILE.exists():
        typer.echo(f"Missing {GOOGLE_CREDS_FILE}", err=True)
        raise typer.Exit(1)

    records = fetch_sheet_data()
    json.dump(records, sys.stdout, indent=2)
    sys.stdout.write("\n")


def fetch_sheet_data() -> list[dict]:
    """Fetch Google Sheet data as list of dicts (header row becomes keys)."""
    creds = service_account.Credentials.from_service_account_file(
        str(GOOGLE_CREDS_FILE),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = google_build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="A:Z"
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return []
    headers = rows[0]
    return [dict(zip(headers, row)) for row in rows[1:]]


@app.command()
def build():
    """Transform sleep data for visualization and write to docs/data.json."""
    sleep_file = DATA_DIR / "sleep.json"
    activities_file = DATA_DIR / "activities.json"
    subjective_file = DATA_DIR / "subjective.json"

    if not sleep_file.exists():
        typer.echo("No data. Run 'sleep sync' first.", err=True)
        raise typer.Exit(1)

    sleep_raw = json.loads(sleep_file.read_text())

    # Group by date, keeping main sleep and naps separate
    by_date: dict[str, dict] = {}
    for record in sleep_raw:
        date = record["dateOfSleep"]
        if date not in by_date:
            by_date[date] = {"main": None, "naps": []}
        if record.get("isMainSleep"):
            by_date[date]["main"] = record
        else:
            by_date[date]["naps"].append(record)

    # Transform main sleep and merge naps
    chart_data = []
    for date, group in by_date.items():
        if not group["main"]:
            continue
        entry = transform_for_chart(group["main"])
        # Add naps - mark their segments
        for nap in group["naps"]:
            nap_data = transform_for_chart(nap)
            entry["deep"] += nap_data["deep"]
            entry["light"] += nap_data["light"]
            entry["rem"] += nap_data["rem"]
            entry["wake"] += nap_data["wake"]
            for seg in nap_data["segments"]:
                seg["isNap"] = True
            entry["segments"].extend(nap_data["segments"])
        entry["segments"].sort(key=lambda s: s["dateTime"])
        chart_data.append(entry)

    chart_data.sort(key=lambda x: x["date"])

    # Merge subjective data by date
    if subjective_file.exists():
        subjective = json.loads(subjective_file.read_text())
        subj_by_date = {row["date"]: parse_subjective(row) for row in subjective if row.get("date")}
        for record in chart_data:
            record["subjective"] = subj_by_date.get(record["date"])

    # Merge activity and run data by date
    if activities_file.exists():
        activities = json.loads(activities_file.read_text())
        activities_by_date = build_activities_by_date(activities)
        all_runs = extract_runs(activities)
        runs_by_date = {}
        for run in all_runs:
            runs_by_date.setdefault(run["date"], []).append(run)

        for record in chart_data:
            record["activities"] = activities_by_date.get(record["date"], [])
            record["runs"] = runs_by_date.get(record["date"], [])

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

    # Handle "stages" vs "classic" tracking
    # Stages: deep, light, rem, wake
    # Classic: asleep, awake, restless (naps use this)
    if "deep" in summary:
        deep = summary.get("deep", {}).get("minutes", 0)
        light = summary.get("light", {}).get("minutes", 0)
        rem = summary.get("rem", {}).get("minutes", 0)
        wake = summary.get("wake", {}).get("minutes", 0)
    else:
        # Classic tracking - map asleep to light, awake+restless to wake
        deep = 0
        light = summary.get("asleep", {}).get("minutes", 0)
        rem = 0
        wake = (summary.get("awake", {}).get("minutes", 0) +
                summary.get("restless", {}).get("minutes", 0))
        # Normalize segment levels for classic tracking
        for seg in segments:
            if seg.get("level") == "asleep":
                seg["level"] = "light"
            elif seg.get("level") in ("awake", "restless"):
                seg["level"] = "wake"

    return {
        "date": record["dateOfSleep"],
        "deep": deep,
        "light": light,
        "rem": rem,
        "wake": wake,
        "efficiency": record.get("efficiency", 0),
        "startTime": record.get("startTime"),
        "endTime": record.get("endTime"),
        "segments": segments,
    }


def process_activity(act: dict) -> dict | None:
    """Extract key fields from a raw activity. Returns None if invalid."""
    start = act.get("startTime", "")
    if not start:
        return None
    duration_min = act.get("activeDuration", 0) / 1000 / 60
    distance_km = act.get("distance", 0)
    return {
        "name": act.get("activityName"),
        "date": start[:10],
        "startTime": start,
        "duration": round(duration_min),
        "distance": round(distance_km, 2),
        "speed": round(distance_km / (duration_min / 60), 1) if duration_min > 0 else 0,
    }


def is_run(activity: dict) -> bool:
    """Heuristic: it's a run if speed > 8 km/h."""
    return activity.get("speed", 0) > 8


def extract_runs(activities: list[dict]) -> list[dict]:
    """Process raw activities and return only runs."""
    processed = [process_activity(a) for a in activities]
    return [a for a in processed if a and is_run(a)]


def build_activities_by_date(activities: list[dict]) -> dict[str, list[dict]]:
    """Group processed activities by date."""
    by_date = {}
    for act in activities:
        processed = process_activity(act)
        if processed:
            by_date.setdefault(processed["date"], []).append(processed)
    return by_date


def parse_subjective(row: dict) -> dict:
    """Parse subjective data row. Extract score from 'data' field like 'c9' -> 9."""
    import re
    data = row.get("data", "")
    exclude = "x" in data.lower()
    clean = data.replace("x", "").replace("X", "")
    match = re.search(r"(\d+)", clean)
    score = int(match.group(1)) if match else None
    code = re.sub(r"\d+", "", clean) or None
    return {"code": code, "score": score, "raw": data, "exclude": exclude}


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

