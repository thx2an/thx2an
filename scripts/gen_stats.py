#!/usr/bin/env python3
"""Generate the GitHub stat cards embedded in README.md.

Talks to the GitHub GraphQL API and writes plain SVG into metrics/. Kept
dependency-free so the workflow needs nothing but the stdlib and a token.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from html import unescape
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"

# tokyonight, matching the theme the README used before.
BG = "#1a1b27"
TITLE = "#70a5fd"
TEXT = "#38bdae"
ICON = "#bf91f3"
MUTED = "#a9b1d6"
FONT = "'Segoe UI', Ubuntu, Helvetica, sans-serif"

# Fallback for languages GitHub reports without a colour.
DEFAULT_LANG_COLOR = "#858585"


def query(token, gql, variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": gql, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "thx2an-profile-stats",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub API returned {exc.code}: {exc.read().decode()[:200]}")
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'])[:200]}")
    return payload["data"]


PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    name
    login
    createdAt
    followers { totalCount }
    pullRequests { totalCount }
    issues { totalCount }
    contributionsCollection { contributionYears }
    repositoriesContributedTo(
      contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]
    ) { totalCount }
    repositories(
      first: 100
      after: $cursor
      ownerAffiliations: OWNER
      isFork: false
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 12, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def fetch_profile(token, login):
    """Page through owned repos, accumulating stars and language bytes."""
    stars = 0
    languages = {}
    cursor = None
    base = None

    while True:
        user = query(token, PROFILE_QUERY, {"login": login, "cursor": cursor})["user"]
        if user is None:
            sys.exit(f"No such GitHub user: {login}")
        if base is None:
            base = user

        repos = user["repositories"]
        for repo in repos["nodes"]:
            stars += repo["stargazerCount"]
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                entry = languages.setdefault(
                    name, {"size": 0, "color": edge["node"]["color"] or DEFAULT_LANG_COLOR}
                )
                entry["size"] += edge["size"]

        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]

    base["_stars"] = stars
    base["_languages"] = languages
    return base


def fetch_contributions(token, login, years):
    """One aliased query covering every year the account has contributed."""
    fields = []
    variables = {"login": login}
    for year in years:
        fields.append(
            f'y{year}: contributionsCollection(from: $from{year}, to: $to{year}) {{'
            " totalCommitContributions"
            " contributionCalendar { weeks { contributionDays { date contributionCount } } }"
            " }"
        )
        variables[f"from{year}"] = f"{year}-01-01T00:00:00Z"
        variables[f"to{year}"] = f"{year}-12-31T23:59:59Z"

    params = "".join(f", $from{y}: DateTime!, $to{y}: DateTime!" for y in years)
    gql = f'query($login: String!{params}) {{ user(login: $login) {{ {" ".join(fields)} }} }}'

    user = query(token, gql, variables)["user"]

    commits = 0
    days = {}
    for year in years:
        block = user[f"y{year}"]
        commits += block["totalCommitContributions"]
        for week in block["contributionCalendar"]["weeks"]:
            for day in week["contributionDays"]:
                days[day["date"]] = day["contributionCount"]
    return commits, days


DAY_CELL = re.compile(
    r'data-date="(\d{4}-\d{2}-\d{2})"\s+id="(contribution-day-component-[\d-]+)"'
)
TOOLTIP = re.compile(
    r'<tool-tip[^>]*\sfor="(contribution-day-component-[\d-]+)"[^>]*>([^<]*)<'
)
LEADING_INT = re.compile(r"^\s*([\d,]+)\s")


def fetch_contributions_public(login, years):
    """Calendar straight off the public profile page — needs no token at all.

    Used when the API token cannot read contributionsCollection, which is a
    real possibility for the Actions GITHUB_TOKEN. Returns commits as None
    because the page reports contributions, not commits specifically.
    """
    days = {}
    for year in years:
        url = (
            f"https://github.com/users/{login}/contributions"
            f"?from={year}-01-01&to={year}-12-31"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "thx2an-profile-stats"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            page = resp.read().decode("utf-8", "replace")

        by_id = {cell_id: day for day, cell_id in DAY_CELL.findall(page)}
        for cell_id, text in TOOLTIP.findall(page):
            day = by_id.get(cell_id)
            if not day:
                continue
            label = unescape(text).strip()
            match = LEADING_INT.match(label)
            days[day] = int(match.group(1).replace(",", "")) if match else 0

    return None, days


def streaks(days):
    """Current and longest run of consecutive contributing days.

    Today never breaks a streak: a day with no commits yet is still in
    progress, so the current run is measured back from yesterday instead.
    """
    empty = {
        "total": 0, "current": 0, "longest": 0, "first": None,
        "current_start": None, "current_end": None,
        "longest_start": None, "longest_end": None,
    }
    if not days:
        return empty

    ordered = sorted(days)
    today = date.today()

    longest = run = 0
    run_start = longest_start = longest_end = None
    previous = None
    for key in ordered:
        day = date.fromisoformat(key)
        if days[key] > 0:
            if previous and day - previous == timedelta(days=1):
                run += 1
            else:
                run, run_start = 1, day
            if run > longest:
                longest, longest_start, longest_end = run, run_start, day
            previous = day
        else:
            previous, run, run_start = None, 0, None

    current = 0
    cursor = today
    if days.get(cursor.isoformat(), 0) == 0:
        cursor -= timedelta(days=1)
    current_end = cursor
    while days.get(cursor.isoformat(), 0) > 0:
        current += 1
        cursor -= timedelta(days=1)

    active = [k for k in ordered if days[k] > 0]
    if not active:
        return empty

    return {
        "total": sum(days.values()),
        "current": current,
        "longest": longest,
        "first": active[0],
        "current_start": (cursor + timedelta(days=1)).isoformat() if current else None,
        "current_end": current_end.isoformat() if current else None,
        "longest_start": longest_start.isoformat() if longest_start else None,
        "longest_end": longest_end.isoformat() if longest_end else None,
    }


def pretty(value):
    if value >= 1000:
        trimmed = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{trimmed}k"
    return str(value)


def pretty_date(iso):
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).strftime("%b %d, %Y")


def frame(width, height, body, label):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
        f'<rect width="{width}" height="{height}" rx="6" fill="{BG}"/>'
        f"{body}</svg>\n"
    )


def overview_card(user, commits, streak):
    rows = [
        ("Total Stars Earned", pretty(user["_stars"])),
        # commits is None when only the public calendar was available, which
        # counts contributions rather than commits — so label it honestly.
        ("Total Commits", pretty(commits)) if commits is not None
        else ("Total Contributions", pretty(streak["total"])),
        ("Total PRs", pretty(user["pullRequests"]["totalCount"])),
        ("Total Issues", pretty(user["issues"]["totalCount"])),
        ("Contributed to", pretty(user["repositoriesContributedTo"]["totalCount"])),
        ("Public Repos", pretty(user["repositories"]["totalCount"])),
    ]

    parts = [
        f'<text x="25" y="35" font-family="{FONT}" font-size="18" font-weight="600" '
        f'fill="{TITLE}">{escape(user["login"])}\'s GitHub Stats</text>'
    ]

    y = 70
    for label, value in rows:
        parts.append(
            f'<circle cx="30" cy="{y - 4}" r="3.5" fill="{ICON}"/>'
            f'<text x="46" y="{y}" font-family="{FONT}" font-size="13" fill="{MUTED}">{label}</text>'
            f'<text x="300" y="{y}" font-family="{FONT}" font-size="13" font-weight="700" '
            f'fill="{TEXT}" text-anchor="end">{value}</text>'
        )
        y += 22

    # Contribution ring on the right, filled by progress toward the best streak.
    ratio = min(streak["current"] / streak["longest"], 1.0) if streak["longest"] else 0.0
    radius = 38
    circumference = 2 * 3.14159265 * radius
    offset = circumference * (1 - ratio)
    parts.append(
        f'<g transform="translate(390, 108)">'
        f'<circle r="{radius}" fill="none" stroke="{ICON}" stroke-opacity="0.25" stroke-width="6"/>'
        f'<circle r="{radius}" fill="none" stroke="{TITLE}" stroke-width="6" stroke-linecap="round" '
        f'stroke-dasharray="{circumference:.1f}" stroke-dashoffset="{offset:.1f}" '
        f'transform="rotate(-90)"/>'
        f'<text y="-2" font-family="{FONT}" font-size="22" font-weight="700" fill="{TEXT}" '
        f'text-anchor="middle">{streak["current"]}</text>'
        f'<text y="16" font-family="{FONT}" font-size="10" fill="{MUTED}" '
        f'text-anchor="middle">day streak</text>'
        f"</g>"
    )

    return frame(480, 200, "".join(parts), f"{user['login']}'s GitHub stats")


def languages_card(languages, limit=8):
    ranked = sorted(languages.items(), key=lambda kv: kv[1]["size"], reverse=True)[:limit]
    total = sum(entry["size"] for _, entry in ranked) or 1

    parts = [
        f'<text x="25" y="35" font-family="{FONT}" font-size="18" font-weight="600" '
        f'fill="{TITLE}">Most Used Languages</text>'
    ]

    # Stacked proportion bar.
    bar_x, bar_w = 25, 430
    x = bar_x
    for index, (name, entry) in enumerate(ranked):
        width = bar_w * entry["size"] / total
        if index == len(ranked) - 1:
            width = bar_x + bar_w - x
        parts.append(
            f'<rect x="{x:.2f}" y="55" width="{max(width, 0):.2f}" height="10" '
            f'fill="{entry["color"]}"/>'
        )
        x += width

    # Two-column legend.
    for index, (name, entry) in enumerate(ranked):
        column, row = divmod(index, 4)
        cx = 32 + column * 220
        cy = 95 + row * 26
        share = 100 * entry["size"] / total
        parts.append(
            f'<circle cx="{cx}" cy="{cy - 4}" r="5" fill="{entry["color"]}"/>'
            f'<text x="{cx + 14}" y="{cy}" font-family="{FONT}" font-size="12" '
            f'fill="{MUTED}">{escape(name)}</text>'
            f'<text x="{cx + 190}" y="{cy}" font-family="{FONT}" font-size="12" '
            f'font-weight="600" fill="{TEXT}" text-anchor="end">{share:.1f}%</text>'
        )

    return frame(480, 200, "".join(parts), "Most used languages")


def span(start, end):
    """Human range for a streak; collapses to one date when it spans a day."""
    if not start or not end:
        return "—"
    if start == end:
        return pretty_date(start)
    return f"{pretty_date(start)} — {pretty_date(end)}"


def streak_card(user, streak):
    panels = [
        (pretty(streak["total"]), "Total Contributions",
         f'{pretty_date(streak["first"] or user["createdAt"])} — Present'),
        (str(streak["current"]), "Current Streak",
         span(streak["current_start"], streak["current_end"])),
        (str(streak["longest"]), "Longest Streak",
         span(streak["longest_start"], streak["longest_end"])),
    ]

    parts = []
    for index, (value, label, sub) in enumerate(panels):
        # Three panels across a 480px card: centres at 80 / 240 / 400.
        cx = 80 + index * 160
        parts.append(
            f'<text x="{cx}" y="62" font-family="{FONT}" font-size="30" font-weight="700" '
            f'fill="{TEXT}" text-anchor="middle">{value}</text>'
            f'<text x="{cx}" y="88" font-family="{FONT}" font-size="13" font-weight="600" '
            f'fill="{TITLE}" text-anchor="middle">{label}</text>'
            f'<text x="{cx}" y="110" font-family="{FONT}" font-size="10" fill="{MUTED}" '
            f'text-anchor="middle">{escape(sub)}</text>'
        )
        if index:
            parts.append(
                f'<line x1="{cx - 80}" y1="30" x2="{cx - 80}" y2="120" '
                f'stroke="{ICON}" stroke-opacity="0.3" stroke-width="1"/>'
            )

    return frame(480, 150, "".join(parts), "GitHub streak")


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set")
    login = os.environ.get("GITHUB_LOGIN", "thx2an")
    out = os.environ.get("OUTPUT_DIR", "metrics")

    try:
        user = fetch_profile(token, login)
    except RuntimeError as exc:
        sys.exit(f"Could not read profile: {exc}")

    years = user["contributionsCollection"]["contributionYears"]

    # The Actions GITHUB_TOKEN may not be allowed to read a user's
    # contributionsCollection; scrape the public calendar if so.
    try:
        commits, days = fetch_contributions(token, login, years)
        if not any(days.values()):
            raise RuntimeError("calendar came back empty")
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"contributions API unavailable ({exc}); using public calendar")
        commits, days = fetch_contributions_public(login, years)

    streak = streaks(days)

    os.makedirs(out, exist_ok=True)
    cards = {
        "overview.svg": overview_card(user, commits, streak),
        "languages.svg": languages_card(user["_languages"]),
        "streak.svg": streak_card(user, streak),
    }
    for name, svg in cards.items():
        path = os.path.join(out, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        print(f"wrote {path} ({len(svg)} bytes)")


if __name__ == "__main__":
    main()
