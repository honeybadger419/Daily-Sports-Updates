"""
Daily Sports & Fantasy Email Bot
---------------------------------
Fetches:
  - NFL, NBA, NCAAF, NCAAB scores/upcoming games (ESPN public API, no key needed)
  - Sleeper fantasy football league matchups (public API, no key needed)
  - ESPN fantasy football league matchups (needs league ID; private leagues
    also need espn_s2 + SWID cookies)

Sends one HTML summary email each morning. Designed to run via GitHub Actions
cron, same pattern as a macro-news bot: secrets -> env vars -> script -> email.
"""

import os
import requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib

# ---------------- CONFIG (all pulled from environment / GitHub Secrets) ----------------
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ["EMAIL_TO"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]          # Gmail App Password (not your normal password)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))

SLEEPER_LEAGUE_ID = os.environ.get("SLEEPER_LEAGUE_ID", "")

ESPN_LEAGUE_ID = os.environ.get("ESPN_LEAGUE_ID", "")
ESPN_SEASON = int(os.environ.get("ESPN_SEASON", datetime.now().year))
ESPN_S2 = os.environ.get("ESPN_S2", "")                # only needed for PRIVATE ESPN leagues
ESPN_SWID = os.environ.get("ESPN_SWID", "")

SCOREBOARD_URLS = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "NCAAB": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
}


# ---------------- SCORES (ESPN public scoreboard API) ----------------
def get_scores(url):
    """Returns a list of formatted game strings for one league."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [f"⚠️ Could not fetch data ({e})"]

    games = []
    for event in data.get("events", []):
        name = event.get("shortName", event.get("name", "Unknown matchup"))
        status = event["status"]["type"]["shortDetail"]
        competitors = event["competitions"][0]["competitors"]

        # Sort so home/away display consistently, show score if game started
        line = name
        if event["status"]["type"]["state"] != "pre":
            scores = {c["homeAway"]: c.get("score", "0") for c in competitors}
            line = f"{name} — {scores.get('away','?')}-{scores.get('home','?')} ({status})"
        else:
            line = f"{name} — {status}"
        games.append(line)

    return games or ["No games scheduled today."]


# ---------------- SLEEPER FANTASY FOOTBALL ----------------
def get_sleeper_matchups(league_id):
    if not league_id:
        return []
    try:
        week = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=10).json()["week"]
        users = {u["user_id"]: u["display_name"] for u in
                  requests.get(f"https://api.sleeper.app/v1/league/{league_id}/users", timeout=10).json()}
        rosters = {r["roster_id"]: users.get(r["owner_id"], "Unknown") for r in
                   requests.get(f"https://api.sleeper.app/v1/league/{league_id}/rosters", timeout=10).json()}
        matchups = requests.get(
            f"https://api.sleeper.app/v1/league/{league_id}/matchups/{week}", timeout=10
        ).json()
    except Exception as e:
        return [f"⚠️ Could not fetch Sleeper data ({e})"]

    # Group by matchup_id (each has 2 teams)
    grouped = {}
    for m in matchups:
        grouped.setdefault(m["matchup_id"], []).append(m)

    lines = []
    for teams in grouped.values():
        if len(teams) == 2:
            t1, t2 = teams
            name1 = rosters.get(t1["roster_id"], "Team A")
            name2 = rosters.get(t2["roster_id"], "Team B")
            lines.append(f"{name1}: {t1.get('points', 0):.1f}  vs  {name2}: {t2.get('points', 0):.1f}")
    return lines or ["No Sleeper matchup data yet this week."]


# ---------------- ESPN FANTASY FOOTBALL ----------------
def get_espn_fantasy(league_id, season, espn_s2, swid):
    if not league_id:
        return []
    url = f"https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
    params = {"view": ["mMatchup", "mTeam"]}
    cookies = {}
    if espn_s2 and swid:
        cookies = {"espn_s2": espn_s2, "SWID": swid}  # required for private leagues

    try:
        resp = requests.get(url, params=params, cookies=cookies, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [f"⚠️ Could not fetch ESPN fantasy data ({e})"]

    teams = {t["id"]: t.get("location", "") + " " + t.get("nickname", "") for t in data.get("teams", [])}
    current_week = data.get("scoringPeriodId")
    lines = []
    for m in data.get("schedule", []):
        if m.get("matchupPeriodId") != current_week:
            continue
        home = m.get("home", {})
        away = m.get("away", {})
        home_name = teams.get(home.get("teamId"), "Team")
        away_name = teams.get(away.get("teamId"), "Team")
        home_pts = home.get("totalPoints", 0)
        away_pts = away.get("totalPoints", 0)
        lines.append(f"{away_name}: {away_pts:.1f}  vs  {home_name}: {home_pts:.1f}")

    return lines or ["No ESPN matchup data yet this week (offseason or week not started)."]


# ---------------- EMAIL BUILD + SEND ----------------
def build_email_html(sections):
    today = datetime.now().strftime("%A, %B %d, %Y")
    html = f"<h2>🏈🏀 Daily Sports Digest — {today}</h2>"
    for title, items in sections.items():
        html += f"<h3>{title}</h3><ul>"
        for item in items:
            html += f"<li>{item}</li>"
        html += "</ul>"
    return html


def send_email(html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Sports Digest — {datetime.now().strftime('%b %d, %Y')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())


def main():
    sections = {}
    for league, url in SCOREBOARD_URLS.items():
        sections[league] = get_scores(url)

    if SLEEPER_LEAGUE_ID:
        sections["Sleeper Fantasy Football"] = get_sleeper_matchups(SLEEPER_LEAGUE_ID)

    if ESPN_LEAGUE_ID:
        sections["ESPN Fantasy Football"] = get_espn_fantasy(
            ESPN_LEAGUE_ID, ESPN_SEASON, ESPN_S2, ESPN_SWID
        )

    html = build_email_html(sections)
    send_email(html)
    print("Email sent successfully.")


if __name__ == "__main__":
    main()
