"""Always-on-top desktop widget that tracks favorite sports teams.

Uses only the Python standard library (tkinter + urllib) and ESPN's public
JSON endpoints. Edit teams.json to choose teams.

Drag to move, right-click for menu (refresh / always-on-top / quit).
"""
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

API = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{team}/schedule"
SEASON_TYPE = "https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/seasons/{year}/types/2"  # 2 = regular season
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


def get_json(url, timeout=10):
    """GET an ESPN endpoint as JSON (gzip-compressed on the wire: ESPN payloads shrink ~10x)."""
    req = urllib.request.Request(url, headers={"User-Agent": "sports-widget/1.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


LOGO_DIR = os.path.join(HERE, "logos")


def logo_path(url, size):
    import hashlib
    return os.path.join(LOGO_DIR, hashlib.md5(f"{url}|{size}|aa8".encode()).hexdigest()[:16] + ".png")


LOGO_SS = 8  # logos are fetched this many times larger and averaged down, which antialiases the edges


def _png_rgba(data):
    """Decode an 8-bit RGB/RGBA/palette PNG to (w, h, RGBA bytes), or None for anything else."""
    import struct
    import zlib
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, idat, plte, trns, hdr = 8, [], b"", b"", None
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
    if not hdr:
        return None
    w, h, depth, ctype, _c, _f, interlace = hdr
    if depth != 8 or interlace or ctype not in (2, 3, 6):
        return None
    bpp = {2: 3, 3: 1, 6: 4}[ctype]
    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    rows, prev = [], bytearray(stride)
    for y in range(h):
        ft, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            left = line[i - bpp] if i >= bpp else 0
            up, ul = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if ft == 1:
                line[i] = (line[i] + left) & 255
            elif ft == 2:
                line[i] = (line[i] + up) & 255
            elif ft == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif ft == 4:
                pa, pb, pc = abs(up - ul), abs(left - ul), abs(left + up - 2 * ul)
                line[i] = (line[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else ul)) & 255
        rows.append(line)
        prev = line
    out = bytearray()
    for line in rows:
        if ctype == 6:
            out += line
        elif ctype == 2:
            for i in range(0, stride, 3):
                out += line[i:i + 3] + b"\xff"
        else:
            for v in line:
                out += plte[v * 3:v * 3 + 3] + bytes([trns[v] if v < len(trns) else 255])
    return w, h, out


def _png_bytes(w, h, rgba):
    import struct
    import zlib

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _shrink_png(data, f):
    """Average f x f blocks of a PNG down to one pixel (alpha-weighted, so edges don't pick up a dark fringe)."""
    dec = _png_rgba(data)
    if not dec:
        return None
    w, h, px = dec
    ow, oh = w // f, h // f
    cols = []  # horizontal pass: per source row, f-pixel sums of (r*a, g*a, b*a, a)
    for y in range(oh * f):
        row = []
        base = y * w * 4
        for ox in range(ow):
            r = g = b = a = 0
            for i in range(base + ox * f * 4, base + (ox + 1) * f * 4, 4):
                al = px[i + 3]
                r += px[i] * al
                g += px[i + 1] * al
                b += px[i + 2] * al
                a += al
            row += (r, g, b, a)
        cols.append(row)
    out = bytearray()
    for oy in range(oh):  # vertical pass: f rows added together
        rows = cols[oy * f:(oy + 1) * f]
        for ox in range(ow):
            k = ox * 4
            r, g, b, a = (sum(rw[k + c] for rw in rows) for c in range(4))
            out += bytes((r // a, g // a, b // a, a // (f * f))) if a else b"\x00\x00\x00\x00"
    return _png_bytes(ow, oh, out)


def _fetch_logo(src, size):
    req = urllib.request.Request(f"https://a.espncdn.com/combiner/i?img={src}&w={size}&h={size}",
                                 headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
    return body if body.startswith(b"\x89PNG") else None


def _smooth_logo(src, size):
    """The logo at size x size: fetched at LOGO_SS times the size and averaged down; plain fetch if that can't be decoded."""
    big = _fetch_logo(src, size * LOGO_SS)
    if not big:
        return None
    try:
        return _shrink_png(big, LOGO_SS) or _fetch_logo(src, size)
    except Exception:
        return _fetch_logo(src, size)


def logo_file(url, size):
    """Local PNG of a team logo scaled to size x size px (ESPN's image resizer), downloaded once; None on failure.

    ESPN's "500-dark" variant is made for dark backgrounds (dark logos stay visible), so it is tried first.
    """
    path = logo_path(url, size)
    if os.path.exists(path):
        return path
    src = re.sub(r"^https?://[^/]+", "", url)
    body = None
    for cand in ([src.replace("/500/", "/500-dark/")] if "/500/" in src else []) + [src]:
        try:
            body = _smooth_logo(cand, size)
        except Exception:
            body = None
        if body:
            break
    if not body:
        return None
    try:
        os.makedirs(LOGO_DIR, exist_ok=True)
        with open(path + ".part", "wb") as f:
            f.write(body)
        os.replace(path + ".part", path)
        return path
    except OSError:
        return None


_cache, _cache_lock, _key_locks = {}, threading.Lock(), {}


def cached(key, max_age, fn):
    """fn() cached under `key` for `max_age` seconds. Concurrent callers of the same key share one request."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    with _cache_lock:
        lock = _key_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        data = fn()
        now = time.monotonic()
        _cache[key] = (now, data)
        if len(_cache) > 300:  # drop entries no caller would still accept (longest max_age is an hour)
            with _cache_lock:
                for k in [k for k, v in list(_cache.items()) if now - v[0] > 3600]:
                    _cache.pop(k, None)
                    _key_locks.pop(k, None)
        return data


def pmap(fn, items, workers=8):
    """list(map(fn, items)) with the calls run concurrently (they are almost all waiting on HTTP)."""
    items = list(items)
    if len(items) < 2:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def fetch_schedule(entry):
    return get_json(API.format(**entry))


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


STANDINGS = "https://site.api.espn.com/apis/v2/sports/{sport}/{league}/standings?level=3"  # level 3 = by division
STANDINGS_LEAGUES = [("NFL", "football", "nfl"), ("NBA", "basketball", "nba"), ("NHL", "hockey", "nhl"),
                     ("MLB", "baseball", "mlb"), ("WNBA", "basketball", "wnba")]
# College sections on the Standings tab: a Top 25 tab plus one tab per conference.
# Each conference is (tab label, name to match in ESPN's conference list, fallback ESPN group id).
COLLEGE_SECTIONS = {
    "CFB": {"title": "College Football", "sport": "football", "league": "college-football", "confs": [
        ("ACC", "acc", "1"), ("Big 12", "big 12", "4"), ("Big Ten", "big ten", "5"), ("Pac-12", "pac-12", "9"),
        ("SEC", "sec", "8"), ("American", "american", "151"), ("C-USA", "conference usa", "12"),
        ("MAC", "mid-american", "15"), ("Mountain West", "mountain west", "17"), ("Sun Belt", "sun belt", "37")]},
    "CBB": {"title": "Men's College Basketball", "sport": "basketball", "league": "mens-college-basketball", "confs": [
        ("ACC", "acc", "2"), ("Big 12", "big 12", "8"), ("Big East", "big east", "4"), ("Big Ten", "big ten", "7"),
        ("Pac-12", "pac-12", "21"), ("SEC", "sec", "23"), ("American", "american", "62"), ("A-10", "atlantic 10", "3"),
        ("Mountain West", "mountain west", "44"), ("WCC", "west coast", "29")]},
    "WCBB": {"title": "Women's College Basketball", "sport": "basketball", "league": "womens-college-basketball", "confs": [
        ("ACC", "acc", "2"), ("Big 12", "big 12", "8"), ("Big East", "big east", "4"), ("Big Ten", "big ten", "7"),
        ("Pac-12", "pac-12", "21"), ("SEC", "sec", "23"), ("American", "american", "62"), ("A-10", "atlantic 10", "3"),
        ("Mountain West", "mountain west", "44"), ("WCC", "west coast", "29")]},
    # College baseball has no AP poll (the first poll ESPN lists is used), and its conference ids are looked up by
    # name only: a conference ESPN doesn't list shows "Standings unavailable" rather than a wrong guess.
    "CBASE": {"title": "College Baseball", "sport": "baseball", "league": "college-baseball", "poll": "Top 25", "confs": [
        ("ACC", "acc", None), ("Big 12", "big 12", None), ("Big Ten", "big ten", None), ("SEC", "sec", None),
        ("American", "american", None), ("Big West", "big west", None), ("C-USA", "conference usa", None),
        ("Mountain West", "mountain west", None), ("Sun Belt", "sun belt", None), ("WCC", "west coast", None)]},
}
STANDINGS_KEYS = [a for a, _, _ in STANDINGS_LEAGUES] + list(COLLEGE_SECTIONS)
CONFERENCES = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard/conferences"
_conf_ids = {}
RANKINGS = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/rankings"
_seed_cache = {}


def standings_json(sport, league, max_age=600, group=None):
    """ESPN's standings response for a league (or one college conference via `group`), cached for `max_age` seconds."""
    url = STANDINGS.format(sport=sport, league=league)
    if group:
        url = url.split("?")[0] + f"?group={group}"
    return cached(("standings", sport, league, group), max_age, lambda: get_json(url))


def parse_standings(data):
    """Flatten ESPN's nested standings into [{"name": group, "rows": [{id, abbr, name, short, stats}]}]."""
    groups = []

    def walk(node):
        if not isinstance(node, dict):
            return
        entries = (node.get("standings") or {}).get("entries")
        if entries:
            rows = []
            for e in entries:
                t = e.get("team") or {}
                stats = {}
                for st in e.get("stats") or []:
                    v = st.get("displayValue", st.get("value"))
                    if v is not None:
                        stats[st.get("name")] = str(v)
                rows.append({"id": str(t.get("id", "")), "abbr": t.get("abbreviation", ""),
                             "name": t.get("displayName") or t.get("name", "?"),
                             "short": t.get("shortDisplayName") or t.get("displayName", "?"), "stats": stats})
            groups.append({"name": node.get("name") or node.get("abbreviation") or "", "rows": rows})
        for ch in node.get("children") or []:
            walk(ch)
    walk(data)
    return groups


def rankings_json(sport, league, max_age=600):
    return cached(("rankings", sport, league), max_age, lambda: get_json(RANKINGS.format(sport=sport, league=league)))


def conference_group(cfg, label, pattern, fallback):
    """ESPN's group id for a conference: looked up in ESPN's conference list (cached), else the built-in fallback."""
    key = (cfg["sport"], cfg["league"])
    if key not in _conf_ids:
        ids = []
        try:
            ids = get_json(CONFERENCES.format(**cfg)).get("conferences") or []
        except Exception:
            pass
        _conf_ids[key] = ids
    for c in _conf_ids[key]:
        names = [str(c.get("shortName", "")).lower(), str(c.get("name", "")).lower()]
        if label.lower() in names or any(n == pattern or n.startswith(pattern + " ") for n in names):
            return str(c.get("groupId") or fallback or "") or None
    return fallback


def parse_poll(data):
    """The AP Top 25 from ESPN's rankings response: [{rank, id, abbr, name, record, points}]."""
    polls = data.get("rankings") or []
    poll = next((p for p in polls if str(p.get("type", "")).lower() == "ap" or "ap" in str(p.get("name", "")).lower().split()),
                polls[0] if polls else None)
    rows = []
    for r in (poll or {}).get("ranks") or []:
        t = r.get("team") or {}
        full = t.get("displayName") or " ".join(x for x in (t.get("location"), t.get("name")) if x) or t.get("nickname", "?")
        rec = r.get("recordSummary") or (t.get("record") if isinstance(t.get("record"), str) else "") or ""
        rows.append({"rank": r.get("current"), "id": str(t.get("id", "")), "abbr": t.get("abbreviation", ""),
                     "name": full, "record": rec, "points": str(r.get("points", "")) if r.get("points") is not None else ""})
    return rows


def college_cells(stats):
    """(conference record, [overall record]) for a college conference-standings row."""
    g = stats.get
    conf = next((g(k) for k in ("vsconf", "vs. Conf.", "conferenceRecord", "leagueRecord", "conference") if g(k)), None)
    overall = g("overall") or (f'{g("wins")}-{g("losses")}' if g("wins") is not None else "-")
    return conf or "-", [overall]


def _num_stat(stats, name):
    try:
        return float(stats.get(name, "") or 0)
    except ValueError:
        return 0.0


def parse_standings_tree(data, league):
    """{"overall": rows, "conference": groups | None, "division": groups | None}, each ranked.

    ESPN nests conferences/leagues around divisions; the Overall and Conference views are built from the
    division-level groups. A league with a single flat group (WNBA) only has Overall.
    """
    leaves = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        name = node.get("name") or node.get("abbreviation") or ""
        one = parse_standings({"standings": node.get("standings"), "name": name}) if (node.get("standings") or {}).get("entries") else []
        if one:
            leaves.append((path + [name], one[0]["rows"]))
        for ch in node.get("children") or []:
            walk(ch, path + [name] if name else path)
    for ch in data.get("children") or []:
        walk(ch, [])
    if not leaves:
        leaves = [([data.get("name") or ""], g["rows"]) for g in parse_standings(data)]

    def ranked(rows):
        if league == "NHL":
            return sorted(rows, key=lambda r: (-_num_stat(r["stats"], "points"), -_num_stat(r["stats"], "wins")))
        return sorted(rows, key=lambda r: (-_num_stat(r["stats"], "winPercent"), -_num_stat(r["stats"], "wins")))

    allrows = ranked([r for _, rows in leaves for r in rows])
    out = {"overall": allrows, "conference": None, "division": None}
    if all(len(path) == 1 for path, _ in leaves):  # only one level of grouping: call it the conference view
        if len(leaves) > 1:
            out["conference"] = [{"name": path[0], "rows": ranked(rows)} for path, rows in leaves]
        return out
    byconf = {}
    for path, rows in leaves:
        byconf.setdefault(path[0], []).extend(rows)
    if len(byconf) > 1:
        out["conference"] = [{"name": n, "rows": ranked(rows)} for n, rows in byconf.items()]
    out["division"] = [{"name": path[-1], "rows": ranked(rows)} for path, rows in leaves]
    return out


def standing_cells(league, stats):
    """(record, [column values]) for a standings row, plus the column headers for the league."""
    g = stats.get
    rec = g("overall") if re.fullmatch(r"\d+-\d+(-\d+)?", g("overall", "") or "") else None
    if league == "NHL":
        rec = rec or f'{g("wins", "0")}-{g("losses", "0")}-{g("otLosses", "0")}'
        return rec, [g("points", "-"), g("gamesPlayed", "-")]
    if league == "NFL":
        t = g("ties", "0")
        rec = rec or f'{g("wins", "0")}-{g("losses", "0")}' + (f"-{t}" if t not in ("0", "0.0", "") else "")
        return rec, [g("winPercent", "-")]
    rec = rec or f'{g("wins", "0")}-{g("losses", "0")}'
    return rec, [g("winPercent", "-"), g("gamesBehind", "-")]


STANDINGS_HEADERS = {"NHL": ["PTS", "GP"], "NFL": ["PCT"]}


def seed_map(sport, league):
    """{team id: playoff seed} from ESPN's standings (cached for an hour; {} if unavailable)."""
    key = (sport, league)
    hit = _seed_cache.get(key)
    now = datetime.now().timestamp()
    if hit and now - hit[0] < hit[2]:
        return hit[1]
    seeds, ttl = {}, 3600
    try:
        data = standings_json(sport, league)

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
    state = _state_of(event)
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
    if ms == "" and os_ == "":  # no score from ESPN (yet): just the status, never a bare hyphen
        return state, f"{sep} {opp_name}", detail
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


def fetch_summary_cached(sport, league, event_id, max_age=20):
    return cached(("summary", sport, league, str(event_id)), max_age, lambda: fetch_summary(sport, league, event_id))


def win_bar(game):
    """Win-probability data for a live game's card ({a, b, names, colors}) or None."""
    try:
        d = game_detail_data(fetch_summary_cached(game["sport"], game["league"], game["id"]))
    except Exception:
        return None
    if d.get("home_win") is None:
        return None
    hw = round(d["home_win"] * 100)
    ca, cb = d.get("colors", ("#60a5fa", "#f59e0b"))
    return {"kind": "versus", "label": "Win probability", "a_name": d["away_abbr"], "a": 100 - hw,
            "b_name": d["home_abbr"], "b": hw, "a_color": ca, "b_color": cb}


def fetch_summary(sport, league, event_id):
    return get_json(SUMMARY.format(sport=sport, league=league, id=event_id))


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


def _logo(team):
    """Logo URL ESPN gives for a team object, or None."""
    return team.get("logo") or next((l.get("href") for l in team.get("logos") or [] if l.get("href")), None)


def comp_logos(comp):
    """[away, home] logo URLs of a game (the competitor order when there is no home/away)."""
    cs = comp.get("competitors", [])
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    pair = (away, home) if away and home else tuple(cs[:2])
    return [_logo(c.get("team", {})) for c in pair]


def comp_teams(comp, first=None):
    """Both teams of a game for the Scoreboard layout, [{logo, abbr, ha}]: `first` (a competitor) first, else away then home."""
    cs = comp.get("competitors", [])
    if len(cs) != 2:
        return []
    if first is not None and first in cs:
        order = [first] + [c for c in cs if c is not first and c != first]
    else:
        away = next((c for c in cs if c.get("homeAway") == "away"), None)
        order = [away, [c for c in cs if c is not away][0]] if away else list(cs)
    return [{"logo": _logo(c.get("team") or {}), "ha": c.get("homeAway", ""),
             "abbr": (c.get("team") or {}).get("abbreviation") or (c.get("athlete") or {}).get("shortName") or "?"}
            for c in order]


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
    sit = comp.get("situation") or {}
    # per-team remaining timeouts / ABS challenges: dots under each team in the Scoreboard layout
    side = lambda k, words: next((int(v) for key, v in sit.items() if key.lower().startswith(k) and isinstance(v, (int, float))
                                  and any(w in key.lower() for w in words)), None)
    for words, total in ((("timeout",), 3), (("challenge",), 2)):
        h_, a_ = side("home", words), side("away", words)
        if h_ is not None or a_ is not None:
            out.append({"kind": "timeouts", "home": h_ or 0, "away": a_ or 0, "total": total})
    return out or None


def fresh_event(entry, event):
    """The live game from ESPN's scoreboard, fetched now (schedule data lacks live scores and situation)."""
    today = datetime.now().astimezone().date()
    rng = f"{(today - timedelta(days=1)):%Y%m%d}-{today:%Y%m%d}"
    try:
        for e in fetch_scoreboard(entry["sport"], entry["league"], rng):
            if str(e.get("id")) == str(event.get("id")) and e.get("competitions"):
                return e
    except Exception:
        pass
    return None


def pick_event(events, days=7):
    """Live game, else the next game within `days`; None if neither."""
    state_of = _state_of
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
    """pre / in / post for an event. Schedule data sometimes lacks `state` but says `completed`."""
    for st in ((e["competitions"][0].get("status") or {}).get("type"), (e.get("status") or {}).get("type")):
        if st:
            if st.get("state"):
                return st["state"]
            if st.get("completed"):
                return "post"
    return "pre"


def recent_result(events, hours=24 * 14):
    """Most recent completed game that started within the last `hours` hours (default: two weeks)."""
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


def scoreboard_event(sport, league, event):
    """The same game from ESPN's scoreboard (whose competitors carry records), or None. Cached for 10 minutes."""
    d = _parse_date(event.get("date"))
    if not d:
        return None
    center = (d - timedelta(hours=5)).date()  # ESPN's day follows US Eastern time
    rng = f"{center - timedelta(days=1):%Y%m%d}-{center + timedelta(days=1):%Y%m%d}"

    def get():
        try:
            return fetch_scoreboard(sport, league, rng)
        except Exception:
            return []
    events = cached(("sb_records", sport, league, rng), 600, get)
    return next((e for e in events if str(e.get("id")) == str(event.get("id")) and e.get("competitions")), None)


def with_records(event, sport, league):
    """Schedule data often lacks team records; borrow the scoreboard's copy of the game when it does."""
    comp = event["competitions"][0]
    started = _state_of(event) != "pre"
    if comp.get("competitors") and all(_record(c) for c in comp["competitors"]) and \
            not (started and any(_score(c) == "" for c in comp["competitors"])):
        return event
    return scoreboard_event(sport, league, event) or event


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
            if nxt:
                nxt = with_records(nxt, entry["sport"], entry["league"])
            s_next = summarize_event(nxt, entry["team"], entry["sport"], entry["league"]) if nxt else None
            if s_next:
                next_line = f"Next: {s_next[1]} \u00b7 {s_next[2]}"
            else:  # e.g. eliminated while the league's playoffs go on: say when next season starts
                start = season_start(entry, (data.get("season") or {}).get("year"))
                next_line = season_label(start) if start else "No upcoming game scheduled"
    if not event:  # nothing live, soon or just played: still list the team, dimmed
        return quiet_status(entry, events, base_name + (f" ({own})" if own else ""), (data.get("season") or {}).get("year"),
                            logo=_logo(team))
    fresh = None
    if _state_of(event) == "in":
        fresh = fresh_event(entry, event)  # live scores, records and situation come from the scoreboard
        event = fresh or event
    else:
        event = with_records(event, entry["sport"], entry["league"])
    s = summarize_event(event, entry["team"], entry["sport"], entry["league"])
    if not s:
        return None
    state, line, detail = s
    me = _find_me(event["competitions"][0], entry["team"]) or {}
    name = _rank(me, seeds_for(event, entry["sport"], entry["league"])) + base_name + (f" ({own})" if own else "")
    info, graphic = "", None
    if state == "in":
        comp_ = event["competitions"][0]
        info, graphic = situation_text(entry["sport"], comp_), situation_graphic(entry["sport"], comp_, entry["league"])
    parts = score_parts(event, entry["team"])
    return {"teams": comp_teams(event["competitions"][0], me or None), "logos": [_logo(me.get("team", {})) or _logo(team)],
            "score": parts["score"], "status": parts["status"], "clock": live_clock(event, entry["sport"]), "name": name, "state": state, "line": line, "detail": detail, "info": info, "graphic": graphic,
            "next": next_line,
            "_key": (entry["league"], str(event.get("id"))), "tint": tint_color(team),
            "url": event_url(event, entry["sport"], entry["league"]),
            "game": {"sport": entry["sport"], "league": entry["league"], "id": str(event.get("id"))}}


def season_start(entry, season_year=None):
    """When the team's next season starts (its first scheduled game, else the league's regular-season start date),
    or None if ESPN doesn't know yet. Cached for 6 hours."""
    def get():
        now = datetime.now(timezone.utc)
        year = int(season_year or now.year)
        try:  # next season's schedule, once it is published: the exact first game
            data = get_json(API.format(**entry) + f"?season={year + 1}")
            starts = [d for d in (_parse_date(e.get("date")) for e in data.get("events", []) if e.get("competitions")) if d and d > now]
            if starts:
                return min(starts)
        except Exception:
            pass
        for y in (year, year + 1):  # the league's announced regular-season start
            try:
                d = _parse_date(get_json(SEASON_TYPE.format(sport=entry["sport"], league=entry["league"], year=y)).get("startDate"))
            except Exception:
                continue
            if d and d > now:
                return d
        return None
    return cached(("season_start", entry["sport"], entry["league"], str(entry["team"])), 6 * 3600, get)


def season_label(d):
    """'Season starts Mar 25' (adds the year when it isn't this year), or 'Out of season' when unknown."""
    if not d:
        return "Out of season"
    d = d.astimezone()
    return f"Season starts {d:%b} {d.day}" + (f", {d.year}" if d.year != datetime.now().year else "")


def quiet_status(entry, events, name, season_year=None, logo=None):
    """Card for a team with no game soon: its last result, and its next game or when its next season starts."""
    sport, league = entry["sport"], entry["league"]
    done = [e for e in events if e.get("competitions") and _state_of(e) == "post"]
    last = max(done, key=lambda e: e.get("date", ""), default=None)
    s_last = summarize_event(last, entry["team"], sport, league) if last else None
    nxt = next_event(events)
    s_next = summarize_event(nxt, entry["team"], sport, league) if nxt else None
    return {"name": name, "state": "none", "line": f"Last: {s_last[1]} \u00b7 {s_last[2]}" if s_last else "",
            "detail": f"Next: {s_next[1]} \u00b7 {s_next[2]}" if s_next else season_label(season_start(entry, season_year)),
            "info": "", "graphic": None, "logos": [logo]}


def fetch_all(entries):
    def one(e):
        try:
            return team_status(e)
        except Exception as ex:  # network / schema errors shouldn't kill the widget
            return {"name": e.get("label", e["team"].upper()), "state": "err",
                    "line": f'{e["sport"]}/{e["league"]}', "detail": str(ex)[:40]}
    return [r for r in pmap(one, entries) if r]


def _try_scoreboard(sport, league, date):
    """fetch_scoreboard, returning the exception instead of raising (for pmap)."""
    try:
        return fetch_scoreboard(sport, league, date)
    except Exception as ex:
        return ex


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
    fetched = pmap(lambda l: _try_scoreboard(l[1], l[2], rng), PLAYOFF_LEAGUES)
    for (name, sport, league), events in zip(PLAYOFF_LEAGUES, fetched):
        if isinstance(events, Exception):
            if debug:
                print(f"{name}: error {events}")
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
            parts = score_parts(e)
            row = {"logos": comp_logos(comp), "teams": comp_teams(comp), "name": matchup, "state": state, "line": name + (f" · {extra}" if extra else ""),
                   "_key": (league, str(e.get("id"))), "tint": home_tint(comp), "url": event_url(e, sport, league),
                   "game": {"sport": sport, "league": league, "id": str(e.get("id"))},
                   "league": name, "extra": extra, "score": parts["score"], "status": parts["status"],
                   "clock": live_clock(e, sport),
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
LAYOUT_CHOICES = [("Default", "default"), ("Scoreboard", "scoreboard")]
DOCK_CHOICES = [("Off", "off"), ("Left edge", "left"), ("Right edge", "right")]
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
    fetched = pmap(lambda l: _try_scoreboard(l[1], l[2], f"{today:%Y%m%d}"), LEAGUE_SECTION)
    for (name, sport, league), events in zip(LEAGUE_SECTION, fetched):
        if isinstance(events, Exception):
            continue
        for e in events:
            if not e.get("competitions") or is_postseason(e):
                continue
            summ = summarize_game(e, sport, league)
            if not summ:
                continue
            state, matchup, detail = summ
            comp = e["competitions"][0]
            parts = score_parts(e)
            out.append({"name": matchup, "state": state, "line": "", "league": name, "detail": detail, "logos": comp_logos(comp),
                        "teams": comp_teams(comp),
                        "score": parts["score"], "status": parts["status"], "clock": live_clock(e, sport),
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
    data = get_json(url)
    # Some responses only carry the season at league level; copy it onto events.
    lg_season = (data.get("leagues") or [{}])[0].get("season", {})
    events = data.get("events", [])
    for e in events:
        e.setdefault("season", lg_season if lg_season.get("type") else data.get("season", {}))
    return events


def fetch_scoreboard(sport, league, date):
    """Scoreboard events for a YYYYMMDD date or YYYYMMDD-YYYYMMDD range.

    Cached for a few seconds, so the many callers in one refresh (live teams, tracked games,
    Leagues, Playoffs) that want the same scoreboard share one request.
    """
    return cached(("scoreboard", sport, league, date), 5, lambda: _fetch_scoreboard(sport, league, date))


def _fetch_scoreboard(sport, league, date):
    """ESPN answers HTTP 400 to parameter combinations it dislikes, so fall back:
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
    days = [f"{start + timedelta(days=i):%Y%m%d}" for i in range((end - start).days + 1)]
    events, seen = [], set()
    for day_events in pmap(lambda day: _get_scoreboard(sport, league, day, None), days):
        for e in day_events:
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                events.append(e)
    return events


def summarize_game(event, sport=None, league=None):
    """Neutral summary (away @ home) for a pinned game."""
    comp = event["competitions"][0]
    status = comp.get("status", {}).get("type", {})
    state, detail = _state_of(event), status.get("shortDetail", "")
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
    elif _score(away) == "" and _score(home) == "":
        text = detail
    else:
        text = f"{_score(away)}-{_score(home)}  {detail}"
        if state == "post" and _date_label(event):
            text += f" \u00b7 {_date_label(event)}"
    return state, f"{ab(away)} @ {ab(home)}", text


def score_parts(event, team_abbr=None):
    """{"score": (a, b) | None, "status": text} for the big score on a card.

    With `team_abbr` the order is (that team, opponent) and a finished game gets a W/L/T; otherwise (away, home).
    """
    if _state_of(event) == "pre":
        return {"score": None, "status": ""}
    state = _state_of(event)
    comp = event["competitions"][0]
    cs = comp.get("competitors", [])
    detail = ((comp.get("status") or {}).get("type") or {}).get("shortDetail", "")
    result = ""
    if team_abbr:
        me = _find_me(comp, team_abbr)
        opp = next((c for c in cs if c is not me), None)
        if (me is None or opp is None) and len(cs) == 2:
            me, opp = cs
        pair = (me, opp)
    else:
        away = next((c for c in cs if c.get("homeAway") == "away"), None)
        home = next((c for c in cs if c.get("homeAway") == "home"), None)
        pair = (away, home) if away and home else (tuple(cs) if len(cs) == 2 else (None, None))
    if not pair[0] or not pair[1]:
        return {"score": None, "status": detail}
    a_, b_ = _score(pair[0]), _score(pair[1])
    if a_ == "" and b_ == "":
        return {"score": None, "status": detail}
    if team_abbr and state == "post":
        try:
            result = "W" if float(a_) > float(b_) else ("L" if float(a_) < float(b_) else "T")
        except ValueError:
            pass
    played = _date_label(event) if state == "post" else ""
    return {"score": (a_, b_), "status": " \u00b7 ".join(x for x in (result, detail, played) if x)}


CLOCK_STALE = 90  # stop running the clock locally this long after the last refresh (ESPN data is stale by then)


def live_clock(event, sport):
    """The game clock at fetch time, so the widget can run it between refreshes; None when it can't."""
    if _state_of(event) != "in":
        return None
    st = event["competitions"][0].get("status") or {}
    try:
        secs = float(st.get("clock"))
    except (TypeError, ValueError):
        return None
    if sport == "soccer":  # counts up in minutes; stoppage time ("45'+2'") is left alone
        m = re.fullmatch(r"(\d+)'", str(st.get("displayClock") or "").strip())
        return {"at": time.time(), "secs": secs, "up": True, "minute": int(m.group(1))} if m else None
    if sport in ("basketball", "hockey", "football") and secs > 0:
        return {"at": time.time(), "secs": secs, "up": False}
    return None


def tick_clock(text, clock, now=None):
    """`text` with its game clock run forward to `now` (ESPN doesn't say when the clock stops; refreshes resync it)."""
    el = min((time.time() if now is None else now) - clock["at"], CLOCK_STALE)
    if el <= 0:
        return text
    if clock["up"]:
        minute = clock["minute"] + int((clock["secs"] % 60 + el) // 60)
        return re.sub(r"(?<![+\d])\d+'(?!\+)", f"{minute}'", text, count=1)
    import math
    rem = max(0, math.ceil(clock["secs"] - el))
    m = re.search(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\.\d\b", text)
    if not m:
        return text
    new = f"{rem // 60}:{rem % 60:02d}" if ":" in m.group() or rem >= 60 else f"{max(0.0, clock['secs'] - el):.1f}"
    return text[:m.start()] + new + text[m.end():]


def pinned_status(pin):
    for e in fetch_scoreboard(pin["sport"], pin["league"], pin["date"]):
        if str(e.get("id")) == str(pin["id"]):
            s = summarize_game(e, pin["sport"], pin["league"])
            if s:
                parts = score_parts(e)
                return {"logos": comp_logos(e["competitions"][0]), "teams": comp_teams(e["competitions"][0]),
                        "score": parts["score"], "status": parts["status"], "clock": live_clock(e, pin["sport"]), "name": s[1], "state": s[0], "line": "", "detail": s[2],
                        "_key": (pin["league"], str(pin["id"])), "tint": home_tint(e["competitions"][0]),
                        "url": event_url(e, pin["sport"], pin["league"]),
                        "game": {"sport": pin["sport"], "league": pin["league"], "id": str(pin["id"])},
                        "info": situation_text(pin["sport"], e["competitions"][0]) if s[0] == "in" else "",
                        "graphic": situation_graphic(pin["sport"], e["competitions"][0], pin["league"]) if s[0] == "in" else None}
    return {"name": pin["label"], "state": "none", "line": "Game not found", "detail": ""}


def fetch_pinned(pins):
    def one(p):
        try:
            return pinned_status(p)
        except Exception as ex:
            return {"name": p["label"], "state": "err", "line": "Unavailable", "detail": str(ex)[:40]}
    return pmap(one, pins)


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

    def get(lg):
        try:
            return get_json(TEAMS_API.format(sport=lg[0], league=lg[1]), timeout=15)
        except Exception as ex:
            return ex
    for (sport, league), data in zip(leagues, pmap(get, leagues)):
        try:
            if isinstance(data, Exception):
                raise data
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


def aa_gear_pixels(size, fg, bg, teeth=8, ss=4):
    """Anti-aliased gear icon (toothed wheel with a centre hole) as rows of hex colors."""
    import math
    c = size / 2
    r_out, r_body, r_hole = c - 7.0, c - 10.0, 3.2
    f, b = _rgb(fg), _rgb(bg)
    step = 2 * math.pi / teeth
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            hit = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px + (sx + 0.5) / ss - c, py + (sy + 0.5) / ss - c
                    d = math.hypot(x, y)
                    ang = math.atan2(y, x) % step
                    in_tooth = abs(ang - step / 2) < step * 0.22  # centred tooth, ~44% of the pitch
                    on = d >= r_hole and (d <= r_body or (d <= r_out and in_tooth))
                    hit += on
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


def app_icon_pixels(size, ss=4):
    """Taskbar icon: dark tile with a light ring and a red live dot. Rows of hex colors."""
    tile, fg, dot = _rgb("#1e1e24"), _rgb("#f2f2f2"), _rgb("#e53935")
    c, R, w, rd = size / 2, size * 0.30, size * 0.09, size * 0.13
    dc = (size * 0.74, size * 0.26)
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0.0, 0.0, 0.0]
            for sy in range(ss):
                for sx in range(ss):
                    x, y = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                    col = tile
                    if abs(((x - c) ** 2 + (y - c) ** 2) ** 0.5 - R) <= w / 2:
                        col = fg
                    if (x - dc[0]) ** 2 + (y - dc[1]) ** 2 <= rd * rd:
                        col = dot
                    for i in range(3):
                        acc[i] += col[i]
            n = ss * ss
            row.append("#%02x%02x%02x" % tuple(int(v / n) for v in acc))
        rows.append(row)
    return rows


def show_in_taskbar(root):
    """Windows: give the borderless (overrideredirect) window a taskbar button that minimizes/restores it."""
    if sys.platform != "win32":
        return
    import ctypes
    user32 = ctypes.windll.user32
    GWL_STYLE, GWL_EXSTYLE = -16, -20
    WS_MINIMIZEBOX, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00020000, 0x00000080, 0x00040000
    root.update_idletasks()
    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
    # Lets a click on the taskbar button minimize the window, like a normal app.
    user32.SetWindowLongW(hwnd, GWL_STYLE, user32.GetWindowLongW(hwnd, GWL_STYLE) | WS_MINIMIZEBOX)
    # The taskbar only picks up the new style when the window is re-shown.
    root.withdraw()
    root.after(10, root.deiconify)


def run_gui():
    import tkinter as tk

    cfg = load_config()
    entries = cfg["teams"]
    default_refresh = int(cfg.get("refresh_seconds", 60))

    BG, FG, DIM = "#1e1e24", "#f2f2f2", "#9aa0a6"
    COLORS = {"in": "#34d399", "pre": DIM, "post": FG, "none": DIM, "err": "#f87171"}
    PANEL, HOVER = "#2a2a33", "#3a3a46"
    TRACK, LIVE_RED = "#3a3a44", "#ef4444"  # empty-bar track, LIVE badge
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

    if sys.platform == "win32":
        try:
            import ctypes
            # Own taskbar group and icon instead of being lumped in with pythonw.exe.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SportsWidget")
        except Exception:
            pass
    root = tk.Tk()
    root.title("Sports")
    try:
        app_icon = tk.PhotoImage(width=32, height=32)
        app_icon.put(" ".join("{" + " ".join(row) + "}" for row in app_icon_pixels(32)))
        root.iconphoto(True, app_icon)
    except tk.TclError:
        pass
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

    def screen_bounds():
        """Virtual-desktop rectangle (all monitors on Windows) as (left, top, right, bottom)."""
        if sys.platform == "win32":
            try:
                import ctypes
                gm = ctypes.windll.user32.GetSystemMetrics
                x, y = gm(76), gm(77)
                return x, y, x + gm(78), y + gm(79)
            except Exception:
                pass
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()

    def clamp_pos(x, y, w=200, h=60):
        """Keep a remembered position on screen (e.g. after a monitor was unplugged)."""
        l, t, r, b = screen_bounds()
        return max(l - w + 80, min(x, r - 80)), max(t, min(y, b - h))

    saved_pos, saved_size = ui_state.get("pos"), ui_state.get("size")
    if isinstance(saved_pos, list) and len(saved_pos) == 2:
        px, py = clamp_pos(int(saved_pos[0]), int(saved_pos[1]))
    else:
        px, py = 40, 40
    root.geometry(f"+{px}+{py}")

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
    gear_btn = tk.Canvas(hbar, width=34, height=34, bg=BG, highlightthickness=0, cursor="hand2")
    gear_btn.pack(side="right", padx=(8, 0))
    gear_img = tk.PhotoImage(width=34, height=34)
    gear_img.put(" ".join("{" + " ".join(row) + "}" for row in aa_gear_pixels(34, FG, BG)))
    gear_btn.create_image(17, 17, image=gear_img)
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

    # Tab bar (Games / Standings)
    tabbar = tk.Frame(root, bg=BG)
    tabbar.pack(fill="x", padx=12, pady=(0, 4))
    tab_pills = {}

    def make_tab(label, key):
        import tkinter.font as tkfont
        w_ = tkfont.Font(font=("Segoe UI", 9, "bold")).measure(label) + 26
        c = tk.Canvas(tabbar, width=w_, height=26, bg=BG, highlightthickness=0, cursor="hand2")
        shape = c.create_polygon(rr_points(1, 1, w_ - 1, 25, 8), smooth=True, fill=BG, outline=BG)
        txt = c.create_text(w_ / 2, 13, text=label, font=("Segoe UI", 9, "bold"), fill=DIM)
        c.pack(side="left", padx=(0, 6))
        c.bind("<ButtonRelease-1>", lambda e: set_tab(key))
        tab_pills[key] = (c, shape, txt)

    def style_tabs():
        cur = ui_state.get("tab", "games")
        for key, (c, shape, txt) in tab_pills.items():
            on = key == cur
            c.itemconfigure(shape, fill=PANEL if on else BG, outline=PANEL if on else BG)
            c.itemconfigure(txt, fill=FG if on else DIM)

    make_tab("Games", "games")
    make_tab("Standings", "standings")

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
    if isinstance(saved_size, list) and len(saved_size) == 2:  # the user had resized the window: restore that too
        user_sized["on"] = True
        root.geometry(f"{max(240, int(saved_size[0]))}x{max(120, int(saved_size[1]))}+{px}+{py}")
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
    def save_geometry():
        """Remember where the window is (and its size, if the user resized it)."""
        try:
            if docked():  # keep the floating position/height for when docking is turned off
                if user_sized["on"] and ui_state.get("size"):
                    ui_state["size"][0] = root.winfo_width()
                save_state(ui_state)
                return
            ui_state["pos"] = [root.winfo_x(), root.winfo_y()]
            if user_sized["on"] and ui_state.get("view", "full") != "title":
                ui_state["size"] = [root.winfo_width(), root.winfo_height()]
            elif not user_sized["on"]:
                ui_state.pop("size", None)
            save_state(ui_state)
        except tk.TclError:
            pass

    def quit_app():
        save_geometry()
        root.destroy()

    def grip_reset(_):
        user_sized["on"] = False
        root.geometry("")
        root.geometry(f"+{root.winfo_x()}+{root.winfo_y()}")
        fit()
        save_geometry()
    VIEWS = [("full", "Full"), ("live", "Live"), ("title", "Title")]
    layout = {}

    def apply_layout():
        mode = ui_state.get("view", "full")
        draw_view_icon(mode)
        hbar.pack_configure(pady=(8, 12) if mode == "title" else (8, 2))  # extra bottom space when only the title shows
        if mode == "title":
            tabbar.pack_forget()
        elif not tabbar.winfo_manager():
            tabbar.pack(fill="x", padx=12, pady=(0, 4), after=hbar)
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
            if not docked():  # no resizing while docked
                grip.pack(side="bottom", anchor="se", padx=2)
            container.pack(fill="both", expand=True, padx=(12, 4), pady=(0, 0))
            if not user_sized["on"]:
                root.geometry("")
            fit()

    def cycle_view(_=None):
        if view_tween["on"] or icon["busy"]:
            return  # let the running transition finish
        order = [m for m, _ in VIEWS if m != "title" or not docked()]  # no minimized title view while docked
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
    grip.bind("<ButtonRelease-1>", lambda e: save_geometry())

    topmost = tk.BooleanVar(value=True)
    pins = load_pinned()

    # ------------------------------------------------------------------------------------------
    # Canvas renderer. Everything is drawn on one canvas instead of one widget per label, so a redraw
    # (refresh, expand, collapse, view change) is a single repaint and cannot flicker.
    # ------------------------------------------------------------------------------------------
    import time as _time
    FONTS = {"score": ("Segoe UI", 20, "bold"), "name": ("Segoe UI", 10, "bold"), "line": ("Segoe UI", 9), "detb": ("Segoe UI", 9, "bold"),
             "sec": ("Segoe UI", 8, "bold"), "hdr": ("Segoe UI", 9, "bold"), "small": ("Segoe UI", 8),
             "smallb": ("Segoe UI", 8, "bold")}
    PAD, GAP = 10, 6
    LOGO_W, BB_W = 52, 112  # width reserved for a logo in front of the name; baseball bases/count graphic
    last = {}
    session = {"live_prev": 0, "expanded": set(), "details": {}, "games": {}, "sig": None,
               "anims": {}, "vis": {}, "hits": {}, "total": 0, "looping": False, "actx": None, "standings": {}, "college": {},
               "roll_last": {}, "rolls": {}, "roll_cells": [], "rolling": False,
               "clock_items": [], "stats": {}, "stats_redraw": False}

    def gkey(g):
        return f'{g["league"]}:{g["id"]}'

    def ctext(x, y, s_, font, fill, width=None, anchor="nw", tags=(), justify="left"):
        kw = {"text": s_, "font": font, "fill": fill, "anchor": anchor, "tags": tags, "justify": justify}
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
            n, gap = len(g["fills"]), 4
            seg = (W - gap * (n - 1)) / n
            for i, f in enumerate(g["fills"]):
                x = i * (seg + gap)
                c.create_line(x + 3, 7, x + seg - 3, 7, fill=TRACK, width=6, capstyle="round")
                if f > 0:
                    c.create_line(x + 3, 7, x + 3 + (seg - 6) * f, 7, fill="#34d399" if f < 1 else "#6b6b78",
                                  width=6, capstyle="round")
            return 14
        if kind == "versus":
            split = W * g["a"] / (g["a"] + g["b"])
            if split - 2 > 3:
                c.create_line(3, 20, split - 2, 20, fill=g.get("a_color", "#60a5fa"), width=6, capstyle="round")
            if W - 3 > split + 2:
                c.create_line(split + 2, 20, W - 3, 20, fill=g.get("b_color", "#f59e0b"), width=6, capstyle="round")
            unit = "%" if g["label"] == "Win probability" else ""
            fmt = lambda v: f"{v:g}{unit}"
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
            px = lambda yd: W * (yd + 10) / 120  # yards from the offense's goal line, with an end zone at each end
            seg = W / 12
            for i in range(12):  # two end zones and ten 10-yard segments
                col = "#52526a" if i in (0, 11) else "#7f3b3b" if g["red"] and i in (9, 10) else TRACK
                c.create_line(i * seg + 4.5, 17, (i + 1) * seg - 4.5, 17, fill=col, width=6, capstyle="round")
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

    def draw_details(x, y, w, bgc, d, win_shown=False):  # win_shown: no win probability here (shown on the card, or game over)
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
        if d.get("home_win") is not None and not win_shown:  # a card already showing it keeps it where it was
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
        if not (d["plays"] or d["scoring"] or d["stats"] or d.get("home_win") is not None and not win_shown):
            _, h = ctext(x, y, "No extra details from ESPN for this game", FONTS["small"], DIM)
            y += h
        return y - y0

    def fetch_details(g):
        try:
            session["details"][gkey(g)] = game_detail_data(fetch_summary_cached(g["sport"], g["league"], g["id"], max_age=5))
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

    # ---- score changes roll like an odometer wheel -----------------------------------
    ROLL_STEP, ROLL_MIN, ROLL_MAX = 0.09, 0.45, 1.1  # seconds per digit passed, shortest and longest roll

    def roll_seq(a, b):
        """Digits one wheel shows going from a to b: counting up through 0-9 like an odometer."""
        if a == b:
            return [b]
        if a.isdigit() and b.isdigit():
            seq, d = [a], int(a)
            while str(d) != b:
                d = (d + 1) % 10
                seq.append(str(d))
            return seq
        return [a, b]  # a digit appearing or disappearing (9 -> 10), or a non-digit

    # ---- seven-segment ("digital") digits, drawn as shapes so no font is needed ----------
    SEGMENTS = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
                "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g"}
    DIG_W, DIG_H, DIG_T, DIG_GAP = 16, 24, 4, 4

    def digital_text(xr, y, text, color, bgc):
        """Right-aligned seven-segment text ending at xr; returns its left edge. Unlit segments show faintly."""
        q = DIG_T / 2
        x = xr
        for ch in reversed(text):
            x -= DIG_W
            lit = SEGMENTS.get(ch, "")
            dim = blend(bgc, color, 0.14)
            xa, xb, h2 = x + q + 1, x + DIG_W - q - 1, DIG_H / 2

            def hseg(yc):
                return [xa, yc, xa + q, yc - q, xb - q, yc - q, xb, yc, xb - q, yc + q, xa + q, yc + q]

            def vseg(xc, ya, yb):
                return [xc, ya, xc + q, ya + q, xc + q, yb - q, xc, yb, xc - q, yb - q, xc - q, ya + q]
            shapes = {"a": hseg(y + q), "g": hseg(y + h2), "d": hseg(y + DIG_H - q),
                      "f": vseg(x + q, y + q + 1, y + h2 - 1), "b": vseg(x + DIG_W - q, y + q + 1, y + h2 - 1),
                      "e": vseg(x + q, y + h2 + 1, y + DIG_H - q - 1), "c": vseg(x + DIG_W - q, y + h2 + 1, y + DIG_H - q - 1)}
            for name, pts in shapes.items():
                if name in lit:
                    canvas.create_polygon(*pts, fill=color, outline="")
                elif ch != "-":
                    canvas.create_polygon(*pts, fill=dim, outline="")
            x -= DIG_GAP
        return x + DIG_GAP

    # ---- team logos: downloaded once to logos/, loaded on first use, cards redraw when they arrive -------------
    logo_imgs, logo_pending, logo_done = {}, set(), set()

    def logos_ready():
        logo_pending.clear()
        logo_done.clear()
        session["sig"] = None
        draw_all()
        session["sig"] = compute_sig()
        fit()

    def logo_img(url, size):
        key = (url, size)
        if key in logo_imgs:
            return logo_imgs[key]
        path = logo_path(url, size)
        if os.path.exists(path):
            try:
                logo_imgs[key] = tk.PhotoImage(file=path)
            except tk.TclError:
                logo_imgs[key] = None
            return logo_imgs[key]
        if key not in logo_pending and not session["anims"]:
            logo_pending.add(key)

            def work():
                if not logo_file(url, size):
                    logo_imgs[key] = None  # unavailable: don't keep retrying
                logo_done.add(key)
                if logo_pending <= logo_done:
                    root.after(0, logos_ready)  # the last outstanding logo arrived
            threading.Thread(target=work, daemon=True).start()
        return None

    def draw_score(xr, y, text, color, bgc, key):
        """One side's score, right-aligned at xr; rolls from the last value drawn for `key`. Returns the left edge."""
        prev = session["roll_last"].get(key)
        session["roll_last"][key] = text
        if ui_state.get("digital"):
            return digital_text(xr, y + 4, text, color, bgc)
        roll = session["rolls"].get(key)
        if prev is not None and prev != text and not (roll and roll["to"] == text):
            n = max(len(prev), len(text))
            old, new = (roll["to"] if roll else prev).rjust(n), text.rjust(n)
            seqs = [roll_seq(a, b) for a, b in zip(old, new)]
            steps = max(len(q) - 1 for q in seqs)
            roll = session["rolls"][key] = {"to": text, "seqs": seqs, "t0": _time.perf_counter(),
                                            "dur": max(ROLL_MIN, min(ROLL_MAX, steps * ROLL_STEP))}
            if not session["rolling"]:
                session["rolling"] = True
                root.after(0, roll_tick)
        if not roll:
            i, _h = ctext(xr, y, text, FONTS["score"], color, anchor="ne")
            return canvas.bbox(i)[0]
        import tkinter.font as tkfont
        f = tkfont.Font(font=FONTS["score"])
        hgt = f.metrics("linespace")
        x = xr
        for seq in reversed(roll["seqs"]):  # one wheel per digit, right to left
            cw = max(f.measure(ch) for ch in seq)
            cx, cy = x - cw / 2, y + hgt / 2
            anchor = canvas.create_rectangle(cx, cy, cx, cy, outline="", state="hidden")  # moves with the card
            items = [canvas.create_text(cx, cy, text="", font=FONTS["score"], fill=color) for _ in range(2)]
            session["roll_cells"].append({"roll": roll, "seq": seq, "anchor": anchor, "items": items,
                                          "color": color, "bg": bgc, "h": hgt})
            x -= cw
        roll_frame()
        return x

    def roll_frame():
        """Place every rolling digit for the current time: the old digit rolls up and away, the next rolls in."""
        now = _time.perf_counter()
        px0 = FONTS["score"][1] * root.winfo_fpixels("1p")
        for c in session["roll_cells"]:
            p = min((now - c["roll"]["t0"]) / c["roll"]["dur"], 1.0)
            v = (1 - (1 - p) ** 3) * (len(c["seq"]) - 1)  # ease out: fast start, settles onto the new digit
            k = min(int(v), len(c["seq"]) - 1)
            frac = v - k
            ax, ay = canvas.coords(c["anchor"])[:2]
            travel = c["h"] * 0.42  # how far a digit rolls before it is out of sight on the drum
            for item, idx, off in ((c["items"][0], k, -frac), (c["items"][1], k + 1, 1 - frac)):
                if idx >= len(c["seq"]) or abs(off) >= 1:
                    canvas.itemconfigure(item, text="")
                    continue
                d = abs(off)
                canvas.coords(item, ax, ay + off * travel)
                canvas.itemconfigure(item, text=c["seq"][idx], fill=blend(c["color"], c["bg"], d),
                                     font=(FONTS["score"][0], -max(6, round(px0 * (1 - 0.35 * d))), "bold"))

    def roll_tick():
        now = _time.perf_counter()
        roll_frame()
        done = [k for k, r_ in session["rolls"].items() if now - r_["t0"] >= r_["dur"]]
        for k in done:
            del session["rolls"][k]
        if done:
            draw_all()  # finished wheels go back to plain text
        if session["rolls"]:
            root.after(16, roll_tick)
        else:
            session["rolling"] = False

    def clock_tick():
        """Run the game clocks on live cards once a second between refreshes."""
        now = time.time()
        for item, base, clock in session["clock_items"]:
            try:
                text = tick_clock(base, clock, now)
                if canvas.itemcget(item, "text") != text:
                    canvas.itemconfigure(item, text=text)
            except tk.TclError:
                pass
        root.after(1000 - int(now * 1000) % 1000 + 5, clock_tick)  # just after each whole second

    def ensure_stats(g):
        """Team stats of a finished game (for the Scoreboard layout), fetched in the background on first use."""
        k = gkey(g)
        if k not in session["stats"] and not session["anims"]:
            session["stats"][k] = None

            def work():
                try:
                    session["stats"][k] = game_detail_data(fetch_summary_cached(g["sport"], g["league"], g["id"], max_age=600))
                except Exception:
                    session["stats"][k] = {"stats": []}
                root.after(0, stats_loaded)
            threading.Thread(target=work, daemon=True).start()
        return session["stats"].get(k)

    def stats_loaded():
        if session["stats_redraw"]:
            return
        session["stats_redraw"] = True

        def redraw():
            if session["anims"]:
                root.after(200, redraw)  # an expand/collapse is running: wait for it
                return
            session["stats_redraw"] = False
            session["sig"] = None
            draw_all()
            session["sig"] = compute_sig()
            fit()
        root.after(150, redraw)

    def score_width(text):
        if ui_state.get("digital"):
            return len(text) * (DIG_W + DIG_GAP) - DIG_GAP
        import tkinter.font as tkfont
        return tkfont.Font(font=FONTS["score"]).measure(text)

    def draw_scoreboard(r, cx0, cw_, y, bgc, tags, gl, tos, info):
        """Scoreboard layout: each team's logo with its score underneath at either side, the status and game details
        (diamond and count, down and possession, timeouts) in the free space between. Returns (bottom y, info left over)."""
        ix, ww = cx0 + PAD, cw_ - 2 * PAD
        COL = 72
        mx, mw = ix + ww / 2, ww - 2 * COL - 8
        top = y
        sc, teams = r.get("score"), r["teams"]
        c = [FG, FG]
        if sc:
            try:
                lead = (float(sc[0]) > float(sc[1])) - (float(sc[0]) < float(sc[1]))
            except ValueError:
                lead = 0
            hi = COLORS["in"] if r["state"] == "in" else FG
            c = [hi if lead >= 0 else DIM, hi if lead <= 0 else DIM]
        rk = r.get("_key") or (gkey(r["game"]) if r.get("game") else r["name"])
        colb = top
        counts = [tos.get(t["ha"]) for t in teams] if tos and r["state"] == "in" else [None, None]
        for i, t in enumerate(teams):
            cx = ix + COL / 2 if i == 0 else ix + ww - COL / 2
            yy = top
            img = logo_img(t["logo"], 44) if t.get("logo") else None
            if img:
                canvas.create_image(cx, yy, image=img, anchor="n", tags=tags)
            yy += 46
            if sc:
                draw_score(cx + score_width(sc[i]) / 2, yy - 3, sc[i], c[i], bgc, (rk, i))
                yy += 30
            _, h = ctext(cx, yy, t["abbr"], FONTS["smallb"], FG if r["state"] != "pre" else DIM, anchor="n", tags=tags)
            yy += h
            if counts[i] is not None:  # remaining timeouts / challenges: filled dots, unlabelled
                total = max(tos.get("total", 3), counts[i])
                x0 = cx - 5 * (total - 1)
                for k in range(total):
                    canvas.create_oval(x0 + 10 * k - 3, yy + 4, x0 + 10 * k + 3, yy + 10, fill=FG if k < counts[i] else bgc,
                                       outline=FG if k < counts[i] else DIM, tags=tags)
                yy += 12
            colb = max(colb, yy)
        my = top + 2
        live = r["state"] == "in"
        if live:
            canvas.create_polygon(rr_points(mx - 17, my, mx + 17, my + 14, 5), smooth=True, fill=LIVE_RED, outline=LIVE_RED, tags=tags)
            canvas.create_text(mx, my + 7, text="LIVE", fill="#ffffff", font=FONTS["sec"], tags=tags)
            my += 18
        elif r["state"] == "pre":
            sep = (r.get("line") or "@").split(" ")[0]
            _, h = ctext(mx, my + 4, sep if sep in ("vs", "@") else "@", FONTS["line"], DIM, anchor="n", tags=tags)
            my += 4 + h
        sid, h = ctext(mx, my, r.get("status") if sc else r["detail"], FONTS["detb"] if live else FONTS["line"],
                       COLORS.get(r["state"], FG), width=mw, anchor="n", tags=tags, justify="center")
        clock = r.get("clock") if live else None
        if clock:
            session["clock_items"].append((sid, canvas.itemcget(sid, "text"), clock))
            canvas.itemconfigure(sid, text=tick_clock(canvas.itemcget(sid, "text"), clock))
        my += h + 4
        if r["state"] == "post" and r.get("game"):  # a finished game: its team stats fill the middle
            d_ = ensure_stats(r["game"])
            if isinstance(d_, dict) and d_.get("stats"):
                flip = teams[0]["ha"] == "home" if teams[0].get("ha") else teams[0]["abbr"] == d_["home_abbr"]
                for label, a_, h_ in d_["stats"][:4]:
                    vl, vr = (h_, a_) if flip else (a_, h_)
                    ctext(mx - mw / 2, my, vl, FONTS["small"], FG, anchor="nw", tags=tags)
                    ctext(mx, my, label[:12], FONTS["small"], DIM, anchor="n", tags=tags)
                    _, h = ctext(mx + mw / 2, my, vr, FONTS["small"], FG, anchor="ne", tags=tags)
                    my += h
        lines = info.split("\n") if info else []
        bb = [g_ for g_ in gl if g_["kind"] == "baseball"]
        fb = next((g_ for g_ in gl if g_["kind"] == "football"), None)
        poss, show_poss = "", False
        if bb and live:
            my += graphics(mx - 52, my, bb, bgc, BB_W)
            for l_ in [l_ for l_ in lines if l_.startswith("AB:")]:
                for part in l_.split(" \u00b7 "):
                    _, h = ctext(mx, my, part, FONTS["small"], FG, anchor="n", tags=tags)
                    my += h
            lines = [l_ for l_ in lines if not l_.startswith(("Runners:", "Bases empty", "AB:"))]
            gl = [g_ for g_ in gl if g_ not in bb]
        elif live and lines and (fb or (r.get("game") or {}).get("sport") == "football"):
            parts = lines[0].split(" \u00b7 ")
            m_ = re.search(r"(\w+) ball", lines[0])
            poss = fb["off"] if fb else (m_.group(1) if m_ else "")
            show_poss = True
            _, h = ctext(mx, my, parts[0], FONTS["detb"], FG, width=mw, anchor="n", tags=tags, justify="center")
            my += h
            if parts[1:]:
                _, h = ctext(mx, my, " \u00b7 ".join(parts[1:]), FONTS["small"], DIM, width=mw, anchor="n", tags=tags, justify="center")
                my += h
            lines = lines[1:]
        if show_poss:  # an arrow for each team, filled in for the one with the ball
            my += 4
            _, h = ctext(mx, my, "Possession", FONTS["small"], DIM, anchor="n", tags=tags)
            for i, t in enumerate(teams):
                d_ = -1 if i == 0 else 1  # each arrow points at its own team's side
                ax, ay = mx + d_ * 46, my + 7
                has = bool(poss) and t["abbr"].upper() == poss.upper()
                canvas.create_polygon(ax - 5 * d_, ay - 6, ax + 5 * d_, ay, ax - 5 * d_, ay + 6, fill="#fbbf24" if has else bgc,
                                      outline="#fbbf24" if has else DIM, width=1, tags=tags)
            my += h
        yy = max(colb, my) + 2
        if gl:
            yy += graphics(ix, yy, gl, bgc, ww)
        return yy, "\n".join(lines)

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
        sc = r.get("score")
        gl = r.get("graphic") or []
        gl = [gl] if isinstance(gl, dict) else gl
        tos = next((g_ for g_ in gl if g_["kind"] == "timeouts"), None)
        gl = [g_ for g_ in gl if g_["kind"] != "timeouts"]
        info = r.get("info") or ""
        lw = ww
        if ui_state.get("layout") == "scoreboard" and len(r.get("teams") or []) == 2 and r["state"] in ("pre", "in", "post"):
            yy, info = draw_scoreboard(r, cx0, cw_, yy, bgc, tags, gl, tos, info)
        else:
            text_w = ww
            if sc:  # big score at the top right; the team names wrap to the space on its left
                xr = cx0 + cw_ - PAD
                try:
                    lead = (float(sc[0]) > float(sc[1])) - (float(sc[0]) < float(sc[1]))
                except ValueError:
                    lead = 0
                live = r["state"] == "in"
                hi = COLORS["in"] if live else FG
                c1 = hi if lead >= 0 else DIM
                c2 = hi if lead <= 0 else DIM
                rk = r.get("_key") or (gkey(r["game"]) if r.get("game") else r["name"])
                x2 = draw_score(xr, yy - 3, sc[1], c2, bgc, (rk, 1))
                if ui_state.get("digital"):
                    x1 = digital_text(x2 - 4, yy + 1, "-", DIM, bgc)
                else:
                    idash, _h = ctext(x2 - 4, yy - 3, "\u2013", FONTS["score"], DIM, anchor="ne")
                    x1 = canvas.bbox(idash)[0]
                text_w = max(ww - (xr - draw_score(x1 - 4, yy - 3, sc[0], c1, bgc, (rk, 0))) - 12, 80)
            urls = [u for u in (r.get("logos") or []) if u]  # team logo(s) in front of the name
            lg_size = 44 if len(urls) == 1 else 22
            tx = ix + (LOGO_W if urls else 0)
            text_w = max(text_w - (tx - ix), 60)
            for i, u in enumerate(urls):
                img = logo_img(u, lg_size)
                if img:
                    canvas.create_image(ix, yy + i * (lg_size + 2), image=img, anchor="nw", tags=tags)
            _, h = ctext(tx, yy, r["name"], FONTS["name"], FG, width=text_w, tags=tags)
            y_head = yy
            yy += h
            if r["line"]:
                _, h = ctext(tx, yy, r["line"], FONTS["line"], DIM, width=text_w, tags=tags)
                yy += h
            if sc:
                yy = max(yy, y + GAP + 28)  # keep the lines below clear of the score
            bb = [g_ for g_ in gl if g_["kind"] == "baseball"]  # bases, count and batter/pitcher get their own row
            sx = tx  # the status line lines up with the name, beside the logo
            if r["state"] == "in":  # red LIVE pill in front of the clock
                bw = 34
                canvas.create_polygon(rr_points(tx, yy + 2, tx + bw, yy + 16, 5), smooth=True, fill=LIVE_RED, outline=LIVE_RED, tags=tags)
                canvas.create_text(tx + bw / 2, yy + 9, text="LIVE", fill="#ffffff", font=FONTS["sec"], tags=tags)
                sx = tx + bw + 6
            ys = yy  # top of the status row: the baseball panel starts here too
            sid, h = ctext(sx, yy, r.get("status") if sc else r["detail"], FONTS["detb"] if r["state"] == "in" else FONTS["line"],
                           COLORS.get(r["state"], FG), width=lw - (sx - ix), tags=tags)
            clock = r.get("clock") if r["state"] == "in" else None
            if clock:
                session["clock_items"].append((sid, canvas.itemcget(sid, "text"), clock))
                canvas.itemconfigure(sid, text=tick_clock(canvas.itemcget(sid, "text"), clock))
            yy += max(h, 18 if r["state"] == "in" else 0)
            if urls:
                yy = max(yy, y_head + len(urls) * (lg_size + 2))  # the logo spans name, opponent and status lines
            if bb:
                yy += graphics(ix, yy, [g_ for g_ in gl if g_ not in bb], bgc, lw)
                rh = graphics(ix, yy, bb, bgc, BB_W)  # bases and count at the left, batter/pitcher beside them
                wy = yy + 4
                for part in (p_ for l_ in info.split("\n") if l_.startswith("AB:") for p_ in l_.split(" \u00b7 ")):
                    _, h = ctext(ix + BB_W + 8, wy, part, FONTS["small"], FG, width=ww - BB_W - 8, tags=tags)
                    wy += h
                yy = max(yy + rh + 2, wy)
                info = "\n".join(l_ for l_ in info.split("\n")
                                 if not l_.startswith(("Runners:", "Bases empty", "AB:")))
            elif gl:
                yy += graphics(ix, yy, gl, bgc, ww)
        g_ = r.get("game")
        if r.get("win"):
            yy += graphics(ix, yy, r["win"], bgc, lw)
        if info:
            _, h = ctext(ix, yy, info, FONTS["line"], DIM, width=ww, tags=tags)
            yy += h
        if r.get("next"):
            _, h = ctext(ix, yy + 3, r["next"], FONTS["small"], DIM, width=ww, tags=tags)
            yy += 3 + h
        g = r.get("game")
        ctx = None
        if g and gkey(g) in session["expanded"]:
            key = "game:" + gkey(g)
            y0 = yy
            H = draw_details(ix, y0, ww, bgc, session["details"].get(gkey(g)), bool(r.get("win")) or r["state"] == "post")
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
                tid, h = ctext(x + 4, y + 10, n["text"].upper(), FONTS["sec"], DIM)
                canvas.create_line(canvas.bbox(tid)[2] + 8, y + 10 + h / 2, x + w - 4, y + 10 + h / 2, fill="#33333d")
                y += 10 + h + 6
            elif t == "text":
                _, h = ctext(x + 2, y + 4, n["text"], FONTS["line"], DIM)
                y += 4 + h + 4
            elif t == "tabs":  # Overall / Conference / Division pills for one league's standings
                import tkinter.font as tkfont
                font = tkfont.Font(font=FONTS["smallb"])
                px = x + 6
                for key, label in n["options"]:
                    pw = font.measure(label) + 20
                    if px + pw > x + w - 4 and px > x + 6:  # wrap onto the next row
                        px, y = x + 6, y + 26
                    on = key == n["sel"]
                    tag = new_hit(("stview", n["league"], key))
                    canvas.create_polygon(rr_points(px, y + 3, px + pw, y + 23, 8), smooth=True,
                                          fill=PANEL if on else BG, outline=PANEL if on else "#33333d", tags=(tag,))
                    canvas.create_text(px + pw / 2, y + 13, text=label, font=FONTS["smallb"], fill=FG if on else DIM, tags=(tag,))
                    px += pw + 6
                y += 28
            elif t == "sub":  # standings sub-header: group name + column titles
                _, h = ctext(x + 6, y + 6, n["text"], FONTS["smallb"], FG)
                for k, title in enumerate(reversed(n["headers"])):
                    ctext(x + w - 8 - 40 * k, y + 7, title, FONTS["small"], DIM, anchor="ne")
                y += 6 + h + 4
            elif t == "srow":  # standings row
                if n["fav"]:
                    canvas.create_rectangle(x + 2, y, x + w - 2, y + 18, fill=blend(BG, "#34d399", 0.18), outline="")
                ctext(x + 24, y + 2, str(n["rank"]), FONTS["small"], DIM, anchor="ne")
                limit = x + w
                for k, val in enumerate(reversed(n["vals"])):
                    vid, _ = ctext(x + w - 8 - 40 * k, y + 2, val, FONTS["line"], FG if k == 0 else DIM, anchor="ne")
                    limit = min(limit, canvas.bbox(vid)[0])
                limit -= 8
                bold = n["fav"]
                nid, _ = ctext(x + 32, y + 2, n["name"], FONTS["detb"] if bold else FONTS["line"], FG)
                if canvas.bbox(nid)[2] > limit:  # always the full "City Name": shrink the font, then trim, to fit
                    canvas.itemconfigure(nid, font=FONTS["smallb"] if bold else FONTS["small"])
                    text = n["name"]
                    while canvas.bbox(nid)[2] > limit and len(text) > 4:
                        text = text[:-1]
                        canvas.itemconfigure(nid, text=text.rstrip() + "\u2026")
                y += 18
            elif t == "card":
                y = draw_card(n["row"], x, y, w, final)
            elif t == "group":
                y = draw_group(n, x, y, w, final)
        return y

    def group_node(key, text, color, indent, persist, default, children):
        store = ui_state if persist else session
        return {"t": "group", "key": key, "text": text, "color": color, "indent": indent, "persist": persist,
                "default": default, "open": bool(store.get(key, default)), "children": children}

    def sort_key(r):
        """Live games first and quiet (off-season) teams last, then alphabetical (ignoring a leading '#12 ' rank or '(3) ' seed)."""
        return ({"in": 0, "none": 2}.get(r["state"], 1), re.sub(r"^(#\d+|\(\d+\))\s+", "", r["name"]).lower())

    def cards(rows, extra_line=False):
        rows = sorted(rows, key=sort_key)
        return [{"t": "card", "row": dict(r, line=r.get("extra", r.get("line", ""))) if extra_line else r} for r in rows]

    def league_nodes(rows, prefix, default_open=False, indent=14):
        out = []
        order = {n: i for i, n in enumerate(["MLB", "NFL", "NBA", "WNBA", "NHL"])}
        names = sorted(dict.fromkeys(r["league"] for r in rows),
                       key=lambda L: (0 if any(r["league"] == L and r["state"] == "in" for r in rows) else 1, order.get(L, 9)))
        for league in names:
            games = [r for r in rows if r["league"] == league]
            live_n = sum(r["state"] == "in" for r in games)
            is_leagues = prefix == "leagues"
            out.append(group_node(
                f"{prefix}:{league}",
                f"{league} · {len(games)}" + (f" · {live_n} live" if live_n and is_leagues else ""),
                COLORS["in"] if live_n and is_leagues else FG, indent, True,
                (live_n > 0) if is_leagues else default_open, cards(games, True)))
        return out

    def build_standings_nodes():
        favs = {(e["league"], str(e["team"]).lower()) for e in entries}
        nodes = []
        for i, (abbr, sport, league) in enumerate(STANDINGS_LEAGUES):
            data = session["standings"].get(abbr)
            if data is None:
                children = [{"t": "text", "text": "Loading..."}]
            elif data == "error":
                children = [{"t": "text", "text": "Standings unavailable"}]
            else:
                options = [("overall", "Overall")] + [(k, k.title()) for k in ("conference", "division") if data.get(k)]
                sel = ui_state.get(f"stview:{abbr}", "conference" if data.get("conference") else "overall")
                if sel not in dict(options):
                    sel = "overall"
                children = []
                if len(options) > 1:
                    children.append({"t": "tabs", "league": abbr, "options": options, "sel": sel})
                groups = [{"name": "Overall", "rows": data["overall"]}] if sel == "overall" else data[sel]
                headers = ["W-L"] + STANDINGS_HEADERS.get(abbr, ["PCT", "GB"])
                for g in groups:
                    children.append({"t": "sub", "text": g["name"], "headers": headers})
                    for rank, r in enumerate(g["rows"], start=1):
                        rec, cols = standing_cells(abbr, r["stats"])
                        fav = (league, r["abbr"].lower()) in favs or (league, r["id"]) in favs
                        children.append({"t": "srow", "rank": rank, "name": r["name"], "vals": [rec] + cols, "fav": fav})
            nodes.append(group_node(f"st:{abbr}", abbr, FG, 0, True, i == 0, children))
        for key, cfg in COLLEGE_SECTIONS.items():
            store = session["college"].get(key, {})
            options = [("top25", "Top 25")] + [(f"conf:{lbl}", lbl) for lbl, _, _ in cfg["confs"]]
            sel = ui_state.get(f"stview:{key}", "top25")
            if sel not in dict(options):
                sel = "top25"
            children = [{"t": "tabs", "league": key, "options": options, "sel": sel}]
            data = store.get(sel)
            if data is None:
                children.append({"t": "text", "text": "Loading..."})
            elif data == "error":
                children.append({"t": "text", "text": "Standings unavailable"})
            elif sel == "top25":
                children.append({"t": "sub", "text": cfg.get("poll", "AP Top 25"), "headers": ["Record", "Pts"]})
                for r in data:
                    fav = (cfg["league"], r["id"]) in favs or (cfg["league"], r["abbr"].lower()) in favs
                    children.append({"t": "srow", "rank": r["rank"], "name": r["name"], "vals": [r["record"], r["points"]], "fav": fav})
            else:
                for g in data:
                    children.append({"t": "sub", "text": g["name"] or dict(options)[sel], "headers": ["Conf", "Ovr"]})
                    for rank, r in enumerate(g["rows"], start=1):
                        conf, cols = college_cells(r["stats"])
                        fav = (cfg["league"], r["id"]) in favs or (cfg["league"], r["abbr"].lower()) in favs
                        children.append({"t": "srow", "rank": rank, "name": r["name"], "vals": [conf] + cols, "fav": fav})
            nodes.append(group_node(f"st:{key}", cfg["title"], FG, 0, True, False, children))
        return nodes

    def build_nodes():
        if ui_state.get("tab", "games") == "standings":
            return build_standings_nodes()
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
        active = [r for r in results if r["state"] != "none"]
        off = [r for r in results if r["state"] == "none"]
        if active or off:
            nodes.append({"t": "section", "text": "My Teams"})
            nodes += cards(active)
            if off:
                nodes.append(group_node("offseason", f"Out of season \u00b7 {len(off)}", DIM, 0, True, False, cards(off)))
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
        if not last and ui_state.get("tab", "games") != "standings":
            return 0
        canvas.delete("all")
        session["hits"].clear()
        session["roll_cells"].clear()
        session["clock_items"].clear()
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
        return json.dumps([last.get("args"), ui_state, sorted(session["expanded"]),
                           {k: session["details"].get(k) for k in session["expanded"]}, session.get("live"),
                           [session["standings"], session["college"]] if ui_state.get("tab", "games") == "standings" else None],
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
        elif key.startswith("st:"):  # standings behave like an accordion: one league open at a time
            others = [k for k in (f"st:{a}" for a in STANDINGS_KEYS)
                      if k != key and ui_state.get(k, k == f"st:{STANDINGS_LEAGUES[0][0]}")]
            if not others:
                set_open(n, True)
                if key[3:] in COLLEGE_SECTIONS:
                    load_college(key[3:], ui_state.get(f"stview:{key[3:]}", "top25"))
                start_anim(key, True)
                return
            for extra in others[1:]:
                set_open({"key": extra, "persist": True}, False)

            def open_new():
                set_open({"key": others[0], "persist": True}, False)
                set_open(n, True)
                if key[3:] in COLLEGE_SECTIONS:
                    load_college(key[3:], ui_state.get(f"stview:{key[3:]}", "top25"))
                start_anim(key, True)
            start_anim(others[0], False, on_done=open_new)  # collapse the open league, then expand this one
        else:
            set_open(n, True)
            if key.startswith("st:") and key[3:] in COLLEGE_SECTIONS:
                load_college(key[3:], ui_state.get(f"stview:{key[3:]}", "top25"))
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

    loading = {"on": True, "phase": 0}

    def spin():
        """Spinner shown on the canvas until the first data arrives."""
        if not loading["on"]:
            return
        try:
            canvas.delete("loading")
            cw = max(canvas.winfo_width(), MIN_BODY_W)
            ch = max(canvas.winfo_height(), 100)
            cx, cy, n = cw / 2, ch / 2 - 10, 8
            import math
            for i in range(n):
                a_ = 2 * math.pi * i / n
                shade = ((i - loading["phase"]) % n) / n  # the lead dot is brightest, the tail fades out
                col = blend(BG, FG, 0.15 + 0.85 * (1 - shade))
                x, y = cx + 12 * math.cos(a_), cy + 12 * math.sin(a_)
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=col, outline="", tags="loading")
            canvas.create_text(cx, cy + 30, text="Loading...", fill=DIM, font=FONTS["line"], tags="loading")
            loading["phase"] = (loading["phase"] + 1) % n
            root.after(90, spin)
        except tk.TclError:
            pass

    def render(results, pin_results, playoffs, leagues=()):
        loading["on"] = False
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

    def load_standings():
        """Fetch all five leagues' standings in the background (cached for 10 minutes)."""
        def one(lg):
            abbr, sport, league = lg
            try:
                session["standings"][abbr] = parse_standings_tree(standings_json(sport, league), abbr)
            except Exception:
                session["standings"].setdefault(abbr, "error")
                if session["standings"][abbr] is None:
                    session["standings"][abbr] = "error"

        def work():
            pmap(one, STANDINGS_LEAGUES)
            root.after(0, standings_loaded)
        threading.Thread(target=work, daemon=True).start()

    def load_college(key, sel):
        """Fetch one college tab (Top 25 or a conference) in the background."""
        cfg = COLLEGE_SECTIONS[key]
        store = session["college"].setdefault(key, {})

        def work():
            try:
                if sel == "top25":
                    rows = parse_poll(rankings_json(cfg["sport"], cfg["league"]))
                    store[sel] = rows or "error"
                else:
                    label, pattern, fallback = next(c for c in cfg["confs"] if f"conf:{c[0]}" == sel)
                    gid = conference_group(cfg, label, pattern, fallback)
                    if gid is None:
                        raise LookupError(label)
                    groups = parse_standings(standings_json(cfg["sport"], cfg["league"], group=gid))
                    store[sel] = groups if any(g["rows"] for g in groups) else "error"
            except Exception:
                store[sel] = "error"
            root.after(0, standings_loaded)
        threading.Thread(target=work, daemon=True).start()

    def load_open_college():
        for key in COLLEGE_SECTIONS:
            if ui_state.get(f"st:{key}", False):
                load_college(key, ui_state.get(f"stview:{key}", "top25"))

    def standings_loaded():
        if ui_state.get("tab", "games") == "standings":
            session["sig"] = None
            draw_all()
            session["sig"] = compute_sig()
            fit()

    def set_tab(key):
        if ui_state.get("tab", "games") == key or session["anims"]:
            return
        ui_state["tab"] = key
        save_state(ui_state)
        style_tabs()
        canvas.yview_moveto(0)
        session["sig"] = None
        view_tween["next"] = True  # ease the window to the new content height
        if key == "standings":
            load_standings()
            load_open_college()
        draw_all()
        session["sig"] = compute_sig()
        fit()

    def refresh():
        if ui_state.get("tab", "games") == "standings":
            load_standings()
            load_open_college()
        if busy["on"]:
            busy["again"] = True  # a refresh is still running: run one more when it finishes
            return
        busy["on"] = True

        def work():
            try:
                pin_list = list(pins)
                expanded = [session["games"][k] for k in list(session["expanded"]) if k in session["games"]]
                res, pres, po, lg, _ = pmap(lambda f: f(), [lambda: fetch_all(entries), lambda: fetch_pinned(pin_list),
                                                            playoff_games, league_games,
                                                            lambda: pmap(fetch_details, expanded)], workers=5)
                shown = {r["_key"] for r in res + pres if r.get("_key")}
                po = [r for r in po if r.get("_key") not in shown]  # already listed above
                lg = [r for r in lg if r.get("_key") not in shown]
                live_rows = [r for grp in (res, pres, po, lg) for r in grp if r.get("state") == "in" and r.get("game")]
                # win probability for the collapsed cards (one cached request per live game)
                for r, wbar in zip(live_rows, pmap(lambda r: win_bar(r["game"]), live_rows, workers=6)):
                    if wbar:
                        r["win"] = wbar
                root.after(0, lambda: render(res, pres, po, lg))
            finally:
                root.after(0, refresh_done)
        threading.Thread(target=work, daemon=True).start()

    busy = {"on": False, "again": False}

    def refresh_done():
        busy["on"] = False
        if busy.pop("again", False):
            refresh()

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
        tk.Label(win, text="Dock", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=4, column=0, padx=16, pady=(6, 4), sticky="w")
        dock_choice = tk.StringVar(value=next((l for l, v in DOCK_CHOICES if v == ui_state.get("dock", "off")), "Off"))
        styled_option(win, dock_choice, [l for l, _ in DOCK_CHOICES], command=lambda label: set_dock(dict(DOCK_CHOICES)[label]),
                      width=12).grid(row=4, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Digital scores", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=5, column=0, padx=16, pady=(6, 4), sticky="w")
        digital_choice = tk.StringVar(value="On" if ui_state.get("digital") else "Off")

        def on_digital(label):
            ui_state["digital"] = label == "On"
            save_state(ui_state)
            session["sig"] = None
            draw_all()
            session["sig"] = compute_sig()
        styled_option(win, digital_choice, ["Off", "On"], command=on_digital, width=12).grid(
            row=5, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Card layout", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=6, column=0, padx=16, pady=(6, 4), sticky="w")
        layout_choice = tk.StringVar(value=next((l for l, v in LAYOUT_CHOICES if v == ui_state.get("layout", "default")), "Default"))

        def on_layout(label):
            ui_state["layout"] = dict(LAYOUT_CHOICES)[label]
            save_state(ui_state)
            session["sig"] = None
            view_tween["next"] = True  # ease the window to the new height
            draw_all()
            session["sig"] = compute_sig()
            fit()
        styled_option(win, layout_choice, [l for l, _ in LAYOUT_CHOICES], command=on_layout, width=12).grid(
            row=6, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Team logos", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=7, column=0, padx=16, pady=(6, 4), sticky="w")

        def clear_logos():
            import shutil
            shutil.rmtree(LOGO_DIR, ignore_errors=True)
            logo_imgs.clear()
            logo_pending.clear()
            logo_done.clear()
            session["sig"] = None
            draw_all()  # redraws and downloads the logos again
            session["sig"] = compute_sig()
        styled_button(win, "Clear cache", clear_logos).grid(row=7, column=1, padx=16, pady=(6, 4), sticky="e")
        styled_button(win, "Close", win.destroy).grid(row=8, column=1, padx=16, pady=(10, 16), sticky="e")
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
            drag["px"] = e.x_root
            drag["moved"] = False
    UNDOCK_DIST, DOCK_SNAP = 60, 8  # px dragged away to undock; px from a screen edge to dock on release

    def move(e):
        if e.widget not in (grip, scroll) and "x" in drag:
            drag["moved"] = True
            if docked():  # a docked widget stays on its edge until it is dragged well away from it
                if abs(e.x_root - drag.get("px", e.x_root)) > UNDOCK_DIST:
                    set_dock("off")
                    root.update_idletasks()
                    drag["x"], drag["y"] = root.winfo_width() // 2, 20  # carry it by its top centre
                    root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
            else:
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
        if drag.get("moved") and e.widget not in (grip, scroll):
            save_geometry()  # the window was dragged somewhere new
            if not docked():  # let go with the pointer at a screen edge: dock there
                l, _t, r, _b = screen_bounds()
                if e.x_root <= l + DOCK_SNAP:
                    set_dock("left")
                elif e.x_root >= r - 1 - DOCK_SNAP:
                    set_dock("right")
        h = hit_at(e)
        if not h or drag.get("moved"):
            return
        if h[0] == "group":
            toggle_group(h[1])
        elif h[0] == "stview":
            if ui_state.get(f"stview:{h[1]}") != h[2]:
                ui_state[f"stview:{h[1]}"] = h[2]
                save_state(ui_state)
                if h[1] in COLLEGE_SECTIONS:
                    load_college(h[1], h[2])
                session["sig"] = None
                view_tween["next"] = True  # ease the window to the new height
                draw_all()
                session["sig"] = compute_sig()
                fit()
        elif h[1].get("game"):
            toggle_expand(h[1]["game"])

    def on_motion(e):
        h = hit_at(e)
        canvas.configure(cursor="hand2" if h and (h[0] in ("group", "stview") or h[1].get("game")) else "")
    canvas.bind("<Motion>", on_motion)

    def restart():
        """Start a fresh copy of this script (picks up code changes from git pull), then close this one."""
        import subprocess
        save_geometry()
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


    def menu_items(at, url=None):
        """The context-menu entries; `at` has x_root/y_root (where sub-menus open); `url` adds the ESPN link."""
        items = []
        if url:
            import webbrowser
            items += [("Open game on ESPN", lambda: webbrowser.open(url)), None]

        def toggle_top():
            topmost.set(not topmost.get())
            root.attributes("-topmost", topmost.get())
        items += [("Track a game...", track_dialog), ("Untrack a game...", lambda: untrack_menu(at)),
                  ("Refresh", refresh), ("Settings...", settings_dialog),
                  ("Always on top", toggle_top, topmost.get()), None,
                  ("Update & Restart", update_and_restart), ("Restart", restart), ("Quit", quit_app)]
        return items

    def popup(e):
        h = hit_at(e)
        url = h[1].get("url") if h and h[0] == "game" else None
        popup_menu(e.x_root, e.y_root, menu_items(e, url))

    gear = {"was_open": False}

    def gear_press(_):
        gear["was_open"] = menu_state["top"] is not None  # this runs before the window-level handler closes it

    def gear_release(_):
        if gear["was_open"]:
            return  # a second click on the gear just closes the menu
        from types import SimpleNamespace
        x, y = gear_btn.winfo_rootx(), gear_btn.winfo_rooty() + gear_btn.winfo_height() + 2
        popup_menu(x, y, menu_items(SimpleNamespace(x_root=x, y_root=y)))
    gear_btn.bind("<ButtonPress-1>", gear_press)
    gear_btn.bind("<ButtonRelease-1>", gear_release)

    # Dock to a screen edge with autohide: the widget slides off-screen leaving a thin strip at the edge,
    # and slides back when the pointer touches that strip. It stays out while the pointer is over it, a mouse
    # button is held (drag, resize) or a menu/dialog is open, and hides again shortly after the pointer leaves.
    DOCK_STRIP, DOCK_HIDE_DELAY = 5, 0.6
    dock = {"x": root.winfo_x(), "shown": True, "leave": None, "held": False}

    def docked():
        return ui_state.get("dock", "off") in ("left", "right")

    def dock_area():
        """(left, top, right, bottom) the docked widget fills: the work area (minus the taskbar) of the monitor at
        the docked outer edge of the desktop; the whole screen elsewhere than Windows."""
        l, t, r, b = screen_bounds()
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class MONITORINFO(ctypes.Structure):
                    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
                user32 = ctypes.windll.user32
                user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
                user32.MonitorFromPoint.restype = wintypes.HANDLE
                user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
                pt = wintypes.POINT(l if ui_state.get("dock") == "left" else r - 1, root.winfo_y() + root.winfo_height() // 2)
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if user32.GetMonitorInfoW(user32.MonitorFromPoint(pt, 2), ctypes.byref(mi)):  # 2 = nearest monitor
                    w = mi.rcWork
                    return w.left, w.top, w.right, w.bottom
            except Exception:
                pass
        return l, t, r, b

    def dock_width():
        return root.winfo_width() if user_sized["on"] else max(MIN_W, root.winfo_reqwidth())

    def dock_shown_x(area=None, w=None):
        l, _t, r, _b = area or dock_area()
        return l if ui_state.get("dock") == "left" else r - (w or dock_width())

    def dock_hidden_x(area=None, w=None):
        l, _t, r, _b = area or dock_area()
        return l - (w or dock_width()) + DOCK_STRIP if ui_state.get("dock") == "left" else r - DOCK_STRIP

    def autohide():
        return ui_state.get("autohide", True)

    def draw_pin():
        """Push-pin button: outlined while autohide is on, filled (pinned open) while it is off."""
        pin_btn.delete("all")
        on = autohide()
        col = DIM if on else FG
        fill = "" if on else FG
        pin_btn.create_polygon(11, 8, 23, 8, 21, 16, 26, 21, 8, 21, 13, 16, outline=col, fill=fill, width=2, joinstyle="round")
        pin_btn.create_line(17, 21, 17, 28, fill=col, width=2, capstyle="round")

    def toggle_autohide(_=None):
        ui_state["autohide"] = not autohide()
        save_state(ui_state)
        draw_pin()
        dock["leave"] = _time.monotonic() if autohide() else None

    pin_btn = tk.Canvas(hbar, width=34, height=34, bg=BG, highlightthickness=0, cursor="hand2")
    pin_btn.bind("<ButtonRelease-1>", toggle_autohide)

    def dock_keep_out():
        """True while the widget must stay visible regardless of where the pointer is."""
        if dock["held"]:
            return True
        return any(isinstance(w, tk.Toplevel) and w.winfo_ismapped() for w in root.winfo_children())

    def dock_poll():
        delay = 60
        try:
            if docked() and root.state() == "normal":
                now = _time.monotonic()
                area = dock_area()
                top, h, w = area[1], area[3] - area[1], dock_width()
                px, py = root.winfo_pointerxy()
                x = dock["x"]
                if (x <= px < x + w and top <= py < top + h) or dock_keep_out() or not autohide():
                    dock["leave"], want = None, True
                elif dock["shown"]:
                    dock["leave"] = dock["leave"] or now
                    want = now - dock["leave"] < DOCK_HIDE_DELAY
                else:
                    want = False
                dock["shown"] = want
                target = dock_shown_x(area, w) if want else dock_hidden_x(area, w)
                if x != target:  # ease toward the target: a quick slide that slows at the end
                    step = (target - x) * 0.3
                    x += int(step) if abs(step) >= 1 else (1 if target > x else -1)
                    dock["x"] = x
                    delay = 12
                if (root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()) != (x, top, w, h):
                    root.geometry(f"{w}x{h}+{x}+{top}")  # the full height of the edge
        except tk.TclError:
            return
        root.after(delay, dock_poll)

    def set_dock(side):
        ui_state["dock"] = side
        save_state(ui_state)
        if side != "off" and ui_state.get("view") == "title":
            ui_state["view"] = "full"
            save_state(ui_state)
            apply_layout()
            if last:
                render(*last["args"])
        if side == "off":  # back to a normal floating window, fully on screen where it was last shown
            l, t, r, b = screen_bounds()
            w = dock_width()
            pos = ui_state.get("pos") or [dock["x"], root.winfo_y()]
            x, y = max(l, min(int(pos[0]), r - w)), max(t, min(int(pos[1]), b - MIN_H))
            if user_sized["on"]:
                sh = (ui_state.get("size") or [w, 400])[1]
                root.geometry(f"{w}x{max(MIN_H, int(sh))}+{x}+{y}")
            else:
                root.geometry("")
                root.geometry(f"+{x}+{y}")
                fit()
        else:
            x = dock_shown_x()
            dock.update(shown=True, leave=None)
        dock["x"] = x
        sync_grip()
        root.update_idletasks()  # apply the new geometry so it is what gets saved
        save_geometry()

    def dock_hold(on):
        dock["held"] = on

    def sync_grip():
        """The resize grip is hidden while docked (and in the Title view); the autohide button shows only while docked."""
        if docked() and not pin_btn.winfo_manager():
            draw_pin()
            pin_btn.pack(side="right", padx=(8, 0))
        elif not docked() and pin_btn.winfo_manager():
            pin_btn.pack_forget()
        if docked() or ui_state.get("view", "full") == "title":
            grip.pack_forget()
        elif not grip.winfo_manager():
            grip.pack(side="bottom", anchor="se", padx=2, **({"before": container} if container.winfo_manager() else {}))

    if docked():
        if ui_state.get("view") == "title":
            ui_state["view"] = "full"
        dock["x"] = dock_shown_x()
        dock["leave"] = _time.monotonic() + 1.0  # stay out briefly at startup, then tuck away
        sync_grip()

    # Bound on the toplevel, so every child widget (rows, labels) drags/pops up too.
    root.bind("<Button-1>", start)
    root.bind("<B1-Motion>", move)
    root.bind("<ButtonRelease-1>", on_release)
    root.bind("<Button-3>", popup)
    root.bind("<ButtonPress-1>", lambda e: dock_hold(True), add="+")
    root.bind("<ButtonRelease-1>", lambda e: dock_hold(False), add="+")
    root.update_idletasks()
    apply_layout()
    threading.Thread(target=icon_precompute, daemon=True).start()
    stamp.config(text="Loading...")
    style_tabs()
    if ui_state.get("tab", "games") == "standings":
        load_standings()
        load_open_college()
    spin()
    clock_tick()
    try:
        round_corners(root)
    except Exception:
        pass  # cosmetic only
    try:
        show_in_taskbar(root)
    except Exception:
        pass
    tick()
    dock_poll()
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
    def row(name, sport, league, line, detail, comp, tint=None, state="in", next_line="", win=None):
        m = re.match(r"^(?:([WLT])\s+)?(\d+)-(\d+)\s+(.*)$", detail)
        score = (m.group(2), m.group(3)) if m else None
        status = " \u00b7 ".join(x for x in ((m.group(1) or "") if m else "", m.group(4) if m else detail) if x)
        wbar = {"kind": "versus", "label": "Win probability", "a_name": win[0], "a": win[1], "b_name": win[2], "b": win[3],
                "a_color": win[4], "b_color": win[5]} if win else None
        cm = re.search(r"(\d+):(\d\d)$|(\d+)'$", detail)
        clock = None if state != "in" or not cm else (
            {"at": time.time(), "secs": int(cm.group(1)) * 60 + int(cm.group(2)), "up": False} if cm.group(1)
            else {"at": time.time(), "secs": int(cm.group(3)) * 60 - 30, "up": True, "minute": int(cm.group(3))})
        return {"name": name, "state": state, "line": line, "detail": detail, "tint": tint, "url": "https://www.espn.com/", "clock": clock,
                "score": score, "status": status, "win": wbar, "next": next_line,
                "teams": comp_teams(comp, (comp.get("competitors") or [None])[0]) if state != "none" else [],
                "info": situation_text(sport, comp), "graphic": situation_graphic(sport, comp, league)}
    nfl = {"competitors": [team("25", "away", "SF", 21), team("6", "home", "DAL", 17)],
           "situation": {"shortDownDistanceText": "3rd & 4", "possession": "25", "possessionText": "DAL 38", "distance": 4,
                         "homeTimeouts": 2, "awayTimeouts": 3}}
    mlb = {"status": {"type": {"shortDetail": "Top 7th"}},
           "competitors": [team("1", "away", "SFG", 3), team("2", "home", "LAD", 2)],
           "situation": {"awayChallengesRemaining": 1, "homeChallengesRemaining": 2, "balls": 1, "strikes": 2, "outs": 2, "onFirst": True, "onThird": True,
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
    return [row("San Francisco 49ers (4-1)", "football", "nfl", "@ Dallas Cowboys (3-2)", "21-17  Q3 5:12", nfl, tint="#aa0000", win=("SF", 62, "DAL", 38, "#b3995d", "#869397")),
            row("San Francisco Giants (85-77)", "baseball", "mlb", "@ Los Angeles Dodgers (98-64)", "3-2  Top 7th", mlb, tint="#fd5a1e", win=("SFG", 58, "LAD", 42, "#fd5a1e", "#005a9c")),
            row("(4) Golden State Warriors (48-34)", "basketball", "nba", "@ (1) Boston Celtics (64-18)", "78-74  Q3 5:12", nba, tint="#1d428a", win=("GS", 59, "BOS", 41, "#1d428a", "#007a33")),
            row("New Jersey Devils (3-1-0)", "hockey", "nhl", "@ Boston Bruins (2-2-0)", "2-1  P2 6:47", nhl, tint="#ce1126"),
            row("Arsenal (6-1-2)", "soccer", "eng.1", "vs Chelsea (5-2-2)", "1-1  67'", soc, tint="#ef0107"),
            row("#12 San Diego State Aztecs Football (7-2)", "football", "college-football", "vs Boise State (8-1)",
                "W 31-24  Final", {}, tint="#c41230", state="post",
                next_line="Next: @ #7 Boise State (8-1) \u00b7 Sat Oct 10 6:00 PM"),
            {"name": "Las Vegas Aces (30-14)", "state": "none", "line": "Last: vs New York Liberty (32-12) \u00b7 L 76-84  Final \u00b7 Sat Sep 19",
             "detail": "Season starts May 15, 2027", "info": "", "graphic": None}]


def demo_leagues():
    def g(lg, name, state, detail, tint):
        m = re.match(r"^(\d+)-(\d+)\s+(.*)$", detail)
        return {"score": (m.group(1), m.group(2)) if m else None, "status": m.group(3) if m else detail,
                "name": name, "state": state, "line": "", "league": lg, "detail": detail,
                                               "tint": tint, "info": "", "graphic": None, "_key": (lg, name),
                                               "teams": [{"logo": None, "ha": h, "abbr": t.split()[-1][:3].upper()}
                                                         for h, t in zip(("away", "home"), name.split(" @ "))],
                                               "url": "https://www.espn.com/"}
    return [g("MLB", "Boston Red Sox @ Toronto Blue Jays", "in", "2-1  Bot 4th", "#134a8e"),
            g("MLB", "Seattle Mariners @ Houston Astros", "pre", "Sat Oct 3 8:10 PM", "#eb6e1f"),
            g("MLB", "Chicago Cubs @ Milwaukee Brewers", "post", "4-2  Final", "#ffc52f"),
            g("NFL", "Green Bay Packers @ Chicago Bears", "pre", "Sun Oct 4 1:00 PM", "#0b162a"),
            g("NBA", "Miami Heat @ New York Knicks", "pre", "Sun Oct 4 7:00 PM", "#f58426")]


if __name__ == "__main__":
    if "--demo" in sys.argv:  # preview the live-game graphics with fake data (no network)
        demo_refreshes = [0]

        def fetch_all(entries):  # each refresh (press the refresh button) bumps the live scores so they roll
            n, rows = demo_refreshes[0], demo_data()
            demo_refreshes[0] += 1
            for r, (da, db) in zip(rows, ((7, 3), (0, 1), (2, 3), (1, 0), (0, 1))):
                r["score"] = (str(int(r["score"][0]) + da * n), str(int(r["score"][1]) + db * n))
            return rows
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
    elif "--debug-team" in sys.argv:  # --debug-team sdsu : why does this team show (or not show) a game?
        q = sys.argv[sys.argv.index("--debug-team") + 1].lower()
        for entry in load_config()["teams"]:
            if q in json.dumps(entry).lower():
                print(f'== {entry.get("label", entry["team"])}  ({entry["sport"]}/{entry["league"]}/{entry["team"]})')
                try:
                    evs = fetch_schedule(entry).get("events", [])
                except Exception as ex:
                    print("   schedule error:", ex)
                    continue
                print(f"   {len(evs)} events; now = {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
                for e in sorted((e for e in evs if e.get("competitions")), key=lambda e: e.get("date", "")):
                    stt = (e["competitions"][0].get("status") or {}).get("type") or {}
                    print("   ", e.get("date"), "|", e.get("shortName") or e.get("name"), "| state:", stt.get("state"),
                          "completed:", stt.get("completed"), "-> read as", _state_of(e))
                r = team_status(entry)
                print("   shows:", r and (r["name"], r["line"], r["detail"], r["next"]))
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
