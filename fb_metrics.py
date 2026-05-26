"""
FB Page metrics puller, daily edition.

Designed for a piecemeal-token reality: pulls from however many Pages you
currently have tokens for, reports dead tokens loudly, keeps going.

Writes to a Google Sheet so Looker Studio always sees fresh data.

Environment variables expected (set as GitHub Actions secrets):
    FB_TOKENS_JSON       JSON string: {"page_id_or_label": "token", ...}
    GOOGLE_CREDS_JSON    Service account JSON (whole file as one string)
    SHEET_ID             Google Sheet ID (from the URL)
    PAGES_CONFIG_JSON    JSON list: [{"name": "...", "page_id": "..."}, ...]

Why split tokens from pages config: tokens rotate, the page list is stable.
Editing one secret is easier than editing a YAML and redeploying.
"""

import csv
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

# Bump when Meta deprecates. Check changelog:
# https://developers.facebook.com/docs/graph-api/changelog
API_VERSION = os.environ.get("FB_API_VERSION", "v23.0")
BASE_URL = "https://graph.facebook.com"

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))  # daily run = 1 day

RATE_LIMIT_THRESHOLD = 75
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 30

# Sheet tab names. Create these tabs manually in your Sheet first.
DATA_SHEET_TAB = "FB_raw"          # daily appended rows for Looker
HEALTH_SHEET_TAB = "Token_Health"  # latest token status snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP / RATE LIMIT
# ---------------------------------------------------------------------------

def _check_rate_limit(response, page_id):
    """Sleep if BUC header shows we're approaching limits."""
    header = response.headers.get("X-Business-Use-Case-Usage") or response.headers.get("X-App-Usage")
    if not header:
        return
    try:
        usage = json.loads(header)
    except json.JSONDecodeError:
        return

    metrics_list = []
    if isinstance(usage, dict):
        if page_id in usage:
            metrics_list = usage[page_id] if isinstance(usage[page_id], list) else [usage[page_id]]
        else:
            metrics_list = [usage]

    for metrics in metrics_list:
        if not isinstance(metrics, dict):
            continue
        max_pct = max(
            (metrics.get(k, 0) or 0) for k in ("call_count", "total_cputime", "total_time")
        )
        if max_pct >= RATE_LIMIT_THRESHOLD:
            sleep_for = 60 + (max_pct - RATE_LIMIT_THRESHOLD) * 2
            log.warning("Rate limit %s%% for %s, sleeping %ss", max_pct, page_id, sleep_for)
            time.sleep(sleep_for)


def graph_get(path, params, page_id=""):
    """GET with retry. Returns (data, error_message). error_message is None on success."""
    url = f"{BASE_URL}/{API_VERSION}/{path}"

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            log.warning("Network error: %s", e)
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue

        _check_rate_limit(response, page_id)

        if response.status_code == 200:
            return response.json(), None

        try:
            err = response.json().get("error", {})
            code = err.get("code")
            subcode = err.get("error_subcode")
            msg = err.get("message", "")
            err_type = err.get("type", "")
        except (ValueError, AttributeError):
            return None, f"HTTP {response.status_code}"

        # Token errors are NOT retryable, surface immediately so we can report
        # which Pages need their tokens refreshed.
        # 190 = invalid token, 102 = session expired, 463 = expired,
        # 467 = invalid access token, 200 = permissions error
        if code in (190, 102, 463, 467, 200):
            return None, f"TOKEN_DEAD: code={code} subcode={subcode} {msg}"

        retryable = code in (1, 2, 4, 17, 32, 613, 80004) or response.status_code in (429, 500, 502, 503, 504)
        if not retryable:
            return None, f"code={code} {msg}"

        time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))

    return None, "max_retries_exceeded"


def graph_get_paginated(path, params, page_id="", max_pages=50):
    results = []
    data, err = graph_get(path, params, page_id)
    if err:
        return results, err
    pages_walked = 0
    while data and "data" in data:
        results.extend(data["data"])
        pages_walked += 1
        next_url = data.get("paging", {}).get("next")
        if not next_url or pages_walked >= max_pages:
            break
        try:
            resp = requests.get(next_url, timeout=30)
            _check_rate_limit(resp, page_id)
            data = resp.json() if resp.status_code == 200 else None
        except (requests.RequestException, ValueError):
            break
    return results, None


# ---------------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------------

def check_token(page_id, token):
    """
    Quick sanity ping to see if the token works. Returns (ok, message).
    Run this for every Page before doing real work, so we get a clean
    health report instead of partial failures mid-run.
    """
    data, err = graph_get(page_id, {"fields": "name", "access_token": token}, page_id)
    if err:
        return False, err
    return True, data.get("name", "")


def fetch_page_basics(page_id, token):
    data, err = graph_get(
        page_id,
        {"fields": "name,followers_count,fan_count", "access_token": token},
        page_id,
    )
    if err:
        return None, err
    followers = data.get("followers_count")
    if followers is None:
        followers = data.get("fan_count")
    return {"name": data.get("name"), "followers_count": followers}, None


def fetch_posts_in_range(page_id, token, since_ts, until_ts):
    fields = (
        "id,created_time,"
        "attachments{media_type,type},"
        "shares,"
        "reactions.summary(total_count).limit(0),"
        "comments.summary(total_count).limit(0),"
        "insights.metric(post_video_views)"
    )
    params = {
        "fields": fields,
        "since": since_ts,
        "until": until_ts,
        "limit": 100,
        "access_token": token,
    }
    return graph_get_paginated(f"{page_id}/published_posts", params, page_id)


def fetch_reels_in_range(page_id, token, since_ts, until_ts):
    """
    /video_reels is the dedicated Reels endpoint. Fragile but accurate.
    If Meta deprecates this, the broader video count from fetch_posts_in_range
    is our fallback signal.
    """
    params = {"fields": "id,created_time", "limit": 100, "access_token": token}
    reels, err = graph_get_paginated(f"{page_id}/video_reels", params, page_id)
    if err:
        return [], err

    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    until_dt = datetime.fromtimestamp(until_ts, tz=timezone.utc)
    filtered = []
    for r in reels:
        ct = r.get("created_time")
        if not ct:
            continue
        try:
            r_dt = datetime.strptime(ct, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if since_dt <= r_dt <= until_dt:
            filtered.append(r)
    return filtered, None


# ---------------------------------------------------------------------------
# AGGREGATION
# ---------------------------------------------------------------------------

def aggregate(page_cfg, token, since_ts, until_ts):
    page_id = page_cfg["page_id"]
    label = page_cfg.get("name", page_id)
    log.info("Processing: %s", label)

    basics, err = fetch_page_basics(page_id, token)
    if err:
        return None, err

    posts, posts_err = fetch_posts_in_range(page_id, token, since_ts, until_ts)
    reels, reels_err = fetch_reels_in_range(page_id, token, since_ts, until_ts)

    # reels_err being TOKEN_DEAD here would have been caught earlier in
    # check_token. If /video_reels itself is broken (Meta change), we still
    # want to return the rest of the data, just with reels count as null.
    reels_endpoint_count = None if reels_err else len(reels)

    total_shares = total_reactions = total_comments = total_views = 0
    video_count = 0

    for post in posts or []:
        total_shares += (post.get("shares") or {}).get("count", 0) or 0
        total_reactions += (post.get("reactions", {}).get("summary") or {}).get("total_count", 0) or 0
        total_comments += (post.get("comments", {}).get("summary") or {}).get("total_count", 0) or 0

        for insight in (post.get("insights", {}).get("data") or []):
            if insight.get("name") == "post_video_views":
                vals = insight.get("values") or [{}]
                total_views += vals[0].get("value", 0) or 0

        atts = (post.get("attachments") or {}).get("data") or []
        for att in atts:
            mtype = (att.get("media_type") or "").lower()
            atype = (att.get("type") or "").lower()
            if "video" in mtype or "video" in atype:
                video_count += 1
                break

    return {
        "page_name": basics.get("name") or label,
        "page_id": page_id,
        "followers_count": basics.get("followers_count"),
        "total_posts": len(posts or []),
        "reels_posted_dedicated_endpoint": reels_endpoint_count,
        "reels_posted_video_filter": video_count,
        "total_views": total_views,
        "total_shares": total_shares,
        "total_reactions": total_reactions,
        "total_comments": total_comments,
    }, None


# ---------------------------------------------------------------------------
# GOOGLE SHEETS OUTPUT
# ---------------------------------------------------------------------------

DATA_COLUMNS = [
    "run_date", "period_start", "period_end", "page_name", "page_id",
    "followers_count", "total_posts",
    "reels_posted_dedicated_endpoint", "reels_posted_video_filter",
    "total_views", "total_shares", "total_reactions", "total_comments",
]


def get_sheets_service():
    creds_json = os.environ["GOOGLE_CREDS_JSON"]
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def append_to_sheet(service, sheet_id, tab_name, rows, columns):
    """
    Appends rows to a Sheet tab. Creates header if tab is empty.
    """
    # Check if header exists
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1:Z1",
    ).execute()
    existing = result.get("values", [])

    values_to_append = []
    if not existing:
        values_to_append.append(columns)

    for row in rows:
        values_to_append.append([row.get(col, "") for col in columns])

    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values_to_append},
    ).execute()


def overwrite_sheet(service, sheet_id, tab_name, rows, columns):
    """For the health tab: latest snapshot, not historical."""
    values = [columns]
    for row in rows:
        values.append([row.get(col, "") for col in columns])

    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A:Z",
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab_name}!A1",
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    # Load config from env (GitHub Actions secrets)
    try:
        tokens = json.loads(os.environ["FB_TOKENS_JSON"])
        pages = json.loads(os.environ["PAGES_CONFIG_JSON"])
        sheet_id = os.environ["SHEET_ID"]
    except KeyError as e:
        log.error("Missing env var: %s", e)
        sys.exit(1)
    except json.JSONDecodeError as e:
        log.error("Bad JSON in env: %s", e)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=LOOKBACK_DAYS)
    since_ts = int(since_dt.timestamp())
    until_ts = int(now.timestamp())
    run_date = now.strftime("%Y-%m-%d")
    period_start = since_dt.strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")

    log.info("Run: %s | period %s to %s | %s pages", run_date, period_start, period_end, len(pages))

    # Step 1: token health check for all Pages upfront
    health_rows = []
    valid_pages = []
    for page_cfg in pages:
        page_id = page_cfg["page_id"]
        label = page_cfg.get("name", page_id)
        token_key = page_cfg.get("token_key", page_id)
        token = tokens.get(token_key)

        if not token:
            health_rows.append({
                "page_name": label, "page_id": page_id, "status": "MISSING_TOKEN",
                "detail": f"No token found for key '{token_key}'", "checked_at": run_date,
            })
            continue

        ok, msg = check_token(page_id, token)
        if ok:
            health_rows.append({
                "page_name": label, "page_id": page_id, "status": "OK",
                "detail": msg, "checked_at": run_date,
            })
            valid_pages.append((page_cfg, token))
        else:
            health_rows.append({
                "page_name": label, "page_id": page_id, "status": "FAILED",
                "detail": msg, "checked_at": run_date,
            })

    ok_count = sum(1 for r in health_rows if r["status"] == "OK")
    dead = [r for r in health_rows if r["status"] != "OK"]
    log.info("Token health: %s OK, %s dead", ok_count, len(dead))
    for d in dead:
        log.warning("  DEAD: %s (%s) - %s", d["page_name"], d["page_id"], d["detail"])

    # Step 2: pull data for valid Pages
    data_rows = []
    for page_cfg, token in valid_pages:
        try:
            row, err = aggregate(page_cfg, token, since_ts, until_ts)
            if err:
                log.error("Failed: %s - %s", page_cfg.get("name"), err)
                continue
            row["run_date"] = run_date
            row["period_start"] = period_start
            row["period_end"] = period_end
            data_rows.append(row)
        except Exception as e:
            log.exception("Crash on %s: %s", page_cfg.get("name"), e)

    # Step 3: write to Sheets
    service = get_sheets_service()
    if data_rows:
        append_to_sheet(service, sheet_id, DATA_SHEET_TAB, data_rows, DATA_COLUMNS)
        log.info("Appended %s rows to %s", len(data_rows), DATA_SHEET_TAB)
    overwrite_sheet(
        service, sheet_id, HEALTH_SHEET_TAB, health_rows,
        ["page_name", "page_id", "status", "detail", "checked_at"],
    )
    log.info("Wrote health snapshot to %s", HEALTH_SHEET_TAB)

    # Exit non-zero if any tokens are dead, so GitHub Actions shows a red X
    # and you actually notice. Comment this out if you want it always green.
    if dead:
        log.warning("Run completed but %s tokens need attention", len(dead))
        sys.exit(2)


if __name__ == "__main__":
    main()
