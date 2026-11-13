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


def easter(year):
    """Dimanche de Pâques (algorithme grégorien anonyme)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def french_holidays(year, reunion=False):
    p = easter(year)
    days = {
        date(year, 1, 1): "Jour de l'An",
        p + timedelta(days=1): "Lundi de Pâques",
        date(year, 5, 1): "Fête du Travail",
        date(year, 5, 8): "Victoire 1945",
        p + timedelta(days=39): "Ascension",
        p + timedelta(days=50): "Lundi de Pentecôte",
        date(year, 7, 14): "Fête nationale",
        date(year, 8, 15): "Assomption",
        date(year, 11, 1): "Toussaint",
        date(year, 11, 11): "L'Armistice",
        date(year, 12, 25): "Noël",
    }
    if reunion:
        days[date(year, 12, 20)] = "Abolition de l'esclavage"
    return days


def builtin_holidays(cal_cfg, start, end):
    events = []
    for year in range(start.year, end.year + 1):
        for d, name in french_holidays(year, cal_cfg.get("reunion", False)).items():
            if start.date() <= d <= end.date():
                events.append({
                    "id": f"holiday|{d.isoformat()}", "title": name,
                    "start": d.isoformat(), "end": d.isoformat(), "allDay": True,
                    "location": "", "description": "", "calendar": cal_cfg["name"],
                    "color": cal_cfg["color"], "sync_to_notion": False,
                })
    print(f"[ok] {cal_cfg['name']}: {len(events)} jours fériés (intégrés)")
    return events


def fetch_calendar(cal_cfg, start, end):
    if cal_cfg.get("builtin") == "holidays_fr":
        return builtin_holidays(cal_cfg, start, end)
    url = os.environ.get(cal_cfg["url_env"], "").strip()
    if not url:
        print(f"[!] {cal_cfg['url_env']} absent -> agenda « {cal_cfg['name']} » ignoré")
        return []
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    if not resp.content.lstrip().startswith(b"BEGIN:VCALENDAR"):
        raise RuntimeError(
            f"{cal_cfg['url_env']} ne renvoie pas un fichier iCal (page web reçue). "
            "Utilise « Adresse secrète au format iCal » : Paramètres de l'agenda -> Intégrer l'agenda."
        )
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
    def __init__(self, token, version=NOTION_VERSION):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
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

    def query_all(self, db_id, body=None, kind="databases"):
        body = dict(body or {})
        out, cursor = [], None
        while True:
            if cursor:
                body["start_cursor"] = cursor
            data = self.call("POST", f"/{kind}/{db_id}/query", json=body)
            out += data["results"]
            if not data.get("has_more"):
                return out
            cursor = data["next_cursor"]


def same_time(a, b):
    """Compare deux dates ISO même si leur écriture diffère (.000, Z, +04:00)."""
    if not a or not b:
        return a == b
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return a == b


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
            changed = (not same_time(old.get("start"), ev["start"])) \
                or (not same_time(old.get("end"), ev["end"] if ev["end"] != ev["start"] else None)) \
                or (old_title != ev["title"][:200])
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
# Animes : chaque nouvel épisode vu dans Notion devient une entrée du calendrier
# --------------------------------------------------------------------------
def _when(value):
    """ISO Notion -> datetime locale (avec heure) ou date (jour seul)."""
    if not value:
        return None
    if len(value) <= 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def sync_animes(now):
    cfg = CONFIG.get("animes") or {}
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not cfg.get("enabled") or not token or "PUT_" in cfg.get("data_source_id", "PUT_"):
        return []

    path = ROOT / "docs" / "anime_log.json"
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        store = {"state": {}, "log": []}
    if any("start" not in e for e in store["log"]):   # ancien format (jour seul) -> on repart propre
        store = {"state": {}, "log": []}
    before = json.dumps(store, sort_keys=True)
    state, log = store["state"], store["log"]

    P = cfg["props"]
    minutes = int(cfg.get("episode_minutes", 24))
    # les bases à plusieurs sources (animes + jeux) exigent l'API 2025-09-03
    pages = Notion(token, "2025-09-03").query_all(cfg["data_source_id"], kind="data_sources")

    seed_from = now - timedelta(days=7)

    def day_of(w):
        return w.date() if isinstance(w, datetime) else w

    def add_entry(pid, title, when, first, last):
        start = when.isoformat()                      # datetime avec heure, ou date seule
        eid = f"{pid}|{start}"
        for e in log:
            if e["id"] == eid:
                e.update(anime=title, first=min(e["first"], first), last=max(e["last"], last))
                return
        log.append({"id": eid, "anime": title, "start": start, "first": first, "last": last})

    for pg in pages:
        pr = pg["properties"]
        ep = (pr.get(P["episode"]) or {}).get("number")
        if ep is None:
            continue
        ep = int(ep)
        title = "".join(t["plain_text"] for t in pr[P["title"]]["title"]).strip() or "(sans titre)"
        lw = (pr.get(P["last_watched"]) or {}).get("date")
        when = _when(lw["start"]) if lw else None
        pid, prev = pg["id"], state.get(pg["id"])

        if prev is None:                       # anime vu pour la première fois par le script
            state[pid] = ep
            if ep > 0 and when and day_of(when) >= seed_from.date():
                add_entry(pid, title, when, ep, ep)
        elif ep > prev:                        # nouveaux épisodes vus
            recent = when is not None and day_of(when) >= (now - timedelta(days=2)).date()
            add_entry(pid, title, when if recent else now, prev + 1, ep)
            state[pid] = ep
        elif ep < prev:                        # compteur remis à zéro / corrigé
            state[pid] = ep

    keep_from = (now - timedelta(days=400)).date()
    store["log"] = log = [e for e in log if day_of(_when(e["start"])) >= keep_from]
    if json.dumps(store, sort_keys=True) != before:
        path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[animes] journal mis à jour ({len(log)} entrées)")
    else:
        print(f"[animes] rien de nouveau ({len(log)} entrées)")

    window_from = (now - timedelta(days=CONFIG["window_days_past"])).date()
    events = []
    for e in log:
        w = _when(e["start"])
        if day_of(w) < window_from:
            continue
        n = e["last"] - e["first"] + 1
        ep_txt = f"ép. {e['first']}" if n == 1 else f"ép. {e['first']}–{e['last']}"
        if isinstance(w, datetime):
            start, end, all_day = w.isoformat(), (w + timedelta(minutes=min(minutes * n, 240))).isoformat(), False
        else:
            start = end = w.isoformat(); all_day = True
        events.append({
            "id": "anime|" + e["id"], "title": f"{e['anime']} — {ep_txt}",
            "start": start, "end": end, "allDay": all_day,
            "location": "", "description": "Épisode(s) marqué(s) comme vu(s) dans Notion",
            "calendar": cfg["name"], "color": cfg["color"], "sync_to_notion": False,
        })
    return events


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

    try:
        all_events += sync_animes(now)
    except Exception as exc:  # les animes ne doivent jamais bloquer le reste
        print(f"[x] Animes: {exc}", file=sys.stderr)

    all_events.sort(key=lambda e: e["start"])
    out = {
        "generated_at": now.isoformat(),
        "timezone": CONFIG["timezone"],
        "calendars": [{"name": c["name"], "color": c["color"]} for c in CONFIG["calendars"]]
        + ([{"name": CONFIG["animes"]["name"], "color": CONFIG["animes"]["color"]}]
           if CONFIG.get("animes", {}).get("enabled") else []),
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
