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


def _date_label(event):
    """'Sat Oct 3' (local time) for a game's start, or ''."""
    d = _parse_date(event.get("date"))
    return f"{d.astimezone():%a %b} {d.astimezone().day}" if d else ""


def _record(c):
    """' (50-32)' for a competitor, or '' when ESPN sends no record."""
    recs = c.get("records") or c.get("record") or []
    if isinstance(recs, str):
        return f" ({recs})" if recs else ""
    if isinstance(recs, dict):
        recs = [recs]
    pick = next((r for r in recs if isinstance(r, dict) and (r.get("type") == "total" or r.get("name") == "overall")),
                next((r for r in recs if isinstance(r, dict)), None))
    summ = (pick or {}).get("summary") or (pick or {}).get("displayValue")
    return f" ({summ})" if isinstance(summ, str) and summ else ""


STANDINGS = "https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings"
_seed_cache = {}


def seed_map(sport, league):
    """{team id: playoff seed} from ESPN's standings (cached for an hour; {} if unavailable)."""
    key = (sport, league)
    hit = _seed_cache.get(key)
    now = datetime.now().timestamp()
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    seeds, ttl = {}, 3600
    try:
        req = urllib.request.Request(STANDINGS.format(sport=sport, league=league), headers={"User-Agent": "sports-widget/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)

        def walk(node):
            if isinstance(node, dict):
                team, stats = node.get("team"), node.get("stats")
                if isinstance(team, dict) and isinstance(stats, list):
                    for st in stats:
                        if st.get("name") in ("playoffSeed", "seed") and float(st.get("value") or 0) > 0:
                            seeds[str(team.get("id"))] = int(float(st["value"]))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
    except Exception:
        ttl = 600  # don't hammer ESPN if the standings request fails
    _seed_cache[key] = (now, seeds, ttl)
    return seeds


def seeds_for(event, sport, league):
    """Playoff seeds for a postseason game's teams (None for regular-season games)."""
    return seed_map(sport, league) if sport and league and is_postseason(event) else None


def _rank(c, seeds=None):
    """'#5 ' for a ranked college team, '(3) ' for a playoff seed, or ''."""
    r = (c.get("curatedRank") or {}).get("current")
    if isinstance(r, int) and 1 <= r <= 25:  # ESPN uses 99 for unranked
        return f"#{r} "
    seed = c.get("seed") or (c.get("team") or {}).get("seed")
    if seed not in (None, "", 0, "0") and str(seed).isdigit():
        return f"({seed}) "
    if seeds:
        tid = str((c.get("team") or {}).get("id", c.get("id", "")))
        if tid in seeds:
            return f"({seeds[tid]}) "
    return ""


def _find_me(comp, team_abbr):
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        if team_abbr.lower() in (t.get("abbreviation", "").lower(), str(t.get("id", c.get("id", ""))).lower()):
            return c
    return None


def summarize_event(event, team_abbr, sport=None, league=None):
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
    seeds = seeds_for(event, sport, league)
    opp_name = _rank(opp, seeds) + (ot.get("displayName") or ot.get("abbreviation", "?")) + _record(opp)
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
    played = f" \u00b7 {_date_label(event)}" if state == "post" and _date_label(event) else ""
    return state, f"{sep} {opp_name}", f"{result}{ms}-{os_}  {detail}{played}"


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


def event_url(event, sport, league):
    """ESPN game page for an event: the link ESPN supplies, else a constructed URL."""
    for want in ("summary", "event", "desktop"):
        for l in event.get("links", []) or []:
            href = str(l.get("href", ""))
            if want in (l.get("rel") or []) and href.startswith("https://"):
                return href
    gid = event.get("id")
    if not gid:
        return None
    if sport == "soccer":
        return f"https://www.espn.com/soccer/match/_/gameId/{gid}"
    return f"https://www.espn.com/{league}/game/_/gameId/{gid}"


SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/summary?event={id}"


def fetch_summary(sport, league, event_id):
    url = SUMMARY.format(sport=sport, league=league, id=event_id)
    req = urllib.request.Request(url, headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _play_label(p):
    per = p.get("period") or {}
    clock = (p.get("clock") or {}).get("displayValue", "")
    prefix = per.get("displayValue") or (f'P{per["number"]}' if per.get("number") else "")
    return " ".join(x for x in (prefix, clock) if x)


_GROUP_SHORT = {"batting": "Bat", "pitching": "Pit", "fielding": "Fld"}


def _flat_stats(team_entry):
    """{key: (label, value)} for one team in a boxscore.

    Most sports send a flat list of stats; baseball nests them in groups (batting / pitching / ...),
    so those are flattened with a short group prefix.
    """
    out = {}
    groups = team_entry.get("statistics") or []
    nested = any(isinstance(g, dict) and g.get("stats") for g in groups)
    for g in groups:
        items = g.get("stats") if nested else [g]
        prefix = ""
        if nested:
            gname = str(g.get("name") or g.get("type") or "")
            prefix = _GROUP_SHORT.get(gname.lower(), gname[:3]) + " "
        for st in items or []:
            val = st.get("displayValue", st.get("value"))
            if val in (None, ""):
                continue
            label = st.get("abbreviation") or st.get("shortDisplayName") or st.get("label") or st.get("displayName") or st.get("name")
            out[f"{prefix}{st.get('name') or label}"] = (f"{prefix}{label}", str(val))
    return out


def game_detail_data(data, max_plays=14, max_stats=14):
    """Boil an ESPN summary response down to what the details window shows."""
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    cs = comp.get("competitors", [])
    away = next((c for c in cs if c.get("homeAway") == "away"), cs[0] if cs else {})
    home = next((c for c in cs if c.get("homeAway") == "home"), cs[1] if len(cs) > 1 else {})
    name = lambda c: (c.get("team") or {}).get("displayName") or (c.get("team") or {}).get("abbreviation", "?")
    status = (comp.get("status") or {}).get("type") or {}
    abbr = lambda c: (c.get("team") or {}).get("abbreviation", "")
    out = {"away": name(away), "home": name(home), "away_abbr": abbr(away), "home_abbr": abbr(home), "away_score": str(away.get("score", "")),
           "home_score": str(home.get("score", "")), "status": status.get("detail") or status.get("shortDetail", ""),
           "state": status.get("state", "pre")}
    if away and home:
        out["colors"] = matchup_colors([away, home])
    wp = data.get("winprobability") or []
    if wp and wp[-1].get("homeWinPercentage") is not None:
        out["home_win"] = float(wp[-1]["homeWinPercentage"])
    plays = data.get("plays") or []
    if not plays:
        drives = data.get("drives") or {}
        for d in (drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else []):
            plays += d.get("plays") or []
    lines = []
    for p in plays[-max_plays:][::-1]:
        text = p.get("text") or p.get("shortText")
        if text:
            lines.append((_play_label(p), text))
    out["plays"] = lines
    out["scoring"] = [((_play_label(p)), p.get("text") or p.get("shortText", "")) for p in (data.get("scoringPlays") or [])][-8:]
    teams = (data.get("boxscore") or {}).get("teams") or []
    if len(teams) == 2:
        by_id = {str((t.get("team") or {}).get("id")): t for t in teams}
        a = by_id.get(str((away.get("team") or {}).get("id")), teams[0])
        h = by_id.get(str((home.get("team") or {}).get("id")), teams[1])
        ha, hh = _flat_stats(a), _flat_stats(h)
        out["stats"] = [(label, val, hh.get(key, ("", ""))[1]) for key, (label, val) in ha.items()][:max_stats]
    else:
        out["stats"] = []
    return out


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


def _state_of(e):
    return e["competitions"][0].get("status", {}).get("type", {}).get("state", "pre")


def recent_result(events, hours=24 * 7):
    """Most recent completed game that started within the last `hours` hours (default: a week)."""
    now = datetime.now(timezone.utc)
    best = None
    for e in events:
        d = _parse_date(e.get("date"))
        if e.get("competitions") and _state_of(e) == "post" and d and now - timedelta(hours=hours + 4) <= d <= now:
            if best is None or e["date"] > best["date"]:
                best = e
    return best


def next_event(events):
    """The next game that has not started yet (any distance away), or None."""
    now = datetime.now(timezone.utc)
    ups = [e for e in events if e.get("competitions") and _state_of(e) == "pre"
           and (_parse_date(e.get("date")) or now) >= now]
    return min(ups, key=lambda e: e["date"]) if ups else None


def _same_day(event, now_iso):
    d, n = _parse_date(event.get("date")), _parse_date(now_iso)
    return bool(d and n and d.astimezone().date() == n.astimezone().date())


def team_status(entry):
    data = fetch_schedule(entry)
    team = data.get("team", {})
    base_name = entry.get("label") or team.get("displayName") or entry["team"].upper()
    own = team.get("recordSummary") or ((team.get("record") or {}).get("items") or [{}])[0].get("summary")
    events = data.get("events", [])
    event = pick_event(events)  # live game, else next game within a week
    next_line = ""
    if not event or _state_of(event) != "in":
        recent = recent_result(events)
        if recent:  # a game that just ended: show the result and when the next one is
            event = recent
            nxt = next_event(events)
            s_next = summarize_event(nxt, entry["team"], entry["sport"], entry["league"]) if nxt else None
            next_line = f"Next: {s_next[1]} \u00b7 {s_next[2]}" if s_next else "No upcoming game scheduled"
    if not event:
        return None
    s = summarize_event(event, entry["team"], entry["sport"], entry["league"])
    if not s:
        return None
    state, line, detail = s
    me = _find_me(event["competitions"][0], entry["team"]) or {}
    name = _rank(me, seeds_for(event, entry["sport"], entry["league"])) + base_name + (f" ({own})" if own else "")
    info, graphic = live_info(entry, event) if state == "in" else ("", None)
    return {"name": name, "state": state, "line": line, "detail": detail, "info": info, "graphic": graphic,
            "next": next_line,
            "_key": (entry["league"], str(event.get("id"))), "tint": tint_color(team),
            "url": event_url(event, entry["sport"], entry["league"]),
            "game": {"sport": entry["sport"], "league": entry["league"], "id": str(event.get("id"))}}


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
    st = e.get("seasonType") or {}
    if (season.get("type") == 3 or "post" in str(season.get("slug", "")).lower()
            or st.get("type") == 3 or str(st.get("id")) == "3"):
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
            summ = summarize_game(e, sport, league)
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
            comp = e["competitions"][0]
            note = (comp.get("notes") or [{}])[0].get("headline", "")
            series = comp.get("series", {}).get("summary", "")
            extra = " · ".join(x for x in (note, series) if x)
            row = {"name": matchup, "state": state, "line": name + (f" · {extra}" if extra else ""),
                   "_key": (league, str(e.get("id"))), "tint": home_tint(comp), "url": event_url(e, sport, league),
                   "game": {"sport": sport, "league": league, "id": str(e.get("id"))},
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
LIVE_REFRESH_CHOICES = [("Same as normal", 0), ("10 seconds", 10), ("15 seconds", 15), ("30 seconds", 30), ("1 minute", 60)]
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


LEAGUE_SECTION = [("MLB", "baseball", "mlb"), ("NFL", "football", "nfl"), ("NBA", "basketball", "nba"),
                  ("WNBA", "basketball", "wnba")]


def league_games():
    """Today's regular games in MLB/NFL/NBA/WNBA (postseason games are shown under Playoffs)."""
    today = datetime.now().astimezone().date()
    out = []
    for name, sport, league in LEAGUE_SECTION:
        try:
            events = fetch_scoreboard(sport, league, f"{today:%Y%m%d}")
        except Exception:
            continue
        for e in events:
            if not e.get("competitions") or is_postseason(e):
                continue
            summ = summarize_game(e, sport, league)
            if not summ:
                continue
            state, matchup, detail = summ
            comp = e["competitions"][0]
            out.append({"name": matchup, "state": state, "line": "", "league": name, "detail": detail,
                        "_key": (league, str(e.get("id"))), "tint": home_tint(comp), "_date": e.get("date", ""),
                        "url": event_url(e, sport, league),
                        "game": {"sport": sport, "league": league, "id": str(e.get("id"))},
                        "info": situation_text(sport, comp) if state == "in" else "",
                        "graphic": situation_graphic(sport, comp, league) if state == "in" else None})
    order = {"in": 0, "pre": 1, "post": 2}
    out.sort(key=lambda r: (order.get(r["state"], 3), r["_date"]))
    return out


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


def summarize_game(event, sport=None, league=None):
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
    seeds = seeds_for(event, sport, league)

    def ab(c):
        a = c.get("athlete", {})
        t = c.get("team", {})
        return _rank(c, seeds) + (t.get("displayName") or t.get("abbreviation") or a.get("displayName") or a.get("shortName", "?")) + _record(c)
    if state == "pre":
        when = _parse_date(event.get("date"))
        text = when.astimezone().strftime("%a %b %d %I:%M %p").replace(" 0", " ") if when else detail
    else:
        text = f"{_score(away)}-{_score(home)}  {detail}"
        if state == "post" and _date_label(event):
            text += f" \u00b7 {_date_label(event)}"
    return state, f"{ab(away)} @ {ab(home)}", text


def pinned_status(pin):
    for e in fetch_scoreboard(pin["sport"], pin["league"], pin["date"]):
        if str(e.get("id")) == str(pin["id"]):
            s = summarize_game(e, pin["sport"], pin["league"])
            if s:
                return {"name": s[1], "state": s[0], "line": "", "detail": s[2],
                        "_key": (pin["league"], str(pin["id"])), "tint": home_tint(e["competitions"][0]),
                        "url": event_url(e, pin["sport"], pin["league"]),
                        "game": {"sport": pin["sport"], "league": pin["league"], "id": str(pin["id"])},
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


def aa_circle_pixels(size, mode, fg, bg, ring=2.0, margin=5.0, ss=4, cut=None):
    """Anti-aliased circle icon as rows of hex colors (supersampled; no Pillow needed).

    mode: "full" (filled), "live" (ring + left half filled), anything else (ring only).
    """
    c = size / 2
    R = c - margin
    f, b = _rgb(fg), _rgb(bg)
    # The fill is the part of the disc left of x = cut (so it wipes in/out horizontally):
    # full -> everything, live -> left half, anything else -> nothing.
    if cut is None:
        cut = {"full": R + 1, "live": 0}.get(mode, -R - 1)
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss - c
                    y = py + (sy + 0.5) / ss - c
                    d = (x * x + y * y) ** 0.5
                    on = (R - ring <= d <= R) or (d <= R and x < cut)
                    hit += on
            a = hit / (ss * ss)
            row.append("#%02x%02x%02x" % tuple(round(bc + (fc - bc) * a) for fc, bc in zip(f, b)))
        rows.append(row)
    return rows


def aa_refresh_pixels(size, fg, bg, ring=2.0, margin=7.0, ss=4):
    """Anti-aliased circular-arrow (refresh) icon as rows of hex colors."""
    import math
    c = size / 2
    R = c - margin
    f, b = _rgb(fg), _rgb(bg)
    rad = math.radians
    base_a, tip_a, start_a = rad(-70), rad(-30), rad(-15)
    pt = lambda a, r: (c + r * math.cos(a), c + r * math.sin(a))
    A, B, C = pt(base_a, R - 4.5), pt(base_a, R + 4.5), pt(tip_a, R)

    def in_tri(x, y):
        d1 = (x - B[0]) * (A[1] - B[1]) - (A[0] - B[0]) * (y - B[1])
        d2 = (x - C[0]) * (B[1] - C[1]) - (B[0] - C[0]) * (y - C[1])
        d3 = (x - A[0]) * (C[1] - A[1]) - (C[0] - A[0]) * (y - A[1])
        return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))

    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                    dx, dy = x - c, y - c
                    d = math.hypot(dx, dy)
                    ang = math.atan2(dy, dx)
                    on_ring = R - ring / 2 <= d <= R + ring / 2 and not (base_a <= ang <= start_a)
                    hit += on_ring or in_tri(x, y)
            a = hit / (ss * ss)
            row.append("#%02x%02x%02x" % tuple(round(bc + (fc - bc) * a) for fc, bc in zip(f, b)))
        rows.append(row)
    return rows


def rr_points(x1, y1, x2, y2, r):
    """Polygon points for a rounded rectangle (draw with smooth=True)."""
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
            x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def dwm_round(win):
    """Windows 11: ask DWM for rounded corners on a window. Returns True if accepted."""
    if sys.platform != "win32":
        return False
    import ctypes
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    pref = ctypes.c_int(2)  # DWMWCP_ROUND
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)) == 0


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
    PANEL, HOVER = "#2a2a33", "#3a3a46"
    UI_FONT = ("Segoe UI", 9)

    def style_menu(m):
        m.config(bg=PANEL, fg=FG, activebackground=HOVER, activeforeground=FG, disabledforeground=DIM,
                 selectcolor=FG, bd=0, relief="flat", activeborderwidth=0, font=UI_FONT)
        return m

    def dark_titlebar(win):
        """Windows 10/11: use the dark title bar so dialogs match the widget."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            one = ctypes.c_int(1)
            for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 on early Win10 builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(one), ctypes.sizeof(one)) == 0:
                    break
            dwm_round(win)
        except Exception:
            pass

    menu_state = {"top": None}

    def close_menu(_=None):
        top = menu_state["top"]
        menu_state["top"] = None
        if top is not None:
            try:
                top.destroy()
            except tk.TclError:
                pass

    def popup_menu(x, y, items):
        """Dark, rounded context menu. items: (label, command[, checked]) tuples, or None for a separator."""
        close_menu()
        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg="#3a3a46")  # 1px border colour
        inner = tk.Frame(top, bg=PANEL, padx=5, pady=5)
        inner.pack(padx=1, pady=1)
        has_check = any(len(i) > 2 for i in items if i)
        for item in items:
            if item is None:
                tk.Frame(inner, bg="#3a3a46", height=1).pack(fill="x", padx=6, pady=4)
                continue
            label, command = item[0], item[1]
            row = tk.Frame(inner, bg=PANEL, cursor="hand2")
            row.pack(fill="x")
            parts = []
            if has_check:
                parts.append(tk.Label(row, text="\u2713" if len(item) > 2 and item[2] else "", bg=PANEL, fg=FG, font=UI_FONT,
                                      width=2, anchor="e", pady=5, cursor="hand2"))
            parts.append(tk.Label(row, text=label, bg=PANEL, fg=FG, font=UI_FONT, anchor="w", padx=6, pady=5, cursor="hand2"))
            for i, w_ in enumerate(parts):
                w_.pack(side="left", fill="x", expand=(i == len(parts) - 1))
            parts_all = [row] + parts

            def hover(on, ws=parts_all):
                for w_ in ws:
                    w_.config(bg=HOVER if on else PANEL)
            for w_ in parts_all:
                w_.bind("<Enter>", lambda e, h=hover: h(True))
                w_.bind("<Leave>", lambda e, h=hover: h(False))
                w_.bind("<ButtonRelease-1>", lambda e, c=command: (close_menu(), root.after(10, c)))
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        x = max(0, min(x, top.winfo_screenwidth() - w - 4))
        y = max(0, min(y, top.winfo_screenheight() - h - 4))
        top.geometry(f"+{x}+{y}")
        menu_state["top"] = top
        try:
            if not dwm_round(top):
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(top.winfo_id()) or top.winfo_id()
                rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, 12, 12)
                ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            pass  # cosmetic only (non-Windows)
        top.bind("<Escape>", close_menu)
        top.bind("<FocusOut>", lambda e: root.after(80, lambda: menu_state["top"] is top and close_menu()))
        top.focus_force()

    def styled_message(title, text, parent=None):
        win = tk.Toplevel(parent or root)
        win.title(title)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        dark_titlebar(win)
        tk.Label(win, text=title, bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", padx=16, pady=(16, 4))
        tk.Label(win, text=text, bg=BG, fg=DIM, font=UI_FONT, justify="left", anchor="w", wraplength=340).pack(fill="x", padx=16)
        styled_button(win, "OK", win.destroy).pack(anchor="e", padx=16, pady=(12, 16))
        win.update_idletasks()
        win.geometry(f"+{root.winfo_x() + 30}+{root.winfo_y() + 30}")

    def styled_button(parent, text, command):
        import tkinter.font as tkfont
        w = tkfont.Font(font=UI_FONT).measure(text) + 28
        c = tk.Canvas(parent, width=w, height=28, bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        shape = c.create_polygon(rr_points(1, 1, w - 1, 27, 8), smooth=True, fill=PANEL, outline=PANEL)
        c.create_text(w / 2, 14, text=text, fill=FG, font=UI_FONT)
        c.bind("<Enter>", lambda e: c.itemconfigure(shape, fill=HOVER, outline=HOVER))
        c.bind("<Leave>", lambda e: c.itemconfigure(shape, fill=PANEL, outline=PANEL))
        c.bind("<ButtonRelease-1>", lambda e: command() if 0 <= e.x <= w and 0 <= e.y <= 28 else None)
        return c

    def styled_option(parent, var, values, command=None, width=None):
        om = tk.OptionMenu(parent, var, *values, **({"command": command} if command else {}))
        om.config(bg=PANEL, fg=FG, activebackground=HOVER, activeforeground=FG, relief="flat", bd=0,
                  highlightthickness=0, font=UI_FONT, indicatoron=True, **({"width": width} if width else {}))
        style_menu(om["menu"])
        return om

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
    view_btn = tk.Canvas(hbar, width=34, height=34, bg=BG, highlightthickness=0, cursor="hand2")
    view_btn.pack(side="right", padx=(8, 0))

    refresh_btn = tk.Canvas(hbar, width=34, height=34, bg=BG, highlightthickness=0, cursor="hand2")
    refresh_btn.pack(side="right", padx=(8, 0))
    refresh_img = tk.PhotoImage(width=34, height=34)
    refresh_img.put(" ".join("{" + " ".join(row) + "}" for row in aa_refresh_pixels(34, FG, BG)))
    refresh_btn.create_image(17, 17, image=refresh_img)
    view_imgs = {}
    icon = {"busy": False, "rows": {}}
    ICON_R = 34 / 2 - 5.0
    ICON_CUT = {"full": ICON_R + 1, "live": 0.0, "title": -ICON_R - 1}  # wipe position of the fill
    ICON_FRAMES = 10

    def icon_precompute():
        """Render the fill-transition frames in the background (pure Python, ~0.3 s)."""
        order = ["full", "live", "title"]
        for i, a in enumerate(order):
            b = order[(i + 1) % 3]
            frames = []
            for k in range(1, ICON_FRAMES + 1):
                t = k / ICON_FRAMES
                e = t * t * (3 - 2 * t)
                cut = ICON_CUT[a] + (ICON_CUT[b] - ICON_CUT[a]) * e
                frames.append(aa_circle_pixels(34, "", FG, BG, cut=cut, ss=3))
            icon["rows"][(a, b)] = frames

    def icon_image(rows):
        img = tk.PhotoImage(width=34, height=34)
        img.put(" ".join("{" + " ".join(row) + "}" for row in rows))
        return img

    def draw_view_icon(mode, animate_from=None):
        """Full = filled circle, Live = half-filled, Title = empty (anti-aliased; transitions animate the fill)."""
        if icon["busy"] and animate_from is None:
            return  # an animation is running; it draws the final state itself
        frames = icon["rows"].get((animate_from, mode)) if animate_from else None
        if frames:
            icon["busy"] = True
            keep = []

            def play(i=0):
                if i < len(frames):
                    img = icon_image(frames[i])
                    keep.append(img)
                    view_btn.delete("all")
                    view_btn.create_image(17, 17, image=img)
                    root.after(18, lambda: play(i + 1))
                else:
                    icon["busy"] = False
                    draw_view_icon(mode)
            play()
            return
        if mode not in view_imgs:
            view_imgs[mode] = icon_image(aa_circle_pixels(34, mode, FG, BG))
        view_btn.delete("all")
        view_btn.create_image(17, 17, image=view_imgs[mode])

    # Resize grip (bottom-right) packed first so it stays visible; content scrolls above it.
    grip = tk.Label(root, text="\u25e2", bg=BG, fg=DIM, cursor="size_nw_se" if sys.platform == "win32" else "bottom_right_corner", font=("Segoe UI", 9))
    grip.pack(side="bottom", anchor="se", padx=2)
    container = tk.Frame(root, bg=BG)
    container.pack(fill="both", expand=True, padx=(12, 4), pady=(0, 0))
    container.grid_rowconfigure(0, weight=1)
    container.grid_columnconfigure(0, weight=1)
    canvas = tk.Canvas(container, bg=BG, highlightthickness=0, width=330, height=100)
    scroll = tk.Canvas(container, width=8, height=1, bg=BG, highlightthickness=0, cursor="arrow")
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
    user_sized = {"on": False}
    view_tween = {"on": False}
    MIN_W, MIN_H = 240, 120
    MIN_BODY_W = 330  # wide enough for an expanded game, so expanding never changes the window width

    def tween(h0, h1, setter, done=None, duration=0.24):
        import time
        t0 = time.perf_counter()

        def step():
            p = min((time.perf_counter() - t0) / duration, 1.0)
            setter(h0 + (h1 - h0) * p * p * (3 - 2 * p))
            if p < 1.0:
                root.after(6, step)
            elif done:
                done()
        step()

    def update_scrollbar():
        target = canvas.winfo_height()
        need = session["total"] > target
        if need and not scroll.winfo_ismapped():
            scroll.grid(row=0, column=1, sticky="ns")
        elif not need and scroll.winfo_ismapped():
            scroll.grid_remove()
            canvas.yview_moveto(0)

    def fit(_=None):
        """Auto-size the window to the drawn content (until the user resizes) and sync the scrollbar."""
        if session["anims"]:
            return  # an expand/collapse is animating; it calls fit() again when done
        if not user_sized["on"]:
            max_h = int(root.winfo_screenheight() * 0.7)
            target = min(session["total"], max_h)
            canvas.configure(width=MIN_BODY_W)
            if view_tween["on"]:
                pass  # a view change is animating the height; it calls fit() again when done
            elif view_tween.pop("next", False):
                start = view_tween.pop("from", canvas.winfo_height())  # "from" is set when leaving Title (height 1)
                if start == target:
                    canvas.configure(height=target)
                else:
                    view_tween["on"] = True

                    def finish():
                        view_tween["on"] = False
                        fit()
                    tween(start, target, lambda h: canvas.configure(height=max(int(h), 1)), finish)
            else:
                canvas.configure(height=target)
        update_scrollbar()

    canvas_w = {"w": 0}

    def on_canvas(e):
        if e.width != canvas_w["w"]:  # width changed (user resize): relayout the text
            canvas_w["w"] = e.width
            draw_all()
        update_scrollbar()

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
        draw_view_icon(mode)
        hbar.pack_configure(pady=(8, 12) if mode == "title" else (8, 2))  # extra bottom space when only the title shows
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
        if view_tween["on"] or icon["busy"]:
            return  # let the running transition finish
        order = [m for m, _ in VIEWS]
        cur = ui_state.get("view", "full")
        new = order[(order.index(cur) + 1) % len(order)] if cur in order else "full"
        ui_state["view"] = new
        save_state(ui_state)
        draw_view_icon(new, animate_from=cur)
        if user_sized["on"]:  # fixed-size window: just swap the content
            apply_layout()
            if last:
                render(*last["args"])
            return
        if new == "title":
            view_tween["on"] = True

            def finish():
                view_tween["on"] = False
                apply_layout()
                if last:
                    render(*last["args"])
            tween(canvas.winfo_height(), 1, lambda h: canvas.configure(height=max(int(h), 1)), finish)
            return
        view_tween["next"] = True  # fit() will animate the height to the new content
        if cur == "title":
            canvas.configure(height=1)
            view_tween["from"] = 1
        apply_layout()
        if last:
            render(*last["args"])
    view_btn.bind("<ButtonRelease-1>", cycle_view)
    refresh_btn.bind("<ButtonRelease-1>", lambda e: tick())  # refresh now (and restart the countdown)
    grip.bind("<Button-1>", grip_start)
    grip.bind("<B1-Motion>", grip_move)
    grip.bind("<Double-Button-1>", grip_reset)

    topmost = tk.BooleanVar(value=True)
    pins = load_pinned()

    # ------------------------------------------------------------------------------------------
    # Canvas renderer. Everything is drawn on one canvas instead of one widget per label, so a redraw
    # (refresh, expand, collapse, view change) is a single repaint and cannot flicker.
    # ------------------------------------------------------------------------------------------
    import time as _time
    FONTS = {"name": ("Segoe UI", 10, "bold"), "line": ("Segoe UI", 9), "detb": ("Segoe UI", 9, "bold"),
             "sec": ("Segoe UI", 8, "bold"), "hdr": ("Segoe UI", 9, "bold"), "small": ("Segoe UI", 8),
             "smallb": ("Segoe UI", 8, "bold")}
    PAD, GAP = 10, 6
    last = {}
    session = {"live_prev": 0, "expanded": set(), "details": {}, "games": {}, "sig": None,
               "anims": {}, "vis": {}, "hits": {}, "total": 0, "looping": False, "actx": None}

    def gkey(g):
        return f'{g["league"]}:{g["id"]}'

    def ctext(x, y, s_, font, fill, width=None, anchor="nw", tags=()):
        kw = {"text": s_, "font": font, "fill": fill, "anchor": anchor, "tags": tags}
        if width:
            kw["width"] = int(width)
        i = canvas.create_text(x, y, **kw)
        b_ = canvas.bbox(i)
        return i, (b_[3] - b_[1] if b_ else 0)

    class Off:
        """Draw with coordinates relative to (ox, oy), so the graphics code can use local coordinates."""
        def __init__(self, ox, oy):
            self.ox, self.oy = ox, oy

        def __getattr__(self, name):
            fn = getattr(canvas, name)
            if not name.startswith("create_"):
                return fn

            def call(*args, **kw):
                flat = []
                for a_ in args:
                    flat.extend(a_) if isinstance(a_, (list, tuple)) else flat.append(a_)
                return fn(*[v + (self.ox if k % 2 == 0 else self.oy) for k, v in enumerate(flat)], **kw)
            return call

    def graphic_one(ox, oy, g, bg, W):
        c = Off(ox, oy)
        kind = g["kind"]
        if kind == "periods":
            n, gap = len(g["fills"]), 3
            seg = (W - gap * (n - 1)) / n
            for i, f in enumerate(g["fills"]):
                x = i * (seg + gap)
                c.create_rectangle(x, 14, x + seg, 20, fill="#33333d", outline="")
                if f > 0:
                    c.create_rectangle(x, 14, x + seg * f, 20, fill="#34d399" if f < 1 else "#4a4a55", outline="")
            c.create_text(0, 6, text=g["label"], anchor="w", fill=FG, font=FONTS["smallb"])
            return 24
        if kind == "versus":
            split = W * g["a"] / (g["a"] + g["b"])
            c.create_rectangle(0, 16, split, 24, fill=g.get("a_color", "#60a5fa"), outline="")
            c.create_rectangle(split, 16, W, 24, fill=g.get("b_color", "#f59e0b"), outline="")
            fmt = lambda v: f"{v:g}"
            c.create_text(0, 6, text=f'{g["a_name"]} {fmt(g["a"])}', anchor="w", fill=FG, font=FONTS["small"])
            c.create_text(W / 2, 6, text=g["label"], fill=DIM, font=FONTS["small"])
            c.create_text(W, 6, text=f'{fmt(g["b"])} {g["b_name"]}', anchor="e", fill=FG, font=FONTS["small"])
            return 30
        if kind == "timeline":
            span = 90 if g["minute"] <= 90 else 120
            px = lambda m: 6 + (W - 12) * min(m, span) / span
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
            return 44
        if kind == "baseball":
            def base(cx, cy, on):
                r = 5
                hl = g.get("color", "#fbbf24")
                c.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy, fill=hl if on else bg,
                                 outline=hl if on else DIM, width=2)
            base(21, 15, g["bases"][1]); base(29, 23, g["bases"][0]); base(13, 23, g["bases"][2])
            for row, (label, n, total, color) in enumerate((("B", min(g["balls"], 3), 3, "#34d399"),
                                                            ("S", min(g["strikes"], 2), 2, "#fbbf24"),
                                                            ("O", min(g["outs"], 3), 3, "#f87171"))):
                y = 8 + row * 14
                c.create_text(52, y, text=label, anchor="w", fill=DIM, font=FONTS["smallb"])
                for i in range(total):
                    on = i < n
                    c.create_oval(66 + i * 12, y - 4, 74 + i * 12, y + 4, fill=color if on else bg,
                                  outline=color if on else DIM, width=1)
            return 44
        if kind == "football":
            px = lambda yd: W * yd / 100
            c.create_rectangle(0, 14, W, 20, fill="#33333d", outline="")
            if g["red"]:
                c.create_rectangle(px(80), 14, W, 20, fill="#7f3b3b", outline="")
            if g["first"] is not None:
                c.create_line(px(g["first"]), 11, px(g["first"]), 23, fill="#fbbf24", width=2)
            bx = px(g["x"])
            c.create_oval(bx - 5, 12, bx + 5, 22, fill=g.get("color", "#34d399"), outline=FG)
            c.create_text(0, 6, text=f"{g['off']} ▶", anchor="w", fill=FG, font=FONTS["smallb"])
            c.create_text(W, 6, text=g["def"], anchor="e", fill=DIM, font=FONTS["smallb"])
            return 24
        return 0

    def graphics(ox, oy, gs, bg, W=240):
        """Stack the graphics for a game; bars stretch to the card's inner width W."""
        h = 0
        for g in ([gs] if isinstance(gs, dict) else gs):
            h += 2 + graphic_one(ox, oy + h + 2, g, bg, int(W))
        return h

    def draw_details(x, y, w, bgc, d):
        """Expanded-game section; returns its height."""
        y0 = y
        if d is None:
            _, h = ctext(x, y + 6, "Loading details...", FONTS["small"], DIM)
            return 6 + h
        if "error" in d:
            _, h = ctext(x, y + 6, "Details unavailable", FONTS["small"], COLORS["err"])
            return 6 + h
        canvas.create_line(x, y + 6, x + w, y + 6, fill=DIM)
        y += 11
        if d.get("home_win") is not None:
            hw = round(d["home_win"] * 100)
            ca, cb = d.get("colors", ("#60a5fa", "#f59e0b"))
            y += graphics(x, y, {"kind": "versus", "label": "Win probability", "a_name": d["away_abbr"], "a": 100 - hw,
                                 "b_name": d["home_abbr"], "b": hw, "a_color": ca, "b_color": cb}, bgc, w)
        for title, items in (("Scoring", d["scoring"]), ("Recent plays", d["plays"][:8])):
            if items:
                _, h = ctext(x, y + 4, title, FONTS["smallb"], DIM)
                y += 4 + h
                for when, text in items:
                    _, h = ctext(x, y, (f"{when} · " if when else "") + text, FONTS["small"], FG, width=w)
                    y += h
        if d["stats"]:
            _, h = ctext(x, y + 4, "Team stats", FONTS["smallb"], DIM)
            y += 4 + h
            mid = x + w / 2  # away value at the left edge, home value at the right edge, label centred between
            ctext(x, y, d["away_abbr"], FONTS["smallb"], DIM, anchor="nw")
            _, h = ctext(x + w, y, d["home_abbr"], FONTS["smallb"], DIM, anchor="ne")
            y += h
            for label, a_, h_ in d["stats"]:
                ctext(x, y, a_, FONTS["small"], FG, anchor="nw")
                ctext(mid, y, label, FONTS["small"], DIM, anchor="n")
                _, h = ctext(x + w, y, h_, FONTS["small"], FG, anchor="ne")
                y += h
        if not (d["plays"] or d["scoring"] or d["stats"] or d.get("home_win") is not None):
            _, h = ctext(x, y, "No extra details from ESPN for this game", FONTS["small"], DIM)
            y += h
        return y - y0

    def fetch_details(g):
        try:
            session["details"][gkey(g)] = game_detail_data(fetch_summary(g["sport"], g["league"], g["id"]))
        except Exception as ex:
            session["details"][gkey(g)] = {"error": str(ex)[:60]}

    def new_hit(payload):
        tag = f"hit{len(session['hits'])}"
        session["hits"][tag] = payload
        return tag

    def visible(spec, H, final=False):
        """Pixels of a collapsible block that are showing right now (eased)."""
        if spec["opening"]:
            a_ = spec["from"] if spec["from"] is not None else 0
            b_ = H
        else:
            a_ = spec["from"] if spec["from"] is not None else H
            b_ = 0
        if final:
            return b_
        p = min((_time.perf_counter() - spec["t0"]) / spec["dur"], 1.0)
        return a_ + (b_ - a_) * p * p * (3 - 2 * p)

    def draw_card(r, x, y, w, final):
        tint = r.get("tint")
        bgc = blend(BG, tint, 0.22) if tint else BG
        cx0, cw_ = x + 2, w - 4
        tags = ()
        if r.get("game") or r.get("url"):
            tags = (new_hit(("game", r)),)
        bgid = canvas.create_polygon(rr_points(cx0, y, cx0 + cw_, y + 10, 10), smooth=True, fill=bgc, outline=bgc) if tint else None
        hit = canvas.create_rectangle(cx0 + 3, y + 3, cx0 + cw_ - 3, y + 10, fill=bgc, outline="", tags=tags) if tags else None
        ix, ww = cx0 + PAD, cw_ - 2 * PAD
        yy = y + GAP
        _, h = ctext(ix, yy, r["name"], FONTS["name"], FG, width=ww, tags=tags)
        yy += h
        if r["line"]:
            _, h = ctext(ix, yy, r["line"], FONTS["line"], DIM, width=ww, tags=tags)
            yy += h
        _, h = ctext(ix, yy, r["detail"], FONTS["detb"] if r["state"] == "in" else FONTS["line"],
                     COLORS.get(r["state"], FG), width=ww, tags=tags)
        yy += h
        if r.get("graphic"):
            yy += graphics(ix, yy, r["graphic"], bgc, ww)
        if r.get("info"):
            _, h = ctext(ix, yy, r["info"], FONTS["line"], DIM, width=ww, tags=tags)
            yy += h
        if r.get("next"):
            _, h = ctext(ix, yy + 3, r["next"], FONTS["small"], DIM, width=ww, tags=tags)
            yy += 3 + h
        g = r.get("game")
        ctx = None
        if g and gkey(g) in session["expanded"]:
            key = "game:" + gkey(g)
            y0 = yy
            H = draw_details(ix, y0, ww, bgc, session["details"].get(gkey(g)))
            yy = y0 + H  # always drawn at full height; an animation only moves things afterwards
            if key in session["anims"]:
                cover = canvas.create_rectangle(cx0 - 1, yy + GAP, cx0 + cw_ + 1, yy + GAP + 3, fill=BG, outline="")
                ctx = {"key": key, "kind": "card", "H": H, "y0": y0, "cover": cover, "x0": cx0 - 1, "x1": cx0 + cw_ + 1,
                       "bg": bgid, "hit": hit, "geo": (cx0, y, cx0 + cw_), "dy": 0}
        bottom = yy + GAP
        if bgid:
            canvas.coords(bgid, *rr_points(cx0, y, cx0 + cw_, bottom, 10))
        if hit:
            canvas.coords(hit, cx0 + 3, y + 3, cx0 + cw_ - 3, bottom - 3)
        if ctx:
            ctx["bottom"] = bottom
            session["actx"] = ctx
        return bottom + GAP

    def draw_group(n, x, y, w, final):
        key = n["key"]
        spec = session["anims"].get(key)
        closing = bool(spec) and not spec["opening"]
        tag = new_hit(("group", n))
        r_ = canvas.create_rectangle(x, y, x + w, y + 22, fill=BG, outline="", tags=(tag,))
        arrow = "▾ " if (n["open"] and not closing) else "▸ "
        _, h = ctext(x + n["indent"] + 2, y + 4, arrow + n["text"], FONTS["hdr"], n["color"], tags=(tag,))
        canvas.coords(r_, x, y, x + w, y + h + 8)
        y += h + 8
        if n["open"] or spec:
            y0 = y
            y = draw_nodes(n["children"], x, y, w, final)
            H = y - y0
            if spec:
                cover = canvas.create_rectangle(x - 1, y, x + w + 1, y + 3, fill=BG, outline="")
                session["actx"] = {"key": key, "kind": "group", "H": H, "y0": y0, "cover": cover, "x0": x - 1,
                                   "x1": x + w + 1, "dy": 0}
        return y

    def draw_nodes(nodes, x, y, w, final):
        for n in nodes:
            t = n["t"]
            if t == "section":
                _, h = ctext(x + 2, y + 8, n["text"], FONTS["sec"], DIM)
                y += 8 + h + 4
            elif t == "text":
                _, h = ctext(x + 2, y + 4, n["text"], FONTS["line"], DIM)
                y += 4 + h + 4
            elif t == "card":
                y = draw_card(n["row"], x, y, w, final)
            elif t == "group":
                y = draw_group(n, x, y, w, final)
        return y

    def group_node(key, text, color, indent, persist, default, children):
        store = ui_state if persist else session
        return {"t": "group", "key": key, "text": text, "color": color, "indent": indent, "persist": persist,
                "default": default, "open": bool(store.get(key, default)), "children": children}

    def cards(rows, extra_line=False):
        return [{"t": "card", "row": dict(r, line=r.get("extra", r.get("line", ""))) if extra_line else r} for r in rows]

    def league_nodes(rows, prefix, default_open=False, indent=14):
        out = []
        for league in dict.fromkeys(r["league"] for r in rows):
            games = [r for r in rows if r["league"] == league]
            live_n = sum(r["state"] == "in" for r in games)
            is_leagues = prefix == "leagues"
            out.append(group_node(
                f"{prefix}:{league}",
                f"{league} · {len(games)}" + (f" · {live_n} live" if live_n and is_leagues else ""),
                COLORS["in"] if live_n and is_leagues else FG, indent, True,
                (live_n > 0) if is_leagues else default_open, cards(games, True)))
        return out

    def build_nodes():
        results, pin_results, playoffs, leagues = last["args"]
        live_view = ui_state.get("view", "full") == "live"
        if live_view:
            results = [r for r in results if r["state"] == "in"]
            pin_results = [r for r in pin_results if r["state"] == "in"]
            playoffs = [r for r in playoffs if r["state"] == "in"]
            leagues = [r for r in leagues if r["state"] == "in"]
        nodes = []
        if live_view and not (results or pin_results or playoffs or leagues):
            nodes.append({"t": "text", "text": "No live games"})
        if pin_results:
            nodes += [{"t": "section", "text": "Tracked Games"}] + cards(pin_results)
        if results:
            nodes += [{"t": "section", "text": "My Teams"}] + cards(results)
        elif not (pin_results or playoffs or leagues or live_view):
            nodes.append({"t": "text", "text": "No games in the next 7 days"})
        if leagues:
            nodes += [{"t": "section", "text": "Leagues"}] + league_nodes(leagues, "leagues", indent=0)
        if playoffs:
            nodes.append({"t": "section", "text": "Playoffs"})
            live = [r for r in playoffs if r["state"] == "in"]
            if live:
                nodes.append(group_node("live", f"Live · {len(live)}", COLORS["in"], 0, False, True, cards(live, True)))
            for title, state, key in (("Upcoming Today", "pre", "upcoming"), ("Previous", "post", "previous")):
                rows = [r for r in playoffs if r["state"] == state]
                if rows:
                    nodes.append(group_node(key, f"{title} · {len(rows)}", FG, 0, True, False, league_nodes(rows, key)))
        return nodes

    def draw_all(final=False):
        if not last:
            return 0
        canvas.delete("all")
        session["hits"].clear()
        session["actx"] = None
        cw = max(canvas.winfo_width(), MIN_BODY_W)
        y = draw_nodes(build_nodes(), 0, 2, cw, final)
        total = int(y + 4)
        canvas.configure(scrollregion=(0, 0, cw, total))
        session["total"] = total
        ctx = session["actx"]
        if ctx:  # tag everything drawn after the animated block so a frame can move it with one call
            ids = canvas.find_all()
            for i in ids[ids.index(ctx["cover"]) + 1:]:
                canvas.addtag_withtag("abelow", i)
            ctx["cw"] = cw
            apply_frame()
        return total

    def compute_sig():
        return json.dumps([last["args"], ui_state, sorted(session["expanded"]),
                           {k: session["details"].get(k) for k in session["expanded"]}, session.get("live")],
                          default=str, sort_keys=True)

    # ---- animations: expand / collapse of games and groups -------------------------
    def apply_frame():
        """Cheap per-frame update of the animating block: cover, everything below it, and the card background."""
        ctx = session["actx"]
        spec = session["anims"].get(ctx["key"]) if ctx else None
        if not ctx or not spec:
            return
        H = ctx["H"]
        vis = visible(spec, H)
        session["vis"][ctx["key"]] = vis
        dy = vis - H
        canvas.move("abelow", 0, dy - ctx["dy"])
        ctx["dy"] = dy
        y0 = ctx["y0"]
        if ctx["kind"] == "card":
            gap = GAP
            canvas.coords(ctx["cover"], ctx["x0"], y0 + vis + gap, ctx["x1"], y0 + H + gap + 3)
            cx0, ytop, cx1 = ctx["geo"]
            if ctx["bg"]:
                canvas.coords(ctx["bg"], *rr_points(cx0, ytop, cx1, ctx["bottom"] + dy, 10))
            if ctx["hit"]:
                canvas.coords(ctx["hit"], cx0 + 3, ytop + 3, cx1 - 3, ctx["bottom"] + dy - 3)
        else:
            canvas.coords(ctx["cover"], ctx["x0"], y0 + vis, ctx["x1"], y0 + H + 3)
        canvas.configure(scrollregion=(0, 0, ctx["cw"], session["total"] + int(dy)))

    def finish_anim(key):
        cb = session["anims"].pop(key).get("on_done")
        if cb:
            cb()

    def anim_step():
        now = _time.perf_counter()
        apply_frame()
        finished = [k for k, sp in session["anims"].items() if now - sp["t0"] >= sp["dur"]]
        if finished:
            for k in finished:
                finish_anim(k)
            session["sig"] = compute_sig()
            draw_all()
        if session["anims"]:
            root.after(4, anim_step)
        else:
            session["looping"] = False
            fit()

    def start_anim(key, opening, from_px=None, on_done=None, dur=0.22):
        for other in [k for k in session["anims"] if k != key]:
            finish_anim(other)  # one animation at a time: jump the previous one to its end
        session["anims"][key] = {"t0": _time.perf_counter(), "dur": dur, "opening": opening, "from": from_px,
                                 "on_done": on_done}
        total = draw_all()  # drawn at full height; apply_frame() positions it for t = 0
        if opening and not user_sized["on"]:  # size the window for the end state once
            canvas.configure(height=max(canvas.winfo_height(), min(total, int(root.winfo_screenheight() * 0.7))))
        if not session["looping"]:
            session["looping"] = True
            root.after(0, anim_step)

    def set_open(n, value):
        store = ui_state if n["persist"] else session
        store[n["key"]] = value
        if n["persist"]:
            save_state(ui_state)

    def toggle_group(n):
        key = n["key"]
        if key in session["anims"]:
            return
        store = ui_state if n["persist"] else session
        if store.get(key, n["default"]):
            start_anim(key, False, on_done=lambda: set_open(n, False))  # shrink first, then flip the state
        else:
            set_open(n, True)
            start_anim(key, True)

    def toggle_expand(g):
        k = gkey(g)
        key = "game:" + k
        if key in session["anims"]:
            return
        if k in session["expanded"]:
            start_anim(key, False, on_done=lambda: session["expanded"].discard(k))
            return
        session["expanded"].add(k)
        session["games"][k] = g
        session["details"].pop(k, None)
        start_anim(key, True)

        def work():
            fetch_details(g)

            def arrived():
                if k in session["expanded"]:  # grow from the "Loading" height to the full details
                    start_anim(key, True, from_px=session["vis"].get(key))
            root.after(0, arrived)
        threading.Thread(target=work, daemon=True).start()

    def render(results, pin_results, playoffs, leagues=()):
        last["args"] = (results, pin_results, playoffs, leagues)  # unfiltered, so view changes can re-render
        stamp.config(text="Last Refreshed " + datetime.now().strftime("%I:%M %p").lstrip("0"))
        any_live = any(r["state"] == "in" for grp in (results, pin_results, playoffs, leagues) for r in grp)
        if any_live != session.get("any_live"):
            session["any_live"] = any_live
            schedule()  # switch between normal and live cadence right away
        live_now = sum(r["state"] == "in" for r in playoffs)
        if live_now and not session["live_prev"]:
            session["live"] = True  # newly live playoff games open automatically
        session["live_prev"] = live_now
        sig = compute_sig()
        if sig == session["sig"]:
            return  # nothing changed
        session["sig"] = sig
        draw_all()
        fit()

    def refresh():
        def work():
            res, pres, po, lg = fetch_all(entries), fetch_pinned(list(pins)), playoff_games(), league_games()
            shown = {r["_key"] for r in res + pres if r.get("_key")}
            po = [r for r in po if r.get("_key") not in shown]  # already listed above
            lg = [r for r in lg if r.get("_key") not in shown]
            for k in list(session["expanded"]):
                if k in session["games"]:
                    fetch_details(session["games"][k])
            root.after(0, lambda: render(res, pres, po, lg))
        threading.Thread(target=work, daemon=True).start()

    timer = {"id": None}

    def schedule():
        if timer["id"]:
            root.after_cancel(timer["id"])
        secs = int(ui_state.get("refresh_seconds", default_refresh))
        live_secs = int(ui_state.get("live_refresh_seconds", 15))
        if session.get("any_live") and live_secs:
            secs = min(secs, live_secs)  # poll faster while something is live
        timer["id"] = root.after(secs * 1000, tick)

    def tick():
        refresh()
        schedule()

    def track_dialog():
        win = tk.Toplevel(root)
        win.title("Track a game")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        dark_titlebar(win)
        league = tk.StringVar(value=LEAGUES[0][0])
        date = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        games = []
        top = tk.Frame(win, bg=BG)
        top.pack(padx=10, pady=8)
        styled_option(top, league, [l[0] for l in LEAGUES], width=16).grid(row=0, column=0)
        tk.Entry(top, textvariable=date, width=10, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                 highlightthickness=1, highlightbackground=PANEL, highlightcolor=DIM, font=UI_FONT).grid(row=0, column=1, padx=6, ipady=3)
        lb = tk.Listbox(win, width=64, height=12, selectmode="extended", bg=PANEL, fg=FG, selectbackground="#34d399",
                        selectforeground="#10201a", relief="flat", bd=0, highlightthickness=0, activestyle="none",
                        font=("Consolas", 9) if sys.platform == "win32" else ("DejaVu Sans Mono", 9))
        lb.pack(padx=10)
        status = tk.Label(win, text="Date is YYYYMMDD", bg=BG, fg=DIM, font=("Segoe UI", 8))
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

        styled_button(top, "Load", load).grid(row=0, column=2)
        styled_button(win, "Track selected", add).pack(pady=(0, 12))
        load()

    def settings_dialog():
        win = tk.Toplevel(root)
        win.title("Settings")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        dark_titlebar(win)
        tk.Label(win, text="Opacity", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, padx=16, pady=(16, 4), sticky="w")
        pct = tk.StringVar()
        val = tk.Spinbox(win, from_=30, to=100, width=4, textvariable=pct, justify="right", bg=PANEL, fg=FG,
                         insertbackground=FG, buttonbackground=PANEL, relief="flat", highlightthickness=1,
                         highlightbackground=PANEL, highlightcolor=DIM, font=UI_FONT)
        val.grid(row=0, column=1, padx=16, pady=(16, 4), sticky="e", ipady=2)

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
                         bg=BG, fg=FG, troughcolor=PANEL, highlightthickness=0, bd=0, sliderrelief="flat",
                         activebackground="#8a8f98")
        scale.set(int(ui_state.get("opacity", 0.95) * 100))
        scale.grid(row=1, column=0, columnspan=2, padx=16, pady=(0, 10))
        scale.bind("<ButtonRelease-1>", on_release)
        tk.Label(win, text="Refresh every", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=2, column=0, padx=16, pady=(6, 4), sticky="w")
        cur = int(ui_state.get("refresh_seconds", default_refresh))
        choice = tk.StringVar(value=next((l for l, v in REFRESH_CHOICES if v == cur), f"{cur} seconds"))

        def on_refresh(label):
            ui_state["refresh_seconds"] = dict(REFRESH_CHOICES)[label]
            save_state(ui_state)
            schedule()  # restart the countdown with the new cadence

        om = styled_option(win, choice, [l for l, _ in REFRESH_CHOICES], command=on_refresh, width=12)
        om.grid(row=2, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="While games are live", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=3, column=0, padx=16, pady=(6, 4), sticky="w")
        cur_live = int(ui_state.get("live_refresh_seconds", 15))
        live_choice = tk.StringVar(value=next((l for l, v in LIVE_REFRESH_CHOICES if v == cur_live), f"{cur_live} seconds"))

        def on_live_refresh(label):
            ui_state["live_refresh_seconds"] = dict(LIVE_REFRESH_CHOICES)[label]
            save_state(ui_state)
            schedule()

        styled_option(win, live_choice, [l for l, _ in LIVE_REFRESH_CHOICES], command=on_live_refresh, width=12).grid(
            row=3, column=1, padx=16, pady=(6, 4), sticky="e")
        styled_button(win, "Close", win.destroy).grid(row=4, column=1, padx=16, pady=(10, 16), sticky="e")
        win.update_idletasks()
        win.geometry(f"+{root.winfo_x() + 30}+{root.winfo_y() + 30}")

    def untrack_menu(event):
        items = [(f"Untrack {p['label']} ({p['date']})", lambda p=p: untrack(p)) for p in list(pins)]
        popup_menu(event.x_root, event.y_root, items or [("No tracked games", lambda: None)])

    def untrack(p):
        pins.remove(p)
        save_pinned(pins)
        refresh()

    # drag to move
    drag = {}
    def start(e):
        close_menu()
        if e.widget not in (grip, scroll):  # grip resizes, scrollbar scrolls
            drag["x"], drag["y"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()
            drag["moved"] = False
    def move(e):
        if e.widget not in (grip, scroll) and "x" in drag:
            drag["moved"] = True
            root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    def hit_at(e):
        if e.widget is not canvas:
            return None
        x, y = canvas.canvasx(e.x), canvas.canvasy(e.y)
        for i in reversed(canvas.find_overlapping(x, y, x, y)):
            for t in canvas.gettags(i):
                if t in session["hits"]:
                    return session["hits"][t]
        return None

    def on_release(e):
        h = hit_at(e)
        if not h or drag.get("moved"):
            return
        if h[0] == "group":
            toggle_group(h[1])
        elif h[1].get("game"):
            toggle_expand(h[1]["game"])

    def on_motion(e):
        h = hit_at(e)
        canvas.configure(cursor="hand2" if h and (h[0] == "group" or h[1].get("game")) else "")
    canvas.bind("<Motion>", on_motion)

    def restart():
        """Start a fresh copy of this script (picks up code changes from git pull), then close this one."""
        import subprocess
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], cwd=HERE)
        root.destroy()
    def update_and_restart():
        """git pull (fast-forward only) in the widget's repo, then restart on success."""
        import subprocess
        prev = stamp.cget("text")
        stamp.config(text="Updating...")

        def work():
            try:
                r = subprocess.run(["git", "pull", "--ff-only"], cwd=HERE, capture_output=True, text=True, timeout=60,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
                ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
            except Exception as ex:  # git missing, timeout, ...
                ok, out = False, str(ex)
            root.after(0, lambda: done(ok, out))

        def done(ok, out):
            if ok:
                restart()
            else:
                stamp.config(text=prev)
                styled_message("Update failed", out[-600:] or "git pull failed")

        threading.Thread(target=work, daemon=True).start()


    def popup(e):
        h = hit_at(e)
        url = h[1].get("url") if h and h[0] == "game" else None
        items = []
        if url:
            import webbrowser
            items += [("Open game on ESPN", lambda: webbrowser.open(url)), None]

        def toggle_top():
            topmost.set(not topmost.get())
            root.attributes("-topmost", topmost.get())
        items += [("Track a game...", track_dialog), ("Untrack a game...", lambda: untrack_menu(e)),
                  ("Refresh", refresh), ("Settings...", settings_dialog),
                  ("Always on top", toggle_top, topmost.get()), None,
                  ("Update & Restart", update_and_restart), ("Restart", restart), ("Quit", root.destroy)]
        popup_menu(e.x_root, e.y_root, items)

    # Bound on the toplevel, so every child widget (rows, labels) drags/pops up too.
    root.bind("<Button-1>", start)
    root.bind("<B1-Motion>", move)
    root.bind("<ButtonRelease-1>", on_release)
    root.bind("<Button-3>", popup)
    root.update_idletasks()
    apply_layout()
    threading.Thread(target=icon_precompute, daemon=True).start()
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
    def row(name, sport, league, line, detail, comp, tint=None, state="in", next_line=""):
        return {"name": name, "state": state, "line": line, "detail": detail, "tint": tint, "url": "https://www.espn.com/",
                "next": next_line,
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
    return [row("San Francisco 49ers (4-1)", "football", "nfl", "@ Dallas Cowboys (3-2)", "21-17  Q3 5:12", nfl, tint="#aa0000"),
            row("San Francisco Giants (85-77)", "baseball", "mlb", "@ Los Angeles Dodgers (98-64)", "3-2  Top 7th", mlb, tint="#fd5a1e"),
            row("(4) Golden State Warriors (48-34)", "basketball", "nba", "@ (1) Boston Celtics (64-18)", "78-74  Q3 5:12", nba, tint="#1d428a"),
            row("New Jersey Devils (3-1-0)", "hockey", "nhl", "@ Boston Bruins (2-2-0)", "2-1  P2 6:47", nhl, tint="#ce1126"),
            row("Arsenal (6-1-2)", "soccer", "eng.1", "vs Chelsea (5-2-2)", "1-1  67'", soc, tint="#ef0107"),
            row("#12 San Diego State Aztecs Football (7-2)", "football", "college-football", "vs Boise State (8-1)",
                "W 31-24  Final", {}, tint="#c41230", state="post",
                next_line="Next: @ #7 Boise State (8-1) \u00b7 Sat Oct 10 6:00 PM")]


def demo_leagues():
    g = lambda lg, name, state, detail, tint: {"name": name, "state": state, "line": "", "league": lg, "detail": detail,
                                               "tint": tint, "info": "", "graphic": None, "_key": (lg, name),
                                               "url": "https://www.espn.com/"}
    return [g("MLB", "Boston Red Sox @ Toronto Blue Jays", "in", "2-1  Bot 4th", "#134a8e"),
            g("MLB", "Seattle Mariners @ Houston Astros", "pre", "Sat Oct 3 8:10 PM", "#eb6e1f"),
            g("MLB", "Chicago Cubs @ Milwaukee Brewers", "post", "4-2  Final", "#ffc52f"),
            g("NFL", "Green Bay Packers @ Chicago Bears", "pre", "Sun Oct 4 1:00 PM", "#0b162a"),
            g("NBA", "Miami Heat @ New York Knicks", "pre", "Sun Oct 4 7:00 PM", "#f58426")]


if __name__ == "__main__":
    if "--demo" in sys.argv:  # preview the live-game graphics with fake data (no network)
        fetch_all = lambda entries: demo_data()
        fetch_pinned = lambda pins: []
        playoff_games = lambda: []
        league_games = demo_leagues
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
    elif "--debug-seeds" in sys.argv:  # where does ESPN put playoff seeds right now?
        today = datetime.now().astimezone().date()
        for name, sport, league in PLAYOFF_LEAGUES:
            sm = seed_map(sport, league)
            print(f"{name}: standings seeds for {len(sm)} teams {dict(list(sm.items())[:6])}")
            try:
                events = [e for e in fetch_scoreboard(sport, league, f"{today:%Y%m%d}") if is_postseason(e)]
            except Exception as ex:
                print("   scoreboard error:", ex)
                continue
            for e in events[:2]:
                comp = e["competitions"][0]
                print("   game:", summarize_game(e, sport, league)[1] if summarize_game(e, sport, league) else e.get("name"))
                for c in comp.get("competitors", []):
                    keys = sorted(c.keys())
                    print("     competitor keys:", keys, "| seed/rank fields:",
                          {k: c[k] for k in keys if "seed" in k.lower() or "rank" in k.lower()},
                          {k: c.get("team", {})[k] for k in c.get("team", {}) if "seed" in k.lower() or "rank" in k.lower()})
                print("     series:", json.dumps(comp.get("series"), default=str)[:300])
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
        for r in fetch_pinned(load_pinned()) + fetch_all(load_config()["teams"]) + league_games() + playoff_games():
            print(f'{r["name"]:<28} {r["line"]:<10} {r["detail"]} {r.get("info", "")}')
    else:
        run_gui()
