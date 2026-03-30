import os
import json
import secrets
import hashlib
import base64
import urllib.parse
import io
from datetime import datetime, timezone

import requests
import anthropic
from flask import Flask, render_template, request, redirect, session, send_file
from supabase import create_client

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
SCOPES = "bookmark.read tweet.read tweet.write users.read offline.access"

# Supabase client
try:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
except Exception:
    sb = None


# -- DB helpers ------------------------------------------------------------

def db_get_or_create_user(x_user_id, username, name, access_token):
    if not sb:
        return None
    existing = sb.table("users").select("id").eq("x_user_id", x_user_id).execute()
    if existing.data:
        sb.table("users").update({
            "username": username, "name": name,
            "access_token": access_token, "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("x_user_id", x_user_id).execute()
        return existing.data[0]["id"]
    else:
        result = sb.table("users").insert({
            "x_user_id": x_user_id, "username": username,
            "name": name, "access_token": access_token,
        }).execute()
        return result.data[0]["id"] if result.data else None


def db_save_cache(table, user_id, data):
    if not sb or not user_id:
        return
    sb.table(table).delete().eq("user_id", user_id).execute()
    sb.table(table).insert({"user_id": user_id, "data": data}).execute()


def db_load_cache(table, user_id):
    if not sb or not user_id:
        return None
    result = sb.table(table).select("data, fetched_at").eq("user_id", user_id).order("fetched_at", desc=True).limit(1).execute()
    if result.data:
        return result.data[0]["data"]
    return None


def db_save_analysis(user_id, analysis_type, data):
    if not sb or not user_id:
        return
    sb.table("analyses").delete().eq("user_id", user_id).eq("type", analysis_type).execute()
    sb.table("analyses").insert({"user_id": user_id, "type": analysis_type, "data": data}).execute()


def db_load_analysis(user_id, analysis_type):
    if not sb or not user_id:
        return None
    result = sb.table("analyses").select("data").eq("user_id", user_id).eq("type", analysis_type).order("created_at", desc=True).limit(1).execute()
    if result.data:
        return result.data[0]["data"]
    return None


def db_save_suggestions(user_id, data):
    if not sb or not user_id:
        return
    sb.table("suggestions").delete().eq("user_id", user_id).execute()
    sb.table("suggestions").insert({"user_id": user_id, "data": data}).execute()


def db_load_suggestions(user_id):
    if not sb or not user_id:
        return None
    result = sb.table("suggestions").select("data, generated_at").eq("user_id", user_id).order("generated_at", desc=True).limit(1).execute()
    if result.data:
        return result.data[0]["data"]
    return None


def db_save_draft(user_id, tweets, fmt, topic, status="draft", scheduled_at=None):
    if not sb or not user_id:
        return None
    result = sb.table("drafts").insert({
        "user_id": user_id, "tweets": tweets, "format": fmt,
        "topic": topic, "status": status, "scheduled_at": scheduled_at,
    }).execute()
    return result.data[0]["id"] if result.data else None


def db_load_drafts(user_id, status="draft"):
    if not sb or not user_id:
        return []
    result = sb.table("drafts").select("*").eq("user_id", user_id).eq("status", status).order("created_at", desc=True).execute()
    return result.data or []


def db_delete_draft(draft_id, user_id):
    if not sb:
        return
    sb.table("drafts").delete().eq("id", draft_id).eq("user_id", user_id).execute()


def db_mark_posted(draft_id, user_id):
    if not sb:
        return
    sb.table("drafts").update({
        "status": "posted", "posted_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", draft_id).eq("user_id", user_id).execute()


# -- X API helpers ---------------------------------------------------------

def get_redirect_uri():
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/callback"


def generate_pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def exchange_code(code, verifier):
    r = requests.post(
        TOKEN_URL, auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": get_redirect_uri(), "code_verifier": verifier},
    )
    return r.json()


def get_me(token):
    r = requests.get("https://api.twitter.com/2/users/me",
                      headers={"Authorization": f"Bearer {token}"})
    d = r.json()
    return d["data"]["id"], d["data"]["username"], d["data"]["name"]


def fetch_all_bookmarks(token, user_id):
    bookmarks, cursor, api_error = [], None, None
    while True:
        params = {"max_results": 100, "tweet.fields": "created_at,text,author_id,public_metrics",
                  "user.fields": "name,username", "expansions": "author_id"}
        if cursor:
            params["pagination_token"] = cursor
        r = requests.get(f"https://api.twitter.com/2/users/{user_id}/bookmarks",
                         headers={"Authorization": f"Bearer {token}"}, params=params)
        data = r.json()
        if "data" not in data:
            if not bookmarks:
                api_error = data
            break
        users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
        for t in data["data"]:
            au = users.get(t.get("author_id", ""), {})
            m = t.get("public_metrics", {})
            bookmarks.append({
                "id": t.get("id", ""), "text": t.get("text", ""),
                "name": au.get("name", ""), "username": au.get("username", ""),
                "date": t.get("created_at", "")[:10],
                "likes": m.get("like_count", 0), "retweets": m.get("retweet_count", 0),
                "url": f"https://twitter.com/{au.get('username', '')}/status/{t.get('id', '')}",
            })
        cursor = data.get("meta", {}).get("next_token")
        if not cursor:
            break
    return bookmarks, api_error


def fetch_user_tweets(token, user_id):
    tweets, cursor, api_error = [], None, None
    pages = 0
    while pages < 5:
        params = {"max_results": 100, "tweet.fields": "created_at,text,public_metrics,source",
                  "exclude": "retweets,replies"}
        if cursor:
            params["pagination_token"] = cursor
        r = requests.get(f"https://api.twitter.com/2/users/{user_id}/tweets",
                         headers={"Authorization": f"Bearer {token}"}, params=params)
        data = r.json()
        if "data" not in data:
            if not tweets:
                api_error = data
            break
        for t in data["data"]:
            m = t.get("public_metrics", {})
            tweets.append({
                "id": t.get("id", ""), "text": t.get("text", ""),
                "date": t.get("created_at", "")[:10],
                "likes": m.get("like_count", 0), "retweets": m.get("retweet_count", 0),
                "replies": m.get("reply_count", 0), "impressions": m.get("impression_count", 0),
                "url": f"https://twitter.com/i/status/{t.get('id', '')}",
            })
        cursor = data.get("meta", {}).get("next_token")
        if not cursor:
            break
        pages += 1
    return tweets, api_error


# -- AI helpers ------------------------------------------------------------

def analyze_bookmarks(bookmarks, username=""):
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"
    condensed = [f"[{i}] ({bm['date']}) @{bm['username']}: {bm['text'][:280]}" for i, bm in enumerate(bookmarks, 1)]
    user_ref = f"@{username}'s" if username else "This person's"
    prompt = f"""Analyze these {len(bookmarks)} X/Twitter bookmarks belonging to {user_ref}. Return ONLY valid JSON:
{{"summary":"...","categories":[{{"name":"...","count":5,"bookmark_ids":[1,5],"summary":"..."}}],"timeline":[{{"period":"...","theme":"...","count":15,"bookmark_ids":[1,2]}}],"gems":[{{"id":5,"title":"...","reason":"..."}}],"stale":[{{"id":12,"title":"...","reason":"..."}}],"actions":[{{"text":"...","bookmark_ids":[20,50]}}]}}
Rules: summary uses you/your, mention {user_ref} once. 5-8 categories. 3-5 timeline phases with count. 5-10 gems. stale items. 3-5 actions with bookmark_ids.
Bookmarks:\n""" + "\n".join(condensed)
    return _call_claude(prompt)


def analyze_tweets(tweets, username=""):
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"
    condensed = [f"[{i}] ({tw['date']}) {tw['text'][:280]} | L:{tw['likes']} RT:{tw['retweets']} R:{tw['replies']} I:{tw['impressions']}" for i, tw in enumerate(tweets, 1)]
    prompt = f"""Analyze these {len(tweets)} tweets from @{username}. Return ONLY valid JSON:
{{"summary":"...","top_performers":[{{"id":1,"title":"...","why":"..."}}],"underperformers":[{{"id":5,"title":"...","why":"..."}}],"patterns":[{{"pattern":"...","evidence":"...","recommendation":"..."}}],"content_suggestions":[{{"tweet":"...","based_on":[1],"rationale":"..."}}],"strategy":{{"best_topics":["..."],"avoid_topics":["..."],"best_formats":["..."],"posting_advice":"..."}}}}
Rules: 5-8 top performers, 3-5 underperformers, 3-5 patterns, 5-8 content suggestions under 280 chars, concrete strategy. Use you/your.
Tweets:\n""" + "\n".join(condensed)
    return _call_claude(prompt)


PROFILE_CONTEXT = """Profile: Angel investor, operator, and tech founder.
Focus areas: AI agents, Claude Code, no-code/low-code, SaaS, startups, entrepreneurship, building in public.
Voice: Direct, practical, experience-driven. Shares frameworks, lessons learned, and actionable insights.
Audience: Founders, builders, developers, AI enthusiasts, indie hackers."""

FORMATTING_RULES = """FORMATTING (Justin Welsh / Sahil Bloom style):
- Line breaks generously. One idea per line. Short sentences. Punchy rhythm.
- Hook → Context → Insight → CTA. Pattern interrupts and bold claims.
- Bullet points with line breaks. Numbers and specifics beat vague claims.
- End with engagement drivers: questions, "Bookmark this.", "RT to help others."
- Threads: First tweet = HOOK (no number). Last = CTA + value summary. Use 2/, 3/ etc.
- Each tweet stands alone as valuable. Under 280 chars each."""


def generate_topic_suggestions(username):
    if not CLAUDE_API_KEY:
        return []
    prompt = f"""Twitter/X content strategist for @{username}. {PROFILE_CONTEXT}
Generate 8 tweet/thread ideas for RIGHT NOW (March 2026). Return ONLY valid JSON:
{{"suggestions":[{{"topic":"5-8 words","hook":"Opening line","format":"tweet or thread","why":"1 sentence"}}]}}
Rules: 4 timely/trending + 4 evergreen. Justin Welsh/Sahil Bloom style hooks. Each must stop the scroll."""
    try:
        result, _ = _call_claude(prompt, max_tokens=2048)
        return result.get("suggestions", []) if result else []
    except Exception:
        return []


def generate_draft(username, idea, format_type):
    if not CLAUDE_API_KEY:
        return []
    if format_type == "thread":
        prompt = f"""Create viral Twitter/X thread for @{username} about: {idea}
{PROFILE_CONTEXT}
Return ONLY valid JSON: {{"tweets":["Hook tweet (no number)","2/ Second","3/ Third",...]}}
{FORMATTING_RULES}
Write 5-8 tweets. First = pure hook. Last = CTA. Each under 280 chars."""
    else:
        prompt = f"""Create single viral tweet for @{username} about: {idea}
{PROFILE_CONTEXT}
Return ONLY valid JSON: {{"tweets":["The tweet"]}}
{FORMATTING_RULES}
Under 280 chars. One clear idea. Strong hook. Engagement driver at end."""
    try:
        result, _ = _call_claude(prompt, max_tokens=2048)
        return result.get("tweets", []) if result else []
    except Exception:
        return []


def _call_claude(prompt, max_tokens=4096):
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        raw = message.content[0].text
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(raw), None
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        return None, f"Failed to parse AI response: {e}"


def build_excel(bookmarks):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bookmarks"
    headers = ["#", "Tweet", "Author", "Username", "Date", "Likes", "RTs", "URL"]
    hfill = PatternFill(start_color="1D9BF0", end_color="1D9BF0", fill_type="solid")
    hfont = Font(bold=True, color="FFFFFF")
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill, cell.font, cell.alignment = hfill, hfont, Alignment(horizontal="center")
    for i, bm in enumerate(bookmarks, 1):
        ws.append([i, bm["text"], bm["name"], f"@{bm['username']}", bm["date"], bm["likes"], bm["retweets"], bm["url"]])
        ws.cell(row=i + 1, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    for col, w in zip("ABCDEFGH", [5, 75, 22, 20, 12, 8, 6, 60]):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# -- routes ----------------------------------------------------------------

@app.route("/")
def index():
    configured = bool(CLIENT_ID and CLIENT_SECRET)
    if not session.get("access_token"):
        return render_template("index.html", configured=configured, connected=False, username="", bookmarks=None)

    uid = session.get("db_user_id")
    bookmarks = db_load_cache("bookmarks_cache", uid)
    analysis = db_load_analysis(uid, "bookmarks")
    return render_template("index.html", configured=configured, connected=True,
                           username=session.get("username", ""), bookmarks=bookmarks, analysis=analysis)


@app.route("/connect", methods=["POST"])
def connect():
    if not CLIENT_ID or not CLIENT_SECRET:
        return redirect("/")
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    session["verifier"] = verifier
    session["state"] = state
    params = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": get_redirect_uri(),
              "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return redirect(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or state != session.get("state"):
        return render_template("index.html", configured=True, error="Authorization failed.")
    token_data = exchange_code(code, session["verifier"])
    token = token_data.get("access_token")
    if not token:
        return render_template("index.html", configured=True, error=f"Token error: {token_data}")
    session["access_token"] = token
    uid, uname, name = get_me(token)
    session["user_id"] = uid
    session["username"] = uname
    session["name"] = name
    db_uid = db_get_or_create_user(uid, uname, name, token)
    session["db_user_id"] = db_uid
    return redirect("/")


@app.route("/fetch")
def fetch():
    token = session.get("access_token")
    uid = session.get("user_id")
    db_uid = session.get("db_user_id")
    if not token or not uid:
        return redirect("/")
    bookmarks, api_error = fetch_all_bookmarks(token, uid)
    if bookmarks:
        db_save_cache("bookmarks_cache", db_uid, bookmarks)
    error = f"X API error: {api_error}" if api_error else None
    analysis = db_load_analysis(db_uid, "bookmarks")
    return render_template("index.html", configured=True, connected=True,
                           username=session.get("username", ""), bookmarks=bookmarks, analysis=analysis, error=error)


@app.route("/analyze", methods=["POST"])
def analyze():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    bookmarks = db_load_cache("bookmarks_cache", db_uid)
    if not bookmarks:
        return redirect("/fetch")
    analysis, ai_error = analyze_bookmarks(bookmarks, session.get("username", ""))
    if analysis:
        db_save_analysis(db_uid, "bookmarks", analysis)
    error = f"AI analysis error: {ai_error}" if ai_error else None
    return render_template("index.html", configured=True, connected=True,
                           username=session.get("username", ""), bookmarks=bookmarks, analysis=analysis, error=error)


@app.route("/download", methods=["POST"])
def download():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    bookmarks = db_load_cache("bookmarks_cache", db_uid)
    if not bookmarks:
        return redirect("/fetch")
    buf = build_excel(bookmarks)
    return send_file(buf, as_attachment=True,
                     download_name=f"bookmarks_{session.get('username', 'x')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/content")
def content():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets = db_load_cache("tweets_cache", db_uid)
    analysis = db_load_analysis(db_uid, "tweets")
    return render_template("content.html", connected=True, username=session.get("username", ""),
                           tweets=tweets, analysis=analysis)


@app.route("/content/fetch")
def content_fetch():
    token = session.get("access_token")
    uid = session.get("user_id")
    db_uid = session.get("db_user_id")
    if not token or not uid:
        return redirect("/")
    tweets, api_error = fetch_user_tweets(token, uid)
    if tweets:
        db_save_cache("tweets_cache", db_uid, tweets)
    error = f"X API error: {api_error}" if api_error else None
    analysis = db_load_analysis(db_uid, "tweets")
    return render_template("content.html", connected=True, username=session.get("username", ""),
                           tweets=tweets, analysis=analysis, error=error)


@app.route("/content/analyze", methods=["POST"])
def content_analyze():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets = db_load_cache("tweets_cache", db_uid)
    if not tweets:
        return redirect("/content/fetch")
    analysis, ai_error = analyze_tweets(tweets, session.get("username", ""))
    if analysis:
        db_save_analysis(db_uid, "tweets", analysis)
    error = f"AI analysis error: {ai_error}" if ai_error else None
    return render_template("content.html", connected=True, username=session.get("username", ""),
                           tweets=tweets, analysis=analysis, error=error)


@app.route("/content/compose")
def content_compose():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    suggestions = db_load_suggestions(db_uid)
    drafts_list = db_load_drafts(db_uid)
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           suggestions=suggestions, saved_drafts=drafts_list)


@app.route("/content/suggestions", methods=["POST"])
def content_suggestions():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    suggestions = generate_topic_suggestions(session.get("username", ""))
    if suggestions:
        db_save_suggestions(db_uid, suggestions)
    drafts_list = db_load_drafts(db_uid)
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           suggestions=suggestions, saved_drafts=drafts_list)


@app.route("/content/ai-draft", methods=["POST"])
def content_ai_draft():
    if not session.get("access_token"):
        return redirect("/")
    idea = request.form.get("idea", "").strip()
    format_type = request.form.get("format", "tweet")
    if not idea:
        return redirect("/content/compose")
    drafts = generate_draft(session.get("username", ""), idea, format_type)
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           drafts=drafts, idea=idea, format_type=format_type)


@app.route("/content/save-draft", methods=["POST"])
def content_save_draft():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets_json = request.form.get("tweets", "[]")
    topic = request.form.get("topic", "")
    fmt = request.form.get("format", "tweet")
    try:
        tweets = json.loads(tweets_json)
    except json.JSONDecodeError:
        return redirect("/content/compose")
    if tweets:
        db_save_draft(db_uid, tweets, fmt, topic)
    return redirect("/content/compose")


@app.route("/content/delete-draft/<draft_id>", methods=["POST"])
def content_delete_draft(draft_id):
    if not session.get("access_token"):
        return redirect("/")
    db_delete_draft(draft_id, session.get("db_user_id"))
    return redirect("/content/compose")


@app.route("/content/post", methods=["POST"])
def content_post():
    token = session.get("access_token")
    if not token:
        return redirect("/")
    tweets_json = request.form.get("tweets", "[]")
    draft_id = request.form.get("draft_id")
    try:
        tweets = json.loads(tweets_json)
    except json.JSONDecodeError:
        return redirect("/content/compose")
    if not tweets:
        return redirect("/content/compose")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    posted, reply_to = [], None
    for tweet_text in tweets:
        body = {"text": tweet_text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = requests.post("https://api.twitter.com/2/tweets", headers=headers, json=body)
        data = r.json()
        if "data" in data:
            reply_to = data["data"]["id"]
            posted.append(data["data"]["id"])
        else:
            return render_template("compose.html", connected=True, username=session.get("username", ""),
                                   error=f"Post failed: {data}", drafts=tweets)
    if draft_id:
        db_mark_posted(draft_id, session.get("db_user_id"))
    first_id = posted[0] if posted else ""
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           success=True, posted_url=f"https://twitter.com/{session.get('username', '')}/status/{first_id}",
                           posted_count=len(posted))


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=False, port=5000)
