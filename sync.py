#!/usr/bin/env python3
"""
Synchronise plusieurs agendas Google (adresses secrètes iCal) vers :
  1. docs/events.json  -> lu par la page calendrier (docs/index.html)
  2. ta base Notion "Calendrier" (optionnel, si NOTION_TOKEN est défini)
     avec une page par matière dans la base "Matières".

Variables d'environnement :
  CAL_URL_1, CAL_URL_2, ...   URL iCal de chaque agenda (voir config.json)
  NOTION_TOKEN                token de l'intégration Notion (optionnel)
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import recurring_ical_events
import requests
from icalendar import Calendar

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CONFIG["timezone"])
NOTION_VERSION = "2022-06-28"


# --------------------------------------------------------------------------
# Lecture des agendas
# --------------------------------------------------------------------------
def to_local_iso(value):
    """Retourne (iso_string, all_day) dans le fuseau configuré."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        return value.astimezone(TZ).isoformat(), False
    return value.isoformat(), True  # objet date -> journée entière


def fetch_calendar(cal_cfg, start, end):
    url = os.environ.get(cal_cfg["url_env"], "").strip()
    if not url:
        print(f"[!] {cal_cfg['url_env']} absent -> agenda « {cal_cfg['name']} » ignoré")
        return []
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.content)
    occurrences = recurring_ical_events.of(cal).between(start, end)

    events = []
    for ev in occurrences:
        s_iso, all_day = to_local_iso(ev.decoded("DTSTART"))
        if "DTEND" in ev:
            e_iso, _ = to_local_iso(ev.decoded("DTEND"))
        else:
            e_iso = s_iso
        uid = str(ev.get("UID", ""))
        # id unique par occurrence (les événements récurrents partagent l'UID)
        eid = f"{uid}|{s_iso}"
        events.append(
            {
                "id": eid,
                "title": str(ev.get("SUMMARY", "(sans titre)")).strip(),
                "start": s_iso,
                "end": e_iso,
                "allDay": all_day,
                "location": str(ev.get("LOCATION", "") or "").strip(),
                "description": str(ev.get("DESCRIPTION", "") or "").strip()[:500],
                "calendar": cal_cfg["name"],
                "color": cal_cfg["color"],
                "sync_to_notion": cal_cfg.get("sync_to_notion", False),
            }
        )
    print(f"[ok] {cal_cfg['name']}: {len(events)} événements")
    return events


# --------------------------------------------------------------------------
# Extraction matière / type depuis le titre
# --------------------------------------------------------------------------
def parse_subject(title):
    rules = CONFIG["subject_rules"]
    t = title.strip()
    kind = None
    for pat in rules["strip_prefixes"]:
        t = re.sub(pat, "", t, flags=re.I)
    m = re.match(rules["type_regex"], t, flags=re.I)
    if m:
        kind = m.group(1).upper()
    t = re.sub(rules["strip_type"], "", t, flags=re.I)
    cut = rules.get("cut_after")
    if cut and cut in t:
        t = t.split(cut)[0]
    t = re.sub(r"\s+", " ", t).strip(" -")
    if not t:
        return None, kind
    key = t.upper()
    name = rules["aliases"].get(key) or (t[:1].upper() + t[1:].lower())
    return name, kind


# --------------------------------------------------------------------------
# Notion
# --------------------------------------------------------------------------
class Notion:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def call(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, f"https://api.notion.com/v1{path}", timeout=60, **kw)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 1)) + 0.5)
                continue
            if not r.ok:
                raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError("Notion: trop de 429")

    def query_all(self, db_id, body=None):
        body = dict(body or {})
        out, cursor = [], None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            data = self.call("POST", f"/databases/{db_id}/query", json=body)
            out += data["results"]
            if not data.get("has_more"):
                return out
            cursor = data["next_cursor"]


def rich(prop):
    return "".join(x["plain_text"] for x in prop.get("rich_text", []))


def sync_notion(events, window_start, window_end):
    token = os.environ.get("NOTION_TOKEN", "").strip()
    cfg = CONFIG["notion"]
    if not token:
        print("[i] NOTION_TOKEN absent -> synchro Notion ignorée")
        return
    if "PUT_" in cfg["calendar_db_id"] or "PUT_" in cfg["subjects_db_id"]:
        print("[i] IDs de bases Notion non renseignés dans config.json -> synchro Notion ignorée")
        return

    P = cfg["props"]
    n = Notion(token)
    to_sync = [e for e in events if e["sync_to_notion"]]

    # --- matières existantes ---
    subjects = {}
    for page in n.query_all(cfg["subjects_db_id"]):
        title = "".join(x["plain_text"] for x in page["properties"][cfg["subjects_title_prop"]]["title"])
        if title:
            subjects[title] = page["id"]

    def subject_id(name):
        if name not in subjects:
            page = n.call(
                "POST",
                "/pages",
                json={
                    "parent": {"database_id": cfg["subjects_db_id"]},
                    "properties": {
                        cfg["subjects_title_prop"]: {"title": [{"text": {"content": name}}]}
                    },
                },
            )
            subjects[name] = page["id"]
            print(f"  + matière créée : {name}")
        return subjects[name]

    # --- pages existantes dans la fenêtre (par ID Google) ---
    existing = {}
    flt = {
        "filter": {
            "and": [
                {"property": P["google_id"], "rich_text": {"is_not_empty": True}},
                {"property": P["date"], "date": {"on_or_after": window_start.date().isoformat()}},
                {"property": P["date"], "date": {"on_or_before": window_end.date().isoformat()}},
            ]
        }
    }
    for page in n.query_all(cfg["calendar_db_id"], flt):
        gid = rich(page["properties"][P["google_id"]])
        if gid:
            existing[gid] = page

    def build_props(ev):
        subject, kind = parse_subject(ev["title"])
        props = {
            P["title"]: {"title": [{"text": {"content": ev["title"][:200]}}]},
            # le décalage horaire (+04:00) est déjà dans l'ISO : ne pas envoyer time_zone (Notion refuse les deux)
            P["date"]: {"date": {"start": ev["start"], "end": ev["end"] if ev["end"] != ev["start"] else None}},
            P["google_id"]: {"rich_text": [{"text": {"content": ev["id"][:1900]}}]},
            P["calendar_select"]: {"select": {"name": ev["calendar"]}},
        }
        if kind:
            props[P["type_select"]] = {"select": {"name": kind}}
        if ev["location"]:
            props[P["location"]] = {"rich_text": [{"text": {"content": ev["location"][:1900]}}]}
        if subject:
            props[P["subject_relation"]] = {"relation": [{"id": subject_id(subject)}]}
        return props

    created = updated = archived = 0
    seen = set()
    for ev in to_sync:
        seen.add(ev["id"])
        page = existing.get(ev["id"])
        props = build_props(ev)
        if page is None:
            n.call("POST", "/pages", json={"parent": {"database_id": cfg["calendar_db_id"]}, "properties": props})
            created += 1
        else:
            old = page["properties"][P["date"]]["date"] or {}
            old_title = "".join(x["plain_text"] for x in page["properties"][P["title"]]["title"])
            changed = (old.get("start") != ev["start"]) or (old_title != ev["title"][:200])
            if changed:
                n.call("PATCH", f"/pages/{page['id']}", json={"properties": props})
                updated += 1

    # événements supprimés de Google Agenda -> archivés dans Notion
    for gid, page in existing.items():
        if gid not in seen:
            n.call("PATCH", f"/pages/{page['id']}", json={"archived": True})
            archived += 1

    print(f"[notion] créés={created} mis à jour={updated} archivés={archived}")


# --------------------------------------------------------------------------
def main():
    now = datetime.now(TZ)
    start = now - timedelta(days=CONFIG["window_days_past"])
    end = now + timedelta(days=CONFIG["window_days_future"])

    all_events, failures = [], 0
    for cal_cfg in CONFIG["calendars"]:
        try:
            all_events += fetch_calendar(cal_cfg, start, end)
        except Exception as exc:  # un agenda en panne ne bloque pas les autres
            failures += 1
            print(f"[x] {cal_cfg['name']}: {exc}", file=sys.stderr)

    if not all_events and failures:
        sys.exit("Aucun agenda récupéré, on ne touche à rien.")

    all_events.sort(key=lambda e: e["start"])
    out = {
        "generated_at": now.isoformat(),
        "timezone": CONFIG["timezone"],
        "calendars": [{"name": c["name"], "color": c["color"]} for c in CONFIG["calendars"]],
        "events": [{k: v for k, v in e.items() if k != "sync_to_notion"} for e in all_events],
    }
    target = ROOT / "docs" / "events.json"
    unchanged = False
    if target.exists():
        try:
            prev = json.loads(target.read_text(encoding="utf-8"))
            unchanged = {k: v for k, v in prev.items() if k != "generated_at"} == \
                        {k: v for k, v in out.items() if k != "generated_at"}
        except Exception:
            pass
    if unchanged:
        print("[ok] events.json inchangé (pas de commit)")
    else:
        target.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[ok] docs/events.json écrit ({len(all_events)} événements)")

    try:
        sync_notion(all_events, start, end)
    except Exception as exc:
        print(f"[x] Notion: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
