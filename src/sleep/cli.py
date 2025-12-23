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
UI_DIR = PROJECT_ROOT / "ui"

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
    """Transform sleep data for visualization and write to ui/data.json."""
    sleep_file = DATA_DIR / "sleep.json"
    fixups_file = DATA_DIR / "fixups.json"
    activities_file = DATA_DIR / "activities.json"
    subjective_file = DATA_DIR / "subjective.json"

    if not sleep_file.exists():
        typer.echo("No data. Run 'sleep sync' first.", err=True)
        raise typer.Exit(1)

    sleep_raw = json.loads(sleep_file.read_text())

    # Merge manual fixups if present
    if fixups_file.exists():
        fixups = json.loads(fixups_file.read_text())
        sleep_raw.extend(fixups)
        typer.echo(f"Merged {len(fixups)} fixup records")

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

    # Transform main sleep and merge naps/fixups
    chart_data = []
    for date, group in by_date.items():
        if not group["main"]:
            continue
        entry = transform_for_chart(group["main"])
        main_start = entry["startTime"]

        # Add naps/fixups - distinguish between true naps and pre-sleep fixups
        for nap in group["naps"]:
            nap_data = transform_for_chart(nap)
            entry["deep"] += nap_data["deep"]
            entry["light"] += nap_data["light"]
            entry["rem"] += nap_data["rem"]
            entry["wake"] += nap_data["wake"]

            # If this record ends at or before main sleep starts, it's a fixup
            # extending the sleep backward, not a true nap
            is_presleep_fixup = nap_data["endTime"] <= main_start

            if is_presleep_fixup:
                # Update the entry's start time to the earlier fixup start
                if nap_data["startTime"] < entry["startTime"]:
                    entry["startTime"] = nap_data["startTime"]
            else:
                # True nap - mark segments so they render separately
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

    ui_dir = PROJECT_ROOT / "ui"
    ui_dir.mkdir(exist_ok=True)
    out_file = ui_dir / "data.json"
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

    # Exclude terminal awake (final wake segment not followed by more sleep)
    terminal_awake = 0
    if segments and segments[-1].get("level") == "wake":
        terminal_awake = segments[-1].get("seconds", 0) / 60

    return {
        "date": record["dateOfSleep"],
        "deep": deep,
        "light": light,
        "rem": rem,
        "wake": max(0, wake - terminal_awake),
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


def generate_fixup_segments(
    reference_record: dict,
    start_time: str,
    end_time: str,
    comment: str = "",
) -> dict:
    """
    Generate a realistic fixup sleep record based on a reference night's data.

    This is used when the Fitbit fails to capture part of a sleep session.
    The fixup uses the stage proportions and segment duration distributions
    from the reference record to generate plausible synthetic segments.

    Args:
        reference_record: A Fitbit sleep record to use as the statistical basis
        start_time: ISO format start time for the fixup (e.g. "2025-12-20T00:00:00.000")
        end_time: ISO format end time for the fixup
        comment: Optional explanation for why this fixup was created

    Returns:
        A sleep record dict in Fitbit format with isMainSleep=False and logType="manual_fixup"
    """
    import random
    from datetime import datetime, timedelta

    # Parse times
    start_dt = datetime.fromisoformat(start_time.replace(".000", ""))
    end_dt = datetime.fromisoformat(end_time.replace(".000", ""))
    total_seconds = int((end_dt - start_dt).total_seconds())

    # Extract stage proportions from reference record
    summary = reference_record.get("levels", {}).get("summary", {})
    stage_minutes = {
        "deep": summary.get("deep", {}).get("minutes", 0),
        "light": summary.get("light", {}).get("minutes", 0),
        "rem": summary.get("rem", {}).get("minutes", 0),
        "wake": summary.get("wake", {}).get("minutes", 0),
    }
    total_minutes = sum(stage_minutes.values()) or 1
    stage_proportions = {k: v / total_minutes for k, v in stage_minutes.items()}

    # Extract segment durations from reference for realistic length distribution
    ref_segments = reference_record.get("levels", {}).get("data", [])
    durations_by_stage = {stage: [] for stage in ["deep", "light", "rem", "wake"]}
    for seg in ref_segments:
        level = seg.get("level")
        if level in durations_by_stage:
            durations_by_stage[level].append(seg.get("seconds", 60))

    # Ensure we have some durations for each stage (fallback to defaults)
    defaults = {"deep": [300, 600, 900], "light": [180, 360, 600], "rem": [300, 600], "wake": [60, 180, 300]}
    for stage in durations_by_stage:
        if not durations_by_stage[stage]:
            durations_by_stage[stage] = defaults[stage]

    # Generate segments
    segments = []
    current_dt = start_dt
    remaining_seconds = total_seconds

    # Typically start with wake then light (falling asleep pattern)
    stages_order = ["wake", "light", "deep", "light", "rem", "light"]  # Initial cycle hint

    while remaining_seconds > 30:
        # Pick stage weighted by proportions, with some temporal bias
        # (more deep early, more REM later in sleep)
        elapsed_ratio = 1 - (remaining_seconds / total_seconds)

        weights = stage_proportions.copy()
        # Bias: more deep in first half, more REM in second half
        if elapsed_ratio < 0.5:
            weights["deep"] *= 1.5
            weights["rem"] *= 0.5
        else:
            weights["deep"] *= 0.5
            weights["rem"] *= 1.5

        # Don't have too much wake
        weights["wake"] *= 0.3

        stages = list(weights.keys())
        stage_weights = [weights[s] for s in stages]
        total_weight = sum(stage_weights)
        stage_weights = [w / total_weight for w in stage_weights]

        stage = random.choices(stages, weights=stage_weights, k=1)[0]

        # Pick duration from reference distribution
        duration = random.choice(durations_by_stage[stage])
        duration = min(duration, remaining_seconds)
        duration = max(30, duration)  # Minimum 30 seconds

        segments.append({
            "dateTime": current_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "level": stage,
            "seconds": duration,
        })

        current_dt += timedelta(seconds=duration)
        remaining_seconds -= duration

    # End with a wake segment
    if segments and segments[-1]["level"] != "wake":
        wake_duration = min(180, max(30, remaining_seconds))
        segments.append({
            "dateTime": current_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "level": "wake",
            "seconds": wake_duration,
        })

    # Calculate summary from generated segments
    generated_summary = {"deep": 0, "light": 0, "rem": 0, "wake": 0}
    for seg in segments:
        generated_summary[seg["level"]] += seg["seconds"] // 60

    # Build the fixup record
    date_of_sleep = end_dt.strftime("%Y-%m-%d")
    if end_dt.hour < 12:  # If ends in morning, date is that day
        date_of_sleep = end_dt.strftime("%Y-%m-%d")
    else:  # If ends in afternoon/evening, might be next day
        date_of_sleep = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d") if end_dt.hour >= 18 else end_dt.strftime("%Y-%m-%d")

    return {
        "dateOfSleep": date_of_sleep,
        "duration": total_seconds * 1000,
        "efficiency": reference_record.get("efficiency", 85),
        "startTime": start_time,
        "endTime": end_time,
        "infoCode": 0,
        "isMainSleep": False,
        "levels": {
            "data": segments,
            "shortData": [],
            "summary": {
                "deep": {"count": sum(1 for s in segments if s["level"] == "deep"), "minutes": generated_summary["deep"], "thirtyDayAvgMinutes": 0},
                "light": {"count": sum(1 for s in segments if s["level"] == "light"), "minutes": generated_summary["light"], "thirtyDayAvgMinutes": 0},
                "rem": {"count": sum(1 for s in segments if s["level"] == "rem"), "minutes": generated_summary["rem"], "thirtyDayAvgMinutes": 0},
                "wake": {"count": sum(1 for s in segments if s["level"] == "wake"), "minutes": generated_summary["wake"], "thirtyDayAvgMinutes": 0},
            },
        },
        "logId": None,
        "minutesAfterWakeup": 0,
        "minutesAwake": generated_summary["wake"],
        "minutesAsleep": generated_summary["deep"] + generated_summary["light"] + generated_summary["rem"],
        "minutesToFallAsleep": 0,
        "logType": "manual_fixup",
        "timeInBed": total_seconds // 60,
        "type": "stages",
        "comment": comment,
    }


@app.command()
def fixup(
    date: str = typer.Argument(..., help="Date of sleep in YYYY-MM-DD format"),
    start: str = typer.Argument(..., help="Start time in HH:MM format (24h)"),
    end: str = typer.Argument(..., help="End time in HH:MM format (24h)"),
    comment: str = typer.Option("", help="Optional comment explaining the fixup"),
):
    """
    Generate a fixup sleep record for when Fitbit missed part of a sleep session.

    Segments are generated randomly using stage proportions and duration distributions
    from that night's actual recorded sleep data, making them statistically realistic.

    Example: sleep fixup 2025-12-20 00:00 03:15 --comment "Watch missed early sleep"
    """
    from datetime import datetime, timedelta

    sleep_file = DATA_DIR / "sleep.json"
    fixups_file = DATA_DIR / "fixups.json"

    if not sleep_file.exists():
        typer.echo("No data. Run 'sleep sync' first.", err=True)
        raise typer.Exit(1)

    sleep_raw = json.loads(sleep_file.read_text())

    # Find the reference record for this date
    reference = None
    for record in sleep_raw:
        if record.get("dateOfSleep") == date and record.get("isMainSleep"):
            reference = record
            break

    if not reference:
        typer.echo(f"No main sleep record found for {date}", err=True)
        raise typer.Exit(1)

    # Parse start/end times - figure out the actual dates
    # Times are relative to a sleep session: evening times (>=18:00) are the night before,
    # early morning times (<12:00) are on the sleep date itself
    start_hour = int(start.split(":")[0])
    end_hour = int(end.split(":")[0])

    sleep_date = datetime.strptime(date, "%Y-%m-%d")

    # Determine start date
    if start_hour >= 18:
        start_date = sleep_date - timedelta(days=1)
    else:
        start_date = sleep_date

    # Determine end date - same logic, but also handle end being after start
    if end_hour >= 18:
        end_date = sleep_date - timedelta(days=1)
    else:
        end_date = sleep_date

    # Sanity check: if end appears before start (e.g., start=23:00, end=02:00), end is next day
    start_time_obj = datetime.strptime(f"{start_date.strftime('%Y-%m-%d')} {start}", "%Y-%m-%d %H:%M")
    end_time_obj = datetime.strptime(f"{end_date.strftime('%Y-%m-%d')} {end}", "%Y-%m-%d %H:%M")
    if end_time_obj <= start_time_obj:
        end_date += timedelta(days=1)

    start_time = f"{start_date.strftime('%Y-%m-%d')}T{start}:00.000"
    end_time = f"{end_date.strftime('%Y-%m-%d')}T{end}:00.000"

    # Generate the fixup
    fixup_record = generate_fixup_segments(
        reference,
        start_time,
        end_time,
        comment or f"Fitbit missed sleep before {end}",
    )

    # Load existing fixups or start fresh
    if fixups_file.exists():
        fixups = json.loads(fixups_file.read_text())
    else:
        fixups = []

    # Remove any existing fixup for same date/time range
    fixups = [f for f in fixups if not (f.get("dateOfSleep") == date and f.get("startTime") == start_time)]

    fixups.append(fixup_record)
    fixups_file.write_text(json.dumps(fixups, indent=2))

    total_mins = fixup_record["timeInBed"]
    summary = fixup_record["levels"]["summary"]
    typer.echo(f"Created fixup for {date}: {start} - {end} ({total_mins} min)")
    typer.echo(f"  Deep: {summary['deep']['minutes']}m, Light: {summary['light']['minutes']}m, "
               f"REM: {summary['rem']['minutes']}m, Wake: {summary['wake']['minutes']}m")
    typer.echo(f"Saved to {fixups_file}")


@app.command()
def serve(port: int = 8000):
    """Serve ui/ locally for development."""
    os.chdir(UI_DIR)
    handler = http.server.SimpleHTTPRequestHandler
    with http.server.HTTPServer(("", port), handler) as httpd:
        typer.echo(f"Serving at http://localhost:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    app()

