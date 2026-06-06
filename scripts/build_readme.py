#!/usr/bin/env python3
"""
Render TEMPLATE.md -> README.md.

Fills {{ PLACEHOLDER }} tokens with live GitHub stats and replaces the
<!--START_SECTION:x-->...<!--END_SECTION:x--> blocks with generated content.

Stdlib only — no pip install needed in CI.

Environment:
  GITHUB_TOKEN            (provided automatically by GitHub Actions)
  GITHUB_REPOSITORY_OWNER (provided automatically; the username to report on)
  USERNAME                (optional override for local runs)
  BLOG_RSS_URL            (optional, e.g. https://lucamanfrin.it/rss.xml)
"""

import os
import re
import json
import datetime as dt
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "TEMPLATE.md")
OUTPUT = os.path.join(ROOT, "README.md")

TOKEN = os.environ.get("GITHUB_TOKEN", "")
USERNAME = (
    os.environ.get("USERNAME")
    or os.environ.get("GITHUB_REPOSITORY_OWNER")
    or "USERNAME"
)
BLOG_RSS_URL = os.environ.get("BLOG_RSS_URL", "").strip()

GRAPHQL = "https://api.github.com/graphql"


def gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL,
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "readme-builder",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "readme-builder"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- stats

def fetch_stats():
    """Return dict of stat placeholders. On any failure, fall back to '?'."""
    stats = {
        "ACCOUNT_AGE": "?",
        "COMMITS": "?",
        "ISSUES": "?",
        "PULL_REQUESTS": "?",
        "STARS": "?",
        "REPOSITORIES": "?",
        "REPOSITORIES_CONTRIBUTED_TO": "?",
        "COMMIT_STREAK": "?",
    }
    if not TOKEN:
        print("No GITHUB_TOKEN — leaving stats as '?'")
        return stats
    try:
        base = gql(
            """
            query($login:String!){
              user(login:$login){
                createdAt
                repositoriesContributedTo(first:1, contributionTypes:[COMMIT,PULL_REQUEST,ISSUE,REPOSITORY]){ totalCount }
                repositories(first:1, ownerAffiliations:OWNER, privacy:PUBLIC){ totalCount }
              }
            }
            """,
            {"login": USERNAME},
        )["user"]

        created = dt.datetime.fromisoformat(base["createdAt"].replace("Z", "+00:00"))
        now = dt.datetime.now(dt.timezone.utc)
        stats["ACCOUNT_AGE"] = max(1, (now - created).days // 365)
        stats["REPOSITORIES"] = base["repositories"]["totalCount"]
        stats["REPOSITORIES_CONTRIBUTED_TO"] = base["repositoriesContributedTo"]["totalCount"]

        # Lifetime contributions: sum per-year contributionsCollection windows.
        commits = issues = prs = 0
        for year in range(created.year, now.year + 1):
            frm = f"{year}-01-01T00:00:00Z"
            to = f"{year}-12-31T23:59:59Z"
            cc = gql(
                """
                query($login:String!,$from:DateTime!,$to:DateTime!){
                  user(login:$login){
                    contributionsCollection(from:$from,to:$to){
                      totalCommitContributions
                      restrictedContributionsCount
                      totalIssueContributions
                      totalPullRequestContributions
                    }
                  }
                }
                """,
                {"login": USERNAME, "from": frm, "to": to},
            )["user"]["contributionsCollection"]
            commits += cc["totalCommitContributions"] + cc["restrictedContributionsCount"]
            issues += cc["totalIssueContributions"]
            prs += cc["totalPullRequestContributions"]
        stats["COMMITS"] = commits
        stats["ISSUES"] = issues
        stats["PULL_REQUESTS"] = prs

        # Stars across owned repos (paginated).
        total_stars = 0
        cursor = None
        while True:
            page = gql(
                """
                query($login:String!,$cursor:String){
                  user(login:$login){
                    repositories(first:100, ownerAffiliations:OWNER, after:$cursor){
                      nodes{ stargazerCount }
                      pageInfo{ hasNextPage endCursor }
                    }
                  }
                }
                """,
                {"login": USERNAME, "cursor": cursor},
            )["user"]["repositories"]
            total_stars += sum(n["stargazerCount"] for n in page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        stats["STARS"] = total_stars

        # Commit streak from the contributions calendar (last 12 months).
        cal = gql(
            """
            query($login:String!){
              user(login:$login){
                contributionsCollection{
                  contributionCalendar{
                    weeks{ contributionDays{ date contributionCount } }
                  }
                }
              }
            }
            """,
            {"login": USERNAME},
        )["user"]["contributionsCollection"]["contributionCalendar"]
        days = []
        for w in cal["weeks"]:
            days.extend(w["contributionDays"])
        days.sort(key=lambda d: d["date"])
        streak = 0
        for d in reversed(days):
            if d["contributionCount"] > 0:
                streak += 1
            elif streak == 0:
                # today may have 0 contributions yet; skip the very last empty day
                continue
            else:
                break
        stats["COMMIT_STREAK"] = streak
    except Exception as e:  # noqa: BLE001
        print(f"Stats fetch failed ({e}); using '?' fallbacks")
    return stats


# ---------------------------------------------------------------- blog

def fetch_blog():
    if not BLOG_RSS_URL:
        return "_No feed configured yet — set `BLOG_RSS_URL` in the workflow to pull posts from [lucamanfrin.it](https://lucamanfrin.it)._"
    try:
        req = urllib.request.Request(BLOG_RSS_URL, headers={"User-Agent": "readme-builder"})
        with urllib.request.urlopen(req, timeout=30) as r:
            tree = ET.fromstring(r.read())
        items = tree.findall(".//item")[:5]
        lines = []
        for it in items:
            title = (it.findtext("title") or "Untitled").strip()
            link = (it.findtext("link") or "").strip()
            lines.append(f"- [{title}]({link})")
        return "\n".join(lines) if lines else "_No posts found in feed._"
    except Exception as e:  # noqa: BLE001
        print(f"Blog fetch failed ({e})")
        return "_Feed temporarily unavailable._"


# ---------------------------------------------------------------- activity

EVENT_VERB = {
    "PushEvent": "Pushed to",
    "PullRequestEvent": "Opened a PR in",
    "IssuesEvent": "Opened an issue in",
    "CreateEvent": "Created",
    "ForkEvent": "Forked",
    "WatchEvent": "Starred",
    "ReleaseEvent": "Released in",
    "IssueCommentEvent": "Commented in",
}


def fetch_activity():
    try:
        events = get_json(
            f"https://api.github.com/users/{USERNAME}/events/public?per_page=30"
        )
        lines = []
        seen = set()
        for ev in events:
            verb = EVENT_VERB.get(ev.get("type"))
            if not verb:
                continue
            repo = ev.get("repo", {}).get("name", "")
            key = (verb, repo)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {verb} [`{repo}`](https://github.com/{repo})")
            if len(lines) >= 5:
                break
        return "\n".join(lines) if lines else "_No recent public activity._"
    except Exception as e:  # noqa: BLE001
        print(f"Activity fetch failed ({e})")
        return "_Activity temporarily unavailable._"


# ---------------------------------------------------------------- render

def replace_section(text, name, content):
    pattern = re.compile(
        rf"(<!--START_SECTION:{name}-->).*?(<!--END_SECTION:{name}-->)",
        re.DOTALL,
    )
    return pattern.sub(rf"\1\n{content}\n\2", text)


def main():
    with open(TEMPLATE, encoding="utf-8") as f:
        text = f.read()

    text = text.replace("{{ USERNAME }}", USERNAME)

    for key, val in fetch_stats().items():
        text = text.replace(f"{{{{ {key} }}}}", f"{val:,}" if isinstance(val, int) else str(val))

    text = replace_section(text, "blog", fetch_blog())
    text = replace_section(text, "activity", fetch_activity())

    # Strip the editor-only HTML comment at the very top of the template.
    text = re.sub(r"^<!--.*?-->\s*", "", text, count=1, flags=re.DOTALL)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
