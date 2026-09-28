#!/usr/bin/env python3
"""Mobile OS release tracker.

Pulls Apple/Android developer feeds and endoflife.date, classifies items with
simple rules, keeps an event store (data/events.json), regenerates an .ics
calendar (docs/os-calendar.ics), and emails a digest of anything new.

Standard library only. Env vars for email: GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_EMAIL.
Without them the script does a dry run and prints the digest (fails in GitHub Actions).
"""
import hashlib
import json
import os
import re
import smtplib
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DATA = ROOT / "data"
DOCS = ROOT / "docs"
ATOM = "{http://www.w3.org/2005/Atom}"


# ---------- fetching and parsing ----------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "os-update-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _text(el, tag):
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def _parse_date(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):  # RSS
        link = _text(it, "link")
        items.append({
            "title": _text(it, "title"),
            "url": link,
            "id": _text(it, "guid") or link,
            "date": _parse_date(_text(it, "pubDate")),
        })
    for en in root.iter(ATOM + "entry"):  # Atom
        link = ""
        for l in en.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        items.append({
            "title": _text(en, ATOM + "title"),
            "url": link,
            "id": _text(en, ATOM + "id") or link,
            "date": _parse_date(_text(en, ATOM + "published") or _text(en, ATOM + "updated")),
        })
    return items


# ---------- classification ----------

def _phase(title, bare_is_ga):
    t = title.lower()
    if "platform stability" in t:
        return "platform_stability", None
    if "public beta" in t:
        return "public_beta", None
    if "release candidate" in t or re.search(r"\brc\b", t):
        return "rc", None
    if "developer preview" in t:
        n = re.search(r"preview\s*(\d+)", t)
        return "dev_preview", n.group(1) if n else None
    if re.search(r"\bbeta\b", t):
        n = re.search(r"beta\s*(\d+)", t)
        return "beta", n.group(1) if n else None
    if re.search(r"\b(released|now available|stable release)\b", t):
        return "ga", None
    if bare_is_ga:
        return "ga", None
    return None, None


def classify(feed, item):
    title = item["title"]
    m = re.search(feed["os_pattern"], title, re.I)
    if m:
        version = m.group(1)
        q = re.search(r"QPR\s*(\d+)", title, re.I)
        if q:
            version += f" QPR{q.group(1)}"
        phase, n = _phase(title, feed.get("bare_version_is_ga", False))
        if phase:
            key = "|".join([feed["os"], version, phase, n or ""])
            return {"key": key, "os": feed["os"], "version": version, "phase": phase,
                    "title": title, "url": item["url"], "date": item["date"],
                    "status": "proposed", "source": feed["name"]}
    if re.search(CONFIG["deadline_pattern"], title, re.I):
        h = hashlib.sha1(item["id"].encode()).hexdigest()[:10]
        return {"key": f"deadline|{h}", "os": feed["os"], "version": "", "phase": "deadline",
                "title": title, "url": item["url"], "date": item["date"],
                "status": "proposed", "source": feed["name"]}
    return None


def eol_events(product):
    """GA dates from endoflife.date (recent or upcoming major versions)."""
    data = json.loads(fetch(f"https://endoflife.date/api/{product['product']}.json"))
    cutoff = date.today() - timedelta(days=400)
    out = []
    for row in data:
        rd = row.get("releaseDate")
        if not rd:
            continue
        try:
            if date.fromisoformat(rd) < cutoff:
                continue
        except ValueError:
            continue
        v = str(row["cycle"])
        out.append({"key": "|".join([product["os"], v, "ga", ""]), "os": product["os"],
                    "version": v, "phase": "ga",
                    "title": f"{product['os']} {v} general release",
                    "url": f"https://endoflife.date/{product['product']}", "date": rd,
                    "status": "proposed", "source": "endoflife.date"})
    return out


def manual_events():
    out = []
    for m in CONFIG.get("manual_events", []):
        out.append({"key": "manual|" + hashlib.sha1((m["title"] + m["date"]).encode()).hexdigest()[:10],
                    "os": m.get("os", ""), "version": "", "phase": "manual",
                    "title": m["title"], "url": m.get("url", ""), "date": m["date"],
                    "status": "confirmed", "source": "manual"})
    return out


# ---------- outputs ----------

def action_for(ev):
    return CONFIG["actions"].get(ev["phase"], "")


def label(ev):
    if ev["phase"] in ("deadline", "manual"):
        return ev["title"]
    return ev["title"]


def _esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line):
    b = line.encode()
    if len(b) <= 75:
        return line
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def build_ics(events):
    cutoff = date.today() - timedelta(days=CONFIG["ics_lookback_days"])
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//os-update-tracker//EN",
             "CALSCALE:GREGORIAN", f"X-WR-CALNAME:{_esc(CONFIG['calendar_name'])}"]
    for ev in events:
        if not ev.get("date"):
            continue
        d = date.fromisoformat(ev["date"])
        if d < cutoff:
            continue
        uid = hashlib.sha1(ev["key"].encode()).hexdigest() + "@os-update-tracker"
        prefix = "[Proposed] " if ev["status"] == "proposed" else ""
        desc = f"{action_for(ev)}\n{ev['url']}".strip()
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}",
                  f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
                  f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
                  f"SUMMARY:{_esc(prefix + label(ev))}",
                  f"DESCRIPTION:{_esc(desc)}",
                  "STATUS:" + ("TENTATIVE" if ev["status"] == "proposed" else "CONFIRMED"),
                  "TRANSP:TRANSPARENT"]
        if ev["url"]:
            lines.append(f"URL:{ev['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def build_digest(new, reminders, warnings):
    parts = []
    if new:
        parts.append("NEW ITEMS\n")
        for ev in new:
            parts.append(f"- {label(ev)}  ({ev.get('date') or 'date unknown'}, {ev['status']})\n"
                         f"  Suggested action: {action_for(ev)}\n  {ev['url']}\n")
    if reminders:
        parts.append("\nUPCOMING DATES\n")
        for ev, days in reminders:
            parts.append(f"- {label(ev)} is in {days} day(s) ({ev['date']})\n"
                         f"  {action_for(ev)}\n")
    if warnings:
        parts.append("\nWARNINGS\n" + "\n".join(f"- {w}" for w in warnings) + "\n")
    parts.append("\nDates are proposed from public feeds. Verify against the source link before scheduling work.")
    return "\n".join(parts)


def send_email(subject, body):
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("ALERT_EMAIL")
    if not (user and pw and to):
        if os.environ.get("GITHUB_ACTIONS"):
            raise RuntimeError("Missing GMAIL_USER / GMAIL_APP_PASSWORD / ALERT_EMAIL secrets")
        print(f"--- DRY RUN (no email creds) ---\nSubject: {subject}\n\n{body}\n")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


# ---------- main ----------

def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main():
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    first_run = not (DATA / "events.json").exists()
    events = {e["key"]: e for e in load_json(DATA / "events.json", [])}
    state = load_json(DATA / "state.json", {"failures": {}, "reminded": []})
    new, warnings = [], []

    def add(ev):
        if ev["key"] not in events:
            events[ev["key"]] = ev
            new.append(ev)

    def record_failure(name, err):
        n = state["failures"].get(name, 0) + 1
        state["failures"][name] = n
        print(f"WARN: {name} failed ({n} in a row): {err}", file=sys.stderr)
        if n == 3:
            warnings.append(f"Source '{name}' has failed 3 days in a row: {err}")

    for feed in CONFIG["feeds"]:
        try:
            for item in parse_feed(fetch(feed["url"])):
                ev = classify(feed, item)
                if ev:
                    add(ev)
            state["failures"][feed["name"]] = 0
        except Exception as e:
            record_failure(feed["name"], e)

    for prod in CONFIG["eol_products"]:
        try:
            for ev in eol_events(prod):
                add(ev)
            state["failures"]["eol:" + prod["product"]] = 0
        except Exception as e:
            record_failure("eol:" + prod["product"], e)

    for ev in manual_events():
        events[ev["key"]] = {**events.get(ev["key"], {}), **ev}  # config is source of truth

    # reminders for future-dated events
    reminders = []
    today = date.today()
    for ev in events.values():
        if not ev.get("date") or ev["phase"] not in ("manual", "ga", "rc", "platform_stability"):
            continue
        days = (date.fromisoformat(ev["date"]) - today).days
        for r in CONFIG["reminder_days"]:
            rk = f"{ev['key']}|{r}"
            if days == r and rk not in state["reminded"]:
                reminders.append((ev, days))
                state["reminded"].append(rk)

    ordered = sorted(events.values(), key=lambda e: e.get("date") or "")

    if first_run:
        subject = f"[OS Tracker] Initialized with {len(events)} events"
        body = ("The tracker ran for the first time and loaded existing items as a baseline "
                "(no individual alerts sent).\n\n" + build_digest([], reminders, warnings))
        send_email(subject, body)
    elif new or reminders or warnings:
        names = ", ".join(label(e) for e in new[:3]) + ("..." if len(new) > 3 else "")
        subject = f"[OS Tracker] {len(new)} new, {len(reminders)} upcoming" + (f": {names}" if names else "")
        send_email(subject, build_digest(new, reminders, warnings))
    else:
        print("No changes.")

    # save only after email succeeded so failures retry next run
    (DATA / "events.json").write_text(json.dumps(ordered, indent=2))
    (DATA / "state.json").write_text(json.dumps(state, indent=2))
    (DOCS / "os-calendar.ics").write_text(build_ics(ordered), newline="")


if __name__ == "__main__":
    main()
