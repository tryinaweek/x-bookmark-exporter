"""Cron job: auto-post scheduled tweets.
Runs every 15 minutes via Vercel Cron.
"""
import os
import json
from datetime import datetime, timezone

import requests as req_lib
from flask import Flask

app = Flask(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
} if SUPABASE_KEY else {}


@app.route("/api/cron")
def run_cron():
    if not SUPABASE_URL:
        return {"status": "no supabase"}, 200

    now = datetime.now(timezone.utc).isoformat()

    # Get all scheduled drafts that are due
    r = req_lib.get(
        f"{SUPABASE_URL}/rest/v1/drafts?status=eq.scheduled&scheduled_at=lte.{now}&select=id,user_id,tweets",
        headers=SB_HEADERS,
    )
    if r.status_code != 200:
        return {"status": "db error", "detail": r.text}, 200

    due_drafts = r.json()
    if not due_drafts:
        return {"status": "nothing due", "checked_at": now}, 200

    results = []
    for draft in due_drafts:
        draft_id = draft["id"]
        user_id = draft["user_id"]
        tweets = draft.get("tweets", [])
        if isinstance(tweets, str):
            tweets = json.loads(tweets)

        # Get user's access token
        ur = req_lib.get(
            f"{SUPABASE_URL}/rest/v1/users?id=eq.{user_id}&select=access_token",
            headers=SB_HEADERS,
        )
        if ur.status_code != 200 or not ur.json():
            results.append({"draft_id": draft_id, "status": "no user token"})
            continue

        token = ur.json()[0].get("access_token", "")
        if not token:
            results.append({"draft_id": draft_id, "status": "empty token"})
            continue

        # Post tweets
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        posted, reply_to, failed = [], None, False
        for tweet_text in tweets:
            body = {"text": tweet_text}
            if reply_to:
                body["reply"] = {"in_reply_to_tweet_id": reply_to}
            tr = req_lib.post("https://api.twitter.com/2/tweets", headers=headers, json=body)
            data = tr.json()
            if "data" in data:
                reply_to = data["data"]["id"]
                posted.append(data["data"]["id"])
            else:
                results.append({"draft_id": draft_id, "status": "post_failed", "error": str(data)})
                failed = True
                break

        if not failed and posted:
            # Mark as posted
            req_lib.patch(
                f"{SUPABASE_URL}/rest/v1/drafts?id=eq.{draft_id}",
                headers=SB_HEADERS,
                json={"status": "posted", "posted_at": now},
            )
            results.append({"draft_id": draft_id, "status": "posted", "tweet_ids": posted})

    return {"status": "ok", "processed": len(due_drafts), "results": results}, 200
