from datetime import date, timedelta

import httpx

from sleep.auth import refresh_access_token

SLEEP_ENDPOINT = "https://api.fitbit.com/1.2/user/-/sleep/date"


def fetch_sleep_data(
    access_token: str, tokens: dict, days: int
) -> tuple[list[dict], dict | None]:
    """
    Fetch sleep data for the last N days.
    Returns (sleep_records, new_tokens_if_refreshed).
    """
    end = date.today()
    start = end - timedelta(days=days - 1)
    url = f"{SLEEP_ENDPOINT}/{start}/{end}.json"

    new_tokens = None
    response = _get_with_auth(url, access_token)

    if response.status_code == 401:
        new_tokens = refresh_access_token(tokens["refresh_token"])
        access_token = new_tokens["access_token"]
        response = _get_with_auth(url, access_token)

    response.raise_for_status()
    return response.json().get("sleep", []), new_tokens


def _get_with_auth(url: str, access_token: str) -> httpx.Response:
    return httpx.get(url, headers={"Authorization": f"Bearer {access_token}"})



