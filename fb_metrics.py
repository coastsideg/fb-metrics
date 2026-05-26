"""
FB Page metrics puller, auto-discovery edition.

Uses one long-lived User token to fetch all Pages you have admin access
to via /me/accounts, then pulls metrics for each. Filters out excluded
Pages and (optionally) restricts to specific FB Page categories.

Env vars (GitHub Actions secrets):
    FB_USER_TOKEN        Single long-lived User token (refresh every ~60 days)
    GOOGLE_CREDS_JSON    Service account JSON
    SHEET_ID             Google Sheet ID
    EXCLUDED_PAGE_IDS    JSON list of Page IDs to skip, e.g. ["123", "456"]
    ALLOWED_CATEGORIES   (optional) JSON list of FB category names to include,
                         e.g. ["Politician", "Political party"]. If unset or
                         empty, all categories are included.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

API_VERSION = os.environ.get("FB_API_VERSION", "v23.0")
BASE_URL = "https://graph.facebook.com"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "1"))

RATE_LIMIT_THRESHOLD = 75
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 30

DATA_SHEET_TAB = "FB_raw"
HEALTH_SHEET_TAB = "Token_Health"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _check_rate_limit(response, page_id):
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
        max_pct = max((metrics.get(k, 0) or 0) for k in ("call_count", "total_cputime", "total_time"))
        if max_pct >= RATE_LIMIT_THRESHOLD:
            sleep_for = 60 + (max_pct - RATE_LIMIT_THRESHOLD) * 2
            log.warning("Rate limit %s%% for %s, sleeping %ss", max_pct, page_id, sleep_for)
            time.sleep(sleep_for)


def graph_get(path, params, page_id=""):
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
        except (ValueError, AttributeError):
            return None, f"HTTP {response.status_code}"
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
# PAGE DISCOVERY
# ---------------------------------------------------------------------------

def discover_pages(user_token, excluded_ids, allowed_categories):
    """
    Calls /me/accounts to get every Page the User token has admin access to.
    Returns list of dicts: [{"name", "id", "access_token", "category"}, ...]
    Filtered by excluded_ids and (optionally) allowed_categories.
    """
    params = {
        "fields": "name,id,access_token,category",
        "limit": 100,
        "access_token": user_token,
    }
    pages, err = graph_get_paginated("me/accounts", params, "me/accounts")
    if err:
        log.error("Could not fetch /me/accounts: %s", err)
        return [], err

    excluded_set = set(excluded_ids or [])
    allowed_set = set(allowed_categories or [])

    filtered = []
    for p in pages:
        if p.get("id") in excluded_set:
            log.info("Skipping (excluded): %s (%s)", p.get("name"), p.get("id"))
            continue
        if allowed_set and p.get("category") not in allowed_set:
            log.info("Skipping (category=%s): %s", p.get("category"), p.get("name"))
            continue
        filtered.append(p)

    log.info("Discovered %s Pages, %s after filtering", len(pages), len(filtered))
    return filtered, None


# ---------------------------------------------------------------------------
# DATA FETCHING (unchanged from previous version)
# ---------------------------------------------------------------------------

def fetch_page_basics(page_id, token):
    data, err = graph_get(page_id, {"fields": "name,followers_count,fan_count", "access_token": token}, page_id)
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


def aggregate(page_info, since_ts, until_ts):
    page_id = page_info["id"]
    token = page_info["access_token"]
    label = page_info.get("name", page_id)
    log.info("Processing: %s", label)

    basics, err = fetch_page_basics(page_id, token)
    if err:
        return None, err

    posts, _ = fetch_posts_in_range(page_id, token, since_ts, until_ts)
    reels, reels_err = fetch_reels_in_range(page_id, token, since_ts, until_ts)
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
# SHEETS
# ---------------------------------------------------------------------------

DATA_COLUMNS = [
    "run_date", "period_start", "period_end", "page_name", "page_id",
    "followers_count", "total_posts",
    "reels_posted_dedicated_endpoint", "reels_posted_video_filter",
    "total_views", "total_shares", "total_reactions", "total_comments",
]


def get_sheets_service():
    creds_info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    creds = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def append_to_sheet(service, sheet_id, tab_name, rows, columns):
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{tab_name}!A1:Z1",
    ).execute()
    existing = result.get("values", [])
    values_to_append = []
    if not existing:
        values_to_append.append(columns)
    for row in rows:
        values_to_append.append([row.get(col, "") for col in columns])
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"{tab_name}!A1",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": values_to_append},
    ).execute()


def overwrite_sheet(service, sheet_id, tab_name, rows, columns):
    values = [columns]
    for row in rows:
        values.append([row.get(col, "") for col in columns])
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"{tab_name}!A:Z",
    ).execute()
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab_name}!A1",
        valueInputOption="RAW", body={"values": values},
    ).execute()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    try:
        user_token = os.environ["FB_USER_TOKEN"]
        sheet_id = os.environ["SHEET_ID"]
    except KeyError as e:
        log.error("Missing required env var: %s", e)
        sys.exit(1)

    # Optional filters
    excluded_raw = os.environ.get("EXCLUDED_PAGE_IDS", "[]")
    allowed_raw = os.environ.get("ALLOWED_CATEGORIES", "[]")
    try:
        excluded_ids = json.loads(excluded_raw)
        allowed_categories = json.loads(allowed_raw)
    except json.JSONDecodeError as e:
        log.error("Bad JSON in EXCLUDED_PAGE_IDS or ALLOWED_CATEGORIES: %s", e)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=LOOKBACK_DAYS)
    since_ts = int(since_dt.timestamp())
    until_ts = int(now.timestamp())
    run_date = now.strftime("%Y-%m-%d")
    period_start = since_dt.strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")

    log.info("Run: %s | period %s to %s", run_date, period_start, period_end)

    # Step 1: discover Pages via User token
    pages, discovery_err = discover_pages(user_token, excluded_ids, allowed_categories)
    health_rows = []

    if discovery_err:
        # User token itself is dead. Write that to health sheet and bail.
        health_rows.append({
            "page_name": "USER_TOKEN", "page_id": "-", "status": "FAILED",
            "detail": discovery_err, "checked_at": run_date,
        })
        service = get_sheets_service()
        overwrite_sheet(service, sheet_id, HEALTH_SHEET_TAB, health_rows,
                        ["page_name", "page_id", "status", "detail", "checked_at"])
        log.error("User token failed. Regenerate it. Aborting.")
        sys.exit(2)

    health_rows.append({
        "page_name": "USER_TOKEN", "page_id": "-", "status": "OK",
        "detail": f"Discovered {len(pages)} Pages", "checked_at": run_date,
    })

    # Step 2: pull metrics for each Page
    data_rows = []
    for page_info in pages:
        try:
            row, err = aggregate(page_info, since_ts, until_ts)
            if err:
                health_rows.append({
                    "page_name": page_info.get("name"), "page_id": page_info.get("id"),
                    "status": "FAILED", "detail": err, "checked_at": run_date,
                })
                continue
            row["run_date"] = run_date
            row["period_start"] = period_start
            row["period_end"] = period_end
            data_rows.append(row)
            health_rows.append({
                "page_name": page_info.get("name"), "page_id": page_info.get("id"),
                "status": "OK", "detail": "", "checked_at": run_date,
            })
        except Exception as e:
            log.exception("Crash on %s: %s", page_info.get("name"), e)
            health_rows.append({
                "page_name": page_info.get("name"), "page_id": page_info.get("id"),
                "status": "CRASHED", "detail": str(e), "checked_at": run_date,
            })

    # Step 3: write to Sheets
    service = get_sheets_service()
    if data_rows:
        append_to_sheet(service, sheet_id, DATA_SHEET_TAB, data_rows, DATA_COLUMNS)
        log.info("Appended %s rows to %s", len(data_rows), DATA_SHEET_TAB)
    overwrite_sheet(service, sheet_id, HEALTH_SHEET_TAB, health_rows,
                    ["page_name", "page_id", "status", "detail", "checked_at"])

    failed = [r for r in health_rows if r["status"] != "OK"]
    if failed:
        log.warning("%s issues, see Token_Health tab", len(failed))
        sys.exit(2)


if __name__ == "__main__":
    main()
