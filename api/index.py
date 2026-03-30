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
    result = sb.table("users").insert({
        "x_user_id": x_user_id, "username": username,
        "name": name, "access_token": access_token,
    }).execute()
    return result.data[0]["id"] if result.data else None


def db_save_cache(table, user_id, data, last_id=None):
    if not sb or not user_id:
        return
    sb.table(table).delete().eq("user_id", user_id).execute()
    row = {"user_id": user_id, "data": data}
    if last_id:
        row["last_id"] = last_id
    sb.table(table).insert(row).execute()


def db_load_cache_full(table, user_id):
    """Returns (data, last_id, fetched_at) or (None, None, None)."""
    if not sb or not user_id:
        return None, None, None
    result = sb.table(table).select("data, last_id, fetched_at").eq("user_id", user_id).order("fetched_at", desc=True).limit(1).execute()
    if result.data:
        row = result.data[0]
        return row.get("data"), row.get("last_id"), row.get("fetched_at")
    return None, None, None


def db_merge_cache(table, user_id, new_data, last_id):
    """Merge new data with existing cached data (delta refresh)."""
    existing, _, _ = db_load_cache_full(table, user_id)
    if existing:
        existing_ids = {item["id"] for item in existing}
        merged = new_data + [item for item in existing if item["id"] not in {n["id"] for n in new_data}]
    else:
        merged = new_data
    db_save_cache(table, user_id, merged, last_id)
    return merged


def db_save_analysis(user_id, analysis_type, data):
    if not sb or not user_id:
        return
    sb.table("analyses").delete().eq("user_id", user_id).eq("type", analysis_type).execute()
    sb.table("analyses").insert({"user_id": user_id, "type": analysis_type, "data": data}).execute()


def db_load_analysis(user_id, analysis_type):
    if not sb or not user_id:
        return None
    result = sb.table("analyses").select("data").eq("user_id", user_id).eq("type", analysis_type).order("created_at", desc=True).limit(1).execute()
    return result.data[0]["data"] if result.data else None


def db_save_suggestions(user_id, data):
    if not sb or not user_id:
        return
    sb.table("suggestions").delete().eq("user_id", user_id).execute()
    sb.table("suggestions").insert({"user_id": user_id, "data": data}).execute()


def db_load_suggestions(user_id):
    if not sb or not user_id:
        return None
    result = sb.table("suggestions").select("data").eq("user_id", user_id).order("generated_at", desc=True).limit(1).execute()
    return result.data[0]["data"] if result.data else None


def db_save_profile(user_id, data):
    if not sb or not user_id:
        return
    existing = sb.table("user_profile").select("id").eq("user_id", user_id).execute()
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    if existing.data:
        sb.table("user_profile").update(data).eq("user_id", user_id).execute()
    else:
        data["user_id"] = user_id
        sb.table("user_profile").insert(data).execute()


def db_load_profile(user_id):
    if not sb or not user_id:
        return None
    result = sb.table("user_profile").select("*").eq("user_id", user_id).limit(1).execute()
    return result.data[0] if result.data else None


def get_voice_context(user_id):
    """Build a rich context string from the user's profile for AI prompts."""
    profile = _safe_db(db_load_profile, user_id)
    if not profile:
        return PROFILE_CONTEXT

    parts = []
    if profile.get("bio"):
        parts.append(f"Bio: {profile['bio']}")
    if profile.get("expertise"):
        parts.append(f"Expertise: {profile['expertise']}")
    if profile.get("current_focus"):
        parts.append(f"Current focus: {profile['current_focus']}")
    if profile.get("opinions"):
        parts.append(f"Strong opinions/takes: {profile['opinions']}")
    if profile.get("donts"):
        parts.append(f"NEVER do these in content: {profile['donts']}")

    voice_examples = profile.get("voice_examples", [])
    if voice_examples:
        examples_text = "\n".join(f"- {ex}" for ex in voice_examples if ex)
        if examples_text:
            parts.append(f"Voice examples (write like these):\n{examples_text}")

    if not parts:
        return PROFILE_CONTEXT

    return "\n".join(parts)


def db_save_draft(user_id, tweets, fmt, topic, status="draft"):
    if not sb or not user_id:
        return None
    result = sb.table("drafts").insert({
        "user_id": user_id, "tweets": tweets, "format": fmt,
        "topic": topic, "status": status,
    }).execute()
    return result.data[0]["id"] if result.data else None


def db_load_drafts(user_id, status=None):
    if not sb or not user_id:
        return []
    q = sb.table("drafts").select("*").eq("user_id", user_id)
    if status:
        q = q.eq("status", status)
    result = q.order("created_at", desc=True).execute()
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
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def exchange_code(code, verifier):
    r = requests.post(TOKEN_URL, auth=(CLIENT_ID, CLIENT_SECRET),
                      data={"grant_type": "authorization_code", "code": code,
                            "redirect_uri": get_redirect_uri(), "code_verifier": verifier})
    return r.json()


def get_me(token):
    r = requests.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
    d = r.json()
    return d["data"]["id"], d["data"]["username"], d["data"]["name"]


def fetch_bookmarks_delta(token, user_id, since_id=None):
    """Fetch bookmarks. If since_id provided, only fetches newer ones."""
    bookmarks, cursor, api_error = [], None, None
    while True:
        params = {"max_results": 100, "tweet.fields": "created_at,text,author_id,public_metrics",
                  "user.fields": "name,username", "expansions": "author_id"}
        if cursor:
            params["pagination_token"] = cursor
        if since_id:
            params["since_id"] = since_id
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


def fetch_tweets_delta(token, user_id, since_id=None):
    """Fetch user tweets. If since_id provided, only fetches newer ones."""
    tweets, cursor, api_error = [], None, None
    pages = 0
    while pages < 5:
        params = {"max_results": 100, "tweet.fields": "created_at,text,public_metrics,source",
                  "exclude": "retweets,replies"}
        if cursor:
            params["pagination_token"] = cursor
        if since_id:
            params["since_id"] = since_id
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

PROFILE_CONTEXT = """Profile: Angel investor, operator, and tech founder.
Focus areas: AI agents, Claude Code, no-code/low-code, SaaS, startups, entrepreneurship, building in public.
Voice: Direct, practical, experience-driven. Shares frameworks, lessons learned, and actionable insights.
Audience: Founders, builders, developers, AI enthusiasts, indie hackers."""

FORMATTING_RULES = """FORMATTING (Justin Welsh / Sahil Bloom style):
- Line breaks generously. One idea per line. Short sentences. Punchy rhythm.
- Hook -> Context -> Insight -> CTA. Pattern interrupts and bold claims.
- Bullet points with line breaks. Numbers and specifics beat vague claims.
- End with engagement drivers: questions, "Bookmark this.", "RT to help others."
- Threads: First tweet = HOOK (no number). Last = CTA + value summary. Use 2/, 3/ etc.
- Each tweet stands alone as valuable. Under 280 chars each."""


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


def analyze_bookmarks(bookmarks, username=""):
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"
    condensed = [f"[{i}] ({bm['date']}) @{bm['username']}: {bm['text'][:280]}" for i, bm in enumerate(bookmarks, 1)]
    user_ref = f"@{username}" if username else "user"
    prompt = f"""Analyze these {len(bookmarks)} X/Twitter bookmarks for {user_ref}. Return ONLY valid JSON:
{{"summary":"2-3 sentences using you/your","categories":[{{"name":"...","count":5,"bookmark_ids":[1,5],"summary":"..."}}],"timeline":[{{"period":"...","theme":"...","count":15,"bookmark_ids":[1,2]}}],"gems":[{{"id":5,"title":"...","reason":"..."}}],"stale":[{{"id":12,"title":"...","reason":"..."}}],"actions":[{{"text":"...","bookmark_ids":[20,50]}}]}}
Rules: 5-8 categories, 3-5 timeline phases with count, 5-10 gems, stale items, 3-5 actions with bookmark_ids. Use you/your.
Bookmarks:\n""" + "\n".join(condensed)
    return _call_claude(prompt)


def analyze_tweets(tweets, username=""):
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"
    condensed = [f"[{i}] ({tw['date']}) {tw['text'][:280]} | L:{tw['likes']} RT:{tw['retweets']} R:{tw['replies']} I:{tw['impressions']}" for i, tw in enumerate(tweets, 1)]
    prompt = f"""Analyze these {len(tweets)} tweets from @{username}. Return ONLY valid JSON:
{{"summary":"...","top_performers":[{{"id":1,"title":"...","why":"..."}}],"underperformers":[{{"id":5,"title":"...","why":"..."}}],"patterns":[{{"pattern":"...","evidence":"...","recommendation":"..."}}],"content_suggestions":[{{"tweet":"...","based_on":[1],"rationale":"..."}}],"strategy":{{"best_topics":["..."],"avoid_topics":["..."],"best_formats":["..."],"posting_advice":"..."}}}}
Rules: 5-8 top, 3-5 under, 3-5 patterns, 5-8 suggestions under 280 chars, strategy. Use you/your.
Tweets:\n""" + "\n".join(condensed)
    return _call_claude(prompt)


def generate_smart_suggestions(username, db_uid):
    """Generate suggestions using bookmark interests + tweet patterns + trends."""
    if not CLAUDE_API_KEY:
        return []

    # Gather intelligence
    bookmark_analysis = db_load_analysis(db_uid, "bookmarks")
    tweet_analysis = db_load_analysis(db_uid, "tweets")

    voice = get_voice_context(db_uid)
    context_parts = [voice]

    if bookmark_analysis:
        cats = bookmark_analysis.get("categories", [])
        if cats:
            topics = ", ".join(c["name"] for c in cats[:5])
            context_parts.append(f"Current bookmark interests: {topics}")

    if tweet_analysis:
        strategy = tweet_analysis.get("strategy", {})
        if strategy.get("best_topics"):
            context_parts.append(f"Top performing tweet topics: {', '.join(strategy['best_topics'])}")
        if strategy.get("best_formats"):
            fmts = strategy["best_formats"]
            if isinstance(fmts, list):
                context_parts.append(f"Best tweet formats: {', '.join(fmts)}")
            else:
                context_parts.append(f"Best tweet formats: {fmts}")
        patterns = tweet_analysis.get("patterns", [])
        if patterns:
            context_parts.append(f"Proven patterns: {patterns[0].get('pattern', '')}")

    full_context = "\n".join(context_parts)

    prompt = f"""Twitter/X content strategist for @{username}.

{full_context}

Generate 8 tweet/thread ideas that would perform well RIGHT NOW (March 2026).
These ideas should combine:
1. The user's current interests (from their bookmarks)
2. What works for their audience (from their tweet performance)
3. Trending topics in AI, tech, startups this week

Return ONLY valid JSON:
{{"suggestions":[{{"topic":"5-8 words","hook":"Opening line that stops the scroll","format":"tweet or thread","why":"Why this would work based on their data + trends"}}]}}

Rules:
- 4 timely/trending ideas aligned with their interests
- 4 evergreen ideas based on proven engagement patterns
- Hooks in Justin Welsh / Sahil Bloom style
- Each idea should feel personalized, not generic"""

    try:
        result, _ = _call_claude(prompt, max_tokens=2048)
        return result.get("suggestions", []) if result else []
    except Exception:
        return []


LINKEDIN_FORMATTING = """LINKEDIN FORMATTING:
- Start with a bold hook line that stops the scroll (1 short sentence)
- Add a blank line after the hook
- Use short paragraphs (1-2 sentences each) with blank lines between
- Use storytelling: situation -> challenge -> insight -> lesson
- Include specific numbers, results, or timeframes
- End with a question or call to engage ("What's your experience?" / "Agree? Drop a comment.")
- Add 3-5 relevant hashtags on the last line
- Total length: 800-1500 characters (the sweet spot for LinkedIn)
- Tone: professional but conversational, share real experience
- No emojis at line starts (LinkedIn algorithm penalizes this)
- Use "I" statements and personal stories"""


def generate_draft(username, idea, format_type, platform="x", db_uid=None):
    if not CLAUDE_API_KEY:
        return [], None
    voice = get_voice_context(db_uid) if db_uid else PROFILE_CONTEXT
    if platform == "linkedin":
        prompt = f"""Create a viral LinkedIn post for {username} about: {idea}

{voice}

IMPORTANT: Write in EXACTLY this person's voice and style. Use their actual perspective, not generic advice.

Return ONLY valid JSON: {{"linkedin_post":"The full post text"}}
{LINKEDIN_FORMATTING}"""
        try:
            result, _ = _call_claude(prompt, max_tokens=2048)
            post = result.get("linkedin_post", "") if result else ""
            return [post] if post else [], "linkedin"
        except Exception:
            return [], "linkedin"
    elif platform == "both":
        prompt = f"""Create content for BOTH Twitter/X and LinkedIn about: {idea}

{voice}

IMPORTANT: Write in EXACTLY this person's voice and style. Use their actual perspective, not generic advice.

Return ONLY valid JSON:
{{"tweets":["Hook tweet (no number)","2/ Second","3/ Third",...],"linkedin_post":"The full LinkedIn post"}}

For Twitter/X thread:
{FORMATTING_RULES}
Write 5-8 tweets. First = pure hook. Last = CTA. Each under 280 chars.

For LinkedIn:
{LINKEDIN_FORMATTING}
Adapt the same core idea but in LinkedIn's longer, storytelling format."""
        try:
            result, _ = _call_claude(prompt, max_tokens=4096)
            if not result:
                return [], "both"
            tweets = result.get("tweets", [])
            li_post = result.get("linkedin_post", "")
            return tweets, "both", li_post
        except Exception:
            return [], "both", ""
    elif format_type == "thread":
        prompt = f"""Create viral Twitter/X thread for @{username} about: {idea}

{voice}

IMPORTANT: Write in EXACTLY this person's voice and style. Use their actual perspective, not generic advice.

Return ONLY valid JSON: {{"tweets":["Hook tweet (no number)","2/ Second","3/ Third",...]}}
{FORMATTING_RULES}
Write 5-8 tweets. First = pure hook. Last = CTA. Each under 280 chars."""
    else:
        prompt = f"""Create single viral tweet for @{username} about: {idea}

{voice}

IMPORTANT: Write in EXACTLY this person's voice and style. Use their actual perspective, not generic advice.

Return ONLY valid JSON: {{"tweets":["The tweet"]}}
{FORMATTING_RULES}
Under 280 chars. Strong hook. Engagement driver at end."""
    try:
        result, _ = _call_claude(prompt, max_tokens=2048)
        return result.get("tweets", []) if result else [], None
    except Exception:
        return [], None


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


def _safe_db(fn, *args, **kwargs):
    """Wrap DB calls so failures don't crash pages."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# -- routes ----------------------------------------------------------------

@app.route("/")
def index():
    """Dashboard - unified home page."""
    configured = bool(CLIENT_ID and CLIENT_SECRET)
    if not session.get("access_token"):
        return render_template("index.html", configured=configured, connected=False)

    db_uid = session.get("db_user_id")

    # Load all cached data
    bm_data, bm_last_id, bm_fetched = (None, None, None)
    tw_data, tw_last_id, tw_fetched = (None, None, None)
    try:
        bm_data, bm_last_id, bm_fetched = db_load_cache_full("bookmarks_cache", db_uid)
        tw_data, tw_last_id, tw_fetched = db_load_cache_full("tweets_cache", db_uid)
    except Exception:
        pass

    bm_analysis = _safe_db(db_load_analysis, db_uid, "bookmarks")
    tw_analysis = _safe_db(db_load_analysis, db_uid, "tweets")
    suggestions = _safe_db(db_load_suggestions, db_uid)
    drafts = _safe_db(db_load_drafts, db_uid, "draft") or []

    return render_template("dashboard.html",
        connected=True, username=session.get("username", ""),
        bookmarks=bm_data, bookmarks_fetched=bm_fetched,
        bookmarks_count=len(bm_data) if bm_data else 0,
        tweets=tw_data, tweets_fetched=tw_fetched,
        tweets_count=len(tw_data) if tw_data else 0,
        bm_analysis=bm_analysis, tw_analysis=tw_analysis,
        suggestions=suggestions, drafts=drafts,
    )


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
    try:
        token_data = exchange_code(code, session.get("verifier", ""))
    except Exception as e:
        return render_template("index.html", configured=True, error=f"Token exchange error: {e}")
    token = token_data.get("access_token")
    if not token:
        return render_template("index.html", configured=True, error=f"Token error: {token_data}")
    session["access_token"] = token
    try:
        uid, uname, name = get_me(token)
    except Exception as e:
        return render_template("index.html", configured=True, error=f"Failed to get user: {e}")
    session["user_id"] = uid
    session["username"] = uname
    session["name"] = name
    try:
        db_uid = db_get_or_create_user(uid, uname, name, token)
        session["db_user_id"] = db_uid
    except Exception:
        session["db_user_id"] = None
    return redirect("/")


@app.route("/sync", methods=["POST"])
def sync():
    """Delta sync - fetches only new bookmarks and tweets."""
    token = session.get("access_token")
    uid = session.get("user_id")
    db_uid = session.get("db_user_id")
    if not token or not uid:
        return redirect("/")

    errors = []

    # Delta bookmarks
    try:
        _, bm_last_id, _ = db_load_cache_full("bookmarks_cache", db_uid)
        new_bm, bm_err = fetch_bookmarks_delta(token, uid, since_id=bm_last_id)
        if bm_err and not bm_last_id:
            errors.append(f"Bookmarks: {bm_err}")
        elif new_bm:
            latest_id = new_bm[0]["id"]
            db_merge_cache("bookmarks_cache", db_uid, new_bm, latest_id)
    except Exception as e:
        errors.append(f"Bookmarks sync error: {e}")

    # Delta tweets
    try:
        _, tw_last_id, _ = db_load_cache_full("tweets_cache", db_uid)
        new_tw, tw_err = fetch_tweets_delta(token, uid, since_id=tw_last_id)
        if tw_err and not tw_last_id:
            errors.append(f"Tweets: {tw_err}")
        elif new_tw:
            latest_id = new_tw[0]["id"]
            db_merge_cache("tweets_cache", db_uid, new_tw, latest_id)
    except Exception as e:
        errors.append(f"Tweets sync error: {e}")

    if errors:
        session["sync_error"] = " | ".join(errors)
    else:
        session["sync_status"] = "Sync complete!"
    return redirect("/")


@app.route("/bookmarks")
def bookmarks_view():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    bookmarks, _, fetched = (None, None, None)
    try:
        bookmarks, _, fetched = db_load_cache_full("bookmarks_cache", db_uid)
    except Exception:
        pass
    analysis = _safe_db(db_load_analysis, db_uid, "bookmarks")
    return render_template("bookmarks.html", connected=True, username=session.get("username", ""),
                           bookmarks=bookmarks, analysis=analysis, fetched_at=fetched)


@app.route("/bookmarks/analyze", methods=["POST"])
def bookmarks_analyze():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    bookmarks, _, fetched = db_load_cache_full("bookmarks_cache", db_uid)
    if not bookmarks:
        return redirect("/bookmarks")
    analysis, ai_error = analyze_bookmarks(bookmarks, session.get("username", ""))
    if analysis:
        _safe_db(db_save_analysis, db_uid, "bookmarks", analysis)
    error = f"AI error: {ai_error}" if ai_error else None
    return render_template("bookmarks.html", connected=True, username=session.get("username", ""),
                           bookmarks=bookmarks, analysis=analysis, fetched_at=fetched, error=error)


@app.route("/bookmarks/download", methods=["POST"])
def bookmarks_download():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    bookmarks, _, _ = db_load_cache_full("bookmarks_cache", db_uid)
    if not bookmarks:
        return redirect("/bookmarks")
    buf = build_excel(bookmarks)
    return send_file(buf, as_attachment=True,
                     download_name=f"bookmarks_{session.get('username', 'x')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/tweets")
def tweets_view():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets, _, fetched = (None, None, None)
    try:
        tweets, _, fetched = db_load_cache_full("tweets_cache", db_uid)
    except Exception:
        pass
    analysis = _safe_db(db_load_analysis, db_uid, "tweets")
    return render_template("content.html", connected=True, username=session.get("username", ""),
                           tweets=tweets, analysis=analysis, fetched_at=fetched)


@app.route("/tweets/analyze", methods=["POST"])
def tweets_analyze():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets, _, fetched = db_load_cache_full("tweets_cache", db_uid)
    if not tweets:
        return redirect("/tweets")
    analysis, ai_error = analyze_tweets(tweets, session.get("username", ""))
    if analysis:
        _safe_db(db_save_analysis, db_uid, "tweets", analysis)
    error = f"AI error: {ai_error}" if ai_error else None
    return render_template("content.html", connected=True, username=session.get("username", ""),
                           tweets=tweets, analysis=analysis, fetched_at=fetched, error=error)


@app.route("/compose")
def compose():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    suggestions = _safe_db(db_load_suggestions, db_uid)
    drafts_list = _safe_db(db_load_drafts, db_uid, "draft") or []
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           suggestions=suggestions, saved_drafts=drafts_list)


@app.route("/compose/suggestions", methods=["POST"])
def compose_suggestions():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    suggestions = generate_smart_suggestions(session.get("username", ""), db_uid)
    if suggestions:
        _safe_db(db_save_suggestions, db_uid, suggestions)
    drafts_list = _safe_db(db_load_drafts, db_uid, "draft") or []
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           suggestions=suggestions, saved_drafts=drafts_list)


@app.route("/compose/generate", methods=["POST"])
def compose_generate():
    if not session.get("access_token"):
        return redirect("/")
    idea = request.form.get("idea", "").strip()
    format_type = request.form.get("format", "tweet")
    platform = request.form.get("platform", "x")
    if not idea:
        return redirect("/compose")

    db_uid = session.get("db_user_id")
    linkedin_post = None
    if platform == "both":
        result = generate_draft(session.get("username", ""), idea, format_type, platform="both", db_uid=db_uid)
        drafts, _, linkedin_post = result if len(result) == 3 else (result[0], "both", "")
    elif platform == "linkedin":
        drafts, _ = generate_draft(session.get("username", ""), idea, format_type, platform="linkedin", db_uid=db_uid)
        linkedin_post = drafts[0] if drafts else ""
        drafts = []
    else:
        drafts, _ = generate_draft(session.get("username", ""), idea, format_type, platform="x", db_uid=db_uid)

    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           drafts=drafts, linkedin_post=linkedin_post,
                           idea=idea, format_type=format_type, platform=platform)


@app.route("/compose/save", methods=["POST"])
def compose_save():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    tweets_json = request.form.get("tweets", "[]")
    topic = request.form.get("topic", "")
    fmt = request.form.get("format", "tweet")
    try:
        tweets = json.loads(tweets_json)
    except json.JSONDecodeError:
        return redirect("/compose")
    if tweets:
        _safe_db(db_save_draft, db_uid, tweets, fmt, topic)
    return redirect("/compose")


@app.route("/compose/delete/<draft_id>", methods=["POST"])
def compose_delete(draft_id):
    if not session.get("access_token"):
        return redirect("/")
    _safe_db(db_delete_draft, draft_id, session.get("db_user_id"))
    return redirect("/compose")


@app.route("/compose/post", methods=["POST"])
def compose_post():
    token = session.get("access_token")
    if not token:
        return redirect("/")
    tweets_json = request.form.get("tweets", "[]")
    draft_id = request.form.get("draft_id")
    try:
        tweets = json.loads(tweets_json)
    except json.JSONDecodeError:
        return redirect("/compose")
    if not tweets:
        return redirect("/compose")
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
        _safe_db(db_mark_posted, draft_id, session.get("db_user_id"))
    first_id = posted[0] if posted else ""
    return render_template("compose.html", connected=True, username=session.get("username", ""),
                           success=True, posted_url=f"https://twitter.com/{session.get('username', '')}/status/{first_id}",
                           posted_count=len(posted))


@app.route("/drafts")
def drafts_view():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    all_drafts = _safe_db(db_load_drafts, db_uid) or []
    draft_list = [d for d in all_drafts if d.get("status") == "draft"]
    posted_list = [d for d in all_drafts if d.get("status") == "posted"]
    return render_template("drafts.html", connected=True, username=session.get("username", ""),
                           drafts=draft_list, posted=posted_list)


@app.route("/settings")
def settings():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")
    profile = _safe_db(db_load_profile, db_uid) or {}
    return render_template("settings.html", connected=True, username=session.get("username", ""), profile=profile)


@app.route("/settings/save", methods=["POST"])
def settings_save():
    if not session.get("access_token"):
        return redirect("/")
    db_uid = session.get("db_user_id")

    # Parse voice examples from textarea (one per line)
    examples_raw = request.form.get("voice_examples", "")
    voice_examples = [line.strip() for line in examples_raw.split("\n---\n") if line.strip()]

    data = {
        "bio": request.form.get("bio", "").strip(),
        "expertise": request.form.get("expertise", "").strip(),
        "current_focus": request.form.get("current_focus", "").strip(),
        "voice_examples": voice_examples,
        "opinions": request.form.get("opinions", "").strip(),
        "donts": request.form.get("donts", "").strip(),
    }
    _safe_db(db_save_profile, db_uid, data)
    return render_template("settings.html", connected=True, username=session.get("username", ""),
                           profile=data, saved=True)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=False, port=5000)
