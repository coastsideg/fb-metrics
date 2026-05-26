# FB Metrics, daily auto-pull

Runs on GitHub Actions every day at 6am Perth. Writes to a Google Sheet that Looker reads. You only ever touch it when a token dies.

## What you build once

### 1. The Google Sheet

Create a Sheet. Add two tabs:
- `FB_raw` (where the daily data appends)
- `Token_Health` (latest snapshot of which tokens work)

Grab the Sheet ID from the URL: `docs.google.com/spreadsheets/d/THIS_PART_HERE/edit`

### 2. Service account for Sheets access

1. Go to https://console.cloud.google.com, create a project (or use existing)
2. Enable Google Sheets API
3. Create a service account, download the JSON key
4. Open the Sheet, click Share, paste the service account email (looks like `something@project.iam.gserviceaccount.com`), give Editor access

### 3. The GitHub repo

1. Create a private repo (tokens are sensitive, do not make this public)
2. Drop these files in:
   - `fb_metrics.py`
   - `requirements.txt`
   - `.github/workflows/daily.yml`
3. Go to repo Settings > Secrets and variables > Actions > New repository secret

Add these four secrets:

**`SHEET_ID`** = the Sheet ID from step 1

**`GOOGLE_CREDS_JSON`** = paste the entire contents of the service account JSON file

**`PAGES_CONFIG_JSON`** = JSON list of your Pages, like:
```json
[
  {"name": "MP Alice Smith", "page_id": "1234567890", "token_key": "alice"},
  {"name": "MP Bob Jones", "page_id": "9876543210", "token_key": "bob"}
]
```

`token_key` is a label, it links a Page to its token in the next secret. Use whatever you want, just keep it consistent.

**`FB_TOKENS_JSON`** = JSON dict mapping token_key to token:
```json
{
  "alice": "EAAxxx...",
  "bob": "EAAyyy..."
}
```

Why split them? When Alice's token dies, you edit `FB_TOKENS_JSON` and change one value. You never touch the Pages config. When you onboard a new MP, you add to both.

### 4. Get long-lived Page tokens (one-time per Page)

The short-lived tokens you get from Graph API Explorer die in 1-2 hours. You want the long-lived version.

For each Page:
1. In Graph API Explorer, pick the Page from the dropdown, generate a User Access Token with permissions: `pages_show_list`, `pages_read_engagement`, `read_insights`
2. Exchange it for a long-lived token:
   ```
   GET https://graph.facebook.com/v23.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id=YOUR_APP_ID
     &client_secret=YOUR_APP_SECRET
     &fb_exchange_token=SHORT_LIVED_USER_TOKEN
   ```
3. With the long-lived User token, call `/me/accounts` to get long-lived Page tokens (these don't expire)
4. Paste that Page token into `FB_TOKENS_JSON` under the right key

If the MP changes admin permissions or removes you, the token dies. That's unavoidable.

### 5. Connect Looker Studio

Add the Sheet as a data source in Looker. Point your existing `For_Looker` formulas at the `FB_raw` tab (or rebuild them in Looker). Your PERCENTRANK scoring keeps working the same.

## What happens daily

1. Action runs at 6am Perth
2. Script pings every token first, builds a health report
3. Pulls metrics from every working token
4. Appends data rows to `FB_raw`
5. Overwrites `Token_Health` with the latest status
6. If any tokens are dead, the Action shows a red X so you notice

## When a token dies

You'll see it two ways:
- Red X on the Actions tab in GitHub
- A row in `Token_Health` saying `FAILED` with the reason

To fix:
1. Re-do step 4 above for that one Page
2. Edit the `FB_TOKENS_JSON` secret, replace the one value
3. Next run picks it up. No code changes, no redeployment.

## Known issues

- **Daily run = 1-day lookback.** If Action fails to run one day (GitHub outage etc.), you have a gap. Either bump `LOOKBACK_DAYS` and dedupe in Sheets, or accept the gap. Looker handles missing dates fine.
- **Reels endpoint** can be deprecated by Meta at any time. Script keeps running, just leaves that column null. Fallback `reels_posted_video_filter` column still works.
- **API version** in the workflow file. Bump it every 12 months or when calls start failing.
- **GitHub Actions free tier** = 2000 minutes/month for private repos. Your daily run takes maybe 1-2 minutes. You'll use ~60 minutes/month. Fine.
- **Service account JSON** in secrets. If your GitHub account is compromised, rotate the service account in Google Cloud Console.

## What's NOT here yet

Instagram. Build that as a separate Action with a separate script using the IG Graph API (`/{ig-user-id}/media`). Same pattern. Tell me when you want it.
