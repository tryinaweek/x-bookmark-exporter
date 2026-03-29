import os
import secrets
import hashlib
import base64
import urllib.parse
import io

import requests
from flask import Flask, render_template, request, redirect, session, send_file

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

app = Flask(__name__, template_folder="../templates")
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))

CLIENT_ID = os.environ.get("X_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
SCOPES = "bookmark.read tweet.read users.read offline.access"


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
    bookmarks, cursor = [], None
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

    return bookmarks


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
        bookmarks=session.get("bookmarks"),
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

    return redirect("/fetch")


@app.route("/fetch")
def fetch():
    token = session.get("access_token")
    uid = session.get("user_id")
    if not token or not uid:
        return redirect("/")

    bookmarks = fetch_all_bookmarks(token, uid)
    session["bookmarks"] = bookmarks
    return redirect("/")


@app.route("/download")
def download():
    bookmarks = session.get("bookmarks")
    if not bookmarks:
        return redirect("/")
    buf = build_excel(bookmarks)
    return send_file(buf, as_attachment=True,
                     download_name=f"bookmarks_{session.get('username', 'x')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=False, port=5000)
