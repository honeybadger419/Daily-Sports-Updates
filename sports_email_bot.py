"""
Daily Sports & Fantasy Email Bot
---------------------------------
Fetches:
  - NFL, NBA, NCAAF, NCAAB scores/upcoming games (ESPN public API, no key needed)
  - League news per sport (trades, injuries, roster moves)
  - Sleeper fantasy football league matchups (public API, no key needed)
  - Individual player watchlist: recent headlines, last game's stat line,
    and next scheduled game — all via ESPN's public (no-login) endpoints

Sends one HTML summary email each morning. Designed to run via GitHub Actions
cron, same pattern as a macro-news bot: secrets -> env vars -> script -> email.
"""

import os
import requests
from datetime import datetime, timedelta
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

# ---- PLAYER WATCHLIST ----
# Fill in the players you want tracked. No ESPN login/cookies needed for this —
# just each player's public ESPN athlete ID, their team abbreviation, and which
# sport/league they play in.
#
# How to find espn_id: go to the player's ESPN page, e.g.
#   https://www.espn.com/nfl/player/_/id/3139477/patrick-mahomes
#   -> espn_id is "3139477"
# team_abbr: ESPN's short team code, e.g. "KC" for Chiefs, "BUF" for Bills.
# sport/league: matches ESPN's URL scheme, e.g. ("football","nfl"),
#   ("basketball","nba"), ("football","college-football"),
#   ("basketball","mens-college-basketball")

PLAYERS_TO_TRACK = [
    # {"name": "Patrick Mahomes", "espn_id": "3139477", "team_abbr": "KC",
    #  "sport": "football", "league": "nfl"},
    # {"name": "Josh Allen", "espn_id": "3918298", "team_abbr": "BUF",
    #  "sport": "football", "league": "nfl"},
    # ... add all 22 here
]

SCOREBOARD_URLS = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard",
    "NCAAB": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard",
}

# ESPN's general "news" feed per league. Headlines here are a mix of trades,
# injuries, roster moves, and general team/player storylines — ESPN doesn't
# expose separate clean feeds for each category, so this is the practical
# single source that covers all of them.
NEWS_URLS = {
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news",
    "NCAAF": "https://site.api.espn.com/apis/site/v2/sports/football/college-football/news",
    "NCAAB": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/news",
}

NEWS_ITEMS_PER_LEAGUE = 5

# Used to filter NCAAF down to Top 25 / Big Ten games only (see ncaaf_filter below).
# ESPN team abbreviations, current Big Ten membership (18 teams incl. 2024 additions).
BIG_TEN_TEAMS = {
    "ILL", "IND", "IOWA", "MD", "MICH", "MSU", "MINN", "NEB", "NW",
    "OSU", "ORE", "PSU", "PUR", "RUTG", "UCLA", "USC", "WASH", "WIS",
}


# ---------------- SCORES (ESPN public scoreboard API) ----------------
def get_week_range():
    """Monday-Sunday of the current week, as YYYYMMDD strings, for the 'dates' param."""
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y%m%d"), sunday.strftime("%Y%m%d")


def ncaaf_filter(event):
    """Keep the game only if a Top 25 team or a Big Ten team is playing."""
    for c in event["competitions"][0]["competitors"]:
        rank = c.get("curatedRank", {}).get("current", 99)
        if rank and rank <= 25:
            return True
        abbr = c.get("team", {}).get("abbreviation", "")
        if abbr in BIG_TEN_TEAMS:
            return True
    return False


def get_scores(url, date_range=None, filter_fn=None):
    """Returns a list of formatted game strings for one league.

    date_range: optional (start, end) YYYYMMDD tuple to pull a whole week
                instead of just today (ESPN defaults to today only).
    filter_fn: optional function(event) -> bool to keep only matching games.
    """
    params = {}
    if date_range:
        params["dates"] = f"{date_range[0]}-{date_range[1]}"

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [f"⚠️ Could not fetch data ({e})"]

    games = []
    for event in data.get("events", []):
        if filter_fn and not filter_fn(event):
            continue

        name = event.get("shortName", event.get("name", "Unknown matchup"))
        status = event["status"]["type"]["shortDetail"]
        competitors = event["competitions"][0]["competitors"]

        line = name
        if event["status"]["type"]["state"] != "pre":
            scores = {c["homeAway"]: c.get("score", "0") for c in competitors}
            line = f"{name} — {scores.get('away','?')}-{scores.get('home','?')} ({status})"
        else:
            line = f"{name} — {status}"
        games.append(line)

    return games or ["No games scheduled."]


# ---------------- NEWS: TRADES, INJURIES, ROSTER/PLAYER UPDATES ----------------
def get_news(url):
    """Pulls top headlines (trades, injuries, roster moves, player storylines)."""
    try:
        resp = requests.get(url, params={"limit": NEWS_ITEMS_PER_LEAGUE}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [f"⚠️ Could not fetch news ({e})"]

    lines = []
    for article in data.get("articles", [])[:NEWS_ITEMS_PER_LEAGUE]:
        headline = article.get("headline", "").strip()
        description = article.get("description", "").strip()
        link = ""
        links = article.get("links", {}).get("web", {}).get("href")
        if links:
            link = f' — <a href="{links}">read more</a>'
        if headline:
            snippet = f" — {description}" if description else ""
            lines.append(f"<b>{headline}</b>{snippet}{link}")

    return lines or ["No major headlines right now."]


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


# ---------------- PLAYER WATCHLIST: NEWS + RECENT STATS + NEXT GAME ----------------
def get_all_news_articles():
    """Fetch raw news articles once per league so player search doesn't refetch per player."""
    articles_by_league = {}
    for league, url in NEWS_URLS.items():
        try:
            resp = requests.get(url, params={"limit": 25}, timeout=10)
            resp.raise_for_status()
            articles_by_league[league] = resp.json().get("articles", [])
        except Exception:
            articles_by_league[league] = []
    return articles_by_league


def find_player_news(player_name, articles_by_league, max_items=2):
    matches = []
    for articles in articles_by_league.values():
        for article in articles:
            text = f"{article.get('headline','')} {article.get('description','')}"
            if player_name.lower() in text.lower():
                headline = article.get("headline", "").strip()
                link = article.get("links", {}).get("web", {}).get("href", "")
                link_html = f' — <a href="{link}">read more</a>' if link else ""
                matches.append(f"{headline}{link_html}")
            if len(matches) >= max_items:
                break
        if len(matches) >= max_items:
            break
    return matches or ["No recent headlines mentioning this player."]


def get_player_recent_stats(espn_id, sport, league):
    """Last logged game's stat line for a player, via ESPN's public gamelog endpoint."""
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/{sport}/{league}/athletes/{espn_id}/gamelog"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"⚠️ Could not fetch stats ({e})"

    events = data.get("events", {})
    season_types = data.get("seasonTypes", [])
    if not season_types:
        return "No games logged yet this season."

    try:
        latest_category = season_types[0]["categories"][0]
        latest_event_entry = latest_category["events"][-1]
        event_id = latest_event_entry["eventId"]
        stat_values = latest_event_entry["stats"]
        labels = latest_category.get("labels", [])
        event_meta = events.get(event_id, {})
        opponent = event_meta.get("opponent", {}).get("abbreviation", "")
        game_date = event_meta.get("gameDate", "")[:10]
        stat_line = ", ".join(
            f"{label}: {value}" for label, value in zip(labels, stat_values) if value not in ("0", "", None)
        )
        return f"{stat_line or 'No notable stats'} ({opponent}, {game_date})" if opponent else (stat_line or "No notable stats")
    except (KeyError, IndexError):
        return "No recent game log available."


def get_player_next_game(team_abbr, sport, league):
    """Finds the team's next scheduled game from ESPN's team schedule endpoint."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team_abbr}/schedule"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"⚠️ Could not fetch schedule ({e})"

    for event in data.get("events", []):
        state = event.get("competitions", [{}])[0].get("status", {}).get("type", {}).get("state")
        if state == "pre":
            opponent = event.get("shortName", "Unknown matchup")
            date = event.get("date", "")[:10]
            return f"{opponent} on {date}"
    return "No upcoming game found."


def build_player_watchlist_section(players):
    if not players:
        return {}

    articles_by_league = get_all_news_articles()
    section = {}
    for p in players:
        news = find_player_news(p["name"], articles_by_league)
        recent = get_player_recent_stats(p["espn_id"], p["sport"], p["league"])
        next_game = get_player_next_game(p["team_abbr"], p["sport"], p["league"])
        lines = [
            "<b>News:</b> " + " | ".join(news),
            f"<b>Last game:</b> {recent}",
            f"<b>Next game:</b> {next_game}",
        ]
        section[p["name"]] = lines
    return section


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
    recipients = [addr.strip() for addr in EMAIL_TO.split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Sports Digest — {datetime.now().strftime('%b %d, %Y')}"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)   # just for display in the email header
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())  # actual recipient list


def main():
    sections = {}
    week_start, week_end = get_week_range()

    for league, url in SCOREBOARD_URLS.items():
        # NFL and NCAAF play once a week, mostly on weekends — pull the whole
        # week's games instead of just today, or off-days would show nothing.
        date_range = (week_start, week_end) if league in ("NFL", "NCAAF") else None
        filter_fn = ncaaf_filter if league == "NCAAF" else None

        sections[league] = get_scores(url, date_range=date_range, filter_fn=filter_fn)
        sections[f"{league} News (Trades, Injuries, Roster Moves)"] = get_news(NEWS_URLS[league])

    if SLEEPER_LEAGUE_ID:
        sections["Sleeper Fantasy Football"] = get_sleeper_matchups(SLEEPER_LEAGUE_ID)

    player_sections = build_player_watchlist_section(PLAYERS_TO_TRACK)
    for player_name, lines in player_sections.items():
        sections[f"👤 {player_name}"] = lines

    html = build_email_html(sections)
    send_email(html)
    print("Email sent successfully.")


if __name__ == "__main__":
    main()
