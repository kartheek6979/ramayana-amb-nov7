#!/usr/bin/env python3
"""ALLU new-show watcher. One request per date; no automatic fetch retries."""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
TIME = re.compile(r"\b(\d{1,2}:\d{2}\s*[AP]M)\b", re.I)


def norm(value):
    return re.sub(r"\s+", " ", value).strip().upper()


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def text(self):
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children)


class Tree(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        if self.current.tag not in {"script", "style"}:
            self.current.children.append(data)


def parse_shows(html, date, cfg):
    root = Tree(html).root
    nodes = list(root.walk())
    if norm(cfg["venue_label"]) not in norm(root.text()):
        raise ValueError("Expected cinema name missing; refusing this response")
    # Cinema pages can silently display a different date. Require the selected
    # date control, not a date merely mentioned elsewhere in the document.
    expected = datetime.strptime(date, "%Y%m%d").strftime("%a%d%b").upper()
    selected = [re.sub(r"\s+", "", n.text()).upper() for n in nodes
                if n.attrs.get("aria-pressed") == "true"]
    if expected not in selected:
        raise ValueError("Selected date does not match requested date, or date markup changed")

    found = {}
    movie_links = [n for n in nodes if n.tag == "a"
                   and "/movies/" in n.attrs.get("href", "")
                   and re.fullmatch(cfg["movie_pattern"], " ".join(n.text().split()), re.I)]
    for title in movie_links:
        group = title.parent
        # Find the smallest ancestor containing this movie header and times.
        while group and group.parent:
            members = list(group.walk())
            buttons = [n for n in members if n.attrs.get("role") == "button"
                       and TIME.search(n.attrs.get("aria-label", "") + " " + n.text())]
            languages = [n for n in members if n.tag == "a"
                         and "languages=" in n.attrs.get("href", "")]
            titles = [n for n in members if n.tag == "a" and "/movies/" in n.attrs.get("href", "")]
            if buttons and languages and len(titles) == 1:
                break
            group = group.parent
        else:
            continue
        if not buttons or len(languages) != 1:
            raise ValueError("Ambiguous movie/language group")
        language = norm(languages[0].text())
        if language not in {norm(x) for x in cfg["languages"]}:
            continue
        label = norm(languages[0].parent.text())
        fmt = label.removeprefix(language).lstrip(" ,")
        if not fmt:
            raise ValueError("Movie format missing")
        for button in buttons:
            text = norm(button.text())
            match = TIME.search(text)
            if not match:
                raise ValueError("Show button time missing")
            clock = datetime.strptime(re.sub(r"\s+", "", match.group(1)), "%I:%M%p").strftime("%H:%M")
            screen = text[match.end():].strip(" ,")
            if not screen:
                raise ValueError("Screen/format label missing")
            show = {"time": clock, "language": language, "format": fmt, "screen": screen}
            key = json.dumps([clock, language, fmt, screen], separators=(",", ":"))
            found[key] = show
    if not found:
        raise ValueError("No supported Ramayana showtimes parsed; preserving saved baseline")
    return found


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_page(url):
    # Never log the request URL/exception: the ScraperAPI URL contains its key.
    key = os.environ["SCRAPERAPI_KEY"]
    try:
        response = requests.get("https://api.scraperapi.com/", params={
            "api_key": key, "country_code": "in", "url": url}, timeout=100)
    except requests.RequestException:
        raise RuntimeError("ScraperAPI connection/timeout failure") from None
    if response.status_code != 200:
        raise RuntimeError(f"ScraperAPI HTTP {response.status_code}")
    return response.text


def telegram(message):
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat, "text": message,
                                       "disable_web_page_preview": True}, timeout=30)
        ok = response.status_code == 200 and response.json().get("ok") is True
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram delivery failed") from None
    if not ok:
        raise RuntimeError(f"Telegram delivery failed (HTTP {response.status_code})")


def process_date(state, date, shows, cfg, notify, persist):
    dates = state["dates"]
    pretty = datetime.strptime(date, "%Y%m%d").strftime("%d %b %Y")
    url = cfg["url_template"].format(date=date)
    if date not in dates:
        # Baseline notices confirm monitoring; existing shows aren't new-show alerts.
        notify(f"ALLU monitoring started — {pretty}\n"
               f"Saved {len(shows)} existing Hindi/Telugu shows as baseline.\n"
               f"Future additions will be reported; Dolby Cinema highlighted.\n{url}")
        dates[date] = {"seen": shows}
        persist()
        return f"baseline saved: {len(shows)} shows"
    seen = dates[date]["seen"]
    additions = [(k, v) for k, v in shows.items() if k not in seen]
    additions.sort(key=lambda pair: ("DOLBY CINEMA" not in pair[1]["screen"], pair[1]["time"], pair[0]))
    # Send small batches and persist each successful delivery. A disappearing
    # show remains in seen, so reappearing later doesn't create a repeat alert.
    for start in range(0, len(additions), 8):
        batch = additions[start:start + 8]
        lines = [f"New ALLU shows listed — {pretty}"]
        for _, show in batch:
            badge = "★ DOLBY CINEMA | " if "DOLBY CINEMA" in show["screen"] else ""
            lines.append(f"{badge}{show['time']} | {show['language']} | {show['format']} | {show['screen']}")
        notify("\n".join(lines) + f"\nCheck seats and book: {url}")
        seen.update(dict(batch))
        persist()
    return f"{len(shows)} listed, {len(additions)} new"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "allu_config.json")
    parser.add_argument("--state", type=Path, default=ROOT / "allu_state.json")
    parser.add_argument("--html", type=Path, help="Parse a local HTML sample without network, Telegram or state writes")
    parser.add_argument("--date", help="Date for --html")
    args = parser.parse_args()
    cfg = read_json(args.config)
    if args.html:
        shows = parse_shows(args.html.read_text(encoding="utf-8"), args.date, cfg)
        print(json.dumps(list(shows.values()), indent=2))
        return 0
    for name in ("SCRAPERAPI_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(name):
            raise ValueError(f"Missing {name}")
    state = read_json(args.state)
    if state.get("schema") != 1 or not isinstance(state.get("dates"), dict):
        raise ValueError("Invalid ALLU state; expected schema 1 and dates object")
    today = datetime.now(IST).strftime("%Y%m%d")
    failures = 0
    for date in cfg["dates"]:
        if date < today:
            print(f"[{date}] past date skipped (India time)")
            continue
        try:
            html = fetch_page(cfg["url_template"].format(date=date))
            shows = parse_shows(html, date, cfg)
            result = process_date(state, date, shows, cfg, telegram,
                                  lambda: save_state(args.state, state))
            print(f"[{date}] {result}")
        except (RuntimeError, ValueError) as error:
            failures += 1
            print(f"[{date}] ERROR: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
