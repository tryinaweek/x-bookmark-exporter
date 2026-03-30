import os
import json
import secrets
import hashlib
import base64
import urllib.parse
import io

import requests
import anthropic
from flask import Flask, render_template, request, redirect, session, send_file

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
SCOPES = "bookmark.read tweet.read tweet.write users.read offline.access"


# -- helpers ---------------------------------------------------------------

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
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": get_redirect_uri(),
            "code_verifier": verifier,
        },
    )
    return r.json()


def get_me(token):
    r = requests.get(
        "https://api.twitter.com/2/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    d = r.json()
    return d["data"]["id"], d["data"]["username"], d["data"]["name"]


def fetch_all_bookmarks(token, user_id):
    bookmarks, cursor, api_error = [], None, None
    while True:
        params = {
            "max_results": 100,
            "tweet.fields": "created_at,text,author_id,public_metrics",
            "user.fields": "name,username",
            "expansions": "author_id",
        }
        if cursor:
            params["pagination_token"] = cursor

        r = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/bookmarks",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
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
                "id": t.get("id", ""),
                "text": t.get("text", ""),
                "name": au.get("name", ""),
                "username": au.get("username", ""),
                "date": t.get("created_at", "")[:10],
                "likes": m.get("like_count", 0),
                "retweets": m.get("retweet_count", 0),
                "url": f"https://twitter.com/{au.get('username', '')}/status/{t.get('id', '')}",
            })

        cursor = data.get("meta", {}).get("next_token")
        if not cursor:
            break

    return bookmarks, api_error


def encode_bookmarks(bookmarks):
    """Encode bookmarks to a base64 string for embedding in HTML."""
    return base64.b64encode(json.dumps(bookmarks).encode()).decode()


def decode_bookmarks(encoded):
    """Decode bookmarks from a base64 string."""
    try:
        return json.loads(base64.b64decode(encoded.encode()).decode())
    except Exception:
        return None


def analyze_bookmarks(bookmarks, username=""):
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"

    condensed = []
    for i, bm in enumerate(bookmarks, 1):
        condensed.append(f"[{i}] ({bm['date']}) @{bm['username']}: {bm['text'][:280]}")
    bookmark_text = "\n".join(condensed)

    user_ref = f"@{username}'s" if username else "This person's"

    prompt = f"""Analyze these {len(bookmarks)} X/Twitter bookmarks belonging to {user_ref}. Return ONLY valid JSON with this exact structure:

{{
  "summary": "2-3 sentence overview addressing the user directly (use 'you/your') about what their bookmarks reveal about their interests and current focus",
  "categories": [
    {{"name": "Category Name", "count": 5, "bookmark_ids": [1, 5, 12], "summary": "Brief description of this category"}}
  ],
  "timeline": [
    {{"period": "Mar 25-27", "theme": "What they were researching", "count": 15, "bookmark_ids": [1, 2, 3]}}
  ],
  "gems": [
    {{"id": 5, "title": "Short title", "reason": "Why this is worth revisiting - be specific about the value"}}
  ],
  "stale": [
    {{"id": 12, "title": "Short title", "reason": "Why this is no longer relevant"}}
  ],
  "actions": [
    {{"text": "Specific, actionable recommendation", "bookmark_ids": [20, 50]}}
  ]
}}

Rules:
- summary: Address the user directly using "you" and "your". Mention their name ({user_ref}) once. Be insightful about patterns.
- categories: Group into 5-8 meaningful topics. Every bookmark should be in at least one category.
- timeline: Identify 3-5 research phases based on date clusters and topic patterns. Include "count" with number of bookmarks in that phase.
- gems: Pick 5-10 bookmarks that contain genuinely valuable, actionable content that's easy to miss in a long list. Prioritize high-engagement tweets with practical advice.
- stale: Pick bookmarks that are time-sensitive announcements, outdated news, or things that are no longer actionable.
- actions: Give 3-5 concrete next steps addressing the user as "you". Each action must include "text" (the recommendation) and "bookmark_ids" (array of referenced bookmark numbers). Be specific.
- bookmark_ids reference the [N] numbers in the list.

Here are the bookmarks:

{bookmark_text}"""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = message.content[0].text
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        analysis = json.loads(raw)
        return analysis, None
    except (json.JSONDecodeError, IndexError, KeyError) as e:
        return None, f"Failed to parse AI response: {e}"


def fetch_user_tweets(token, user_id):
    """Fetch user's own tweets with metrics."""
    tweets, cursor, api_error = [], None, None
    pages = 0
    while pages < 5:  # Max 500 tweets
        params = {
            "max_results": 100,
            "tweet.fields": "created_at,text,public_metrics,source",
            "exclude": "retweets,replies",
        }
        if cursor:
            params["pagination_token"] = cursor

        r = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        data = r.json()
        if "data" not in data:
            if not tweets:
                api_error = data
            break

        for t in data["data"]:
            m = t.get("public_metrics", {})
            tweets.append({
                "id": t.get("id", ""),
                "text": t.get("text", ""),
                "date": t.get("created_at", "")[:10],
                "likes": m.get("like_count", 0),
                "retweets": m.get("retweet_count", 0),
                "replies": m.get("reply_count", 0),
                "impressions": m.get("impression_count", 0),
                "url": f"https://twitter.com/i/status/{t.get('id', '')}",
            })

        cursor = data.get("meta", {}).get("next_token")
        if not cursor:
            break
        pages += 1

    return tweets, api_error


def analyze_tweets(tweets, username=""):
    """Analyze tweet performance and generate content suggestions."""
    if not CLAUDE_API_KEY:
        return None, "CLAUDE_API_KEY not configured"

    condensed = []
    for i, tw in enumerate(tweets, 1):
        engagement = tw['likes'] + tw['retweets'] + tw['replies']
        condensed.append(
            f"[{i}] ({tw['date']}) {tw['text'][:280]} "
            f"| Likes:{tw['likes']} RTs:{tw['retweets']} Replies:{tw['replies']} Impressions:{tw['impressions']}"
        )
    tweet_text = "\n".join(condensed)

    prompt = f"""Analyze these {len(tweets)} tweets from @{username}. Return ONLY valid JSON with this exact structure:

{{
  "summary": "2-3 sentence performance overview addressing the user as 'you'. Be specific about what's working and what's not.",
  "top_performers": [
    {{"id": 1, "title": "Short description", "why": "Why this tweet worked - be specific about the hook, format, or topic"}}
  ],
  "underperformers": [
    {{"id": 5, "title": "Short description", "why": "Why this didn't resonate - constructive feedback"}}
  ],
  "patterns": [
    {{"pattern": "What you noticed", "evidence": "Specific examples from the tweets", "recommendation": "How to use this insight"}}
  ],
  "content_suggestions": [
    {{"tweet": "Full draft tweet text ready to post (under 280 chars)", "based_on": [1, 5], "rationale": "Why this would work based on your data"}}
  ],
  "strategy": {{
    "best_topics": ["topic1", "topic2"],
    "avoid_topics": ["topic1"],
    "best_formats": ["What formats work (threads, single tweets, questions, etc.)"],
    "posting_advice": "Specific advice on frequency and timing based on the data"
  }}
}}

Rules:
- top_performers: Pick the 5-8 highest engagement tweets. Explain WHY they worked - the hook, the format, the topic, the timing.
- underperformers: Pick 3-5 low engagement tweets. Give constructive feedback on what could be improved.
- patterns: Identify 3-5 clear patterns. What topics get engagement? What formats? What hooks?
- content_suggestions: Generate 5-8 NEW tweet drafts that apply the successful patterns. Each must be under 280 characters, ready to post. Make them sound like @{username}'s voice based on the successful tweets.
- strategy: Concrete advice based on the data.
- Address the user directly as "you".
- tweet IDs reference the [N] numbers in the list.

Here are the tweets:

{tweet_text}"""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = message.content[0].text
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        analysis = json.loads(raw)
        return analysis, None
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
        cell.fill = hfill
        cell.font = hfont
        cell.alignment = Alignment(horizontal="center")

    for i, bm in enumerate(bookmarks, 1):
        ws.append([i, bm["text"], bm["name"], f"@{bm['username']}",
                   bm["date"], bm["likes"], bm["retweets"], bm["url"]])
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
    return render_template(
        "index.html",
        configured=configured,
        connected=bool(session.get("access_token")),
        username=session.get("username", ""),
        bookmarks=None,
    )


@app.route("/connect", methods=["POST"])
def connect():
    if not CLIENT_ID or not CLIENT_SECRET:
        return redirect("/")

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    session["verifier"] = verifier
    session["state"] = state

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": get_redirect_uri(),
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or state != session.get("state"):
        return render_template("index.html", configured=True, error="Authorization failed. Please try again.")

    token_data = exchange_code(code, session["verifier"])
    token = token_data.get("access_token")
    if not token:
        return render_template("index.html", configured=True, error=f"Token error: {token_data}")

    session["access_token"] = token
    uid, uname, name = get_me(token)
    session["user_id"] = uid
    session["username"] = uname
    session["name"] = name

    return redirect("/")


@app.route("/fetch")
def fetch():
    """Fetch bookmarks from X API (costs credits)."""
    token = session.get("access_token")
    uid = session.get("user_id")
    if not token or not uid:
        return redirect("/")

    bookmarks, api_error = fetch_all_bookmarks(token, uid)
    error = f"X API error: {api_error}" if api_error else None
    return render_template(
        "index.html",
        configured=True,
        connected=True,
        username=session.get("username", ""),
        bookmarks=bookmarks,
        bookmarks_cache=encode_bookmarks(bookmarks) if bookmarks else None,
        error=error,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    """Analyze cached bookmarks with AI (no X API call)."""
    if not session.get("access_token"):
        return redirect("/")

    cached = request.form.get("bookmarks_cache", "")
    bookmarks = decode_bookmarks(cached)
    if not bookmarks:
        return redirect("/fetch")

    analysis, ai_error = analyze_bookmarks(bookmarks, session.get("username", ""))
    error = f"AI analysis error: {ai_error}" if ai_error else None
    return render_template(
        "index.html",
        configured=True,
        connected=True,
        username=session.get("username", ""),
        bookmarks=bookmarks,
        bookmarks_cache=cached,
        analysis=analysis,
        error=error,
    )


@app.route("/download", methods=["POST"])
def download():
    """Download cached bookmarks as Excel (no X API call)."""
    if not session.get("access_token"):
        return redirect("/")

    cached = request.form.get("bookmarks_cache", "")
    bookmarks = decode_bookmarks(cached)
    if not bookmarks:
        return redirect("/fetch")

    buf = build_excel(bookmarks)
    return send_file(buf, as_attachment=True,
                     download_name=f"bookmarks_{session.get('username', 'x')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/content")
def content():
    """Content engine dashboard."""
    if not session.get("access_token"):
        return redirect("/")
    return render_template(
        "content.html",
        connected=True,
        username=session.get("username", ""),
        tweets=None,
    )


@app.route("/content/fetch")
def content_fetch():
    """Fetch user's tweets from X API."""
    token = session.get("access_token")
    uid = session.get("user_id")
    if not token or not uid:
        return redirect("/")

    tweets, api_error = fetch_user_tweets(token, uid)
    error = f"X API error: {api_error}" if api_error else None
    return render_template(
        "content.html",
        connected=True,
        username=session.get("username", ""),
        tweets=tweets,
        tweets_cache=encode_bookmarks(tweets) if tweets else None,
        error=error,
    )


@app.route("/content/analyze", methods=["POST"])
def content_analyze():
    """Analyze tweets with AI."""
    if not session.get("access_token"):
        return redirect("/")

    cached = request.form.get("tweets_cache", "")
    tweets = decode_bookmarks(cached)
    if not tweets:
        return redirect("/content/fetch")

    analysis, ai_error = analyze_tweets(tweets, session.get("username", ""))
    error = f"AI analysis error: {ai_error}" if ai_error else None
    return render_template(
        "content.html",
        connected=True,
        username=session.get("username", ""),
        tweets=tweets,
        tweets_cache=cached,
        analysis=analysis,
        error=error,
    )


@app.route("/content/compose")
def content_compose():
    """Compose a tweet or thread."""
    if not session.get("access_token"):
        return redirect("/")
    return render_template(
        "compose.html",
        connected=True,
        username=session.get("username", ""),
    )


@app.route("/content/ai-draft", methods=["POST"])
def content_ai_draft():
    """Generate a tweet or thread with AI."""
    if not session.get("access_token"):
        return redirect("/")

    idea = request.form.get("idea", "").strip()
    format_type = request.form.get("format", "tweet")
    if not idea or not CLAUDE_API_KEY:
        return redirect("/content/compose")

    username = session.get("username", "")
    if format_type == "thread":
        prompt = f"""Create a Twitter/X thread for @{username} about: {idea}

Return ONLY valid JSON:
{{
  "tweets": ["Tweet 1 text (under 280 chars)", "Tweet 2 text", "Tweet 3 text", ...]
}}

Rules:
- Write 4-8 tweets for the thread
- First tweet must hook the reader - make it compelling
- Each tweet under 280 characters
- Last tweet should be a call to action (follow, retweet, bookmark)
- Number each tweet (1/, 2/, etc.) at the start
- Write in a natural, conversational voice"""
    else:
        prompt = f"""Create a tweet for @{username} about: {idea}

Return ONLY valid JSON:
{{
  "tweets": ["The tweet text (under 280 chars)"]
}}

Rules:
- Under 280 characters
- Make it engaging with a strong hook
- Write in a natural, conversational voice
- Just one tweet in the array"""

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    try:
        raw = message.content[0].text
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)
        drafts = data.get("tweets", [])
    except Exception:
        drafts = []

    return render_template(
        "compose.html",
        connected=True,
        username=session.get("username", ""),
        drafts=drafts,
        idea=idea,
        format_type=format_type,
    )


@app.route("/content/post", methods=["POST"])
def content_post():
    """Post a tweet or thread to X."""
    token = session.get("access_token")
    if not token:
        return redirect("/")

    tweets_json = request.form.get("tweets", "[]")
    try:
        tweets = json.loads(tweets_json)
    except json.JSONDecodeError:
        return redirect("/content/compose")

    if not tweets:
        return redirect("/content/compose")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    posted = []
    reply_to = None
    for tweet_text in tweets:
        body = {"text": tweet_text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}

        r = requests.post(
            "https://api.twitter.com/2/tweets",
            headers=headers,
            json=body,
        )
        data = r.json()
        if "data" in data:
            reply_to = data["data"]["id"]
            posted.append(data["data"]["id"])
        else:
            return render_template(
                "compose.html",
                connected=True,
                username=session.get("username", ""),
                error=f"Post failed: {data}",
                drafts=tweets,
            )

    first_id = posted[0] if posted else ""
    return render_template(
        "compose.html",
        connected=True,
        username=session.get("username", ""),
        success=True,
        posted_url=f"https://twitter.com/{session.get('username', '')}/status/{first_id}",
        posted_count=len(posted),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=False, port=5000)
