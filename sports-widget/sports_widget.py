"""Always-on-top desktop widget that tracks favorite sports teams.

Uses only the Python standard library (tkinter + urllib) and ESPN's public
JSON endpoints. Edit teams.json to choose teams.

Drag to move, right-click for menu (refresh / always-on-top / quit).
"""
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team}/schedule"
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates={date}"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "teams.json")
PINNED = os.path.join(HERE, "pinned.json")

LEAGUES = [
    ("NBA", "basketball", "nba"), ("WNBA", "basketball", "wnba"),
    ("NCAA Men's BB", "basketball", "mens-college-basketball"),
    ("NFL", "football", "nfl"), ("College Football", "football", "college-football"),
    ("MLB", "baseball", "mlb"), ("NHL", "hockey", "nhl"),
    ("Premier League", "soccer", "eng.1"), ("La Liga", "soccer", "esp.1"),
    ("Champions League", "soccer", "uefa.champions"), ("MLS", "soccer", "usa.1"),
    ("Boxing", "boxing", "boxing"),
]


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def fetch_schedule(entry):
    url = API.format(**entry)
    req = urllib.request.Request(url, headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _score(competitor):
    s = competitor.get("score")
    if isinstance(s, dict):
        return s.get("displayValue", s.get("value", ""))
    return "" if s is None else str(s)


def _parse_date(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def summarize_event(event, team_abbr):
    comp = event["competitions"][0]
    state = comp.get("status", {}).get("type", {}).get("state") or \
        event.get("status", {}).get("type", {}).get("state", "pre")
    detail = comp.get("status", {}).get("type", {}).get("shortDetail", "")
    me = opp = None
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        if team_abbr.lower() in (t.get("abbreviation", "").lower(), str(t.get("id", c.get("id", ""))).lower()):
            me = c
        else:
            opp = c
    if me is None or opp is None:
        # Fall back to the first/second competitor if abbreviation differs.
        cs = comp.get("competitors", [])
        if len(cs) != 2:
            return None
        me, opp = cs
    sep = "vs" if me.get("homeAway") == "home" else "@"
    ot = opp.get("team", {})
    opp_name = ot.get("displayName") or ot.get("abbreviation", "?")
    when = _parse_date(event.get("date"))
    if state == "pre":
        text = when.astimezone().strftime("%a %b %d %I:%M %p").replace(" 0", " ") if when else detail
        line = f"{sep} {opp_name}"
        return state, line, text
    ms, os_ = _score(me), _score(opp)
    result = ""
    if state == "post":
        try:
            result = "W " if float(ms) > float(os_) else ("L " if float(ms) < float(os_) else "T ")
        except ValueError:
            pass
    return state, f"{sep} {opp_name}", f"{result}{ms}-{os_}  {detail}"


# Per-sport team stats to show on live games: (label, candidate ESPN stat names).
STAT_SPECS = {
    "basketball": [("Fouls", ("fouls", "personalFouls", "teamFouls", "totalFouls")),
                   ("Reb", ("rebounds", "totalRebounds")), ("TO", ("turnovers", "totalTurnovers"))],
    "hockey": [("Hits", ("hits",)),
               ("PIM", ("penaltyMinutes", "penaltyMins"))],
    "soccer": [("Shots", ("totalShots", "shots")),
               ("On target", ("shotsOnTarget",)), ("Corners", ("wonCorners", "corners")),
               ("Fouls", ("foulsCommitted", "fouls")), ("Yellow", ("yellowCards",)), ("Red", ("redCards",))],
    "football": [("Yards", ("totalYards",)), ("TO", ("turnovers",))],
}


def _stat(c, names):
    for st in c.get("statistics", []) or []:
        if st.get("name") in names or st.get("abbreviation") in names:
            return st.get("displayValue", st.get("value"))
    return None


def _stat_lines(sport, comp):
    cs = comp.get("competitors", [])
    if len(cs) != 2:
        return []
    abbr = lambda c: c.get("team", {}).get("abbreviation", "")
    lines = []
    for label, names in STAT_SPECS.get(sport, []):
        vals = [_stat(c, names) for c in cs]
        if all(v is None for v in vals):
            continue
        lines.append(f"{label}: " + " · ".join(f"{abbr(c)} {v}" for c, v in zip(cs, vals) if v is not None))
    return lines


def situation_text(sport, comp):
    """Sport-specific live info: situation (down/possession, count/runners, power play) and team stats."""
    sit = comp.get("situation") or {}
    lines = []
    if sport == "football" and sit:
        parts = [sit.get("shortDownDistanceText") or sit.get("downDistanceText")]
        poss = str(sit.get("possession", ""))
        for c in comp.get("competitors", []):
            if poss and str(c.get("id", c.get("team", {}).get("id", ""))) == poss:
                parts.append(f'{c.get("team", {}).get("abbreviation", "")} ball')
        if sit.get("possessionText"):
            parts.append(sit["possessionText"])
        if sit.get("isRedZone"):
            parts.append("Red zone")
        lines.append(" · ".join(p for p in parts if p))
    elif sport == "baseball" and ("balls" in sit or "outs" in sit):
        bases = [n for n, k in (("1st", "onFirst"), ("2nd", "onSecond"), ("3rd", "onThird")) if sit.get(k)]
        lines.append("Runners: " + ", ".join(bases) if bases else "Bases empty")
        who = []
        for label, k in (("AB", "batter"), ("P", "pitcher")):
            a = (sit.get(k) or {}).get("athlete", sit.get(k) or {})
            if a.get("shortName") or a.get("displayName"):
                who.append(f'{label}: {a.get("shortName") or a.get("displayName")}')
        if who:
            lines.append(" · ".join(who))
    elif sport == "hockey":
        pp = sit.get("powerPlay") or sit.get("isPowerPlay")
        if pp:
            team = sit.get("powerPlayTeam") or (pp if isinstance(pp, str) else "")
            lines.append("Power play" + (f" ({team})" if team else ""))
        if sit.get("emptyNet"):
            lines.append("Empty net")
    lines += _stat_lines(sport, comp)
    return "\n".join(l for l in lines if l)


def _situation_graphic(sport, comp):
    """Data for the live-game graphic: baseball diamond or football field position."""
    sit = comp.get("situation") or {}
    if sport == "baseball" and ("onFirst" in sit or "outs" in sit):
        stype = comp.get("status", {}).get("type", {})
        half = str(stype.get("shortDetail") or stype.get("detail") or "").strip().lower()
        side = "away" if half.startswith("top") else "home" if half.startswith("bot") else ""
        batting = next((c for c in comp.get("competitors", []) if c.get("homeAway") == side), {})
        return {"kind": "baseball", "bases": [bool(sit.get(k)) for k in ("onFirst", "onSecond", "onThird")],
                "outs": int(sit.get("outs") or 0), "balls": int(sit.get("balls") or 0),
                "strikes": int(sit.get("strikes") or 0), "color": team_colors(batting, "#fbbf24") if batting else "#fbbf24"}
    if sport == "football" and sit:
        teams = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            teams[str(c.get("id", t.get("id", "")))] = t.get("abbreviation", "")
        poss = str(sit.get("possession", ""))
        off = teams.get(poss, "")
        defn = next((a for k, a in teams.items() if k != poss), "")
        x = None  # yards from the offense's own goal line (0-100), driving toward 100
        if sit.get("yardsToEndzone") is not None:
            x = 100 - int(sit["yardsToEndzone"])
        else:
            m = re.match(r"\s*([A-Za-z.]+)\s+(\d+)", str(sit.get("possessionText") or sit.get("downDistanceText") or ""))
            if m:
                n = int(m.group(2))
                x = n if off and m.group(1).upper() == off.upper() else 100 - n
        if x is None or not off:
            return None
        dist = sit.get("distance")
        first = min(100, x + int(dist)) if dist not in (None, "") else None
        off_c = next((c for c in comp.get("competitors", []) if str(c.get("id", c.get("team", {}).get("id", ""))) == poss), {})
        return {"kind": "football", "x": max(0, min(100, x)), "first": first, "off": off, "def": defn,
                "color": team_colors(off_c, "#34d399"),
                "red": bool(sit.get("isRedZone")) or x >= 80}
    return None


PERIODS = {  # (sport, league) -> (regulation periods, minutes per period, overtime minutes)
    ("basketball", "nba"): (4, 12, 5), ("basketball", "wnba"): (4, 10, 5),
    ("basketball", "mens-college-basketball"): (2, 20, 5), ("basketball", "womens-college-basketball"): (4, 10, 5),
    ("hockey", "nhl"): (3, 20, 5),
}


def _num(v):
    try:
        return float(str(v).replace("%", "").strip())
    except ValueError:
        return None


def _minute(text):
    m = re.match(r"\s*(\d+)", str(text or ""))
    return int(m.group(1)) if m else None


def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _valid_hex(c):
    return bool(c) and bool(re.fullmatch(r"#?[0-9a-fA-F]{6}", str(c)))


def _lum(h):
    r, g, b = _rgb(h)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _dist(a, b):
    return sum(abs(x - y) for x, y in zip(_rgb(a), _rgb(b)))


def _lighten(h, amt=0.45):
    return "#%02x%02x%02x" % tuple(int(c + (255 - c) * amt) for c in _rgb(h))


def team_colors(c, fallback):
    """Pick a readable-on-dark color for a competitor: primary, else alternate, else lightened primary."""
    t = c.get("team", {})
    cands = [("#" + str(x).lstrip("#")) for x in (t.get("color"), t.get("alternateColor")) if _valid_hex(x)]
    for h in cands:
        if _lum(h) >= 0.2:
            return h
    return _lighten(cands[0]) if cands else fallback


def tint_color(team):
    """Primary team color (alternate if the primary is nearly black), or None."""
    cands = [("#" + str(x).lstrip("#")) for x in (team.get("color"), team.get("alternateColor")) if _valid_hex(x)]
    for h in cands:
        if _lum(h) >= 0.08:
            return h
    return None


def blend(base, top, amt):
    return "#%02x%02x%02x" % tuple(int(b + (t - b) * amt) for b, t in zip(_rgb(base), _rgb(top)))


def home_tint(comp):
    home = next((c for c in comp.get("competitors", []) if c.get("homeAway") == "home"), None)
    return tint_color(home.get("team", {})) if home else None


def matchup_colors(cs, defaults=("#60a5fa", "#f59e0b")):
    """Colors for the two competitors, kept distinguishable from each other."""
    a, b = team_colors(cs[0], defaults[0]), team_colors(cs[1], defaults[1])
    if _dist(a, b) < 120:
        alt = [("#" + str(x).lstrip("#")) for x in (cs[1].get("team", {}).get("alternateColor"),) if _valid_hex(x)]
        b = next((h for h in alt if _dist(a, h) >= 120 and _lum(h) >= 0.2), defaults[1] if _dist(a, defaults[1]) >= 120 else defaults[0])
    return a, b


def _versus(label, comp, names):
    cs = comp.get("competitors", [])
    if len(cs) != 2:
        return None
    vals = [_num(_stat(c, names)) for c in cs]
    if None in vals or sum(vals) <= 0:
        return None
    ab = [c.get("team", {}).get("abbreviation", "") for c in cs]
    ca, cb = matchup_colors(cs)
    return {"kind": "versus", "label": label, "a_name": ab[0], "a": vals[0], "b_name": ab[1], "b": vals[1],
            "a_color": ca, "b_color": cb}


def situation_graphic(sport, comp, league=""):
    """Graphics for a live game: list of dicts, or None."""
    out = []
    status = comp.get("status", {})
    if (sport, league) in PERIODS or (sport in ("basketball", "hockey") and not league):
        n, mins, ot = PERIODS.get((sport, league), (4, 12, 5) if sport == "basketball" else (3, 20, 5))
        period, clock = int(status.get("period") or 0), status.get("clock")
        if period >= 1 and clock is not None:
            segs = max(n, period)
            fills = []
            for i in range(1, segs + 1):
                length = mins * 60 if i <= n else ot * 60
                if i < period:
                    fills.append(1.0)
                elif i == period:
                    fills.append(max(0.0, min(1.0, 1 - float(clock) / length)))
                else:
                    fills.append(0.0)
            name = (f"Q{period}" if sport == "basketball" and n == 4 else f"H{period}" if sport == "basketball"
                    else f"P{period}") if period <= n else ("OT" if period == n + 1 else f"{period - n}OT")
            out.append({"kind": "periods", "fills": fills, "reg": n, "label": f'{name} {status.get("displayClock", "")}'.strip()})
    if sport == "hockey":
        v = _versus("Shots on goal", comp, ("shotsOnGoal", "shotsTotal", "shots"))
        if v:
            out.append(v)
    if sport == "soccer":
        minute = _minute(status.get("displayClock"))
        if minute is not None:
            cs = comp.get("competitors", [])
            home = next((str(c.get("id", c.get("team", {}).get("id", ""))) for c in cs if c.get("homeAway") == "home"), "")
            events = []
            colors = dict(zip((str(c.get("id", c.get("team", {}).get("id", ""))) for c in cs), matchup_colors(cs))) if len(cs) == 2 else {}
            for d in comp.get("details", []) or []:
                kind = "goal" if d.get("scoringPlay") else "red" if d.get("redCard") else "yellow" if d.get("yellowCard") else None
                m = _minute((d.get("clock") or {}).get("displayValue"))
                if kind and m is not None:
                    tid = str((d.get("team") or {}).get("id", ""))
                    events.append({"min": m, "kind": kind, "home": tid == home, "color": colors.get(tid, "#ffffff")})
            out.append({"kind": "timeline", "minute": minute, "events": events})
        v = _versus("Possession", comp, ("possessionPct", "possession"))
        if v:
            out.append(v)
    core = _situation_graphic(sport, comp)
    if core:
        out.append(core)
    return out or None


def live_info(entry, event):
    """Fetch the scoreboard entry for a live game (schedule data lacks situation)."""
    today = datetime.now().astimezone().date()
    rng = f"{(today - timedelta(days=1)):%Y%m%d}-{today:%Y%m%d}"
    try:
        for e in fetch_scoreboard(entry["sport"], entry["league"], rng):
            if str(e.get("id")) == str(event.get("id")) and e.get("competitions"):
                comp = e["competitions"][0]
                return situation_text(entry["sport"], comp), situation_graphic(entry["sport"], comp, entry["league"])
    except Exception:
        pass
    return "", None


def pick_event(events, days=7):
    """Live game, else the next game within `days`; None if neither."""
    def state_of(e):
        return e["competitions"][0].get("status", {}).get("type", {}).get("state", "pre")

    events = sorted((e for e in events if e.get("competitions")), key=lambda e: e.get("date", ""))
    live = [e for e in events if state_of(e) == "in"]
    if live:
        return live[0]
    now = datetime.now(timezone.utc)
    for e in events:
        d = _parse_date(e.get("date"))
        if state_of(e) == "pre" and d and now <= d <= now + timedelta(days=days):
            return e
    return None


def _same_day(event, now_iso):
    d, n = _parse_date(event.get("date")), _parse_date(now_iso)
    return bool(d and n and d.astimezone().date() == n.astimezone().date())


def team_status(entry):
    data = fetch_schedule(entry)
    team = data.get("team", {})
    name = entry.get("label") or team.get("displayName") or entry["team"].upper()
    event = pick_event(data.get("events", []))
    if not event:
        return None
    s = summarize_event(event, entry["team"])
    if not s:
        return None
    state, line, detail = s
    info, graphic = live_info(entry, event) if state == "in" else ("", None)
    return {"name": name, "state": state, "line": line, "detail": detail, "info": info, "graphic": graphic,
            "_key": (entry["league"], str(event.get("id"))), "tint": tint_color(team)}


def fetch_all(entries):
    out = []
    for e in entries:
        try:
            r = team_status(e)
            if r:
                out.append(r)
        except Exception as ex:  # network / schema errors shouldn't kill the widget
            out.append({"name": e.get("label", e["team"].upper()), "state": "err",
                        "line": f'{e["sport"]}/{e["league"]}', "detail": str(ex)[:40]})
    return out


PLAYOFF_LEAGUES = [("NBA", "basketball", "nba"), ("NFL", "football", "nfl"), ("MLB", "baseball", "mlb"),
                   ("WNBA", "basketball", "wnba"), ("NHL", "hockey", "nhl")]


_POST_WORDS = ("wild card", "division series", "championship series", "world series", "finals",
               "semifinal", "round 1", "round 2", "conference", "playoff")


def is_postseason(e):
    season = e.get("season", {})
    if season.get("type") == 3 or "post" in str(season.get("slug", "")).lower():
        return True
    comp = (e.get("competitions") or [{}])[0]
    note = " ".join(n.get("headline", "") for n in comp.get("notes", [])).lower()
    return any(w in note for w in _POST_WORDS)


def playoff_games(debug=False, days=7):
    """Postseason games: live and today's, plus the latest result per matchup from the last `days` days."""
    today = datetime.now().astimezone().date()
    rng = f"{(today - timedelta(days=days)):%Y%m%d}-{today:%Y%m%d}"
    best = {}  # matchup -> (priority, date, row); live > scheduled today > latest completed
    for name, sport, league in PLAYOFF_LEAGUES:
        try:
            events = fetch_scoreboard(sport, league, rng)
        except Exception as ex:
            if debug:
                print(f"{name}: error {ex}")
            continue
        if debug:
            print(f"{name}: {len(events)} events, season types {sorted({str(e.get('season', {}).get('type')) for e in events})}")
        for e in events:
            if not e.get("competitions") or not is_postseason(e):
                continue
            summ = summarize_game(e)
            if not summ:
                continue
            state, matchup, detail = summ
            d = _parse_date(e.get("date"))
            local = d.astimezone() if d else None
            if state == "in":
                prio = 0
            elif state == "pre":
                if not (local and local.date() == today):
                    continue
                prio = 1
            else:
                prio = 2
                if local:
                    detail += f" · {local:%b} {local.day}"
            comp = e["competitions"][0]
            note = (comp.get("notes") or [{}])[0].get("headline", "")
            series = comp.get("series", {}).get("summary", "")
            extra = " · ".join(x for x in (note, series) if x)
            row = {"name": matchup, "state": state, "line": name + (f" · {extra}" if extra else ""),
                   "_key": (league, str(e.get("id"))), "tint": home_tint(comp),
                   "league": name, "extra": extra,
                   "detail": detail, "_date": e.get("date", ""),
                   "info": situation_text(sport, comp) if state == "in" else "",
                   "graphic": situation_graphic(sport, comp, league) if state == "in" else None}
            teams = frozenset((league, str(c.get("team", {}).get("id", c.get("id", "")))) for c in comp.get("competitors", []))
            row["_teams"] = teams
            key = (league, teams)
            cur = best.get(key)
            # lower priority number wins; within completed games the most recent wins
            if cur is None or prio < cur[0] or (prio == cur[0] == 2 and row["_date"] > cur[1]):
                best[key] = (prio, row["_date"], row)
    rows = sorted((v for v in best.values()), key=lambda v: (v[0], v[1] if v[0] < 2 else ""))
    # Completed games: hide a game if either team has a more recent completed game (newest first).
    seen, done = set(), []
    for v in sorted((v for v in rows if v[0] == 2), key=lambda v: v[1], reverse=True):
        if not (v[2]["_teams"] & seen):
            done.append(v[2])
        seen |= v[2]["_teams"]
    return [v[2] for v in rows if v[0] < 2] + done


STATE = os.path.join(HERE, "state.json")
REFRESH_CHOICES = [("15 seconds", 15), ("30 seconds", 30), ("1 minute", 60), ("2 minutes", 120),
                   ("5 minutes", 300), ("10 minutes", 600), ("15 minutes", 900)]


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def load_pinned():
    try:
        with open(PINNED, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_pinned(pins):
    with open(PINNED, "w", encoding="utf-8") as f:
        json.dump(pins, f, indent=2)


def _get_scoreboard(sport, league, date, limit):
    url = SCOREBOARD.format(sport=sport, league=league, date=date)
    if limit:
        url += f"&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    # Some responses only carry the season at league level; copy it onto events.
    lg_season = (data.get("leagues") or [{}])[0].get("season", {})
    events = data.get("events", [])
    for e in events:
        e.setdefault("season", lg_season if lg_season.get("type") else data.get("season", {}))
    return events


def fetch_scoreboard(sport, league, date):
    """Scoreboard events for a YYYYMMDD date or YYYYMMDD-YYYYMMDD range.

    ESPN answers HTTP 400 to parameter combinations it dislikes, so fall back:
    range + limit -> range alone -> one request per day.
    """
    try:
        return _get_scoreboard(sport, league, date, 300)
    except urllib.error.HTTPError as ex:
        if ex.code != 400:
            raise
    try:
        return _get_scoreboard(sport, league, date, None)
    except urllib.error.HTTPError as ex:
        if ex.code != 400 or "-" not in date:
            raise
    start, end = (datetime.strptime(d, "%Y%m%d") for d in date.split("-"))
    events, seen = [], set()
    for i in range((end - start).days + 1):
        day = f"{start + timedelta(days=i):%Y%m%d}"
        for e in _get_scoreboard(sport, league, day, None):
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                events.append(e)
    return events


def summarize_game(event):
    """Neutral summary (away @ home) for a pinned game."""
    comp = event["competitions"][0]
    status = comp.get("status", {}).get("type", {})
    state, detail = status.get("state", "pre"), status.get("shortDetail", "")
    comps = comp.get("competitors", [])
    cs = {c.get("homeAway"): c for c in comps}
    home, away = cs.get("home"), cs.get("away")
    if (not home or not away) and len(comps) == 2:  # fights: no home/away
        home, away = comps[1], comps[0]
    if not home or not away:
        return None
    def ab(c):
        a = c.get("athlete", {})
        t = c.get("team", {})
        return t.get("displayName") or t.get("abbreviation") or a.get("displayName") or a.get("shortName", "?")
    if state == "pre":
        when = _parse_date(event.get("date"))
        text = when.astimezone().strftime("%a %b %d %I:%M %p").replace(" 0", " ") if when else detail
    else:
        text = f"{_score(away)}-{_score(home)}  {detail}"
    return state, f"{ab(away)} @ {ab(home)}", text


def pinned_status(pin):
    for e in fetch_scoreboard(pin["sport"], pin["league"], pin["date"]):
        if str(e.get("id")) == str(pin["id"]):
            s = summarize_game(e)
            if s:
                return {"name": pin["label"], "state": s[0], "line": s[1], "detail": s[2],
                        "_key": (pin["league"], str(pin["id"])), "tint": home_tint(e["competitions"][0]),
                        "info": situation_text(pin["sport"], e["competitions"][0]) if s[0] == "in" else "",
                        "graphic": situation_graphic(pin["sport"], e["competitions"][0], pin["league"]) if s[0] == "in" else None}
    return {"name": pin["label"], "state": "none", "line": "Game not found", "detail": ""}


def fetch_pinned(pins):
    out = []
    for p in pins:
        try:
            out.append(pinned_status(p))
        except Exception as ex:
            out.append({"name": p["label"], "state": "err", "line": "Unavailable", "detail": str(ex)[:40]})
    return out


TEAMS_API = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams?limit=1000"
SPORT_NAMES = {"college-football": "Football", "mens-college-basketball": "Men's Basketball",
               "womens-college-basketball": "Women's Basketball", "college-baseball": "Baseball",
               "college-softball": "Softball", "womens-college-volleyball": "Volleyball",
               "mens-college-volleyball": "Men's Volleyball", "mens-college-soccer": "Men's Soccer",
               "womens-college-soccer": "Women's Soccer", "mens-college-lacrosse": "Men's Lacrosse",
               "womens-college-lacrosse": "Women's Lacrosse", "mens-college-hockey": "Men's Hockey",
               "womens-college-hockey": "Women's Hockey", "womens-college-field-hockey": "Field Hockey"}
COLLEGE = [("football", "college-football"), ("basketball", "mens-college-basketball"),
           ("basketball", "womens-college-basketball"), ("baseball", "college-baseball"),
           ("baseball", "college-softball"), ("volleyball", "womens-college-volleyball"),
           ("volleyball", "mens-college-volleyball"), ("soccer", "mens-college-soccer"),
           ("soccer", "womens-college-soccer"), ("lacrosse", "mens-college-lacrosse"),
           ("lacrosse", "womens-college-lacrosse"), ("hockey", "mens-college-hockey"),
           ("hockey", "womens-college-hockey"), ("field-hockey", "womens-college-field-hockey")]


def find_teams(query, leagues=COLLEGE):
    """Search each league's team list for `query`; return teams.json-style entries."""
    q = query.lower()
    found = []
    for sport, league in leagues:
        try:
            req = urllib.request.Request(TEAMS_API.format(sport=sport, league=league),
                                         headers={"User-Agent": "sports-widget/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            teams = [t["team"] for lg in data["sports"][0]["leagues"] for t in lg["teams"]]
        except Exception as ex:
            print(f"  {sport}/{league}: skipped ({str(ex)[:40]})")
            continue
        for t in teams:
            if q in t.get("displayName", "").lower() or q == t.get("abbreviation", "").lower():
                kind = SPORT_NAMES.get(league, league)
                found.append({"sport": sport, "league": league, "team": str(t["id"]),
                              "label": f'{t.get("displayName") or t.get("abbreviation")} {kind}'})
    return found


def add_found(entries):
    cfg = load_config()
    have = {(t["league"], t["team"]) for t in cfg["teams"]}
    new = [e for e in entries if (e["league"], e["team"]) not in have]
    cfg["teams"].extend(new)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return new


def round_corners(root):
    """Windows 11: DWM rounded corners. Windows 10: clip window to a rounded region."""
    if sys.platform != "win32":
        return
    import ctypes
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)) == 0
    if ok:
        return

    def clip(_=None):
        w, h = root.winfo_width(), root.winfo_height()
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 16, 16)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    clip()
    root.bind("<Configure>", clip)


def run_gui():
    import tkinter as tk

    cfg = load_config()
    entries = cfg["teams"]
    default_refresh = int(cfg.get("refresh_seconds", 60))

    BG, FG, DIM = "#1e1e24", "#f2f2f2", "#9aa0a6"
    COLORS = {"in": "#34d399", "pre": DIM, "post": FG, "none": DIM, "err": "#f87171"}

    root = tk.Tk()
    root.title("Sports")
    root.configure(bg=BG)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    ui_state = load_state()

    def set_alpha(v):
        try:
            root.attributes("-alpha", max(0.3, min(1.0, float(v))))
        except tk.TclError:
            pass
    set_alpha(ui_state.get("opacity", 0.95))
    root.geometry("+40+40")

    hbar = tk.Frame(root, bg=BG)
    hbar.pack(fill="x", padx=12, pady=(8, 2))
    titles = tk.Frame(hbar, bg=BG)
    titles.pack(side="left", fill="x", expand=True)
    header = tk.Label(titles, text="Sports Tracker", bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w")
    header.pack(fill="x")
    stamp = tk.Label(titles, text="", bg=BG, fg=DIM, font=("Segoe UI", 8), anchor="w")
    stamp.pack(fill="x")
    view_btn = tk.Label(hbar, bg="#33333d", fg=FG, font=("Segoe UI", 8, "bold"), padx=7, pady=1, cursor="hand2")
    view_btn.pack(side="right", padx=(8, 0))
    # Resize grip (bottom-right) packed first so it stays visible; content scrolls above it.
    grip = tk.Label(root, text="\u25e2", bg=BG, fg=DIM, cursor="size_nw_se" if sys.platform == "win32" else "bottom_right_corner", font=("Segoe UI", 9))
    grip.pack(side="bottom", anchor="se", padx=2)
    container = tk.Frame(root, bg=BG)
    container.pack(fill="both", expand=True, padx=(12, 4), pady=(0, 0))
    container.grid_rowconfigure(0, weight=1)
    container.grid_columnconfigure(0, weight=1)
    canvas = tk.Canvas(container, bg=BG, highlightthickness=0, width=260, height=100)
    scroll = tk.Canvas(container, width=8, bg=BG, highlightthickness=0, cursor="arrow")
    sb = {"lo": 0.0, "hi": 1.0, "off": 0.0, "hover": False}

    def sb_draw():
        scroll.delete("all")
        h = scroll.winfo_height()
        y0, y1 = sb["lo"] * h, sb["hi"] * h
        if y1 - y0 < 24:
            y1 = y0 + 24
        color = "#8a8f98" if sb["hover"] else "#4a4a55"
        scroll.create_line(4, y0 + 3, 4, max(y1 - 3, y0 + 4), width=5, capstyle="round", fill=color)

    def sb_set(lo, hi):
        sb["lo"], sb["hi"] = float(lo), float(hi)
        sb_draw()

    def sb_press(e):
        h = max(scroll.winfo_height(), 1)
        if sb["lo"] * h <= e.y <= sb["hi"] * h:
            sb["off"] = e.y / h - sb["lo"]
        else:  # click in the trough: centre the thumb there
            sb["off"] = (sb["hi"] - sb["lo"]) / 2
            canvas.yview_moveto(e.y / h - sb["off"])

    def sb_drag(e):
        canvas.yview_moveto(e.y / max(scroll.winfo_height(), 1) - sb["off"])

    def sb_hover(on):
        sb["hover"] = on
        sb_draw()

    scroll.bind("<Button-1>", sb_press)
    scroll.bind("<B1-Motion>", sb_drag)
    scroll.bind("<Enter>", lambda e: sb_hover(True))
    scroll.bind("<Leave>", lambda e: sb_hover(False))
    scroll.bind("<Configure>", lambda e: sb_draw())
    canvas.configure(yscrollcommand=sb_set)
    canvas.grid(row=0, column=0, sticky="nsew")
    body = tk.Frame(canvas, bg=BG)
    body_id = canvas.create_window((0, 0), window=body, anchor="nw")
    user_sized = {"on": False}
    MIN_W, MIN_H = 240, 120

    def fit(_=None):
        """Keep scroll region in sync; auto-size to content until the user resizes."""
        canvas.configure(scrollregion=canvas.bbox("all"))
        if not user_sized["on"]:
            max_h = int(root.winfo_screenheight() * 0.7)
            canvas.configure(width=max(body.winfo_reqwidth(), 200), height=min(body.winfo_reqheight(), max_h))
        need = body.winfo_reqheight() > canvas.winfo_height()
        if need and not scroll.winfo_ismapped():
            scroll.grid(row=0, column=1, sticky="ns")
        elif not need and scroll.winfo_ismapped():
            scroll.grid_remove()
            canvas.yview_moveto(0)

    def on_canvas(e):
        canvas.itemconfigure(body_id, width=e.width)
        fit()

    body.bind("<Configure>", fit)
    canvas.bind("<Configure>", on_canvas)

    def wheel(e):
        if scroll.winfo_ismapped():
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    root.bind_all("<MouseWheel>", wheel)

    rs = {}
    def grip_start(e):
        rs.update(x=e.x_root, y=e.y_root, w=root.winfo_width(), h=root.winfo_height())
    def grip_move(e):
        user_sized["on"] = True
        w = max(MIN_W, rs["w"] + e.x_root - rs["x"])
        h = max(MIN_H, rs["h"] + e.y_root - rs["y"])
        root.geometry(f"{w}x{h}+{root.winfo_x()}+{root.winfo_y()}")
    def grip_reset(_):
        user_sized["on"] = False
        root.geometry("")
        root.geometry(f"+{root.winfo_x()}+{root.winfo_y()}")
        fit()
    VIEWS = [("full", "Full"), ("live", "Live"), ("title", "Title")]
    layout = {}

    def apply_layout():
        mode = ui_state.get("view", "full")
        view_btn.config(text=dict(VIEWS).get(mode, "Full"))
        if mode == "title":
            stamp.pack_forget()
        elif not stamp.winfo_manager():
            stamp.pack(fill="x")
        if mode == "title" and container.winfo_manager():
            width = root.winfo_width()
            grip.pack_forget()
            container.pack_forget()
            root.update_idletasks()
            root.geometry(f"{width}x{root.winfo_reqheight()}")
        elif mode != "title" and not container.winfo_manager():
            grip.pack(side="bottom", anchor="se", padx=2)
            container.pack(fill="both", expand=True, padx=(12, 4), pady=(0, 0))
            if not user_sized["on"]:
                root.geometry("")
            fit()

    def cycle_view(_=None):
        order = [m for m, _ in VIEWS]
        cur = ui_state.get("view", "full")
        ui_state["view"] = order[(order.index(cur) + 1) % len(order)] if cur in order else "full"
        save_state(ui_state)
        apply_layout()
        if last:
            render(*last["args"])
    view_btn.bind("<ButtonRelease-1>", cycle_view)
    grip.bind("<Button-1>", grip_start)
    grip.bind("<B1-Motion>", grip_move)
    grip.bind("<Double-Button-1>", grip_reset)

    topmost = tk.BooleanVar(value=True)
    pins = load_pinned()

    def section(title):
        tk.Label(body, text=title, bg=BG, fg=DIM, font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", pady=(6, 2))

    def draw_graphic(parent, g):
        if isinstance(g, list):
            for item in g:
                draw_graphic(parent, item)
            return
        bg = parent.cget("bg")
        if g["kind"] == "periods":
            W, H = 240, 24
            c = tk.Canvas(parent, width=W, height=H, bg=bg, highlightthickness=0)
            n, gap = len(g["fills"]), 3
            seg = (W - gap * (n - 1)) / n
            for i, f in enumerate(g["fills"]):
                x = i * (seg + gap)
                c.create_rectangle(x, 14, x + seg, 20, fill="#33333d", outline="")
                if f > 0:
                    c.create_rectangle(x, 14, x + seg * f, 20, fill="#34d399" if f < 1 else "#4a4a55", outline="")
            c.create_text(0, 6, text=g["label"], anchor="w", fill=FG, font=("Segoe UI", 8, "bold"))
            c.pack(anchor="w", pady=(2, 0))
        elif g["kind"] == "versus":
            W, H = 240, 30
            c = tk.Canvas(parent, width=W, height=H, bg=bg, highlightthickness=0)
            total = g["a"] + g["b"]
            split = W * g["a"] / total
            c.create_rectangle(0, 16, split, 24, fill=g.get("a_color", "#60a5fa"), outline="")
            c.create_rectangle(split, 16, W, 24, fill=g.get("b_color", "#f59e0b"), outline="")
            fmt = lambda v: f"{v:g}"
            c.create_text(0, 6, text=f'{g["a_name"]} {fmt(g["a"])}', anchor="w", fill=FG, font=("Segoe UI", 8))
            c.create_text(W / 2, 6, text=g["label"], fill=DIM, font=("Segoe UI", 8))
            c.create_text(W, 6, text=f'{fmt(g["b"])} {g["b_name"]}', anchor="e", fill=FG, font=("Segoe UI", 8))
            c.pack(anchor="w", pady=(2, 0))
        elif g["kind"] == "timeline":
            W, H = 240, 44
            span = 90 if g["minute"] <= 90 else 120
            px = lambda m: 6 + (W - 12) * min(m, span) / span
            c = tk.Canvas(parent, width=W, height=H, bg=bg, highlightthickness=0)
            c.create_line(6, 22, W - 6, 22, fill="#33333d", width=4, capstyle="round")
            c.create_line(6, 22, px(g["minute"]), 22, fill="#34d399", width=4, capstyle="round")
            for m in (45, 90):
                c.create_line(px(m), 17, px(m), 27, fill="#4a4a55")
            for ev in g["events"]:
                x, y = px(ev["min"]), 9 if ev["home"] else 35
                if ev["kind"] == "goal":
                    c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=ev.get("color", FG), outline=FG)
                else:
                    c.create_rectangle(x - 3, y - 4, x + 3, y + 4, fill="#fbbf24" if ev["kind"] == "yellow" else "#ef4444", outline="")
            c.create_oval(px(g["minute"]) - 4, 18, px(g["minute"]) + 4, 26, fill="#34d399", outline=FG)
            c.pack(anchor="w", pady=(2, 0))
        elif g["kind"] == "baseball":
            c = tk.Canvas(parent, width=130, height=44, bg=bg, highlightthickness=0)
            def base(cx, cy, on):
                r = 5
                hl = g.get("color", "#fbbf24")
                c.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy, fill=hl if on else bg,
                                 outline=hl if on else DIM, width=2)
            base(21, 15, g["bases"][1]); base(29, 23, g["bases"][0]); base(13, 23, g["bases"][2])
            # count: balls (0-3), strikes (0-2), outs (0-3)
            for row, (label, n, total, color) in enumerate((("B", min(g["balls"], 3), 3, "#34d399"),
                                                            ("S", min(g["strikes"], 2), 2, "#fbbf24"),
                                                            ("O", min(g["outs"], 3), 3, "#f87171"))):
                y = 8 + row * 14
                c.create_text(52, y, text=label, anchor="w", fill=DIM, font=("Segoe UI", 8, "bold"))
                for i in range(total):
                    on = i < n
                    c.create_oval(66 + i * 12, y - 4, 74 + i * 12, y + 4, fill=color if on else bg,
                                  outline=color if on else DIM, width=1)
            c.pack(anchor="w", pady=(2, 0))
        elif g["kind"] == "football":
            W, H = 240, 24
            c = tk.Canvas(parent, width=W, height=H, bg=bg, highlightthickness=0)
            px = lambda yd: W * yd / 100
            c.create_rectangle(0, 14, W, 20, fill="#33333d", outline="")
            if g["red"]:
                c.create_rectangle(px(80), 14, W, 20, fill="#7f3b3b", outline="")
            if g["first"] is not None:
                c.create_line(px(g["first"]), 11, px(g["first"]), 23, fill="#fbbf24", width=2)
            bx = px(g["x"])
            c.create_oval(bx - 5, 12, bx + 5, 22, fill=g.get("color", "#34d399"), outline=FG)
            c.create_text(0, 6, text=f"{g['off']} \u25b6", anchor="w", fill=FG, font=("Segoe UI", 8, "bold"))
            c.create_text(W, 6, text=g["def"], anchor="e", fill=DIM, font=("Segoe UI", 8, "bold"))
            c.pack(anchor="w", pady=(2, 0))

    def add_rows(rows):
        for r in rows:
            tint = r.get("tint")
            bgc = blend(BG, tint, 0.22) if tint else BG
            row = tk.Frame(body, bg=bgc, **({"padx": 8, "pady": 4} if tint else {}))
            row.pack(fill="x", pady=3)
            tk.Label(row, text=r["name"], bg=bgc, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            if r["line"]:
                tk.Label(row, text=r["line"], bg=bgc, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            tk.Label(row, text=r["detail"], bg=bgc, fg=COLORS.get(r["state"], FG),
                     font=("Segoe UI", 9, "bold" if r["state"] == "in" else "normal"), anchor="w").pack(fill="x")
            if r.get("graphic"):
                draw_graphic(row, r["graphic"])
            if r.get("info"):
                tk.Label(row, text=r["info"], bg=bgc, fg=DIM, font=("Segoe UI", 9), anchor="w", justify="left").pack(fill="x")

    last = {}
    session = {"live_prev": 0}  # in-session only: live games re-open themselves when they appear

    def toggle(key, was_open, persist=True):
        if persist:
            ui_state[key] = not was_open
            save_state(ui_state)
        else:
            session[key] = not was_open
        render(*last["args"])

    def render(results, pin_results, playoffs):
        last["args"] = (results, pin_results, playoffs)  # unfiltered, so view changes can re-render
        for w in body.winfo_children():
            w.destroy()
        live_view = ui_state.get("view", "full") == "live"
        if live_view:
            results = [r for r in results if r["state"] == "in"]
            pin_results = [r for r in pin_results if r["state"] == "in"]
            playoffs = [r for r in playoffs if r["state"] == "in"]
            if not (results or pin_results or playoffs):
                tk.Label(body, text="No live games", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", pady=4)
        if pin_results:
            section("Tracked Games")
            add_rows(pin_results)
        if results:
            section("My Teams")
            add_rows(results)
        elif not (pin_results or playoffs or live_view):
            tk.Label(body, text="No games in the next 7 days", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        if playoffs:
            section("Playoffs")
            live = [r for r in playoffs if r["state"] == "in"]
            upcoming = [r for r in playoffs if r["state"] == "pre"]
            previous = [r for r in playoffs if r["state"] == "post"]
            if live and not session["live_prev"]:
                session["live"] = True  # newly live games open automatically
            session["live_prev"] = len(live)

            def header_label(text, key, is_open, color, indent=0, persist=True):
                hdr = tk.Label(body, text=("\u25be " if is_open else "\u25b8 ") + text, bg=BG, fg=color,
                               font=("Segoe UI", 9, "bold"), anchor="w", cursor="hand2")
                hdr.pack(fill="x", pady=(4, 0), padx=(indent, 0))
                hdr.bind("<ButtonRelease-1>", lambda e: toggle(key, is_open, persist))

            def league_groups(rows, prefix):
                for league in dict.fromkeys(r["league"] for r in rows):
                    games = [r for r in rows if r["league"] == league]
                    key = f"{prefix}:{league}"
                    is_open = ui_state.get(key, False)
                    header_label(f"{league} · {len(games)}", key, is_open, FG, indent=14)
                    if is_open:
                        add_rows([dict(r, line=r.get("extra", "")) for r in games])

            if live:
                is_open = session.get("live", True)
                header_label(f"Live · {len(live)}", "live", is_open, COLORS["in"], persist=False)
                if is_open:
                    add_rows(live)
            for title, rows, key in (("Upcoming Today", upcoming, "upcoming"), ("Previous", previous, "previous")):
                if rows:
                    is_open = ui_state.get(key, False)
                    header_label(f"{title} · {len(rows)}", key, is_open, FG)
                    if is_open:
                        league_groups(rows, key)
        stamp.config(text="Last Refreshed " + datetime.now().strftime("%I:%M %p").lstrip("0"))

    def refresh():
        def work():
            res, pres, po = fetch_all(entries), fetch_pinned(list(pins)), playoff_games()
            shown = {r["_key"] for r in res + pres if r.get("_key")}
            po = [r for r in po if r.get("_key") not in shown]  # already listed above
            root.after(0, lambda: render(res, pres, po))
        threading.Thread(target=work, daemon=True).start()

    timer = {"id": None}

    def schedule():
        if timer["id"]:
            root.after_cancel(timer["id"])
        timer["id"] = root.after(int(ui_state.get("refresh_seconds", default_refresh)) * 1000, tick)

    def tick():
        refresh()
        schedule()

    def track_dialog():
        win = tk.Toplevel(root)
        win.title("Track a game")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        league = tk.StringVar(value=LEAGUES[0][0])
        date = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        games = []
        top = tk.Frame(win, bg=BG)
        top.pack(padx=10, pady=8)
        tk.OptionMenu(top, league, *[l[0] for l in LEAGUES]).grid(row=0, column=0)
        tk.Entry(top, textvariable=date, width=10).grid(row=0, column=1, padx=6)
        lb = tk.Listbox(win, width=44, height=12)
        lb.pack(padx=10)
        status = tk.Label(win, text="Date is YYYYMMDD", bg=BG, fg=DIM)
        status.pack(pady=4)

        def load():
            _, sport, lg = next(l for l in LEAGUES if l[0] == league.get())
            d = date.get().strip()
            status.config(text="Loading...")
            def work():
                try:
                    evs = fetch_scoreboard(sport, lg, d)
                    err = ""
                except Exception as ex:
                    evs, err = [], str(ex)[:50]
                def done():
                    games.clear()
                    lb.delete(0, "end")
                    for e in evs:
                        s = summarize_game(e)
                        if s:
                            games.append({"sport": sport, "league": lg, "date": d, "id": str(e["id"]),
                                          "label": s[1]})
                            lb.insert("end", f"{s[1]:<14} {s[2]}")
                    status.config(text=err or (f"{len(games)} games" if games else "No games"))
                root.after(0, done)
            threading.Thread(target=work, daemon=True).start()

        def add():
            for i in lb.curselection():
                g = games[i]
                if not any(p["id"] == g["id"] for p in pins):
                    pins.append(g)
            save_pinned(pins)
            win.destroy()
            refresh()

        tk.Button(top, text="Load", command=load).grid(row=0, column=2)
        tk.Button(win, text="Track selected", command=add).pack(pady=(0, 10))
        load()

    def settings_dialog():
        win = tk.Toplevel(root)
        win.title("Settings")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        tk.Label(win, text="Opacity", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, padx=14, pady=(14, 4), sticky="w")
        pct = tk.StringVar()
        val = tk.Spinbox(win, from_=30, to=100, width=4, textvariable=pct, justify="right", bg="#33333d", fg=FG,
                         insertbackground=FG, buttonbackground="#33333d", relief="flat", highlightthickness=0)
        val.grid(row=0, column=1, padx=14, pady=(14, 4), sticky="e")

        def on_scale(v):
            n = int(float(v))
            pct.set(str(n))
            set_alpha(n / 100)

        def on_release(_=None):
            ui_state["opacity"] = round(scale.get() / 100, 2)
            save_state(ui_state)

        def on_entry(_=None):
            try:
                n = max(30, min(100, int(float(pct.get().strip().rstrip("%")))))
            except ValueError:
                n = scale.get()
            scale.set(n)  # also applies the opacity through on_scale
            pct.set(str(n))
            on_release()

        val.bind("<Return>", on_entry)
        val.bind("<FocusOut>", on_entry)
        val.config(command=on_entry)

        scale = tk.Scale(win, from_=30, to=100, orient="horizontal", showvalue=False, length=240, command=on_scale,
                         bg=BG, fg=FG, troughcolor="#33333d", highlightthickness=0, bd=0, sliderrelief="flat",
                         activebackground="#8a8f98")
        scale.set(int(ui_state.get("opacity", 0.95) * 100))
        scale.grid(row=1, column=0, columnspan=2, padx=14, pady=(0, 8))
        scale.bind("<ButtonRelease-1>", on_release)
        tk.Label(win, text="Refresh every", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=2, column=0, padx=14, pady=(6, 4), sticky="w")
        cur = int(ui_state.get("refresh_seconds", default_refresh))
        choice = tk.StringVar(value=next((l for l, v in REFRESH_CHOICES if v == cur), f"{cur} seconds"))

        def on_refresh(label):
            ui_state["refresh_seconds"] = dict(REFRESH_CHOICES)[label]
            save_state(ui_state)
            schedule()  # restart the countdown with the new cadence

        om = tk.OptionMenu(win, choice, *[l for l, _ in REFRESH_CHOICES], command=on_refresh)
        om.config(bg="#33333d", fg=FG, activebackground="#44444f", activeforeground=FG, relief="flat",
                  highlightthickness=0, width=12)
        om["menu"].config(bg="#33333d", fg=FG)
        om.grid(row=2, column=1, padx=14, pady=(6, 4), sticky="e")
        tk.Button(win, text="Close", command=win.destroy, bg="#33333d", fg=FG, relief="flat",
                  activebackground="#44444f", activeforeground=FG).grid(row=3, column=1, padx=14, pady=(8, 14), sticky="e")
        win.update_idletasks()
        win.geometry(f"+{root.winfo_x() + 30}+{root.winfo_y() + 30}")

    def untrack_menu(event):
        m = tk.Menu(root, tearoff=0)
        for p in list(pins):
            m.add_command(label=f"Untrack {p['label']} ({p['date']})", command=lambda p=p: untrack(p))
        if not pins:
            m.add_command(label="No tracked games", state="disabled")
        m.tk_popup(event.x_root, event.y_root)

    def untrack(p):
        pins.remove(p)
        save_pinned(pins)
        refresh()

    # drag to move
    drag = {}
    def start(e):
        if e.widget not in (grip, scroll):  # grip resizes, scrollbar scrolls
            drag["x"], drag["y"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()
    def move(e):
        if e.widget not in (grip, scroll) and drag:
            root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Track a game...", command=track_dialog)
    menu.add_command(label="Untrack a game...", command=lambda: untrack_menu(menu_pos["e"]))
    menu.add_command(label="Refresh", command=refresh)
    menu.add_command(label="Settings...", command=settings_dialog)
    menu.add_checkbutton(label="Always on top", variable=topmost,
                         command=lambda: root.attributes("-topmost", topmost.get()))
    menu.add_separator()
    def restart():
        """Start a fresh copy of this script (picks up code changes from git pull), then close this one."""
        import subprocess
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], cwd=HERE)
        root.destroy()
    menu.add_command(label="Restart", command=restart)
    menu.add_command(label="Quit", command=root.destroy)
    menu_pos = {}
    def popup(e):
        menu_pos["e"] = e
        menu.tk_popup(e.x_root, e.y_root)

    # Bound on the toplevel, so every child widget (rows, labels) drags/pops up too.
    root.bind("<Button-1>", start)
    root.bind("<B1-Motion>", move)
    root.bind("<Button-3>", popup)
    root.update_idletasks()
    apply_layout()
    try:
        round_corners(root)
    except Exception:
        pass  # cosmetic only
    tick()
    root.mainloop()


TEAM_COLORS = {"SF": {"color": "aa0000", "alternateColor": "b3995d"}, "DAL": {"color": "041e42", "alternateColor": "869397"},
               "NJ": {"color": "ce1126", "alternateColor": "000000"}, "BOS": {"color": "000000", "alternateColor": "fdb71a"},
               "SFG": {"color": "fd5a1e", "alternateColor": "27251f"}, "LAD": {"color": "005a9c", "alternateColor": "ffffff"},
               "ARS": {"color": "ef0107", "alternateColor": "ffffff"}, "CHE": {"color": "034694", "alternateColor": "ffffff"}}


def demo_data():
    """Fake live games for every sport, built through the real graphic/text code paths."""
    st = lambda n, v: {"name": n, "displayValue": str(v)}
    team = lambda i, ha, a, score, stats=(): {"id": i, "homeAway": ha, "score": str(score), "statistics": list(stats),
                                              "team": {"id": i, "abbreviation": a, **TEAM_COLORS.get(a, {})}}
    def row(name, sport, league, line, detail, comp, tint=None):
        return {"name": name, "state": "in", "line": line, "detail": detail, "tint": tint,
                "info": situation_text(sport, comp), "graphic": situation_graphic(sport, comp, league)}
    nfl = {"competitors": [team("25", "away", "SF", 21), team("6", "home", "DAL", 17)],
           "situation": {"shortDownDistanceText": "3rd & 4", "possession": "25", "possessionText": "DAL 38", "distance": 4}}
    mlb = {"status": {"type": {"shortDetail": "Top 7th"}},
           "competitors": [team("1", "away", "SFG", 3), team("2", "home", "LAD", 2)],
           "situation": {"balls": 1, "strikes": 2, "outs": 2, "onFirst": True, "onThird": True,
                         "batter": {"athlete": {"shortName": "M. Chapman"}}, "pitcher": {"athlete": {"shortName": "T. Glasnow"}}}}
    nba = {"status": {"period": 3, "clock": 312.0, "displayClock": "5:12"},
           "competitors": [team("9", "away", "GS", 78, [st("fouls", 9), st("rebounds", 31)]),
                           team("2", "home", "BOS", 74, [st("fouls", 12), st("rebounds", 28)])]}
    nhl = {"status": {"period": 2, "clock": 407.0, "displayClock": "6:47"}, "situation": {"powerPlay": True, "powerPlayTeam": "NJ"},
           "competitors": [team("1", "away", "NJ", 2, [st("shotsOnGoal", 24)]), team("2", "home", "BOS", 1, [st("shotsOnGoal", 17)])]}
    soc = {"status": {"displayClock": "67'"},
           "details": [{"scoringPlay": True, "clock": {"displayValue": "23'"}, "team": {"id": "1"}},
                       {"yellowCard": True, "clock": {"displayValue": "41'"}, "team": {"id": "2"}},
                       {"scoringPlay": True, "clock": {"displayValue": "55'"}, "team": {"id": "2"}},
                       {"redCard": True, "clock": {"displayValue": "62'"}, "team": {"id": "2"}}],
           "competitors": [team("1", "home", "ARS", 1, [st("possessionPct", 61), st("totalShots", 12)]),
                           team("2", "away", "CHE", 1, [st("possessionPct", 39), st("totalShots", 6)])]}
    return [row("San Francisco 49ers", "football", "nfl", "@ Dallas Cowboys", "21-17  Q3 5:12", nfl, tint="#aa0000"),
            row("San Francisco Giants", "baseball", "mlb", "@ Los Angeles Dodgers", "3-2  Top 7th", mlb, tint="#fd5a1e"),
            row("Golden State Warriors", "basketball", "nba", "@ Boston Celtics", "78-74  Q3 5:12", nba, tint="#1d428a"),
            row("New Jersey Devils", "hockey", "nhl", "@ Boston Bruins", "2-1  P2 6:47", nhl, tint="#ce1126"),
            row("Arsenal", "soccer", "eng.1", "vs Chelsea", "1-1  67'", soc, tint="#ef0107")]


if __name__ == "__main__":
    if "--demo" in sys.argv:  # preview the live-game graphics with fake data (no network)
        fetch_all = lambda entries: demo_data()
        fetch_pinned = lambda pins: []
        playoff_games = lambda: []
        STATE = os.path.join(HERE, "demo_state.json")
        run_gui()
        sys.exit()
    if "--debug-live" in sys.argv:  # show what ESPN sends for live games, to tune extra info
        seen = {(t["sport"], t["league"]) for t in load_config()["teams"]} | {(sp, lg) for _, sp, lg in PLAYOFF_LEAGUES}
        today = datetime.now().astimezone().date()
        for sp, lg in sorted(seen):
            try:
                evs = fetch_scoreboard(sp, lg, f"{today:%Y%m%d}")
            except Exception as ex:
                print(f"{sp}/{lg}: error {ex}")
                continue
            for e in evs:
                comp = e["competitions"][0]
                if comp.get("status", {}).get("type", {}).get("state") != "in":
                    continue
                stats = sorted({st.get("name") for c in comp.get("competitors", []) for st in c.get("statistics", []) or []})
                print(f"{sp}/{lg} {e.get('shortName')}: situation keys={sorted((comp.get('situation') or {}).keys())} stats={stats}")
                print("  ->", situation_text(sp, comp).replace("\n", " | ") or "(nothing)")
    elif "--debug-playoffs" in sys.argv:
        for r in playoff_games(debug=True):
            print(f'  {r["name"]} | {r["line"]} | {r["detail"]}')
    elif "--find" in sys.argv:  # --find "san diego state" [--add]
        name = sys.argv[sys.argv.index("--find") + 1]
        hits = find_teams(name)
        for h in hits:
            print(json.dumps(h))
        if "--add" in sys.argv:
            print(f"Added {len(add_found(hits))} new team(s) to teams.json")
        elif hits:
            print("Re-run with --add to add these to teams.json")
        else:
            print("No matches")
    elif "--print" in sys.argv:  # headless check: print statuses to console
        for r in fetch_pinned(load_pinned()) + fetch_all(load_config()["teams"]) + playoff_games():
            print(f'{r["name"]:<28} {r["line"]:<10} {r["detail"]} {r.get("info", "")}')
    else:
        run_gui()
