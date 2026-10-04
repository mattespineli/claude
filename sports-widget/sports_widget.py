"""Always-on-top desktop widget that tracks favorite sports teams.

Uses only the Python standard library (tkinter + urllib) and ESPN's public
JSON endpoints. Edit teams.json to choose teams.

Drag to move, right-click for menu (refresh / always-on-top / quit).
"""
import functools
import gzip
import json
import math
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
    return os.path.join(LOGO_DIR, hashlib.md5(f"{url}|{size}|svg1".encode()).hexdigest()[:16] + ".png")


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


_LOGO_GAMMA = 1.4  # between plain sRGB averaging (1.0) and linear light (2.2), which washes out thin dark rings
_TO_LIN = [(i / 255) ** _LOGO_GAMMA for i in range(256)]


def _area_resize(w, h, px, ow, oh):
    """Resize RGBA pixels to ow x oh with a Lanczos-3 filter (windowed sinc, stretched to the reduction so it
    antialiases), weighting by alpha and averaging colours at a gamma between sRGB and linear light. Sharper than a
    box or Gaussian average: thin rings and diagonals stay crisp without stepping. Overshoot is clamped."""
    def spans(n, on):
        """Per output index: [(source index, weight)], the weights adding up to 1."""
        step, out = n / on, []
        sc = max(step, 1.0)
        rad = 3 * sc
        def lanczos(t):
            t = abs(t)
            if t >= 3:
                return 0.0
            if t < 1e-9:
                return 1.0
            return 3 * math.sin(math.pi * t) * math.sin(math.pi * t / 3) / (math.pi * t) ** 2
        for o in range(on):
            c = (o + 0.5) * step
            row = []
            for i in range(max(0, int(c - rad)), min(n, math.ceil(c + rad))):
                row.append((i, lanczos((i + 0.5 - c) / sc)))
            tot = sum(wt for _i, wt in row)
            out.append([(i, wt / tot) for i, wt in row])
        return out
    xs, ys = spans(w, ow), spans(h, oh)
    lin = _TO_LIN
    tmp = []  # horizontal pass: per source row, (r*a, g*a, b*a, a) per output column, colours in linear light
    for y in range(h):
        base, row = y * w * 4, []
        for sp in xs:
            r = g = b = a = 0.0
            for i, wt in sp:
                k = base + i * 4
                al = px[k + 3] * wt
                r += lin[px[k]] * al
                g += lin[px[k + 1]] * al
                b += lin[px[k + 2]] * al
                a += al
            row.append((r, g, b, a))
        tmp.append(row)
    out = bytearray()
    inv = 1 / _LOGO_GAMMA
    for sp in ys:  # vertical pass
        for ox in range(ow):
            r = g = b = a = 0.0
            for i, wt in sp:
                t = tmp[i][ox]
                r += t[0] * wt
                g += t[1] * wt
                b += t[2] * wt
                a += t[3] * wt
            if a > 1e-6:
                cl = lambda v: max(0, min(255, round(255 * max(0.0, v / a) ** inv)))
                out += bytes((cl(r), cl(g), cl(b), max(0, min(255, round(a)))))
            else:
                out += b"\x00\x00\x00\x00"
    return _png_bytes(ow, oh, out)


def _full_logo(src, size):
    """The logo at size x size from ESPN's original file (its largest, usually 500 px), area-averaged down in one step."""
    req = urllib.request.Request("https://a.espncdn.com" + src, headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        dec = _png_rgba(r.read())
    if not dec or dec[0] < size or dec[1] < size:
        return None  # a format the decoder doesn't read, or smaller than asked: the resizer path instead
    w, h, px = dec
    if w != h:  # centre a non-square logo on a transparent square
        n = max(w, h)
        sq = bytearray(n * n * 4)
        ox, oy = (n - w) // 2, (n - h) // 2
        for y in range(h):
            sq[((oy + y) * n + ox) * 4:((oy + y) * n + ox + w) * 4] = px[y * w * 4:(y + 1) * w * 4]
        w = h = n
        px = sq
    return _area_resize(w, h, px, size, size)


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


# Team SVGs from the leagues' own sites, rendered at the exact size (resvg: pip install resvg-py). Anything missing
# (no resvg, no SVG for the team, a logo too dark for the card) falls back to ESPN's PNG.
NBA_IDS = {"ATL": 1610612737, "BOS": 1610612738, "CLE": 1610612739, "NO": 1610612740, "CHI": 1610612741, "DAL": 1610612742,
           "DEN": 1610612743, "GS": 1610612744, "HOU": 1610612745, "LAC": 1610612746, "LAL": 1610612747, "MIA": 1610612748,
           "MIL": 1610612749, "MIN": 1610612750, "BKN": 1610612751, "NY": 1610612752, "ORL": 1610612753, "IND": 1610612754,
           "PHI": 1610612755, "PHX": 1610612756, "POR": 1610612757, "SAC": 1610612758, "SA": 1610612759, "OKC": 1610612760,
           "TOR": 1610612761, "UTAH": 1610612762, "MEM": 1610612763, "WSH": 1610612764, "DET": 1610612765, "CHA": 1610612766}
MLB_IDS = {"LAA": 108, "ARI": 109, "BAL": 110, "BOS": 111, "CHC": 112, "CIN": 113, "CLE": 114, "COL": 115, "DET": 116,
           "HOU": 117, "KC": 118, "LAD": 119, "WSH": 120, "NYM": 121, "OAK": 133, "ATH": 133, "PIT": 134, "SD": 135,
           "SEA": 136, "SF": 137, "STL": 138, "TB": 139, "TEX": 140, "TOR": 141, "MIN": 142, "PHI": 143, "ATL": 144,
           "CHW": 145, "MIA": 146, "NYY": 147, "MIL": 158}
NFL_ABBR = {"WSH": "WAS"}


def svg_urls(url):
    """Candidate SVG URLs for an ESPN team-logo URL (best first), or []."""
    m = re.search(r"/teamlogos/([a-z-]+)/\d+(?:-dark)?/([^/.]+)\.png", url)
    if not m:
        return []
    league, abbr = m.group(1), m.group(2).upper()
    if league == "nfl":
        return [f"https://static.www.nfl.com/league/api/clubs/logos/{NFL_ABBR.get(abbr, abbr)}.svg"]
    if league == "nba" and abbr in NBA_IDS:
        return [f"https://cdn.nba.com/logos/nba/{NBA_IDS[abbr]}/global/L/logo.svg"]
    if league == "mlb" and abbr in MLB_IDS:
        return [f"https://www.mlbstatic.com/team-logos/{MLB_IDS[abbr]}.svg"]
    if league == "ncaa":  # ncaa.com names its files by school name: try the forms ESPN's names suggest
        t = LOGO_HINTS.get(url) or {}
        out = []
        for name in (t.get("shortDisplayName"), t.get("location"), t.get("displayName")):
            slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"\bState\b", "St", name or "").lower().replace("&", "and")).strip("-")
            u = f"https://www.ncaa.com/sites/default/files/images/logos/schools/bgl/{slug}.svg"
            if slug and u not in out:
                out.append(u)
        return out
    return []


def _svg_logo(url, size):
    """PNG bytes of the team's SVG rendered at size x size (centred, transparent), or None."""
    try:
        import resvg_py
    except ImportError:
        return None
    for u in svg_urls(url):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 sports-widget/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                svg = r.read().decode("utf-8", "replace")
            if "<svg" not in svg:
                continue
            kw = {"svg_string": svg, "skip_system_fonts": True}
            dec = _png_rgba(bytes(resvg_py.svg_to_bytes(width=size, **kw)))
            if dec and dec[1] > size:
                dec = _png_rgba(bytes(resvg_py.svg_to_bytes(height=size, **kw)))
            if not dec:
                continue
            w, h, px = dec
            lum = n = 0
            for i in range(0, len(px), 4):
                if px[i + 3] > 200:
                    lum += (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) / 255000
                    n += 1
            if n < 4 or lum / n < 0.22:
                continue  # empty, or mostly dark: it would vanish on the card (ESPN's -dark PNG handles those)
            out = bytearray(size * size * 4)
            ox, oy = (size - w) // 2, (size - h) // 2
            for y in range(min(h, size)):
                out[((oy + y) * size + ox) * 4:((oy + y) * size + ox + w) * 4] = px[y * w * 4:(y + 1) * w * 4]
            return _png_bytes(size, size, out)
        except Exception:
            continue
    return None


def logo_file(url, size):
    """Local PNG of a team logo scaled to size x size px (ESPN's image resizer), downloaded once; None on failure.

    ESPN's "500-dark" variant is made for dark backgrounds (dark logos stay visible), so it is tried first. The original,
    full-size file is used when it can be decoded; ESPN's resizer (fetched larger and averaged down) otherwise.
    """
    path = logo_path(url, size)
    if os.path.exists(path):
        return path
    src = re.sub(r"^https?://[^/]+", "", url)
    body = _svg_logo(url, size)
    for cand in ([] if body else ([src.replace("/500/", "/500-dark/")] if "/500/" in src else []) + [src]):
        try:
            body = _full_logo(cand, size)
        except Exception:
            body = None
        if not body:
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
                             "short": t.get("shortDisplayName") or t.get("displayName", "?"), "stats": stats,
                             "note": str((e.get("note") or {}).get("description") or "")})
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
# What ESPN's "clincher" mark on a standings row means, per league (ESPN's own legends)
CLINCH_MARKS = {
    "MLB": {"*": "Clinched best record", "w": "Clinched bye", "z": "Clinched division", "y": "Clinched wild card",
            "x": "Clinched playoff berth", "e": "Eliminated"},
    "NFL": {"*": "Clinched bye", "z": "Clinched division", "y": "Clinched wild card", "x": "Clinched playoff berth", "e": "Eliminated"},
    "NBA": {"z": "Clinched best record in conference", "y": "Clinched division", "x": "Clinched playoff berth",
            "pi": "Clinched play-in", "e": "Eliminated"},
    "NHL": {"p": "Clinched Presidents' Trophy", "z": "Clinched conference", "y": "Clinched division",
            "x": "Clinched playoff berth", "e": "Eliminated"},
    "WNBA": {"z": "Clinched best record", "x": "Clinched playoff berth", "e": "Eliminated"},
}


def clinch_mark(row):
    """ESPN's clinch mark for a standings row ("x", "z", "e", ...), or ""."""
    m = str(row["stats"].get("clincher") or "").strip().lower()
    return "" if m in ("", "-", "0", "none") else m


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
        d = game_detail_data(fetch_summary_cached(game["sport"], game["league"], game["id"]), sport=game["sport"], league=game["league"])
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


def _play_label(p, sport="", league=""):
    per = p.get("period") or {}
    clock = (p.get("clock") or {}).get("displayValue", "")
    num = per.get("number")
    if per.get("displayValue"):
        prefix = per["displayValue"]
    elif not num:
        prefix = ""
    else:  # no label from ESPN: Q for football, Q or H for basketball, P only for hockey
        reg = 2 if league == "mens-college-basketball" else 4
        pre = "Q" if sport == "football" or (sport == "basketball" and reg == 4) else "H" if sport == "basketball" else "P"
        prefix = ("OT" if num == reg + 1 else f"{num - reg}OT") if sport in ("football", "basketball") and num > reg else f"{pre}{num}"
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
            label = st.get("displayName") or st.get("shortDisplayName") or st.get("label") or st.get("abbreviation") or st.get("name")
            out[f"{prefix}{st.get('name') or label}"] = (f"{prefix}{label}", str(val))
    return out


# Box score columns worth a narrow window, per ESPN stat category (the first ones ESPN sends otherwise)
BOX_COLS = {
    "passing": ["C/ATT", "YDS", "TD", "INT", "RTG"], "rushing": ["CAR", "YDS", "TD", "LONG"], "receiving": ["REC", "YDS", "TD", "LONG"],
    "defensive": ["TOT", "SACKS", "TFL", "PD"], "interceptions": ["INT", "YDS", "TD"], "fumbles": ["FUM", "LOST", "REC"],
    "kicking": ["FG", "PCT", "LONG", "XP", "PTS"], "punting": ["NO", "YDS", "AVG", "LONG"],
    "kickreturns": ["NO", "YDS", "AVG", "LONG"], "puntreturns": ["NO", "YDS", "AVG", "LONG"],
    "batting": ["AB", "R", "H", "RBI", "BB", "K"], "pitching": ["IP", "H", "R", "ER", "BB", "K"],
    "forwards": ["G", "A", "+/-", "S", "TOI"], "defenses": ["G", "A", "+/-", "S", "TOI"], "goalies": ["SA", "SV", "GA", "SV%"],
    "": ["MIN", "PTS", "REB", "AST", "STL", "BLK", "+/-"],  # basketball: one unnamed category
}
# Column titles spelled out where the box score has the room (it falls back to ESPN's short form column by column)
FULL_COLS = {"C/ATT": "Comp/Att", "YDS": "Yards", "CAR": "Carries", "REC": "Catches", "LONG": "Longest", "TOT": "Tackles", "SACKS": "Sacks",
             "TFL": "TFL", "PD": "Pass Def", "INT": "Int", "FUM": "Fumbles", "LOST": "Lost", "PCT": "Pct", "NO": "Number", "AVG": "Average",
             "AB": "At Bats", "R": "Runs", "H": "Hits", "BB": "Walks", "K": "Strikeouts", "IP": "Innings", "ER": "Earned", "G": "Goals",
             "A": "Assists", "S": "Shots", "TOI": "Ice Time", "SA": "Shots Against", "SV": "Saves", "GA": "Goals Against", "SV%": "Save %",
             "RTG": "Rating", "MIN": "Minutes", "PTS": "Points", "REB": "Rebounds", "AST": "Assists", "STL": "Steals", "BLK": "Blocks"}
BOX_SHOW = ("passing", "rushing", "receiving", "defensive", "kicking", "batting", "pitching", "forwards", "defenses", "goalies", "")


def box_score(data, away_id="", home_id=""):
    """[away, home] box scores from an ESPN summary: each a list of {"title", "cols", "rows": [(name, values)], "totals"};
    None when ESPN has no player stats (e.g. most soccer)."""
    players = (data.get("boxscore") or {}).get("players") or []
    if len(players) != 2:
        return None
    by_id = {str((p.get("team") or {}).get("id")): p for p in players}
    sides = [by_id.get(str(away_id), players[0]), by_id.get(str(home_id), players[1])]
    out = []
    for side in sides:
        cats = []
        for st in side.get("statistics") or []:
            name = str(st.get("name") or st.get("type") or "").lower()
            if name not in BOX_SHOW:
                continue
            labels = [str(l) for l in (st.get("labels") or st.get("names") or [])]
            want = [c for c in BOX_COLS.get(name, []) if c in labels] or labels[:5]
            idx = [labels.index(c) for c in want]
            rows = []
            for a in st.get("athletes") or []:
                vals = a.get("stats") or []
                if a.get("didNotPlay") or not vals:
                    continue
                ath = a.get("athlete") or {}
                nm = ath.get("shortName") or ath.get("displayName") or "?"
                rows.append((nm, [str(vals[i]) if i < len(vals) else "" for i in idx]))
            if not rows:
                continue
            tot = st.get("totals") or []
            title = str(st.get("text") or "").split(" ")[-1] if st.get("text") else name.title()
            cats.append({"title": title if name else "", "cols": want, "rows": rows,
                         "totals": [str(tot[i]) if i < len(tot) else "" for i in idx] if tot else None})
        out.append(cats)
    return out if any(out) else None


def game_detail_data(data, max_plays=14, max_stats=14, sport="", league=""):
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
    if wp and wp[0].get("homeWinPercentage") is not None:
        out["home_win_start"] = float(wp[0]["homeWinPercentage"])  # at the start of the game
    else:  # ESPN's pregame projection, when the game has no win-probability series
        try:
            out["home_win_start"] = float(((data.get("predictor") or {}).get("homeTeam") or {}).get("gameProjection")) / 100
        except (TypeError, ValueError):
            pass
    def periods(c):
        return [str(l.get("displayValue") if l.get("displayValue") not in (None, "") else int(float(l.get("value") or 0)))
                for l in c.get("linescores") or []]
    out["hits_errors"] = [(c.get("hits"), c.get("errors")) for c in (away, home)]
    if periods(away) or periods(home):
        out["linescore"] = {"away": periods(away), "home": periods(home)}
    plays = data.get("plays") or []
    if not plays:
        drives = data.get("drives") or {}
        for d in (drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else []):
            plays += d.get("plays") or []
    lines = []
    for p in plays[-max_plays:][::-1]:
        text = p.get("text") or p.get("shortText")
        if text:
            lines.append((_play_label(p, sport, league), text))
    out["plays"] = lines
    out["box"] = box_score(data, (away.get("team") or {}).get("id", ""), (home.get("team") or {}).get("id", ""))
    out["scoring"] = [((_play_label(p, sport, league)), p.get("text") or p.get("shortText", "")) for p in (data.get("scoringPlays") or [])][-8:]
    teams = (data.get("boxscore") or {}).get("teams") or []
    if len(teams) == 2:
        by_id = {str((t.get("team") or {}).get("id")): t for t in teams}
        a = by_id.get(str((away.get("team") or {}).get("id")), teams[0])
        h = by_id.get(str((home.get("team") or {}).get("id")), teams[1])
        ha, hh = _flat_stats(a), _flat_stats(h)
        both = [(key, label, val, hh.get(key, ("", ""))[1]) for key, (label, val) in ha.items()]
        out["stats"] = [(label, a_, h_) for _k, label, a_, h_ in both][:max_stats]
        out["all_stats"] = both  # untruncated, for picking a few key stats per sport
    else:
        out["stats"] = []
        out["all_stats"] = []
    return out


def _halftime(comp):
    t = (comp.get("status") or {}).get("type") or {}
    return t.get("name") == "STATUS_HALFTIME" or "halftime" in str(t.get("shortDetail") or t.get("detail") or "").lower()


def _possession_id(sit):
    """Id of the team with the ball. Some games (college) omit `possession`: the team that made the last play stands in."""
    poss = sit.get("possession")
    if poss in (None, ""):
        lp = sit.get("lastPlay") or {}
        poss = (lp.get("team") or {}).get("id") or lp.get("teamId") or ""
    return str(poss)


# Key team stats for a finished game's Scoreboard card: (label, candidate ESPN stat names, is a percentage).
STAT_PICKS = {
    "baseball": [("Hits", ("bathits", "bath"), False), ("Home runs", ("bathomeruns", "bathr"), False),
                 ("Strikeouts", ("batstrikeouts", "batk", "batso"), False), ("Errors", ("flderrors", "flde"), False),
                 ("Walks", ("batwalks", "batbb", "batbaseonballs"), False), ("Batting avg", ("batavg",), False)],
    "football": [("Total yds", ("totalYards",), False), ("Pass yds", ("netPassingYards", "passingYards"), False),
                 ("Rush yds", ("rushingYards",), False), ("Turnovers", ("turnovers",), False), ("1st downs", ("firstDowns",), False),
                 ("3rd down", ("thirdDownEff",), False), ("Possession", ("possessionTime",), False)],
    "basketball": [("FG%", ("fieldGoalPct",), True), ("3P%", ("threePointFieldGoalPct",), True),
                   ("Rebounds", ("totalRebounds", "rebounds"), False), ("Assists", ("assists",), False),
                   ("Turnovers", ("turnovers", "totalTurnovers"), False), ("Steals", ("steals",), False),
                   ("Blocks", ("blocks",), False)],
    "hockey": [("Shots", ("shotsTotal", "shots", "shotsOnGoal"), False), ("Hits", ("hits",), False),
               ("Penalty min", ("penaltyMinutes", "penaltyMins"), False), ("Power play", ("powerPlay",), False),
               ("Blocks", ("blockedShots",), False), ("Takeaways", ("takeaways",), False)],
    "soccer": [("Possession", ("possessionPct", "possession"), True), ("Shots", ("totalShots", "shots"), False),
               ("On target", ("shotsOnTarget",), False), ("Corners", ("wonCorners", "corners"), False),
               ("Fouls", ("foulsCommitted", "fouls"), False), ("Saves", ("saves",), False)],
}


def pick_stats(sport, full, n=4):
    """[(label, away, home)] for the sport's key stats found in `full` ([(key, label, away, home)]);
    the first few stats ESPN lists when too few of them are there."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", str(x).lower())
    by = {}
    for key, label, a_, h_ in full:
        by.setdefault(norm(key), (a_, h_))
        by.setdefault(norm(label), (a_, h_))
    out = []
    for label, names, pct in STAT_PICKS.get(sport, []):
        v = next((by[norm(nm)] for nm in names if norm(nm) in by), None)
        if v:
            out.append((label, *[x if not pct or "%" in x else x + "%" for x in v]))
    return out[:n] if len(out) >= 2 else [(label, a_, h_) for _k, label, a_, h_ in full[:n]]


def period_labels(sport, league, n):
    """Column titles for n periods: innings, quarters, halves or periods, with overtime after regulation."""
    if sport == "baseball":
        return [str(i + 1) for i in range(n)]
    reg = {"football": 4, "hockey": 3, "soccer": 2}.get(sport, 2 if league == "mens-college-basketball" else 4)
    pre = {"football": "Q", "basketball": "" if reg == 2 else "Q", "hockey": "", "soccer": ""}.get(sport, "")
    out = []
    for i in range(n):
        if i < reg:
            out.append(f"{pre}{i + 1}")
        elif sport == "soccer":
            out.append("ET" if i == reg else f"ET{i - reg + 1}")
        else:
            ot = i - reg + 1
            out.append("OT" if ot == 1 else f"{ot}OT")
    return out


def situation_text(sport, comp):
    """Sport-specific live info: situation (down/possession, count/runners, power play) and team stats."""
    sit = comp.get("situation") or {}
    if sport == "football" and _halftime(comp):
        sit = {}  # the down and distance left over from the first half no longer apply
    lines = []
    if sport == "football" and sit:
        parts = [sit.get("shortDownDistanceText") or sit.get("downDistanceText")]
        poss = _possession_id(sit)
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
    if sport == "football" and _halftime(comp):
        return None
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
        poss = _possession_id(sit)
        off = teams.get(poss, "")
        defn = next((a for k, a in teams.items() if k != poss), "")
        x = None  # yards from the offense's own goal line (0-100), driving toward 100
        if sit.get("yardsToEndzone") is not None:
            x = 100 - int(sit["yardsToEndzone"])
        else:
            m = re.match(r"\s*([A-Za-z.]+)\s+(\d+)", str(sit.get("possessionText") or sit.get("downDistanceText") or ""))
            if m:
                n = int(m.group(2))
                side_ = m.group(1).upper()
                if side_ == off.upper():
                    x = n
                elif side_ == defn.upper() or n > 50:  # the other team's side, or a name ESPN spells differently
                    x = 100 - n
                else:
                    x = n
        if x is None or not off:
            return None
        dist = sit.get("distance")
        first = min(100, x + int(dist)) if dist not in (None, "") else None
        off_c = next((c for c in comp.get("competitors", []) if str(c.get("id", c.get("team", {}).get("id", ""))) == poss), {})
        def_c = next((c for c in comp.get("competitors", []) if c is not off_c), {})
        return {"kind": "football", "x": max(0, min(100, x)), "first": first, "off": off, "def": defn,
                "color": team_colors(off_c, "#34d399"), "def_color": team_colors(def_c, "#52526a"),
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


@functools.lru_cache(maxsize=1024)
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


LOGO_HINTS = {}  # ESPN logo URL -> its team object (name, location, abbreviation), for finding the team's SVG


def _logo(team):
    """Logo URL ESPN gives for a team object, or None."""
    url = team.get("logo") or next((l.get("href") for l in team.get("logos") or [] if l.get("href")), None)
    if url:
        LOGO_HINTS.setdefault(url, team)
    return url


def comp_logos(comp):
    """[away, home] logo URLs of a game (the competitor order when there is no home/away)."""
    cs = comp.get("competitors", [])
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    pair = (away, home) if away and home else tuple(cs[:2])
    return [_logo(c.get("team", {})) for c in pair]


# Badge colors (background, text) for the networks that show games; others get a neutral badge.
NETWORK_STYLES = {
    "espn": ("#cc0000", "#ffffff"), "espn2": ("#cc0000", "#ffffff"), "espnu": ("#cc0000", "#ffffff"), "espn+": ("#d4a017", "#111111"),
    "abc": ("#f2f2f2", "#111111"), "fox": ("#0b3d91", "#ffffff"), "fs1": ("#0b3d91", "#ffffff"), "fs2": ("#0b3d91", "#ffffff"),
    "nbc": ("#e0a800", "#111111"), "cbs": ("#0b4aa6", "#ffffff"), "tnt": ("#2a2a2a", "#ffcc00"), "tbs": ("#1e5fd8", "#ffffff"),
    "trutv": ("#ffcc00", "#111111"), "peacock": ("#111111", "#ffffff"), "prime video": ("#00a8e1", "#ffffff"),
    "amazon prime video": ("#00a8e1", "#ffffff"), "apple tv+": ("#f2f2f2", "#111111"), "apple tv": ("#f2f2f2", "#111111"),
    "netflix": ("#e50914", "#ffffff"), "paramount+": ("#0064ff", "#ffffff"), "max": ("#5b2ee6", "#ffffff"),
    "nba tv": ("#c9082a", "#ffffff"), "nfl network": ("#013369", "#ffffff"), "mlb network": ("#0a2a5c", "#ffffff"),
    "nhl network": ("#222222", "#ffffff"), "youtube": ("#ff0000", "#ffffff"), "cw": ("#00a651", "#ffffff"),
    "usa": ("#1a73e8", "#ffffff"), "big ten network": ("#0088ce", "#ffffff"), "acc network": ("#013ca6", "#ffffff"),
    "sec network": ("#004b8d", "#ffffff"), "pac-12 network": ("#0a5a9c", "#ffffff"),
}


def tv_channels(comp, limit=3):
    """'ESPN · Peacock': the TV and streaming channels showing a game (national TV first, then streaming), or ''."""
    found = []  # (not national, is streaming, name)
    market = lambda b: str(((b.get("market") or {}).get("type") if isinstance(b.get("market"), dict) else b.get("market")) or "").lower()
    for b in comp.get("broadcasts") or []:
        names = b.get("names") or [(b.get("media") or {}).get("shortName")]
        found += [(market(b) != "national", False, n) for n in names if n]
    for b in comp.get("geoBroadcasts") or []:
        kind = str((b.get("type") or {}).get("shortName", "TV")).lower()
        name = (b.get("media") or {}).get("shortName")
        if name and kind != "radio":
            found.append((market(b) != "national", kind in ("web", "streaming", "stream", "online"), name))
    names = list(dict.fromkeys(n for _loc, _stream, n in sorted(found, key=lambda t: t[:2])))
    return " \u00b7 ".join(names[:limit])


def streak_text(events, team_abbr):
    """'W5' / 'L3': the team's current run of results from its schedule (two or more in a row), else ''."""
    res = []
    for e in sorted((e for e in events if e.get("competitions") and _state_of(e) == "post"), key=lambda e: e.get("date", "")):
        comp = e["competitions"][0]
        me = _find_me(comp, team_abbr)
        opp = next((c for c in comp.get("competitors", []) if c is not me), None) if me else None
        try:
            a_, b_ = float(_score(me)), float(_score(opp))
        except (TypeError, ValueError):
            continue
        res.append("W" if a_ > b_ else "L" if a_ < b_ else "T")
    if not res or res[-1] == "T":
        return ""
    n = 0
    for x in reversed(res):
        if x != res[-1]:
            break
        n += 1
    return f"{res[-1]}{n}" if n >= 2 else ""


def series_info(comp):
    """{"head": 'East 1st Round - Game 3', "text": 'BOS leads series 2-1' / 'BOS wins series 4-2'} for a playoff game, else None."""
    ser = comp.get("series") or {}
    head = str((comp.get("notes") or [{}])[0].get("headline") or "").strip()
    text = str(ser.get("summary") or "").strip()
    wins = [(c.get("id"), int(c.get("wins") or 0)) for c in ser.get("competitors") or []]
    if len(wins) == 2 and "win" not in text.lower():  # ESPN's summary doesn't always name the winner
        (wid, w_), (_lid, l_) = sorted(wins, key=lambda t: -t[1])
        try:
            need = int(ser.get("totalCompetitions") or 0) // 2 + 1  # wins that take a best-of-N series
        except (TypeError, ValueError):
            need = 0
        done = bool(ser.get("completed")) or (need and w_ >= need)
        if not done:
            wins = []
        abbr = next((c.get("team", {}).get("abbreviation") for c in comp.get("competitors", [])
                     if str(c.get("id", c.get("team", {}).get("id"))) == str(wid)), "")
        if wins and abbr:
            text = f"{abbr} wins series {w_}-{l_}"
    return {"head": head, "text": text} if head or text else None


def _postseason_event(e):
    st = e.get("seasonType") or (e.get("season") or {}).get("type")
    t = st.get("type") if isinstance(st, dict) else st
    try:
        return int(t) == 3
    except (TypeError, ValueError):
        return False


def series_from_schedule(events, event, team_abbr):
    """Series status worked out from the team's own schedule, for when ESPN's game data carries none:
    'GS wins series 2-1' once no more games against that opponent are scheduled, else 'GS leads series 2-1' / 'Series tied 1-1'."""
    def sides(e):
        comp = e["competitions"][0]
        me = _find_me(comp, team_abbr)
        opp = next((c for c in comp.get("competitors", []) if c is not me), None)
        return (me, opp) if me and opp else (None, None)
    _me, opp0 = sides(event)
    if not opp0:
        return ""
    oid = str(opp0.get("team", {}).get("id", opp0.get("id")))
    won = lost = ahead = 0
    for e in events:
        if not e.get("competitions") or not _postseason_event(e):
            continue
        me, opp = sides(e)
        if not opp or str(opp.get("team", {}).get("id", opp.get("id"))) != oid:
            continue
        st = _state_of(e)
        if st == "pre":
            ahead += 1
        elif st == "post":
            try:
                a_, b_ = float(_score(me)), float(_score(opp))
            except ValueError:
                continue
            won += a_ > b_
            lost += a_ < b_
    if not won and not lost:
        return ""
    mine = (_me.get("team", {}).get("abbreviation") or team_abbr).upper()
    theirs = opp0.get("team", {}).get("abbreviation", "OPP")
    lead, trail, who = (won, lost, mine) if won >= lost else (lost, won, theirs)
    if not ahead and won != lost:
        return f"{who} wins series {lead}-{trail}"
    return f"{who} leads series {lead}-{trail}" if won != lost else f"Series tied {won}-{lost}"


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
    return [{"logo": _logo(c.get("team") or {}), "ha": c.get("homeAway", ""), "record": _record(c).strip(" ()"), "id": str((c.get("team") or {}).get("id", c.get("id", ""))),
             "color": team_colors(c, "#34d399"),
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
    last = str((sit.get("lastPlay") or {}).get("text") or "").strip()  # what just happened (Scoreboard layout)
    if last and not _halftime(comp):
        out.append({"kind": "lastplay", "text": last, "team": str(((sit.get("lastPlay") or {}).get("team") or {}).get("id", ""))})
    # per-team remaining timeouts / ABS challenges: dots under each team in the Scoreboard layout
    def side(k, words):  # a team's count, or None when ESPN leaves it out (which is not the same as 0)
        for key, v in sit.items():
            if key.lower().startswith(k) and any(w in key.lower() for w in words):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None
        return None
    for words, total in ((("timeout",), 3), (("challenge",), 2)):
        h_, a_ = side("home", words), side("away", words)
        if h_ is not None or a_ is not None:
            out.append({"kind": "timeouts", "home": h_, "away": a_, "total": total})
    return out or None


COLLEGE_GROUPS = {"college-football": ("80", "81"), "mens-college-basketball": ("50",),
                  "womens-college-basketball": ("50",)}  # FBS/FCS, Division I


def fresh_event(entry, event):
    """The live game from ESPN's scoreboard, fetched now (schedule data lacks live scores and situation)."""
    today = datetime.now().astimezone().date()
    rng = f"{(today - timedelta(days=1)):%Y%m%d}-{today:%Y%m%d}"
    # ESPN's default college scoreboard lists only featured games; the other games need their conference group
    for groups in (None,) + COLLEGE_GROUPS.get(entry["league"], ()):
        try:
            for e in fetch_scoreboard(entry["sport"], entry["league"], rng, groups):
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
    next_line, next_tv = "", ""
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
                next_tv = tv_channels(nxt["competitions"][0])
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
    streak = streak_text(events, entry["team"])
    ser = series_info(event["competitions"][0]) if state == "post" else None
    if state == "post" and not ser:  # schedule data often lacks the series; the scoreboard's copy of the game has it
        sb_ev = scoreboard_event(entry["sport"], entry["league"], event)
        ser = series_info(sb_ev["competitions"][0]) if sb_ev else None
    if state == "post" and not (ser or {}).get("text") and _postseason_event(event):  # still nothing: count it from the schedule
        text = series_from_schedule(events, event, entry["team"])
        if text:
            ser = {"head": (ser or {}).get("head", ""), "text": text}
    return {"tv": tv_channels(event["competitions"][0]) if state in ("in", "pre") else "", "series": ser,
            "teams": [dict(t, record=(t["record"] or (own if i == 0 and own else "")) + (
                          f" \u00b7 {streak}" if i == 0 and streak else ""))
                      for i, t in enumerate(comp_teams(event["competitions"][0], me or None))], "logos": [_logo(me.get("team", {})) or _logo(team)],
            "score": parts["score"], "status": parts["status"], "clock": live_clock(event, entry["sport"]), "name": name, "state": state, "line": line, "detail": detail, "info": info, "graphic": graphic,
            "next": next_line, "next_tv": next_tv,
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
            "info": "", "graphic": None, "logos": [logo],
            "next_tv": tv_channels(nxt["competitions"][0]) if s_next else ""}


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
            row = {"tv": tv_channels(comp) if state in ("in", "pre") else "", "logos": comp_logos(comp), "teams": comp_teams(comp), "series": series_info(comp) if state == "post" else None,
                   "name": matchup, "state": state, "line": name + (f" · {extra}" if extra else ""),
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
SCORE_ANIM_CHOICES = [("Ripple", "pulse"), ("Off", "off")]  # (a saved "flash" from older versions plays as Ripple)
LAYOUT_CHOICES = [("Scoreboard", "scoreboard"), ("List", "list")]  # "default" in an older state.json means List
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
                        "teams": comp_teams(comp), "tv": tv_channels(comp) if state in ("in", "pre") else "",
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


def _get_scoreboard(sport, league, date, limit, groups=None):
    url = SCOREBOARD.format(sport=sport, league=league, date=date)
    if groups:
        url += f"&groups={groups}"
    if limit:
        url += f"&limit={limit}"
    data = get_json(url)
    # Some responses only carry the season at league level; copy it onto events.
    lg_season = (data.get("leagues") or [{}])[0].get("season", {})
    events = data.get("events", [])
    for e in events:
        e.setdefault("season", lg_season if lg_season.get("type") else data.get("season", {}))
    return events


def fetch_scoreboard(sport, league, date, groups=None):
    """Scoreboard events for a YYYYMMDD date or YYYYMMDD-YYYYMMDD range.

    Cached for a few seconds, so the many callers in one refresh (live teams, tracked games,
    Leagues, Playoffs) that want the same scoreboard share one request.
    """
    return cached(("scoreboard", sport, league, date, groups), 5, lambda: _fetch_scoreboard(sport, league, date, groups))


def _fetch_scoreboard(sport, league, date, groups=None):
    """ESPN answers HTTP 400 to parameter combinations it dislikes, so fall back:
    range + limit -> range alone -> one request per day.
    """
    try:
        return _get_scoreboard(sport, league, date, 300, groups)
    except urllib.error.HTTPError as ex:
        if ex.code != 400:
            raise
    try:
        return _get_scoreboard(sport, league, date, None, groups)
    except urllib.error.HTTPError as ex:
        if ex.code != 400 or "-" not in date:
            raise
    start, end = (datetime.strptime(d, "%Y%m%d") for d in date.split("-"))
    days = [f"{start + timedelta(days=i):%Y%m%d}" for i in range((end - start).days + 1)]
    events, seen = [], set()
    for day_events in pmap(lambda day: _get_scoreboard(sport, league, day, None, groups), days):
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
                return {"tv": tv_channels(e["competitions"][0]) if s[0] in ("in", "pre") else "",
                        "logos": comp_logos(e["competitions"][0]), "teams": comp_teams(e["competitions"][0]),
                        "series": series_info(e["competitions"][0]) if s[0] == "post" else None,
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
    FLAG_YELLOW = "#facc15"  # PENALTY badge (the color of a penalty flag)
    UI_FONT = ("Segoe UI", 9)
    _font_objs = {}

    def font_obj(spec):
        """One tkinter Font per font spec, reused (creating a Font is several Tcl calls and a new named font)."""
        f = _font_objs.get(spec)
        if f is None:
            import tkinter.font as tkfont
            f = _font_objs[spec] = tkfont.Font(font=spec)
        return f

    @functools.lru_cache(maxsize=4096)
    def text_width(spec, text):
        """Pixel width of `text` in font `spec` (cached: the same labels are measured on every redraw)."""
        return font_obj(spec).measure(text)

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
        w = text_width(UI_FONT, text) + 28
        c = tk.Canvas(parent, width=w, height=28, bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        shape = c.create_polygon(rr_points(1, 1, w - 1, 27, 8), smooth=True, fill=PANEL, outline=PANEL)
        c.create_text(w / 2, 14, text=text, fill=FG, font=UI_FONT)
        c.bind("<Enter>", lambda e: c.itemconfigure(shape, fill=HOVER, outline=HOVER))
        c.bind("<Leave>", lambda e: c.itemconfigure(shape, fill=PANEL, outline=PANEL))
        c.bind("<ButtonRelease-1>", lambda e: command() if 0 <= e.x <= w and 0 <= e.y <= 28 else None)
        return c

    def styled_option(parent, var, values, command=None, width=None):
        """A rounded dropdown button; clicking it opens the app's own dark context menu under it."""
        w = max(text_width(UI_FONT, v) for v in values) + 52
        c = tk.Canvas(parent, width=w, height=28, bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        shape = c.create_polygon(rr_points(1, 1, w - 1, 27, 8), smooth=True, fill=PANEL, outline=PANEL)
        label = c.create_text(12, 14, text=var.get(), fill=FG, font=UI_FONT, anchor="w")
        c.create_line(w - 20, 12, w - 15, 17, w - 10, 12, fill=DIM, width=2)  # the chevron

        def pick(v):
            var.set(v)
            c.itemconfigure(label, text=v)
            if command:
                command(v)

        def open_menu(e):
            if not (0 <= e.x <= w and 0 <= e.y <= 28):
                return
            c.itemconfigure(label, text=var.get())
            popup_menu(c.winfo_rootx(), c.winfo_rooty() + 30, [(v, lambda v_=v: pick(v_), v == var.get()) for v in values])
        c.bind("<Enter>", lambda e: c.itemconfigure(shape, fill=HOVER, outline=HOVER))
        c.bind("<Leave>", lambda e: c.itemconfigure(shape, fill=PANEL, outline=PANEL))
        c.bind("<ButtonRelease-1>", open_menu)
        return c

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
                    root.after(8, lambda: play(i + 1))
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
        w_ = text_width(("Segoe UI", 9, "bold"), label) + 26
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
             "smallb": ("Segoe UI", 8, "bold"), "ban": ("Segoe UI", 14, "bold")}
    PAD, GAP = 10, 6
    LOGO_W, BB_W = 52, 112  # width reserved for a logo in front of the name; baseball bases/count graphic
    last = {}
    session = {"live_prev": 0, "expanded": set(), "details": {}, "games": {}, "sig": None,
               "anims": {}, "vis": {}, "hits": {}, "total": 0, "looping": False, "actx": None, "standings": {}, "college": {},
               "roll_last": {}, "rolls": {}, "roll_cells": [], "rolling": False,
               "clock_items": [], "stats": {}, "stats_redraw": False,
               "score_prev": {}, "play_prev": {}, "win_prev": {}, "down_prev": {}, "poss_prev": {}, "was_live": set(), "bases_prev": {}, "run_hold": {}, "celebs": {}, "celeb_next": {}, "daggers": set(), "celeb_on": False,
               "pulse_items": [], "pulse_on": False, "cur_celeb": (None, 0), "force_clutch": {}, "force_red": {}, "test_scores": {}, "xfade": None, "hcards": [], "hshow": {}, "box_side": {}, "opening": set(), "hseen": {}, "h_on": False,
               "layers": {}, "cur_layer": None, "ring_center": None, "celeb_dirty": False,
               "tweens": {}, "shown": {}, "gcount": {}, "cur_key": None, "tween_on": False}

    SESSION0 = {k_: type(v_)() if isinstance(v_, (dict, list, set)) else v_ for k_, v_ in session.items()}

    MAIN = session  # the real session (the dummy card swaps its own in while a test runs)

    def run_in(view, fn, *a):
        """Run fn with the drawing canvas and session swapped for a (canvas, session) view, e.g. the Settings dummy card."""
        nonlocal canvas, session
        if view is None:
            return fn(*a)
        try:
            if not view[0].winfo_exists():
                return None
        except tk.TclError:
            return None
        saved = canvas, session
        canvas, session = view
        try:
            return fn(*a)
        finally:
            canvas, session = saved

    FRAME_MS = 8  # every animation loop aims at 120 frames a second

    def frame_delay(t0):
        """ms to wait before the next frame, given when this frame began (so slow frames don't push the rate down further)."""
        return max(1, FRAME_MS - int((_time.perf_counter() - t0) * 1000))

    if sys.platform == "win32":  # Windows timers tick every 15.6 ms by default; ask for 1 ms so 120 fps is possible
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

    def gkey(g):
        return f'{g["league"]}:{g["id"]}'

    def item_mark():
        """An id below every canvas item drawn after this call (item ids only grow)."""
        i = canvas.create_line(0, 0, 0, 0)
        canvas.delete(i)
        return i

    def items_since(m):
        """Ids of the items drawn since item_mark() returned m, without listing the whole canvas."""
        return range(m + 1, item_mark())

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

    # ---- eased movement of bars and markers when a value changes (win probability, ball, first-down marker) ----
    TWEEN_SECS = 1.1
    ease_io = lambda p: 0.5 - 0.5 * math.cos(math.pi * p)  # gentle start and finish

    def tkey(kind, label=""):
        """A key for one animated element of the card being drawn (the n-th of its kind, so a card's bar and its details' bar differ)."""
        base = (session["cur_key"], kind, label)
        n = session["gcount"].get(base, 0)
        session["gcount"][base] = n + 1
        return base + (n,)

    def tween_value(key, target):
        """The value to draw now for `key`: eased from where it was to `target` when the target has changed."""
        now = _time.perf_counter()
        shown = session["shown"].get(key)
        tw = session["tweens"].get(key)
        if shown is None:
            session["shown"][key] = target
            return target
        if shown != target:
            cur = target
            if tw:
                p = min(1.0, (now - tw["t0"]) / tw["dur"])
                cur = tw["v0"] + (tw["v1"] - tw["v0"]) * ease_io(p)
            else:
                cur = shown
            tw = session["tweens"][key] = {"v0": cur, "v1": target, "t0": now, "dur": TWEEN_SECS, "apply": None}
            session["shown"][key] = target
            if not session["tween_on"]:
                session["tween_on"] = True
                root.after(1, run_in, session.get("view"), tween_tick)
        if tw:
            p = (now - tw["t0"]) / tw["dur"]
            if p >= 1:
                session["tweens"].pop(key, None)
                return target
            return tw["v0"] + (tw["v1"] - tw["v0"]) * ease_io(p)
        return target

    def tween_apply(key, fn):
        """Say how to move the items just drawn for `key`, for the frames between full redraws."""
        tw = session["tweens"].get(key)
        if tw:
            tw["apply"] = fn

    def tween_tick():
        now = _time.perf_counter()
        for key, tw in list(session["tweens"].items()):
            p = min(1.0, (now - tw["t0"]) / tw["dur"])
            if tw["apply"]:
                try:
                    tw["apply"](tw["v0"] + (tw["v1"] - tw["v0"]) * ease_io(p))
                except tk.TclError:
                    pass
            if p >= 1:
                session["tweens"].pop(key, None)
        if session["tweens"]:
            root.after(frame_delay(now), run_in, session.get("view"), tween_tick)
        else:
            session["tween_on"] = False

    def graphic_one(ox, oy, g, bg, W):
        c = Off(ox, oy)
        kind = g["kind"]
        if kind == "periods":
            n, gap = len(g["fills"]), 4
            seg = (W - gap * (n - 1)) / n
            for i, f in enumerate(g["fills"]):
                x = i * (seg + gap)
                c.create_line(x + 3, 7, x + seg - 3, 7, fill=TRACK, width=6, capstyle="round")
                pk = tkey("period", str(i))  # each segment eases to its new fill
                fill_ = c.create_line(x + 3, 7, x + 3, 7, fill="#34d399" if f < 1 else "#6b6b78", width=6, capstyle="round")

                def put_fill(v, it=fill_, x=x):
                    if v > 0.001:
                        canvas.coords(it, c.ox + x + 3, c.oy + 7, c.ox + x + 3 + (seg - 6) * v, c.oy + 7)
                        canvas.itemconfigure(it, state="normal")
                    else:
                        canvas.itemconfigure(it, state="hidden")
                put_fill(tween_value(pk, f))
                tween_apply(pk, put_fill)
            return 14
        if kind == "versus":
            vk = tkey("versus", g["label"])
            la = c.create_line(3, 20, 3, 20, fill=g.get("a_color", "#60a5fa"), width=6, capstyle="round")
            lb = c.create_line(3, 20, 3, 20, fill=g.get("b_color", "#f59e0b"), width=6, capstyle="round")

            win_bar_ = "win probability" in g["label"].lower()
            mid = c.create_line(0, 14, 0, 26, fill="#ffffff", width=2) if win_bar_ else None  # where the two sides meet

            def put_bar(share):  # the two halves meet at `share` of the width; easing moves that point left and right
                split = W * share
                if mid:
                    canvas.coords(mid, c.ox + split, c.oy + 14, c.ox + split, c.oy + 26)
                for it, x0, x1 in ((la, 3, split - 2), (lb, split + 2, W - 3)):
                    if x1 - x0 > 0:
                        canvas.coords(it, c.ox + x0, c.oy + 20, c.ox + x1, c.oy + 20)
                        canvas.itemconfigure(it, state="normal")
                    else:
                        canvas.itemconfigure(it, state="hidden")
            put_bar(tween_value(vk, g["a"] / (g["a"] + g["b"])))
            tween_apply(vk, put_bar)
            unit = "%" if "win probability" in g["label"].lower() else ""
            fmt = lambda v: f"{v:g}{unit}"
            c.create_text(0, 6, text=f'{g["a_name"]} {fmt(g["a"])}', anchor="w", fill=FG, font=FONTS["small"])
            c.create_text(W / 2, 6, text=g["label"], fill=DIM, font=FONTS["small"])
            c.create_text(W, 6, text=f'{fmt(g["b"])} {g["b_name"]}', anchor="e", fill=FG, font=FONTS["small"])
            return 30
        if kind == "timeline":
            span = 90 if g["minute"] <= 90 else 120
            px = lambda m: 6 + (W - 12) * min(m, span) / span
            c.create_line(6, 22, W - 6, 22, fill="#33333d", width=4, capstyle="round")
            mk = tkey("minute")  # the progress line and the minute dot ease along the timeline
            mv = tween_value(mk, g["minute"])
            prog = c.create_line(6, 22, px(mv), 22, fill="#34d399", width=4, capstyle="round")
            for m in (45, 90):
                c.create_line(px(m), 17, px(m), 27, fill="#4a4a55")
            for ev in g["events"]:
                x, y = px(ev["min"]), 9 if ev["home"] else 35
                if ev["kind"] == "goal":
                    c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=ev.get("color", FG), outline=FG)
                else:
                    c.create_rectangle(x - 3, y - 4, x + 3, y + 4, fill="#fbbf24" if ev["kind"] == "yellow" else "#ef4444", outline="")
            dot = c.create_oval(px(mv) - 4, 18, px(mv) + 4, 26, fill="#34d399", outline=FG)

            def put_minute(v):
                canvas.coords(prog, c.ox + 6, c.oy + 22, c.ox + px(v), c.oy + 22)
                canvas.coords(dot, c.ox + px(v) - 4, c.oy + 18, c.ox + px(v) + 4, c.oy + 26)
            tween_apply(mk, put_minute)
            return 44
        if kind == "baseball":
            def base(cx, cy, on):
                r = 5
                hl = g.get("color", "#fbbf24")
                c.create_polygon(cx, cy - r, cx + r, cy, cx, cy + r, cx - r, cy, fill=hl if on else bg,
                                 outline=hl if on else DIM, width=2)
            base(21, 15, g["bases"][1]); base(29, 23, g["bases"][0]); base(13, 23, g["bases"][2])
            c.create_polygon(18, 31, 24, 31, 24, 34, 21, 37, 18, 34, fill=bg, outline=DIM)  # home plate
            ce_, ct_ = session["cur_celeb"]
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
            import math

            def cap(x0, x1, left, right):
                """Polygon for the bar from x0 to x1 (y 14-20) with a half-circle end where `left` / `right` is set."""
                pts = []
                for k in range(9):  # left end: from the top round to the bottom
                    t = math.pi / 2 + math.pi * k / 8
                    pts += [x0 + 3 + 3 * math.cos(t), 17 - 3 * math.sin(t)] if left else ([x0, 14] if k == 0 else [x0, 20] if k == 8 else [])
                for k in range(9):  # right end: from the bottom round to the top
                    t = -math.pi / 2 + math.pi * k / 8
                    pts += [x1 - 3 + 3 * math.cos(t), 17 - 3 * math.sin(t)] if right else ([x1, 20] if k == 0 else [x1, 14] if k == 8 else [])
                return pts
            c.create_polygon(cap(0, W, True, True), fill=TRACK, outline="")
            red = g["red"] or session["force_red"].get(session["cur_key"], 0) > _time.perf_counter()  # (or the Settings test)
            if red:
                c.create_rectangle(px(80), 14, px(100), 20, fill="#7f3b3b", outline="")
            c.create_polygon(cap(0, px(0), True, False), fill=g.get("color", "#52526a"), outline="")  # end zones in team colors:
            c.create_polygon(cap(px(100), W, False, True), fill=g.get("def_color", "#52526a"), outline="")  # own, then the one attacked
            for yd in range(0, 101, 10):  # goal lines and a line every 10 yards
                c.create_line(px(yd), 14, px(yd), 20, fill=FG if yd in (0, 100) else "#7a7a88")
            if red:  # red zone: the field bar glows
                pid = c.create_rectangle(-2, 11, W + 2, 23, outline="#ef4444", fill="", width=2)
                session["pulse_items"].append((pid, "#ef4444", bg))
                start_pulse()
            if g["first"] is not None:  # the first-down marker and the ball ease along the field when they move
                fk = tkey("first")
                fx = px(tween_value(fk, g["first"]))
                fl = c.create_line(fx, 11, fx, 23, fill="#fbbf24", width=2)
                tween_apply(fk, lambda v: canvas.coords(fl, c.ox + px(v), c.oy + 11, c.ox + px(v), c.oy + 23))
            bk = tkey("ball")
            bx = px(tween_value(bk, g["x"]))
            ball = c.create_oval(bx - 5, 12, bx + 5, 22, fill=g.get("color", "#34d399"), outline=FG)
            tween_apply(bk, lambda v: canvas.coords(ball, c.ox + px(v) - 5, c.oy + 12, c.ox + px(v) + 5, c.oy + 22))
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

    def draw_linescore(x, y, w, d, g):
        """Points per period (quarter, half, inning...) as a small table with the total at the right; returns its height."""
        ls = d["linescore"]
        n = max(len(ls["away"]), len(ls["home"]))
        sport = g["sport"] if g else ""
        labels = period_labels(sport, g["league"] if g else "", n)
        extra = []  # baseball is the classic line score: an inning per column, then runs, hits and errors
        if sport == "baseball":
            find = lambda names: next(((a_, h_) for k_, lb, a_, h_ in d.get("all_stats", [])
                                       if re.sub(r"[^a-z0-9]", "", k_.lower()) in names), None)
            for j, (lab, names) in enumerate((("H", ("bathits", "bath")), ("E", ("flderrors", "flde")))):
                he = d.get("hits_errors") or [(None, None), (None, None)]
                v = tuple(str(he[t][j]) if he[t][j] is not None else None for t in (0, 1))
                if None in v:
                    v = find(names) or ("-", "-")
                extra.append((lab, v))
        cols = labels + ["R" if sport == "baseball" else "TOT"] + [lab for lab, _ in extra]
        lab_w = 38
        cw = min((w - lab_w) / len(cols), 34)
        rows = [(d["away_abbr"], ls["away"], d["away_score"], [v[0] for _, v in extra]),
                (d["home_abbr"], ls["home"], d["home_score"], [v[1] for _, v in extra])]
        h0 = 0
        for i, lab in enumerate(cols):
            xr = x + lab_w + cw * (i + 1) - 2
            _, h = ctext(xr, y, lab, FONTS["small"], DIM if i < n else FG, anchor="ne")
            h0 = max(h0, h)
        yy = y + h0 + 1
        canvas.create_line(x, yy, x + w, yy, fill="#33333d")
        if extra:  # a divider between the innings and the R H E columns
            sx_ = x + lab_w + cw * n + 1
            canvas.create_line(sx_, y, sx_, yy + 2 * (h0 + 2), fill="#33333d")
        for abbr, vals, total, ex in rows:
            _, h = ctext(x, yy + 1, abbr, FONTS["smallb"], FG)
            cells = list(vals) + [""] * (n - len(vals)) + [total] + ex
            for i, v in enumerate(cells):
                ctext(x + lab_w + cw * (i + 1) - 2, yy + 1, v, FONTS["smallb"] if i >= n else FONTS["small"], FG if i >= n or v not in ("0", "") else DIM, anchor="ne")
            yy += h + 1
        return yy - y

    def draw_details(x, y, w, bgc, d, win_shown=False, g=None):  # win_shown: no win probability here (shown on the card, or game over)
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
        if d.get("linescore") and d.get("state") != "pre":
            y += draw_linescore(x, y, w, d, g) + 4
        if d.get("state") == "post" and d.get("home_win_start") is not None:  # the final is 100-0: show where the game began
            hw = round(d["home_win_start"] * 100)
            ca, cb = d.get("colors", ("#60a5fa", "#f59e0b"))
            y += graphics(x, y, {"kind": "versus", "label": "Pregame win probability", "a_name": d["away_abbr"], "a": 100 - hw,
                                 "b_name": d["home_abbr"], "b": hw, "a_color": ca, "b_color": cb}, bgc, w)
        elif d.get("home_win") is not None and not win_shown:  # a card already showing it keeps it where it was
            hw = round(d["home_win"] * 100)
            ca, cb = d.get("colors", ("#60a5fa", "#f59e0b"))
            y += graphics(x, y, {"kind": "versus", "label": "Win probability", "a_name": d["away_abbr"], "a": 100 - hw,
                                 "b_name": d["home_abbr"], "b": hw, "a_color": ca, "b_color": cb}, bgc, w)
        recent = d["plays"][:8] if d.get("state") != "post" else []  # a finished game has its box score instead
        for title, items in (("Scoring", d["scoring"]), ("Recent plays", recent)):
            if items:
                _, h = ctext(x, y + 4, title, FONTS["smallb"], DIM)
                y += 4 + h
                for when, text in items:
                    _, h = ctext(x, y, (f"{when} · " if when else "") + text, FONTS["small"], FG, width=w)
                    y += h
        if d.get("box"):
            y += draw_box(x, y, w, bgc, d, g)
        show_stats = bool(d["stats"]) and not (g and g.get("sport") == "baseball")  # baseball's team stats just repeat the box score
        if show_stats:
            _, h = ctext(x, y + 4, "Team stats", FONTS["smallb"], DIM)
            y += 4 + h
            mid = x + w / 2  # away value at the left edge, home value at the right edge, label centred between
            ctext(x, y, d["away_abbr"], FONTS["smallb"], DIM, anchor="nw")
            _, h = ctext(x + w, y, d["home_abbr"], FONTS["smallb"], DIM, anchor="ne")
            y += h
            for n_, (label, a_, h_) in enumerate(d["stats"]):
                first, _ = ctext(x, y, a_, FONTS["small"], FG, anchor="nw")
                room_ = w - 2 * max(text_width(FONTS["small"], a_), text_width(FONTS["small"], h_)) - 16  # spelled out while it fits
                while len(label) > 4 and text_width(FONTS["small"], label) > room_:
                    label = label.rstrip("\u2026")[:-1] + "\u2026"
                ctext(mid, y, label, FONTS["small"], DIM, anchor="n")
                _, h = ctext(x + w, y, h_, FONTS["small"], FG, anchor="ne")
                if n_ % 2 == 0:
                    stripe(x, y, w, h, bgc, first)
                y += h
        if not (d["plays"] or d["scoring"] or show_stats or d.get("box") or d.get("linescore") or d.get("home_win_start") is not None
                or d.get("home_win") is not None and not win_shown):
            _, h = ctext(x, y, "No extra details from ESPN for this game", FONTS["small"], DIM)
            y += h
        return y - y0

    def stripe(x, y, w, h, bgc, below):
        """A darker rounded band behind a table row (every other row), under the canvas item `below`."""
        i = canvas.create_polygon(rr_points(x - 4, y, x + w + 4, y + h, 7), smooth=True, fill=blend(bgc, "#000000", 0.3), outline="")
        canvas.tag_lower(i, below)

    def draw_box(x, y, w, bgc, d, g):
        """Box score of one team, with a pill per team to switch between them. Returns its height."""
        y0 = y
        gk = gkey(g) if g else None
        side = session["box_side"].get(gk, 0) if gk else 0
        _, h = ctext(x, y + 6, "Box score", FONTS["smallb"], DIM)
        px = x + w
        for i in (1, 0):  # team pills at the right of the heading: away, then home
            label = (d["away_abbr"], d["home_abbr"])[i] or ("Away", "Home")[i]
            pw = text_width(FONTS["smallb"], label) + 16
            px -= pw
            on = i == side
            tag = new_hit(("boxside", gk, i)) if gk else ()
            fill = PANEL if on else BG
            canvas.create_polygon(rr_points(px, y + 4, px + pw, y + 22, 7), smooth=True, fill=fill, outline=HOVER if on else PANEL,
                                  tags=tag)
            canvas.create_text(px + pw / 2, y + 13, text=label, font=FONTS["smallb"], fill=FG if on else DIM, tags=tag)
            px -= 4
        y += max(6 + h, 24) + 2
        for cat in d["box"][side]:
            cols = cat["cols"]
            vals = [r_[1] for r_ in cat["rows"]] + ([cat["totals"]] if cat["totals"] else [])
            valw = [max([text_width(FONTS["small"], v[j]) for v in vals if j < len(v)] or [0]) for j in range(len(cols))]
            disp = [FULL_COLS.get(c, c) for c in cols]
            widths = lambda ds: [max(text_width(FONTS["smallb"], d_), valw[j]) + 8 for j, d_ in enumerate(ds)]
            while w - sum(widths(disp)) < 90 and any(disp[j] != cols[j] for j in range(len(cols))):  # keep room for the player names
                j = max((j for j in range(len(cols)) if disp[j] != cols[j]),
                        key=lambda j: text_width(FONTS["smallb"], disp[j]) - text_width(FONTS["smallb"], cols[j]))
                disp[j] = cols[j]
            cw = widths(disp)
            name_w = w - sum(cw)
            ctext(x, y + 2, cat["title"], FONTS["smallb"], FG)
            xr = x + w
            for c, wd in zip(reversed(disp), reversed(cw)):
                ctext(xr, y + 2, c, FONTS["smallb"], DIM, anchor="ne")
                xr -= wd
            y += 18
            rows = [(nm, v, FONTS["small"], FG) for nm, v in cat["rows"]] + (
                [("Team", cat["totals"], FONTS["smallb"], DIM)] if cat["totals"] and any(cat["totals"]) else [])
            for n_, (nm, v, font, col) in enumerate(rows):
                while len(nm) > 3 and text_width(font, nm) > name_w - 4:  # long names lose letters, not columns
                    nm = nm.rstrip("\u2026")[:-1] + "\u2026"
                first, _ = ctext(x, y, nm, font, col)
                if n_ % 2 == 0:
                    stripe(x, y, w, 15, bgc, first)
                xr = x + w
                for val, wd in zip(reversed(v), reversed(cw)):
                    ctext(xr, y, val, font, col, anchor="ne")
                    xr -= wd
                y += 15
            y += 4
        return y - y0

    def fetch_details(g):
        try:
            session["details"][gkey(g)] = game_detail_data(fetch_summary_cached(g["sport"], g["league"], g["id"], max_age=5), sport=g["sport"], league=g["league"])
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
        """Digits one wheel shows going from a to b: counting up to a higher digit, down to a lower one;
        9 -> 0 is one step up (a carry, like an odometer) and 0 -> 9 one step down."""
        if a == b:
            return [b]
        if (a, b) in (("9", "0"), ("0", "9")):
            return [a, b]
        if a.isdigit() and b.isdigit():
            step = 1 if int(b) > int(a) else -1
            return [str(d) for d in range(int(a), int(b) + step, step)]
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
    logo_imgs, logo_pending, logo_done, logo_failed = {}, set(), set(), set()

    def logos_ready():
        logo_pending.clear()
        logo_done.clear()
        session["sig"] = None
        draw_all()
        session["sig"] = compute_sig()
        fit()

    def logo_img(url, size):
        """The logo as a Tk image, its antialiased edges already blended into the card's colour (Tk may draw
        partial transparency as all-or-nothing, which leaves edges jagged)."""
        bg = session.get("card_bg") or BG
        key, dl = (url, size, bg), (url, size)
        if dl in logo_failed:
            return None
        if key in logo_imgs:
            return logo_imgs[key]
        path = logo_path(url, size)
        if os.path.exists(path):
            try:
                with open(path, "rb") as f_:
                    dec = _png_rgba(f_.read())
                if dec:
                    w_, h_, px_ = dec
                    br, bgg, bb = _rgb(bg)
                    out = bytearray()
                    for i_ in range(0, len(px_), 4):
                        a_ = px_[i_ + 3]
                        if a_ == 0:
                            out += b"\x00\x00\x00\x00"
                        else:  # opaque, pre-blended with the background
                            out += bytes((br + (px_[i_] - br) * a_ // 255, bgg + (px_[i_ + 1] - bgg) * a_ // 255,
                                          bb + (px_[i_ + 2] - bb) * a_ // 255, 255))
                    import base64
                    logo_imgs[key] = tk.PhotoImage(data=base64.b64encode(_png_bytes(w_, h_, out)))
                else:
                    logo_imgs[key] = tk.PhotoImage(file=path)
            except (tk.TclError, OSError):
                logo_imgs[key] = None
            return logo_imgs[key]
        if dl not in logo_pending and not session["anims"]:
            logo_pending.add(dl)

            def work():
                if not logo_file(url, size):
                    logo_failed.add(dl)  # unavailable: don't keep retrying
                logo_done.add(dl)
                if logo_pending <= logo_done:
                    root.after(0, logos_ready)  # the last outstanding logo arrived
            threading.Thread(target=work, daemon=True).start()
        return None

    pill_imgs = {}

    def crisp_rr(x1, y1, x2, y2, r, color, tags=()):
        """A rounded pill as an anti-aliased image, its edge pixels blended into the card's colour (Tk draws
        partial transparency all-or-nothing, and its own rounded shapes have jagged corners)."""
        x1, y1, x2, y2 = (int(round(v_)) for v_ in (x1, y1, x2, y2))
        w, h, r = x2 - x1, y2 - y1, 4
        bg = session.get("card_bg") or BG
        key = (w, h, r, color, bg)
        img = pill_imgs.get(key)
        if img is None:
            import base64
            fr, fg_, fb = _rgb(color)
            br, bgg, bb = _rgb(bg)
            ss, out = 4, bytearray()
            for py in range(h):
                for px in range(w):
                    hit = 0
                    for sy in range(ss):
                        for sx in range(ss):
                            x, y = px + (sx + 0.5) / ss, py + (sy + 0.5) / ss
                            dx, dy = max(r - x, x - (w - r), 0), max(r - y, y - (h - r), 0)
                            hit += dx * dx + dy * dy <= r * r
                    a = hit / (ss * ss)
                    out += bytes((round(br + (fr - br) * a), round(bgg + (fg_ - bgg) * a), round(bb + (fb - bb) * a), 255))
            img = pill_imgs[key] = tk.PhotoImage(data=base64.b64encode(_png_bytes(w, h, out)))
        return [canvas.create_image(x1, y1, image=img, anchor="nw", tags=tags)]

    def tv_badges(x, y, text, tags=(), center=False, limit=3):
        """Channel names as small badges in each network's colors (ESPN gives names, not logos); returns the width used."""
        names = text.split(" \u00b7 ")[:limit]
        widths = [text_width(FONTS["small"], n) + 10 for n in names]
        total = sum(widths) + 4 * (len(names) - 1)
        px = x - total / 2 if center else x
        for n, pw in zip(names, widths):
            bg_, fg_ = NETWORK_STYLES.get(n.lower(), (PANEL, FG))
            crisp_rr(px, y, px + pw, y + 14, 3, bg_, tags)
            canvas.create_text(round(px + pw / 2), round(y + 7), text=n, font=FONTS["small"], fill=fg_, tags=tags)
            px += pw + 4
        return total

    def draw_score(xr, y, text, color, bgc, key, center=False):
        """One side's score, right-aligned at xr; rolls from the last value drawn for `key`. Returns the left edge.
        `center`: the caller centres the score (xr is its centre plus half its width), so a crossfade text centres too."""
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
                root.after(0, run_in, session.get("view"), roll_tick)
        if not roll:
            i, _h = ctext(xr, y, text, FONTS["score"], color, anchor="ne")
            xf, lay = session["xfade"], session["cur_layer"]
            if xf and xf[0] == key and lay:  # a test score: the real one fades back in as the animation fades out
                real = xf[1]
                j, _h = (ctext(xr - score_width(text) / 2, y, real, FONTS["score"], color, anchor="n") if center
                         else ctext(xr, y, real, FONTS["score"], color, anchor="ne"))
                lay["xfade"] = (i, j, color)
                xfade_apply(lay, _time.perf_counter() - lay["ce"]["t0"])
            return canvas.bbox(i)[0]
        hgt = font_obj(FONTS["score"]).metrics("linespace")
        x = xr
        for seq in reversed(roll["seqs"]):  # one wheel per digit, right to left
            cw = max(text_width(FONTS["score"], ch) for ch in seq)
            cx, cy = x - cw / 2, y + hgt / 2
            anchor = canvas.create_rectangle(cx, cy, cx, cy, outline="", state="hidden")  # moves with the card
            items = [canvas.create_text(cx, cy, text="", font=FONTS["score"], fill=color) for _ in range(2)]
            down = len(seq) > 1 and seq[0].isdigit() and seq[1].isdigit() and int(seq[1]) == (int(seq[0]) - 1) % 10
            session["roll_cells"].append({"roll": roll, "seq": seq, "anchor": anchor, "items": items, "dir": -1 if down else 1,
                                          "color": color, "bg": bgc, "h": hgt})
            x -= cw
        roll_frame()
        return x

    def roll_frame():
        """Place every rolling digit for the current time: the old digit rolls up and away, the next rolls in."""
        now = _time.perf_counter()
        if "px_per_pt" not in session:
            session["px_per_pt"] = root.winfo_fpixels("1p")
        px0 = FONTS["score"][1] * session["px_per_pt"]
        for c in session["roll_cells"]:
            p = min((now - c["roll"]["t0"]) / c["roll"]["dur"], 1.0)
            v = ease_io(p) * (len(c["seq"]) - 1)  # ease in and out: spins up, then settles onto the new digit
            k = min(int(v), len(c["seq"]) - 1)
            frac = v - k
            ax, ay = canvas.coords(c["anchor"])[:2]
            travel = c["h"] * 0.42  # how far a digit rolls before it is out of sight on the drum
            for item, idx, off in ((c["items"][0], k, -frac), (c["items"][1], k + 1, 1 - frac)):
                if idx >= len(c["seq"]) or abs(off) >= 1:
                    canvas.itemconfigure(item, text="")
                    continue
                d = abs(off)
                canvas.coords(item, ax, ay + off * travel * c["dir"])  # up when counting up, down when counting down
                canvas.itemconfigure(item, text=c["seq"][idx], fill=blend(c["color"], c["bg"], d),
                                     font=(FONTS["score"][0], -max(6, round(px0 * (1 - 0.35 * d))), "bold"))

    TEST_POINTS = {"TOUCHDOWN!": 6, "FIELD GOAL": 3, "GOAL!": 1, "HOME RUN!": 1, "INSIDE THE PARK HOME RUN!": 1, "GRAND SLAM!": 4, "THREE-POINTER": 3, "TWO-POINTER": 2, "SLAM DUNK!": 2, "SAFETY": 2,
                   "RUN SCORES": 1, "PICK SIX!": 6, "EXTRA POINT": 1, "2-PT CONVERSION": 2, "BLOCKED PUNT TOUCHDOWN!": 6, "BLOCKED FIELD GOAL TOUCHDOWN!": 6}

    def test_score(r, k, side, head, pts=None):
        """A scoring test adds its points to one side (the digits roll to it) until the animation ends."""
        try:
            a_, b_ = float(r["score"][0]), float(r["score"][1])
        except (ValueError, TypeError, IndexError):
            return
        mine, other = (a_, b_) if side == 0 else (b_, a_)
        if head == "TIES IT UP":
            new = max(mine, other)
        else:
            runs = re.match(r"(\d+)-RUN ", head)  # N-RUN SINGLE / DOUBLE / TRIPLE score N
            new = mine + (pts if pts is not None else int(runs.group(1)) if runs else TEST_POINTS.get(head) or max(1, int(other - mine) + 1))  # TAKES THE LEAD: one more than it trails by
        session["test_scores"][k] = {"side": side, "text": f"{new:g}", "ce": session["celebs"].get(k)}

    tv = {"view": None}  # the Settings dummy card's (canvas, session), while Settings is open

    def redraw():
        if session.get("view"):
            draw_test()
        else:
            draw_all()

    def draw_test():
        """Draw the dummy card alone on its own canvas (called with that canvas and session swapped in)."""
        canvas.delete("all")
        for key in ("hits", "roll_cells", "clock_items", "pulse_items", "layers", "gcount", "hcards", "hseen"):
            session[key].clear()
        session["actx"] = None
        y = draw_card(session["dummy"], 0, 2, int(canvas.cget("width")), False)
        canvas.configure(height=int(y + 4))

    def field_scored(ce, t):
        """Runners home so far in a base-running event at t seconds (the count the diamond shows)."""
        fhead, focc, fn, fafter = ce["field"]
        u = t - 0.5
        return sum(1 for d_, p_ in field_runners(fhead, focc, fn, fafter) if p_[-1] == 0 and (u - d_) / FIELD_LEG >= len(p_) - 1)

    def run_view(r):
        """The card as a baseball run's animation shows it: the old score until the runners start, +1 as each reaches home."""
        k = card_key(r)
        hold = session["run_hold"].get(k)
        if not hold:
            return r
        ce = session["celebs"].get(k)
        now = _time.perf_counter()
        live = ce is not None and now - ce["t0"] < ce["secs"]
        side = hold["side"]
        if not live and not session["celeb_next"].get(k):  # over: the real score
            del session["run_hold"][k]
            session["roll_last"][(k, side)] = str(r["score"][side])
            session["rolls"].pop((k, side), None)
            return r
        if live and ce.get("field"):
            hold["started"] = True
            add = min(hold["n"], field_scored(ce, now - ce["t0"]))
        else:
            add = hold["n"] if hold["started"] else 0
        sc = list(r["score"])
        sc[side] = f"{hold['base'] + add:g}"
        return dict(r, score=sc, _real=r)

    def test_view(r):
        """The card as a running scoring test shows it (its points added); the real card once the test is over."""
        k = card_key(r)
        ov = session["test_scores"].get(k)
        if not ov:
            return r
        ce = session["celebs"].get(k)
        queued = bool(session["celeb_next"].get(k))  # the test's follow-ups (runners, takes the lead...) keep its score showing
        if not queued and (ce is None or _time.perf_counter() - ce["t0"] >= ce["secs"]):  # over: back to the real score, no roll
            del session["test_scores"][k]
            session["roll_last"][(k, ov["side"])] = str(r["score"][ov["side"]])
            session["rolls"].pop((k, ov["side"]), None)
            return r
        sc = list(r["score"])
        real = str(sc[ov["side"]])
        sc[ov["side"]] = ov["text"]
        last_ = ce is not None and not queued and not ce.get("chained_out")  # only the last event fades the test score back to the real one
        return dict(r, score=sc, _real=r, **({"_xfade": ((k, ov["side"]), real)} if last_ else {}))

    def xfade_apply(lay, t):
        """Test score -> real score while the banner fades out: the test score fades away, then the real one fades in."""
        fake, real, col = lay["xfade"]
        ce = lay["ce"]
        o = max(0.0, min(1.0, (t - (ce["secs"] - out_secs(ce))) / out_secs(ce)))
        if o < 0.5:
            canvas.itemconfigure(fake, state="normal", fill=blend(lay["bgc"], col, 1 - 2 * o))
            canvas.itemconfigure(real, state="hidden")
        else:
            canvas.itemconfigure(fake, state="hidden")
            canvas.itemconfigure(real, state="normal", fill=blend(lay["bgc"], col, 2 * o - 1))

    def roll_tick():
        now = _time.perf_counter()  # (frame budget FRAME_MS: every animation loop aims at 120 frames a second)
        roll_frame()
        done = [k for k, r_ in session["rolls"].items() if now - r_["t0"] >= r_["dur"]]
        for k in done:
            del session["rolls"][k]
        if done:
            redraw()  # finished wheels go back to plain text
        if session["rolls"]:
            root.after(frame_delay(now), run_in, session.get("view"), roll_tick)
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
                    session["stats"][k] = game_detail_data(fetch_summary_cached(g["sport"], g["league"], g["id"], max_age=600), sport=g["sport"], league=g["league"])
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
        return text_width(FONTS["score"], text)

    def draw_scoreboard(r, cx0, cw_, y, bgc, tags, gl, tos, info, lp=None, ce=None, ct=0):
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
        my = top + 2
        n0 = item_mark()  # everything drawn from here on belongs to the middle section
        live = r["state"] == "in"
        if live:  # the LIVE flag with the channel(s) beside it, centred together
            names_ = r["tv"].split(" \u00b7 ")[:2] if r.get("tv") else []
            tv_w = sum(text_width(FONTS["small"], n) + 10 for n in names_) + 4 * max(len(names_) - 1, 0)
            x0 = mx - (34 + (8 + tv_w if tv_w else 0)) / 2
            crisp_rr(x0, my, x0 + 34, my + 14, 3, LIVE_RED, tags)
            canvas.create_text(round(x0 + 17), round(my + 7), text="LIVE", fill="#ffffff", font=FONTS["sec"], tags=tags)
            if tv_w:
                tv_badges(x0 + 42, my, " \u00b7 ".join(names_), tags)
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
        if r["state"] == "pre" and r.get("tv"):  # upcoming: the channel(s) under the start time
            tv_badges(mx, my - 2, r["tv"], tags, center=True)
            my += 16
        if r["state"] == "post" and r.get("game"):  # a finished game: its team stats fill the middle
            d_ = ensure_stats(r["game"])
            if isinstance(d_, dict) and d_.get("all_stats"):
                flip = teams[0]["ha"] == "home" if teams[0].get("ha") else teams[0]["abbr"] == d_["home_abbr"]
                room = 46 + (30 if sc else 0) + 14 + (13 if any(t.get("record") for t in teams) else 0) - (my - top)  # as many stats as fill the teams' height
                for label, a_, h_ in pick_stats(r["game"]["sport"], d_.get("all_stats", []), max(4, min(6, -(-int(room) // 14)))):
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
        if lp and live:  # what just happened, in the free space at the bottom of the middle
            cap_ = 60 if bb else 84  # baseball's middle is already full
            text = lp["text"]
            football = (r.get("game") or {}).get("sport") == "football" or any(g_["kind"] == "football" for g_ in gl)
            if football and re.search(r"\bpenalty\b", text, re.I):
                # a flag on the play: PENALTY in a yellow box (like the LIVE badge), the rest of the play below it
                text = re.sub(r"^\s*penalty\b[\s,:-]*", "", text, flags=re.I) or text
                pw = text_width(FONTS["sec"], "PENALTY") + 12
                canvas.create_polygon(rr_points(mx - pw / 2, my + 4, mx + pw / 2, my + 18, 5), smooth=True, fill=FLAG_YELLOW,
                                      outline=FLAG_YELLOW, tags=tags)
                canvas.create_text(mx, my + 11, text="PENALTY", fill="#1e1e24", font=FONTS["sec"], tags=tags)
                my += 18
            text = text if len(text) <= cap_ else text[:cap_ - 1].rstrip() + "\u2026"
            _, h = ctext(mx, my + 4, text, FONTS["small"], DIM, width=mw, anchor="n", tags=tags, justify="center")
            my += 4 + h
        banner = bool(ce and ce.get("banner"))
        if banner:  # the banner takes the middle for a few seconds (crossfading), without changing the card's height
            fade_items(items_since(n0), bgc, info_alpha(ce, ct))  # never deleted: the frames fade it back in
        mh = my - top  # the middle section sets the height; the teams scale up to match it
        counts = [tos.get(t["ha"]) for t in teams] if tos and r["state"] == "in" else [None, None]
        nat = 46 + (30 if sc else 0) + 14 + (12 if counts[0] is not None else 0) + (13 if any(t.get("record") for t in teams) else 0)
        if mh < nat - 2:  # a short middle is centred against the teams
            for i_ in items_since(n0):
                canvas.move(i_, 0, (nat - mh) / 2)
            my += (nat - mh) / 2
            mh = nat
        if banner:
            draw_banner(mx, top, top + max(nat, mh), mw, ce, ct, bgc)  # natural height of a team column
        lg = max(44, min(64, 44 + int(max(mh - nat, 0) // 4) * 4))  # a bigger logo, in steps so few sizes are cached
        gap = max(0, min(10, (mh - nat - (lg - 44)) / 3))  # what is left over is spread between the rows
        colb = top
        for i, t in enumerate(teams):
            cx = ix + COL / 2 if i == 0 else ix + ww - COL / 2
            yy = top
            img = logo_img(t["logo"], lg) if t.get("logo") else None
            if ce and i == ce["side"]:
                session["ring_center"] = (cx, yy + lg / 2, lg / 2)
            if img:
                canvas.create_image(cx, yy, image=img, anchor="n", tags=tags)
            yy += lg + 2 + gap
            if sc:
                draw_score(cx + score_width(sc[i]) / 2, yy - 3, sc[i], c[i], bgc, (rk, i), center=True)
                yy += 30 + gap
            _, h = ctext(cx, yy, t["abbr"], FONTS["smallb"], FG if r["state"] != "pre" else DIM, anchor="n", tags=tags)
            yy += h
            if t.get("record"):  # the team's record under its abbreviation
                _, h = ctext(cx, yy, t["record"], FONTS["small"], DIM, anchor="n", tags=tags)
                yy += h
            if counts[i] is not None:  # remaining timeouts / challenges: filled dots, unlabelled
                total = max(tos.get("total", 3), counts[i])
                x0 = cx - 5 * (total - 1)
                for k in range(total):
                    canvas.create_oval(x0 + 10 * k - 3, yy + gap + 4, x0 + 10 * k + 3, yy + gap + 10, fill=FG if k < counts[i] else bgc,
                                       outline=FG if k < counts[i] else DIM, tags=tags)
                yy += 12 + gap
            colb = max(colb, yy)
        yy = max(colb, my) + 2
        sr = r.get("series") if r["state"] == "post" else None
        if sr:  # a finished playoff game: which game of the series it was, and where the series stands
            canvas.create_line(ix, yy + 2, ix + ww, yy + 2, fill=blend(bgc, FG, 0.12))
            yy += 6
            if sr["head"]:
                _, h = ctext(mx, yy, sr["head"], FONTS["small"], DIM, width=ww, anchor="n", tags=tags, justify="center")
                yy += h
            if sr["text"]:
                won = "win" in sr["text"].lower()
                _, h = ctext(mx, yy, sr["text"], FONTS["detb"], COLORS["in"] if won else FG, width=ww, anchor="n", tags=tags, justify="center")
                yy += h
            yy += 2
        if gl:
            yy += graphics(ix, yy, gl, bgc, ww)
        return yy, "\n".join(lines)

    # ---- scoring celebrations: a ring pulse or card flash, plus a banner with the play ----------------
    BANNER_SECS, GRAND_SECS, RING_SECS, FLASH_SECS = 6.5, 8.5, 1.3, 1.2
    FOLLOW_SECS = 5.0  # how long a follow-up (takes the lead, ties it up, momentum swing) stays up after the play that caused it
    GOLD = "#fbbf24"

    def card_key(r):
        return r.get("_key") or (gkey(r["game"]) if r.get("game") else r["name"])

    def celeb_of(r):
        """(celebration, seconds since it began) for a card whose team just scored, else (None, 0)."""
        e = session["celebs"].get(card_key(r))
        t = _time.perf_counter() - e["t0"] if e else 0
        return (e, t) if e and t < e["secs"] else (None, 0)

    def headline_for(sport, n, prev, cur, side, text):
        """(headline, show a banner) for a score of n points; baseball knows its grand slams."""
        low = text.lower()
        if sport == "baseball":
            if "grand slam" in low or (n == 4 and prev[2]):
                return "GRAND SLAM!", True
            if "homer" in low or "home run" in low:
                return ("INSIDE THE PARK HOME RUN!" if re.search(r"inside[- ]the[- ]park", low) else "HOME RUN!"), True
            hit = next((h_ for h_, w_ in (("TRIPLE", "tripled"), ("DOUBLE", "doubled"), ("SINGLE", "singled")) if re.search(rf"\b{w_}\b(?! off)", low)), None)
            if hit:  # runs scored on a hit: 1-RUN SINGLE, 2-RUN DOUBLE
                return f"{n}-RUN {hit}", True
            return ("RUN SCORES" if n == 1 else f"{n} RUNS SCORE"), True
        if sport == "football":
            if n == 2 and "safety" not in low and ("two-point" in low or "conversion" in low):
                return "2-PT CONVERSION", True
            if n >= 6 and "intercept" in low:  # the defence takes it back for a touchdown
                return "PICK SIX!", True
            if n >= 6 and "blocked" in low and ("punt" in low or "field goal" in low):
                return ("BLOCKED PUNT TOUCHDOWN!" if "punt" in low else "BLOCKED FIELD GOAL TOUCHDOWN!"), True
            return {6: "TOUCHDOWN!", 7: "TOUCHDOWN!", 8: "TOUCHDOWN!", 3: "FIELD GOAL", 2: "SAFETY", 1: "EXTRA POINT"}.get(n, "SCORE"), True
        if sport in ("hockey", "soccer"):
            return "GOAL!", True
        if sport == "basketball":  # shots only (no free throws); detect_scores plays them only as the lead-in to a Then animation
            head = "SLAM DUNK!" if "dunk" in low else "THREE-POINTER" if n == 3 else "TWO-POINTER" if n == 2 else ""
            return head, bool(head)
        return "SCORE", True

    def classify_play(sport, text):
        """(headline, color, seconds) for a big play named in ESPN's last-play text, or None."""
        low = text.lower()
        if sport == "football":
            if "blocked" in low and "field goal" in low:
                return "BLOCKED FG!", "#a78bfa", 4.0
            if "blocked" in low and "punt" in low:
                return "BLOCKED PUNT!", "#a78bfa", 4.0
            if "blocked" in low and ("extra point" in low or "kick" in low):
                return "BLOCKED PAT!", "#a78bfa", 3.5
            if "onside" in low:
                k_, rec = onside_teams(text)
                if k_ and rec and k_ == rec:
                    return "ONSIDE KICK RECOVERED!", "#fbbf24", 4.5
                return "ONSIDE KICK", "#9aa0a6", 2.5
            if "intercept" in low:
                return "INTERCEPTION", "#f87171", 3.5
            if "fumble" in low:
                return "FUMBLE", "#f87171", 3.5
            if re.search(r"\bpenalty\b", low):  # a flag on the play (a takeaway or block above outranks it)
                return "PENALTY", "#facc15", 3.0
            if "sacked" in low or " sack" in low:
                return "SACK", "#fb923c", 3.0
            if "turnover on downs" in low:
                return "TURNOVER ON DOWNS!", "#f87171", 3.5
            if " punts" in low:
                return "PUNT", "#9aa0a6", 2.5
            if "kicks off" in low or "kickoff" in low:
                return "KICKOFF", "#9aa0a6", 2.5
        elif sport == "baseball":
            if "triple play" in low:
                return "TRIPLE PLAY!", "#fbbf24", 4.0
            if "double play" in low:
                return "DOUBLE PLAY", "#34d399", 3.5
            if re.search(r"\btripled\b", low):
                return "TRIPLE", "#fbbf24", 3.5
            if re.search(r"\bdoubled\b(?! off)", low):  # "doubled off first" is an out
                return "DOUBLE", "#34d399", 3.0
            if re.search(r"\bsingled\b", low):
                return "SINGLE", "#38bdf8", 2.5
            if "strikes out" in low or "struck out" in low or "strikeout" in low:
                return "STRIKEOUT", "#60a5fa", 3.0
            if "caught stealing" in low:
                return "CAUGHT STEALING", "#fb923c", 3.0
            if "picked off" in low:
                return "PICKED OFF", "#fb923c", 3.0
            if any(w in low for w in (" flies out", " grounds out", " lines out", " pops out", " fouls out", "forceout", "force out")):
                return "OUT", "#9aa0a6", 2.0
        elif sport == "basketball":
            if " blocks " in low:
                return "BLOCK", "#a78bfa", 2.5
            if " steals " in low or "steal" in low and "stolen" not in low:
                return "STEAL", "#fb923c", 2.5
        elif sport == "hockey" and "penalty" in low:
            return "PENALTY", "#fb923c", 3.0
        return None

    def onside_teams(text):
        """(kicking team, recovering team) abbreviations from an onside kick's play text, either '' when not named.
        ESPN writes e.g. 'kicks onside 12 yards from DAL 35 to DAL 47. ... RECOVERED by DAL-J.Doe.'"""
        kick = re.search(r"\bfrom ([A-Z]{2,4}) \d+", text)
        rec = re.search(r"recovered by ([A-Z]{2,4})-", text, re.I)
        return (kick.group(1).upper() if kick else ""), (rec.group(1).upper() if rec else "")

    KICK_PLAYS = ("BLOCKED FG!", "BLOCKED PUNT!", "BLOCKED PAT!", "ONSIDE KICK RECOVERED!")

    def kick_side(r, head, text, kicker):
        """Index of the team that made a special-teams play: the kicking team for an onside recovery, the other team for
        a block. `kicker` is who had the ball before the play (the kicking team), used when the text names nobody."""
        tm = r.get("teams") or []
        if len(tm) != 2:
            return None
        idx = lambda ab: next((i for i, t in enumerate(tm) if ab and t.get("abbr", "").upper() == str(ab).upper()), None)
        if head == "ONSIDE KICK RECOVERED!":
            k_, _rec = onside_teams(text)
            return idx(k_) if idx(k_) is not None else idx(kicker)
        rec = re.search(r"recovered by ([A-Z]{2,4})-", text, re.I)  # a block recovered by the blocking team names it
        i = idx(kicker)
        if i is not None:
            return 1 - i
        return idx(rec.group(1)) if rec else None

    SOUND_TONES = {"score": [(660, 80), (880, 130)], "turnover": [(440, 100), (330, 170)],
                   "grand": [(523, 90), (659, 90), (784, 90), (1047, 260)], "final": [(392, 280)],
                   "swing": [(587, 90), (494, 130)], "fourth": [(330, 110), (330, 110), (392, 180)]}
    SOUNDBOARD = [("4th down", "fourth"), ("Final", "final"), ("Grand slam", "grand"), ("Momentum swing", "swing"),
                  ("Score", "score"), ("Turnover", "turnover")]  # (label, sound): what the Settings soundboard plays

    def play_sound(kind, force=False):
        """A short chime (Windows beeps; the system bell elsewhere), unless sounds are muted in Settings (the soundboard forces it)."""
        if not force and not ui_state.get("sound", True):
            return
        tones = SOUND_TONES.get(kind)
        if not tones:
            return

        def run():
            try:
                import winsound
                for f_, ms in tones:
                    winsound.Beep(f_, ms)
            except Exception:
                try:
                    root.after(0, root.bell)
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    def acting_side(r, head, ptid=""):
        """Index (0/1) of the team that did the thing: the defense for strikeouts, outs, interceptions, sacks...; the offense for
        runs, 4th downs, kicks; the team that takes over after a turnover on downs."""
        tm = r.get("teams") or []
        if len(tm) != 2:
            return None
        sport = (r.get("game") or {}).get("sport", "")
        gl = r.get("graphic") or []
        gl = [gl] if isinstance(gl, dict) else gl
        fb_ = next((g_ for g_ in gl if g_["kind"] == "football"), None)
        if sport == "football" and fb_:
            off = next((i for i, t in enumerate(tm) if t.get("abbr", "").upper() == str(fb_["off"]).upper()), None)
            if off is not None:
                return 1 - off if head in ("INTERCEPTION", "FUMBLE", "SACK", "BLOCKED FG!", "BLOCKED PUNT!", "BLOCKED PAT!") else off
        if sport == "baseball":
            m = re.match(r"\s*(Top|Bot)", str(r.get("status") or r.get("detail") or ""))
            bat = next((i for i, t in enumerate(tm) if m and t.get("ha") == ("away" if m.group(1) == "Top" else "home")), None)
            if bat is not None:
                fielding = ("STRIKEOUT", "OUT", "DOUBLE PLAY", "TRIPLE PLAY!", "CAUGHT STEALING", "PICKED OFF")
                return 1 - bat if head in fielding else bat
        return next((i for i, t in enumerate(tm) if ptid and t.get("id") == ptid), None)  # None: no team to attach it to

    def make_event(r, k, side, head, color, secs, mode, detail="", banner=True, grand=False, run=False, tag="", sound=None,
                   chained_in=False, chained_out=False, field=None, out=None):
        """Start a celebration (animation + optional sound) on card k. `field`: a baseball run's (head, men on, runs), drawn as
        a little diamond instead of text; `out`: how long its fade-out takes."""
        now = _time.perf_counter()
        secs = max(secs, ripple_end(secs) + 0.4)  # long enough for the ripples to clear before the fade-out
        teams = r.get("teams") or []
        t = teams[side] if len(teams) == 2 and side in (0, 1) else {}
        session["celebs"][k] = {
            "t0": now, "side": side if t else None, "abbr": t.get("abbr", ""), "color": color or t.get("color") or "#e5e7eb",
            "head": head, "detail": detail if len(detail) <= 90 else detail[:89].rstrip() + "\u2026", "mode": mode, "banner": banner,
            "grand": grand, "run": run, "tag": tag, "secs": secs, "chained_in": chained_in, "chained_out": chained_out,
            "field": field, "out": out}
        session["celeb_dirty"] = True  # the next tick redraws once; frames after that only recolor
        if sound:
            play_sound(sound)
        if not session["celeb_on"]:
            session["celeb_on"] = True
            root.after(0, run_in, session.get("view"), celeb_tick)

    def game_time(sport, status):
        """What ESPN's status text says about the clock: {"period", "secs"} (OT counts as a late period), {"inning"}, or {"minute"}."""
        t = {}
        s_ = status or ""
        m = re.search(r"\b[QPH](\d)\s+(\d+):(\d\d)", s_) or re.search(r"(\d+):(\d\d)\s*-\s*(\d)(?:st|nd|rd|th)", s_)
        if m:
            g_ = m.groups()
            per, mm, ss = (int(g_[0]), int(g_[1]), int(g_[2])) if re.match(r"[QPH]", m.group(0)) else (int(g_[2]), int(g_[0]), int(g_[1]))
            t.update(period=per, secs=mm * 60 + ss)
        if re.search(r"\bOT\b|\dOT", s_):
            mm_ = re.search(r"(\d+):(\d\d)", s_)
            t.update(period=99, secs=int(mm_.group(1)) * 60 + int(mm_.group(2)) if mm_ else 300)
        m = re.search(r"\b(Top|Bot|Mid|End)\s+(\d+)", s_)
        if m:
            t["inning"] = int(m.group(2))
        m = re.search(r"(\d+)'(?:\+(\d+))?", s_)
        if m and sport == "soccer":
            t["minute"] = int(m.group(1)) + int(m.group(2) or 0)
        return t

    def out_of_reach(sport, league, t, lead):
        """Is a lead of `lead` safe at this point of the game? Late in the game and more than the trailing team can usually
        make up in the time left."""
        if lead <= 0:
            return False
        secs, per = t.get("secs"), t.get("period")
        if sport == "basketball":
            reg = 2 if "college" in (league or "") else 4
            return per is not None and secs is not None and per >= reg and secs <= 240 and lead > 2.5 * secs / 60 + 3
        if sport == "football":
            return per is not None and secs is not None and per >= 4 and any(secs <= s_ and lead >= need for s_, need in ((480, 17), (300, 14), (180, 9)))
        if sport == "hockey":
            return per is not None and secs is not None and per >= 3 and ((secs <= 600 and lead >= 3) or (secs <= 150 and lead >= 2))
        if sport == "soccer":
            m = t.get("minute")
            return m is not None and ((m >= 70 and lead >= 3) or (m >= 80 and lead >= 2))
        if sport == "baseball":
            inn = t.get("inning")
            return inn is not None and inn >= 7 and lead >= {7: 6, 8: 5}.get(inn, 4)
        return False

    def is_dagger(r, side, prev, cur):
        """A score that has just put the game out of reach: the lead was still catchable before it and is not now (and, when
        ESPN has win probability, the scoring team is now 95% or better)."""
        g_ = r.get("game") or {}
        sport, league = g_.get("sport", ""), g_.get("league", "")
        t = game_time(sport, r.get("status", ""))
        before, after = prev[side] - prev[1 - side], cur[side] - cur[1 - side]
        if not out_of_reach(sport, league, t, after) or out_of_reach(sport, league, t, before):
            return False
        wa = (r.get("win") or {}).get("a")
        return wa is None or (wa if side == 0 else 100 - wa) >= 95

    def recovery_side(r, text):
        """Index of the team that recovered a fumble ("... RECOVERED by DAL-J.Doe"), or None when the play names nobody."""
        m = re.search(r"recovered by ([A-Za-z]{2,4})\b", text, re.I)
        tm = [t_.get("abbr", "").upper() for t_ in r.get("teams") or []]
        return tm.index(m.group(1).upper()) if m and m.group(1).upper() in tm else None

    def chain_event(k, args, kw):
        """Play an event on card k once its current one ends, the card's own info staying hidden in between."""
        queue = session["celeb_next"].setdefault(k, [])
        if queue:
            queue[-1][1]["chained_out"] = True
        elif k in session["celebs"]:
            session["celebs"][k]["chained_out"] = True
        else:
            make_event(*args, **kw)
            return
        queue.append((args, dict(kw, chained_in=True)))

    def chain_field(k, r, side, head, mode, occ=None, n=0, after=None, base=None):
        """After a baseball run's banner has completely faded, a diamond of its own plays the runners round the bases."""
        runners = field_runners(head, occ, n, after)
        if not runners:
            return
        end = max(d_ + (len(p_) - 1) * FIELD_LEG for d_, p_ in runners)
        if base is not None and n and side is not None:  # the score holds its old value, then counts up as each runner reaches home
            session["run_hold"][k] = {"side": side, "base": base, "n": n, "started": False}
        chain_event(k, (r, k, side, "", None, end + 2.5, mode, ""), {"field": (head, occ, n, after), "out": 0.6})

    def detect_scores(groups):
        """Compare live games with the last refresh and start an animation for each card where something happened:
        a score, a big play, a swing in win probability, or the final whistle."""
        mode = ui_state.get("score_anim", "pulse")
        mode = "pulse" if mode == "flash" else mode  # the Flash option is gone: Ripple flashes the card too
        if ui_state.get("anim_scope", "all") == "mine":
            groups = groups[:2]  # My Teams and tracked games only
        now, seen, plays, wins, live_keys = _time.perf_counter(), {}, {}, {}, set()
        downs, possessions, bases = {}, {}, {}
        for r in (r for grp in groups for r in grp):
            if not r.get("score"):
                continue
            k = card_key(r)
            if r["state"] == "post":  # a game that was live a moment ago has just ended
                if k in session["was_live"] and mode != "off" and k not in session["celebs"]:
                    try:
                        a_, b_ = float(r["score"][0]), float(r["score"][1])
                    except ValueError:
                        a_ = b_ = 0
                    win = 0 if a_ >= b_ else 1
                    teams = r.get("teams") or []
                    nm = [t_["abbr"] for t_ in teams] if len(teams) == 2 else ["", ""]
                    make_event(r, k, win, "FINAL", None, BANNER_SECS, mode, f"{nm[0]} {r['score'][0]} \u2013 {nm[1]} {r['score'][1]}",
                               sound="final")
                session["was_live"].discard(k)
                continue
            if r["state"] != "in" or k in seen:
                continue
            try:
                cur = (float(r["score"][0]), float(r["score"][1]))
            except ValueError:
                continue
            session["was_live"].add(k)
            live_keys.add(k)
            gl = r.get("graphic") or []
            gl = [gl] if isinstance(gl, dict) else gl
            sport = (r.get("game") or {}).get("sport", "")
            loaded = any(g_["kind"] == "baseball" and all(g_["bases"]) for g_ in gl)
            prev = session["score_prev"].get(k)
            seen[k] = (cur[0], cur[1], loaded)
            ptext = next((g_["text"] for g_ in gl if g_["kind"] == "lastplay"), "")
            ptid = next((g_.get("team", "") for g_ in gl if g_["kind"] == "lastplay"), "")
            plays[k] = ptext
            bb_ = next((tuple(g_["bases"]) for g_ in gl if g_["kind"] == "baseball"), None)
            if bb_ is not None:
                bases[k] = bb_  # the men on base now: the next hit starts from them
            wv = (r.get("win") or {}).get("a")
            if wv is not None:
                wins[k] = wv
            fb_ = next((g_ for g_ in gl if g_["kind"] == "football"), None)
            m4 = re.match(r"\s*(\d)(?:st|nd|rd|th) & ", (r.get("info") or "").split("\n")[0]) if sport == "football" else None
            if fb_:
                possessions[k] = fb_["off"]
            if m4:
                downs[k] = int(m4.group(1))
            if prev is None or mode == "off":
                continue
            old_w = session["win_prev"].get(k)  # a big swing in win probability: shown after whatever caused it
            swing = None
            if wv is not None and old_w is not None and abs(wv - old_w) >= 25:
                w_ = r["win"]
                up_away = wv > old_w
                gain, pct = (w_["a_name"], wv) if up_away else (w_["b_name"], 100 - wv)
                swing = ((r, k, 0 if up_away else 1, "MOMENTUM SWING", None, FOLLOW_SECS, mode, f"{gain} win probability now {pct:g}%"),
                         {"sound": "swing"})
            if m4:
                if downs[k] == 4 and session["down_prev"].get(k, 4) != 4 and k not in session["celebs"]:
                    make_event(r, k, acting_side(r, "4TH DOWN"), "4TH DOWN", None, 3.5, mode, (r.get("info") or "").split("\n")[0].replace(" \u00b7 ", "  \u00b7  "),
                               sound="fourth")
                    if swing:
                        chain_event(k, *swing)
                    continue
            d = (cur[0] - prev[0], cur[1] - prev[1])
            if max(d) > 0:  # somebody scored
                side = 0 if d[0] >= d[1] else 1
                head, banner = headline_for(sport, int(d[side]), prev, cur, side, ptext)
                before, after = prev[0] - prev[1], cur[0] - cur[1]
                tag = ("TIES IT UP" if after == 0 else "TAKES THE LEAD" if before * after < 0 or (before == 0 and after != 0) else "")
                grand = head == "GRAND SLAM!"
                dag = (k, side) not in session["daggers"] and is_dagger(r, side, prev, cur)  # late and out of reach now, once per team per game
                if sport == "basketball" and banner and not (tag or dag or swing):
                    continue  # a basket only plays as the lead-in to a Then animation
                make_event(r, k, side, head if banner else tag, None, GRAND_SECS if grand else BANNER_SECS, mode, ptext,
                           banner=banner or bool(tag), grand=grand, run=sport == "baseball", sound="grand" if grand else "score")
                if sport == "baseball" and banner and is_field_play(head):
                    chain_field(k, r, side, head, mode, session["bases_prev"].get(k), int(d[side]), bases.get(k), prev[side])
                if banner and tag and head != tag:  # the lead changing hands follows the score that did it
                    tm_ = [t_.get("abbr", "") for t_ in r.get("teams") or []]
                    line = f"{tm_[0]} {cur[0]:g} \u2013 {tm_[1]} {cur[1]:g}" if len(tm_) == 2 else ""
                    chain_event(k, (r, k, side, tag, None, FOLLOW_SECS, mode, line), {})
                if dag:
                    session["daggers"].add((k, side))
                    chain_event(k, (r, k, side, "DAGGER!", None, FOLLOW_SECS, mode, ptext), {})
                if swing:
                    chain_event(k, *swing)
                continue
            old = session["play_prev"].get(k)  # nobody scored: a big play?
            big = classify_play(sport, ptext) if old is not None and ptext and ptext != old else None
            if (not big and m4 and fb_ and session["down_prev"].get(k) == 4 and downs.get(k) == 1 and session["poss_prev"].get(k)
                    and session["poss_prev"][k] != fb_["off"] and not re.search(r"punt|field goal|kick|intercept|fumble", ptext.lower())):
                big = ("TURNOVER ON DOWNS!", "#f87171", 3.5)  # 4th down, now 1st down for the other team, and no kick or takeaway
            if big and sport == "basketball" and not swing:
                big = None  # blocks and steals only play as the lead-in to a Then animation
            if big and k not in session["celebs"]:
                side = (kick_side(r, big[0], ptext, session["poss_prev"].get(k)) if big[0] in KICK_PLAYS
                        else acting_side(r, big[0], ptid))
                flag = big[0] == "PENALTY" and sport == "football"
                if flag:  # a flag is the penalized team's ("PENALTY on DAL-M.Parsons ..."), in flag yellow
                    pm = re.search(r"penalty on ([A-Za-z]{2,4})\b", ptext, re.I)
                    tm_ = [t_.get("abbr", "").upper() for t_ in r.get("teams") or []]
                    if pm and pm.group(1).upper() in tm_:
                        side = tm_.index(pm.group(1).upper())
                make_event(r, k, side, big[0], FLAG_YELLOW if flag else None, big[2], mode, ptext, run=big[0] in ("SINGLE", "DOUBLE", "TRIPLE"), sound="turnover" if big[0] in (
                    "INTERCEPTION", "FUMBLE", "SACK", "TURNOVER ON DOWNS!") + KICK_PLAYS else None)
                if sport == "baseball" and big[0] in ("SINGLE", "DOUBLE", "TRIPLE"):
                    chain_field(k, r, side, big[0], mode, session["bases_prev"].get(k), 0, bases.get(k))
                if big[0] == "FUMBLE":  # then who recovered it
                    rec = recovery_side(r, ptext)
                    if rec is not None:
                        chain_event(k, (r, k, rec, "FUMBLE RECOVERED", None, FOLLOW_SECS, mode, ptext), {})
                if swing:
                    chain_event(k, *swing)
                continue
            if swing:  # on its own, or after an animation still playing from the last refresh
                chain_event(k, *swing)
        session["score_prev"] = seen
        session["play_prev"] = plays
        session["win_prev"] = wins
        session["down_prev"] = downs
        session["poss_prev"] = possessions
        session["bases_prev"] = bases
        session["was_live"] &= live_keys | {card_key(r) for grp in groups for r in grp if r["state"] == "post"}

    def start_pulse():
        if not session["pulse_on"]:
            session["pulse_on"] = True
            root.after(FRAME_MS, run_in, session.get("view"), pulse_tick)

    def pulse_tick():
        """Breathe the outline of the clutch border / red-zone glow items (no redraw: only their colors change)."""
        items = session["pulse_items"]
        if not items:
            session["pulse_on"] = False
            return
        k = 0.5 + 0.5 * math.sin(_time.perf_counter() * 4)
        for i, col, bgc in list(items):
            try:
                canvas.itemconfigure(i, outline=blend(bgc, col, 0.3 + 0.65 * k))
            except tk.TclError:
                pass
        root.after(FRAME_MS, run_in, session.get("view"), pulse_tick)

    def clutch_of(r):
        """A close game in its closing minutes (or extra innings): the card gets a pulsing border."""
        if r["state"] != "in" or not r.get("score"):
            return False
        sport, txt = (r.get("game") or {}).get("sport", ""), str(r.get("status") or "")
        try:
            diff = abs(float(r["score"][0]) - float(r["score"][1]))
        except ValueError:
            return False
        if sport == "baseball":
            m = re.search(r"(?:Top|Bot|Mid|End)\s+(\d+)", txt)
            return bool(m) and int(m.group(1)) >= 9 and diff <= 1
        if sport in ("basketball", "football", "hockey"):
            close = diff <= {"basketball": 5, "football": 8, "hockey": 1}[sport]
            if re.search(r"\bOT\b|\dOT", txt):
                return close
            m = re.search(r"[QP](\d)\s+(\d+):(\d\d)", txt)
            return bool(m) and int(m.group(1)) >= (3 if sport == "hockey" else 4) and int(m.group(2)) * 60 + int(m.group(3)) <= 120 and close
        return False

    # (label, headline, kind, color): what the Settings "Test animations" window can fire
    TESTS = [("Touchdown", "TOUCHDOWN!", "score", None), ("Field goal", "FIELD GOAL", "score", None), ("Goal", "GOAL!", "score", None),
             ("Home run", "HOME RUN!", "run", None), ("Inside-the-park HR", "INSIDE THE PARK HOME RUN!", "run", None), ("Grand slam", "GRAND SLAM!", "grand", GOLD),
             ("Three-pointer", "THREE-POINTER", "score", None), ("Two-pointer", "TWO-POINTER", "score", None), ("Slam dunk", "SLAM DUNK!", "score", None), ("Interception", "INTERCEPTION", "turnover", "#f87171"), ("Pick six", "PICK SIX!", "score", None), ("Fumble", "FUMBLE", "turnover", "#f87171"),
             ("Sack", "SACK", "turnover", "#fb923c"), ("Strikeout", "STRIKEOUT", "play", "#60a5fa"),
             ("Double play", "DOUBLE PLAY", "play", "#34d399"), ("Out", "OUT", "play", "#9aa0a6"),
             ("Block", "BLOCK", "play", "#a78bfa"), ("Penalty", "PENALTY", "play", "#fb923c"),
             ("Kickoff", "KICKOFF", "play", "#9aa0a6"), ("4th down", "4TH DOWN", "fourth", None),
             ("Turnover on downs", "TURNOVER ON DOWNS!", "turnover", None),
             ("Safety", "SAFETY", "score", None), ("Blocked FG", "BLOCKED FG!", "turnover", "#a78bfa"),
             ("Blocked punt", "BLOCKED PUNT!", "turnover", "#a78bfa"), ("Onside recovery", "ONSIDE KICK RECOVERED!", "turnover", "#fbbf24"),
             ("Single", "SINGLE", "play", "#38bdf8"), ("Double", "DOUBLE", "play", "#34d399"), ("Triple", "TRIPLE", "play", "#fbbf24"),
             ("Run scores", "RUN SCORES", "run", None),
             ("Triple play", "TRIPLE PLAY!", "play", "#fbbf24"), ("Caught stealing", "CAUGHT STEALING", "play", "#fb923c"),
             ("Picked off", "PICKED OFF", "play", "#fb923c"), ("Steal", "STEAL", "play", "#fb923c"),
             ("Extra point", "EXTRA POINT", "score", None),
             ("2-pt conversion", "2-PT CONVERSION", "score", None), ("Blocked punt touchdown", "BLOCKED PUNT TOUCHDOWN!", "score", None), ("Blocked field goal touchdown", "BLOCKED FIELD GOAL TOUCHDOWN!", "score", None),
             ("Blocked PAT", "BLOCKED PAT!", "turnover", "#a78bfa"), ("Onside kick", "ONSIDE KICK", "play", "#9aa0a6"),
             ("Punt", "PUNT", "play", "#9aa0a6"),
             ("Final", "FINAL", "final", None), ("Clutch border", "", "clutch", None),
             ("Red zone", "", "redzone", None)]

    win_ref = [None]

    def fire_test(label):
        return run_in(tv["view"], _fire_test, label)

    def _fire_test(label):
        """Play one animation on the Settings dummy card. The reason when it can't play."""
        r = session["dummy"]
        shown = [(0, r)]
        _l, head, kind, color = next(t_ for t_ in TESTS if t_[0] == label)
        if session.get("sport") == "Basketball" and head in ("THREE-POINTER", "TWO-POINTER", "SLAM DUNK!", "BLOCK", "STEAL") and not MAIN.get("test_follow"):
            return "Basketball plays this only as the lead-in to a Then animation. Pick one above."
        gb_ = next((g_ for g_ in ([r["graphic"]] if isinstance(r.get("graphic"), dict) else r.get("graphic") or []) if g_["kind"] == "baseball"), None)
        occ = tuple(gb_["bases"]) if gb_ else None  # the men on base decide the runs
        runs = None
        if occ:
            c_ = sum(occ)
            if head == "GRAND SLAM!":
                occ, runs = (True, True, True), 4
            elif head in ("HOME RUN!", "INSIDE THE PARK HOME RUN!"):
                runs = 1 + c_
                if c_ == 3 and head == "HOME RUN!":
                    head, kind = "GRAND SLAM!", "grand"
            elif head in ("SINGLE", "DOUBLE", "TRIPLE"):  # a single scores the runners on 2nd and 3rd, a double or triple everyone
                runs = occ[1] + occ[2] if head == "SINGLE" else c_
                if runs:
                    head, kind = f"{runs}-RUN {head}", "run"
            elif head == "RUN SCORES":
                runs = 1
        if kind == "redzone":  # needs a card with the football field strip
            def has_field(r_):
                gl_ = r_.get("graphic") or []
                return any(g_["kind"] == "football" for g_ in ([gl_] if isinstance(gl_, dict) else gl_))
            r = next((r_ for _y, r_ in shown if has_field(r_)), None)
            if not r:
                return "The red zone shows on a live football game's field strip. Scroll to one and try again."
        k = card_key(r)
        if kind in ("clutch", "redzone"):
            session["force_clutch" if kind == "clutch" else "force_red"][k] = _time.perf_counter() + 8
            session["sig"] = None
            redraw()
            root.after(8200, run_in, session.get("view"), redraw)
            return
        mode = ui_state.get("score_anim", "pulse")
        mode = "pulse" if mode in ("off", "flash") else mode
        scoring = kind in ("score", "run", "grand")
        import random
        mine = session.get("mine", 0)  # the dummy card's "Trigger for" choice: the team every test plays for
        side = mine if len(r.get("teams") or []) == 2 else None
        make_event(r, k, side, head, None, GRAND_SECS if kind == "grand" else BANNER_SECS if scoring or kind == "final" else 3.5,
                   mode, "4th & 7  \u00b7  Test animation" if kind == "fourth" else "Test animation", grand=kind == "grand", run=kind in ("run", "grand") or head in ("SINGLE", "DOUBLE", "TRIPLE"),
                   sound={"score": "score", "run": "score", "grand": "grand", "turnover": "turnover", "swing": "swing",
                          "final": "final", "fourth": "fourth"}.get(kind))
        session["celeb_next"].pop(k, None)
        if occ is not None and is_field_play(head):  # the runners go round once the banner has faded
            chain_field(k, r, side, head, mode, occ, runs or 0)
        follows = list(MAIN.get("test_follow", []))  # what the play caused, in the order picked, each after the one before
        for follow in follows:
            fside = mine if side is not None else None  # follow-ups play for the same team
            chain_event(k, (r, k, fside, follow, None, FOLLOW_SECS, mode, "Test animation"),
                        {"sound": "swing"} if follow == "MOMENTUM SWING" else {})
        session["test_scores"].pop(k, None)
        if runs and occ is not None and side is not None and is_field_play(head) and r.get("score"):
            try:
                session["run_hold"][k] = {"side": side, "base": float(r["score"][side]), "n": runs, "started": False}
            except (ValueError, TypeError, IndexError):
                pass
        elif scoring and side is not None and r.get("score"):
            lead = next((f_ for f_ in follows if f_ in ("TAKES THE LEAD", "TIES IT UP")), None)
            test_score(r, k, side, lead or head, runs if runs is not None and not lead else None)

    def test_buttons(parent):
        """A frame of buttons that play each animation, for the Settings window to show beside its options."""
        f = tk.Frame(parent, bg=BG)
        head_ = tk.Frame(f, bg=BG)  # the dummy card the tests play on, with the sport and its options beside it
        head_.grid(row=0, column=0, columnspan=3, pady=(0, 8), sticky="w")
        tcanvas = tk.Canvas(head_, bg=BG, highlightthickness=0, width=330, height=200)
        tcanvas.grid(row=0, column=0, rowspan=2, padx=(4, 16), sticky="n")
        tsession = {k_: type(v_)() if isinstance(v_, (dict, list, set)) else v_ for k_, v_ in SESSION0.items()}
        tview = (tcanvas, tsession)
        tsession["view"] = tview
        tsession["mine"] = 0
        tv["view"] = tview
        side_ = tk.Frame(head_, bg=BG)
        side_.grid(row=0, column=1, sticky="nw")
        opts_ = tk.Frame(head_, bg=BG)
        opts_.grid(row=1, column=1, sticky="nw", pady=(8, 0))
        relayout = [lambda: None]  # re-lists the test buttons for the chosen sport (set once they exist)
        sports_ = [("Football", 0), ("Baseball", 1), ("Basketball", 2), ("Hockey", 3), ("Soccer", 4)]
        MINE_ = ("Trigger for", "mine", (("Left", 0), ("Right", 1)))  # the side of the dummy card every test plays for
        LEAD_ = ("Lead", "lead", (("Tied", "tied"), ("Close", "close"), ("Blowout", "blowout")))
        QTR_ = lambda title, n: (title, "q", tuple((str(i), i) for i in range(1, n + 1)))
        OPTS_ = {  # what each sport's dummy card can be set to: (title, key, ((label, value), ...))
            "Football": [MINE_, LEAD_, QTR_("Quarter", 4), ("Clock", "clock", (("10:00", "10:00"), ("4:10", "4:10"), ("1:30", "1:30"))),
                         ("Down", "down", tuple((str(i), i) for i in range(1, 5))), ("To go", "dist", (("1", 1), ("4", 4), ("10", 10), ("20", 20))),
                         ("Field", "field", (("Own 25", "own"), ("Midfield", "mid"), ("Red zone", "red"), ("Goal line", "goal"))),
                         ("Ball", "poss", (("SF", "SF"), ("DAL", "DAL")))],
            "Baseball": [("Men on", "bases", ("1st", "2nd", "3rd")), ("Outs", "outs", (("0", 0), ("1", 1), ("2", 2))),
                         ("Balls", "balls", tuple((str(i), i) for i in range(4))), ("Strikes", "strikes", tuple((str(i), i) for i in range(3))), MINE_,
                         ("Half", "half", (("Top", "Top"), ("Bottom", "Bot"))), ("Inning", "inning", (("1st", 1), ("7th", 7), ("9th", 9))), LEAD_],
            "Basketball": [MINE_, LEAD_, QTR_("Quarter", 4), ("Clock", "clock", (("8:00", "8:00"), ("4:00", "4:00"), ("1:30", "1:30")))],
            "Hockey": [MINE_, LEAD_, QTR_("Period", 3), ("Clock", "clock", (("15:00", "15:00"), ("10:00", "10:00"), ("2:00", "2:00"))),
                       ("Power play", "pp", (("On", True), ("Off", False)))],
            "Soccer": [MINE_, LEAD_, ("Minute", "min", (("20'", 20), ("67'", 67), ("85'", 85))), ("Red card", "red", (("On", True), ("Off", False)))]}
        DEFAULTS_ = {"Football": {"mine": 0, "lead": "close", "q": 3, "clock": "4:10", "down": 3, "dist": 4, "field": "mid", "poss": "SF"},
                     "Baseball": {"mine": 0, "bases": [True, False, True], "outs": 2, "balls": 1, "strikes": 2, "half": "Top", "inning": 7, "lead": "close"},
                     "Basketball": {"mine": 0, "lead": "close", "q": 3, "clock": "4:00"},
                     "Hockey": {"mine": 0, "lead": "close", "q": 2, "clock": "10:00", "pp": True},
                     "Soccer": {"mine": 0, "lead": "close", "min": 67, "red": True}}
        dstate_ = {k_: dict(v_, **({"bases": list(v_["bases"])} if "bases" in v_ else {})) for k_, v_ in DEFAULTS_.items()}

        def build_dummy(sport_):
            import copy
            row_ = copy.deepcopy(demo_data(dstate_[sport_])[dict(sports_)[sport_]])
            lg_ = {"Football": "nfl", "Baseball": "mlb", "Basketball": "nba", "Hockey": "nhl"}.get(sport_)
            for t_ in row_.get("teams") or []:
                if lg_ and not t_.get("logo"):
                    t_["logo"] = f"https://a.espncdn.com/i/teamlogos/{lg_}/500/{ {'SFG': 'sf'}.get(t_['abbr'], t_['abbr'].lower()) }.png"
            return row_

        def show_dummy(rebuild=True):
            def go():
                if rebuild:
                    tsession["dummy"] = build_dummy(sport_var.get())
                    tsession["sport"] = sport_var.get()
                    for key in ("celebs", "celeb_next", "test_scores", "force_clutch", "force_red", "daggers"):
                        tsession[key].clear()
                draw_test()
            run_in(tview, go)

        def draw_options():
            for w_ in opts_.winfo_children():
                w_.destroy()
            sport_ = sport_var.get()
            st_ = dstate_[sport_]
            for r_, (title_, key_, choices_) in enumerate(OPTS_[sport_]):
                tk.Label(opts_, text=title_, bg=BG, fg=DIM, font=("Segoe UI", 9), width=11, anchor="w").grid(row=r_, column=0, padx=(2, 4), pady=2, sticky="w")
                line_ = tk.Frame(opts_, bg=BG)
                line_.grid(row=r_, column=1, sticky="w")
                multi_ = key_ == "bases"  # men on base: any combination; the rest are one choice each
                pills_ = []
                for i_, ch_ in enumerate(choices_):
                    lab_, val_ = (ch_, i_) if multi_ else ch_
                    pw_ = text_width(FONTS["smallb"], lab_) + 18
                    c_ = tk.Canvas(line_, width=pw_, height=24, bg=BG, highlightthickness=0, cursor="hand2")
                    shape_ = c_.create_polygon(rr_points(1, 2, pw_ - 1, 22, 8), smooth=True, fill=BG, outline="#33333d")
                    txt_ = c_.create_text(pw_ / 2, 12, text=lab_, font=FONTS["smallb"], fill=DIM)
                    c_.pack(side="left", padx=2)
                    pills_.append((val_, c_, shape_, txt_))

                    def click(e, v_=val_, k_=key_, ps_=pills_, m_=multi_):
                        if m_:
                            st_[k_][v_] = not st_[k_][v_]
                        else:
                            st_[k_] = v_
                            if k_ == "mine":  # one choice for every sport
                                tsession["mine"] = v_
                                for d_ in dstate_.values():
                                    d_["mine"] = v_
                        paint(k_, ps_, m_)
                        show_dummy()
                    c_.bind("<ButtonRelease-1>", click)

                def paint(k_, ps_, m_):
                    for v_, c_, shape_, txt_ in ps_:
                        on = st_[k_][v_] if m_ else st_[k_] == v_
                        c_.itemconfigure(shape_, fill=PANEL if on else BG, outline=PANEL if on else "#33333d")
                        c_.itemconfigure(txt_, fill=FG if on else DIM)
                paint(key_, pills_, multi_)

        sport_var = tk.StringVar(value=ui_state.get("test_sport", "Football"))

        def on_sport(v_):
            ui_state["test_sport"] = v_
            save_state(ui_state)
            draw_options()
            relayout[0]()
            show_dummy()
        tk.Label(side_, text="Dummy card", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", padx=2, pady=(0, 4))
        styled_option(side_, sport_var, [n_ for n_, _ in sports_], command=on_sport, width=12).pack(anchor="w")
        draw_options()
        tcanvas.bind("<Map>", lambda e: show_dummy(False) if tsession.get("dummy") else None)
        f.after(50, show_dummy)
        for ms_ in (1500, 4000):  # logos arrive a moment after the first draw
            f.after(ms_, lambda: show_dummy(False))
        # what follows the animation (these only ever play after the play that caused them): one choice, like radio buttons
        def choice_row(row, title, key, options, default):
            """A row of pills where exactly one is on, kept in session[key]."""
            fr = tk.Frame(f, bg=BG)
            fr.grid(row=row, column=0, columnspan=3, pady=(0, 6), sticky="w")
            tk.Label(fr, text=title, bg=BG, fg=DIM, font=("Segoe UI", 9), width=5, anchor="w").pack(side="left", padx=(4, 6))
            pills = {}

            def pick(val):
                session[key] = val
                for v_, (c_, shape_, txt_) in pills.items():
                    on = v_ == val
                    c_.itemconfigure(shape_, fill=PANEL if on else BG, outline=PANEL if on else "#33333d")
                    c_.itemconfigure(txt_, fill=FG if on else DIM)
            for val, label in options:
                pw = text_width(FONTS["smallb"], label) + 20
                c_ = tk.Canvas(fr, width=pw, height=24, bg=BG, highlightthickness=0, cursor="hand2")
                shape_ = c_.create_polygon(rr_points(1, 2, pw - 1, 22, 8), smooth=True, fill=BG, outline="#33333d")
                txt_ = c_.create_text(pw / 2, 12, text=label, font=FONTS["smallb"], fill=DIM)
                c_.bind("<ButtonRelease-1>", lambda e, v_=val: pick(v_))
                c_.pack(side="left", padx=2)
                pills[val] = (c_, shape_, txt_)
            pick(session.get(key, default))
            return fr
        # Then: any number of follow-ups, played one after the other in the order they were picked
        then = tk.Frame(f, bg=BG)
        then.grid(row=1, column=0, columnspan=3, pady=(0, 6), sticky="w")
        tk.Label(then, text="Then", bg=BG, fg=DIM, font=("Segoe UI", 9), width=5, anchor="w").pack(side="left", padx=(4, 6))
        then_pills = {}
        session.setdefault("test_follow", [])

        def toggle_then(val):
            chosen = session["test_follow"]
            chosen.remove(val) if val in chosen else chosen.append(val)
            for v_, (c_, shape_, txt_, base_) in then_pills.items():
                on = v_ in chosen
                c_.itemconfigure(shape_, fill=PANEL if on else BG, outline=PANEL if on else "#33333d")
                c_.itemconfigure(txt_, fill=FG if on else DIM, text=(f"{chosen.index(v_) + 1}  " if on else "") + base_)
        for val, label in (("TAKES THE LEAD", "Takes the lead"), ("TIES IT UP", "Ties it up"),
                           ("MOMENTUM SWING", "Momentum swing"), ("FUMBLE RECOVERED", "Fumble recovery"), ("DAGGER!", "Dagger")):
            pw = text_width(FONTS["smallb"], "1  " + label) + 20
            c_ = tk.Canvas(then, width=pw, height=24, bg=BG, highlightthickness=0, cursor="hand2")
            shape_ = c_.create_polygon(rr_points(1, 2, pw - 1, 22, 8), smooth=True, fill=BG, outline="#33333d")
            txt_ = c_.create_text(pw / 2, 12, text=label, font=FONTS["smallb"], fill=DIM)
            c_.bind("<ButtonRelease-1>", lambda e, v_=val: toggle_then(v_))
            then_pills[val] = (c_, shape_, txt_, label)
        def relist_then():
            """Only the follow-ups that make sense for the sport (a fumble recovery is football's)."""
            for v_, (c_, *_r) in then_pills.items():
                c_.pack_forget()
                if v_ != "FUMBLE RECOVERED" or sport_var.get() == "Football":
                    c_.pack(side="left", padx=2)
                elif v_ in session["test_follow"]:
                    toggle_then(v_)
        relist_then()
        for v_ in list(session["test_follow"]):  # picks from earlier in the session
            session["test_follow"].remove(v_)
            toggle_then(v_)
        err = tk.Label(f, text="", bg=BG, fg=COLORS["err"], font=("Segoe UI", 9), anchor="w", justify="left", wraplength=420)
        err.grid(row=3 + (len(TESTS) + 2) // 3, column=0, columnspan=3, padx=4, pady=(6, 0), sticky="w")
        marks = []

        def run_test(label, btn):
            why = fire_test(label)
            for m_ in marks:  # a new try clears the last error
                m_.destroy()
            marks.clear()
            err.configure(text=why or "")
            if why:  # couldn't play: a red X beside its button, the reason below the buttons
                x_ = tk.Label(f, text="\u2715", bg=BG, fg=COLORS["err"], font=("Segoe UI", 10, "bold"))
                x_.place(in_=btn, relx=1.0, x=2, rely=0.5, anchor="w")
                marks.append(x_)
        # which animations each sport can show (Final and Clutch border suit every sport)
        by_sport = {"Football": ("Touchdown", "Field goal", "Interception", "Pick six", "Fumble", "Sack", "Penalty", "Kickoff", "4th down",
                                 "Turnover on downs", "Safety", "Blocked FG", "Blocked punt", "Onside recovery", "Extra point", "2-pt conversion",
                                 "Blocked punt touchdown", "Blocked field goal touchdown", "Blocked PAT", "Onside kick", "Punt", "Red zone"),
                    "Baseball": ("Home run", "Inside-the-park HR", "Grand slam", "Strikeout", "Double play", "Out", "Single", "Double", "Triple",
                                 "Run scores", "Triple play", "Caught stealing", "Picked off"),
                    "Basketball": ("Three-pointer", "Two-pointer", "Slam dunk", "Block", "Steal"), "Hockey": ("Goal", "Penalty"), "Soccer": ("Goal",)}
        everywhere = ("Final", "Clutch border")
        btns = []
        for label, *_rest in sorted(TESTS, key=lambda t_: t_[0].lower()):  # alphabetical, across the rows
            b_ = styled_button(f, label, lambda: None)
            b_.bind("<ButtonRelease-1>", lambda e, lb=label, b2=b_: run_test(lb, b2) if 0 <= e.x <= b2.winfo_width() and 0 <= e.y <= 28 else None)
            btns.append((label, b_))

        def relayout_tests():
            sport_ = sport_var.get()
            n_ = 0
            for m_ in marks:
                m_.destroy()
            marks.clear()
            for label, b_ in btns:
                if label in everywhere or label in by_sport[sport_]:
                    b_.grid(row=3 + n_ // 3, column=n_ % 3, padx=(4, 16), pady=3, sticky="w")
                    n_ += 1
                else:
                    b_.grid_remove()
            relist_then()
        relayout[0] = relayout_tests
        relayout_tests()
        return f

    def celeb_frame():
        """One cheap animation frame: recolor the banner / crossfade / flash and redraw the ripple. No full redraw."""
        now = _time.perf_counter()
        for lay in session["layers"].values():
            ce = lay["ce"]
            t = now - ce["t0"]
            if t >= ce["secs"]:
                continue
            bgc = lay["bgc"]
            if lay["flash"]:  # text fades toward the card as it is right now, flash included, so hidden text stays hidden
                bgc = blend(lay["flash"][2], ce["color"], 0.5 * max(0.0, 1 - t / FLASH_SECS) ** 2)
            a = banner_alpha(ce, t)
            try:
                for i_, col in lay["banner"]:
                    canvas.itemconfigure(i_, fill=blend(bgc, col, a))
                ia = info_alpha(ce, t)
                for i_, opt, base in lay["fade"]:
                    canvas.itemconfigure(i_, **{opt: blend(bgc, base, ia)})
                if lay["flash"]:
                    bgid, hit, base = lay["flash"]
                    col = blend(base, ce["color"], 0.5 * max(0.0, 1 - t / FLASH_SECS) ** 2)
                    for i_ in (bgid, hit):
                        if i_:
                            canvas.itemconfigure(i_, fill=col, **({"outline": col} if i_ == bgid else {}))
                if lay.get("xfade"):
                    xfade_apply(lay, t)
                canvas.delete(lay["tag"])
                if lay["ring"]:
                    draw_rings(*lay["ring"][0], ce, t, bgc, lay["ring"][1], lay["tag"])
                if lay.get("field"):
                    draw_field(lay, ce, t)
            except tk.TclError:
                pass

    def celeb_tick():
        now = _time.perf_counter()
        live = {k: e for k, e in session["celebs"].items() if now - e["t0"] < e["secs"]}
        finished = len(live) != len(session["celebs"])
        session["celebs"] = live
        for k in [k for k, q_ in session["celeb_next"].items() if q_ and k not in live]:  # the next chained event starts as this one ends
            args, kw = session["celeb_next"][k].pop(0)
            make_event(*args, **kw)
        live = session["celebs"]
        if session["anims"]:
            pass  # an expand / collapse is running; it redraws everything itself
        elif finished or session["celeb_dirty"]:
            session["celeb_dirty"] = False
            redraw()
        else:
            celeb_frame()
        if live:
            root.after(frame_delay(now), run_in, session.get("view"), celeb_tick)
        else:
            session["celeb_on"] = False

    UNIT_CIRCLE = [(math.cos(a_ * math.pi / 45), math.sin(a_ * math.pi / 45)) for a_ in range(90)]

    FIELD_LEG = 0.4  # seconds a runner takes between two bases
    FIELD_GAP = 0.35  # seconds between one runner starting and the next

    def is_field_play(head):
        """Baseball headlines that get the base-running diamond after their banner."""
        return bool(re.search(r"HOME RUN|GRAND SLAM|^\d+-RUN |RUN SCORES|RUNS SCORE|(SINGLE|DOUBLE|TRIPLE)$", head)) and "PLAY" not in head

    def field_runners(head, occ, n, after=None):
        """Who runs: [(delay s, [spots: 0 home, 1 first, 2 second, 3 third])]. occ: the men on base before the play (None when
        unknown), n: the runs that score. The runner farthest round scores first; on a hit the others move up by as many bases
        as the batter takes, and the batter goes last."""
        hit = next((n_ for h_, n_ in (("SINGLE", 1), ("DOUBLE", 2), ("TRIPLE", 3)) if head.endswith(h_)), None)
        homer = "HOME RUN" in head or head.startswith("GRAND SLAM")
        on = [b_ for b_ in (3, 2, 1) if occ and occ[b_ - 1]]  # who is on, farthest first
        need = max(0, n - 1 if homer else n)
        pool = on + [b_ for b_ in (3, 2, 1) if b_ not in on]  # a run can score with nobody on (a wild pitch, a walk-off...)
        scorers = pool[:need]
        runners = [(FIELD_GAP * i, list(range(b_, 4)) + [0]) for i, b_ in enumerate(scorers)]
        delay = FIELD_GAP * len(scorers)
        if hit and after is not None:  # a real game: the men still on and the batter end up exactly where ESPN now has them
            movers = [b_ for b_ in on if b_ not in scorers] + [0]  # farthest round first, the batter last
            finals = [b_ for b_ in (3, 2, 1) if after[b_ - 1]]
            for start, final in zip(movers, finals):
                if final > start:
                    runners.append((delay, list(range(start, final + 1))))
        elif hit:
            for b_ in on:
                if b_ not in scorers and min(b_ + hit, 3) > b_:
                    runners.append((delay, list(range(b_, min(b_ + hit, 3) + 1))))  # the men still on move up
            runners.append((delay, list(range(hit + 1))))
        elif homer:
            runners.append((delay, [0, 1, 2, 3, 0]))
        return runners

    def draw_field(lay, ce, t):
        """The banner's little diamond: runners glide base to base, each base lights as it is touched, home pulses on a score."""
        cx, top, S = lay["field"]
        a = banner_alpha(ce, t)
        bg = lay["bgc"]
        cy = top + S
        spot = ((cx, cy + S), (cx + S, cy), (cx, cy - S), (cx - S, cy))
        fhead, focc, fn, fafter = ce["field"]
        runners = field_runners(fhead, focc, fn, fafter)
        total = sum(1 for _d, p_ in runners if p_[-1] == 0)
        u = t - 0.5  # the diamond has faded in
        LEG = FIELD_LEG
        ease = lambda v: v * v * (3 - 2 * v)

        def at(path, p):  # position after p legs (eased within each leg)
            p = max(0.0, min(len(path) - 1.0, p))
            i = min(int(p), len(path) - 2)
            f = ease(p - i)
            (x0, y0), (x1, y1) = spot[path[i]], spot[path[i + 1]]
            return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
        lit, scored, arrivals = set(), 0, []
        for delay, path in runners:
            p = (u - delay) / LEG
            if path[0] != 0 and p < 0:
                lit.add(path[0])  # runners wait on their bases
            for idx in range(1, len(path)):
                if p >= idx and path[idx] != 0:
                    lit.add(path[idx])
            if path[-1] == 0 and p >= len(path) - 1:
                scored += 1
                arrivals.append((p - (len(path) - 1)) * LEG)
        for i in range(4):  # the basepaths
            x0, y0 = spot[i]
            x1, y1 = spot[(i + 1) % 4]
            canvas.create_line(x0, y0, x1, y1, fill=blend(bg, DIM, 0.5 * a), tags=lay["tag"])
        fill = blend(ce["color"], "#ffffff", 0.2)
        for b_ in range(4):
            x0, y0 = spot[b_]
            r_ = 5
            on = b_ in lit
            canvas.create_polygon(x0, y0 - r_, x0 + r_, y0, x0, y0 + r_, x0 - r_, y0, tags=lay["tag"],
                                  fill=blend(bg, fill, a) if on else blend(bg, bg, a), outline=blend(bg, fill if on else DIM, a), width=2)
        for age in arrivals:  # home plate pulses
            if 0 <= age < 0.5:
                rr_ = 5 + age * 26
                canvas.create_oval(spot[0][0] - rr_, spot[0][1] - rr_, spot[0][0] + rr_, spot[0][1] + rr_, tags=lay["tag"],
                                   outline=blend(bg, fill, a * (1 - age / 0.5)), width=2)
        for delay, path in runners:
            p = (u - delay) / LEG
            if p < 0 or (path[-1] == 0 and p >= len(path) - 1):
                if p < 0 and path[0] != 0:
                    x, y = spot[path[0]]
                    canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=blend(bg, "#ffffff", a), outline="", tags=lay["tag"])
                elif p < 0:
                    x, y = spot[0]
                    canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=blend(bg, "#ffffff", a), outline="", tags=lay["tag"])
                continue
            moving = p < len(path) - 1
            for k in range(5, 0, -1) if moving else ():  # a short fading trail
                x, y = at(path, p - k * 0.14)
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=blend(bg, fill, a * (1 - k / 6) * 0.7), outline="", tags=lay["tag"])
            x, y = at(path, p)
            canvas.create_oval(x - 3.5, y - 3.5, x + 3.5, y + 3.5, fill=blend(bg, "#ffffff", a), outline=blend(bg, fill, a), tags=lay["tag"])
        if ce.get("shown_count") != scored:  # the card's score follows the runners: redraw it when another one gets home
            ce["shown_count"] = scored
            session["celeb_dirty"] = True
        if total >= 2 and scored:  # runs count up in the middle: bigger with every run, eased, and shaking when it changes
            sizes = (5, 10, 14, 19, 25)  # pt for 0 (where it grows in from), 1, 2, 3 and 4+ runs
            now_s, was_s = sizes[min(scored, 4)], sizes[min(scored, 4) - 1]
            age = min(arrivals)  # seconds since the latest run scored
            if scored >= 4:  # the fourth run (a grand slam) blows up well past its size, shaking hard, then settles back to it
                peak = 38
                size = was_s + (peak - was_s) * ease(min(1.0, age / 0.3)) if age < 0.3 else peak + (now_s - peak) * ease(min(1.0, (age - 0.3) / 0.5))
                shake = max(0.0, 1 - age / 0.8)
                amp = 8
            else:
                size = now_s if scored == 1 else was_s + (now_s - was_s) * ease(min(1.0, age / 0.35))  # the 1 just appears (and shakes)
                shake = max(0.0, 1 - age / 0.45)
                amp = 4
            canvas.create_text(cx + amp * shake * math.sin(age * 70), cy + amp / 2 * shake * math.cos(age * 85), text=str(scored),
                               font=("Segoe UI", max(6, int(round(size))), "bold"), fill=blend(bg, fill, a), tags=lay["tag"])

    def draw_rings(cx, cy, rad, ce, t, bgc, bounds, tag):
        """Ripples spreading from a logo across the whole card (3 sets of 3 rings, clipped to the card x0, y0, x1, y1)."""
        if ce["mode"] != "pulse" and not ce["grand"] or ce.get("chained_in"):
            return  # a follow-up (takes the lead, momentum swing) flashes the card but adds no ripples
        x0, y0, x1, y1 = bounds
        rmax = max(math.hypot(cx - px_, cy - py_) for px_ in (x0, x1) for py_ in (y0, y1)) + 4
        sets = 3 if ce["secs"] >= 6 else 1  # short events get one set
        for n in range(sets * 3):  # sets of three ripples; every ripple has cleared before the text starts to fade out
            delay = (n // 3) * 1.4 + (n % 3) * 0.25
            pp = (t - delay) / RING_SECS
            if not 0 <= pp < 1:
                continue
            r_ = rad + 4 + (rmax - rad - 4) * (1 - (1 - pp) ** 2)
            intensity = 0.9 * (1 - pp) ** 1.5
            pts = [(cx + r_ * ux, cy + r_ * uy) for ux, uy in UNIT_CIRCLE]
            inside = [x0 <= px_ <= x1 and y0 <= py_ <= y1 for px_, py_ in pts]
            if not any(inside):
                continue
            start = inside.index(False) if not all(inside) else 0  # begin outside the card so runs don't wrap around
            order = [(start + i_) % 90 for i_ in range(90)]
            run = []
            col, wd = blend(bgc, ce["color"], intensity), 3 if pp < 0.5 else 2
            for i_ in order + [order[0]] if all(inside) else order:
                if inside[i_]:
                    run.append(pts[i_])
                else:
                    if len(run) > 1:
                        canvas.create_line(*[c_ for pt in run for c_ in pt], fill=col, width=wd, tags=(tag,))
                    run = []
            if len(run) > 1:
                canvas.create_line(*[c_ for pt in run for c_ in pt], fill=col, width=wd, tags=(tag,))

    def ripple_end(secs):
        """When the last ripple has cleared: three sets for long events, one for short ones."""
        return ((3 if secs >= 6 else 1) - 1) * 1.4 + 0.5 + RING_SECS

    def out_secs(ce):
        """The fade-out starts only once the last ripple has cleared, and takes as long as a set of ripples (0.5 s stagger + 1.3 s)."""
        if ce.get("out"):
            return ce["out"]
        return max(0.4, min(RING_SECS + 0.5, ce["secs"] - ripple_end(ce["secs"])))

    def info_alpha(ce, t):
        """0..1 visibility of the card's own middle info under a banner: hidden while it shows, and kept hidden
        across two chained events (the first fades out, the next fades in, then the info returns)."""
        if t < ce["secs"] / 2 and ce.get("chained_in") or t >= ce["secs"] / 2 and ce.get("chained_out"):
            return 0.0
        return 1 - banner_alpha(ce, t)

    def banner_alpha(ce, t):
        """0..1 visibility of a banner: eased (smoothstep) fades, slower out than in."""
        ease = lambda v: (lambda u: u * u * (3 - 2 * u))(max(0.0, min(1.0, v)))
        return ease(t / 0.45) * ease((ce["secs"] - t) / out_secs(ce))

    def fade_items(ids, bgc, f):
        """Blend the colors of existing canvas items toward the card background (f = 1: unchanged, 0: gone)."""
        for i_ in ids:
            for opt in ("fill", "outline"):
                try:
                    c_ = canvas.itemcget(i_, opt)
                    if len(c_) == 7 and c_.startswith("#"):
                        canvas.itemconfigure(i_, **{opt: blend(bgc, c_, f)})
                        if session["cur_layer"]:
                            session["cur_layer"]["fade"].append((i_, opt, c_))
                except tk.TclError:
                    pass

    def draw_banner(cx, y0, y1, w, ce, t, bgc):
        """The scoring banner centred in the box (y0..y1): team, what happened, the play. Fades in and out."""
        a = banner_alpha(ce, t)
        lay = session["cur_layer"]
        y = y0
        if ce.get("field"):  # a baseball run's diamond: the card frames draw it (draw_field), centred in the box
            S = max(14, min(30, int((y1 - y0 - 10) / 2)))
            if lay is not None:
                lay["field"] = (cx, y0 + (y1 - y0) / 2 - S, S)
            return
        parts = []  # (text item, its full-strength color): the frames recolor these as the banner fades
        if ce["abbr"]:
            col = blend(bgc, FG, 0.7)
            i_, h = ctext(cx, y, ce["abbr"], FONTS["smallb"], blend(bgc, col, a), anchor="n")
            parts.append((i_, col))
            y += h
        col = blend(ce["color"], "#ffffff", 0.2)
        bfont = FONTS["ban"]
        for size_ in (14, 13, 12, 11, 10, 9):  # the longest word (with its "!") has to fit on one line, so a "!" never wraps alone
            bfont = ("Segoe UI", size_, "bold")
            if max(text_width(bfont, w_) for w_ in ce["head"].split()) <= w - 4:
                break
        i_, h = ctext(cx, y, ce["head"], bfont, blend(bgc, col, a), width=w, anchor="n", justify="center")
        parts.append((i_, col))
        y += h
        if ce["detail"]:
            col = blend(bgc, FG, 0.9)
            i_, h = ctext(cx, y + 2, ce["detail"], FONTS["small"], blend(bgc, col, a), width=w, anchor="n", justify="center")
            parts.append((i_, col))
            y += 2 + h
        shift = (y1 - y0 - (y - y0)) / 2
        if shift > 0:
            for i_, _col in parts:
                canvas.move(i_, 0, shift)
        if lay:
            lay["banner"] += parts

    def draw_card(r, x, y, w, final):
        r = run_view(test_view(r))
        session["xfade"] = r.get("_xfade")
        tint = r.get("tint")
        bgc = blend(BG, tint, 0.22) if tint else BG
        ce, ct = celeb_of(r)  # this card's team just scored
        session["cur_celeb"] = (ce, ct)
        session["cur_key"] = card_key(r)
        base_bgc = bgc
        session["card_bg"] = base_bgc  # logos blend their edges into this
        lay = {"ce": ce, "bgc": bgc, "banner": [], "fade": [], "flash": None, "ring": None,
               "tag": f"fx{len(session['layers'])}"} if ce else None
        session["cur_layer"], session["ring_center"] = lay, None
        flashing = bool(ce) and not ce.get("field") and ce["mode"] == "pulse" and ct < FLASH_SECS and ce["side"] is not None
        if flashing:
            bgc = blend(bgc, ce["color"], 0.5 * (1 - ct / FLASH_SECS) ** 2)
        cx0, cw_ = x + 2, w - 4
        tags = ()
        if r.get("game") or r.get("url"):
            tags = (new_hit(("game", r)),)
        bgid = canvas.create_polygon(rr_points(cx0, y, cx0 + cw_, y + 10, 10), smooth=True, fill=bgc, outline=bgc) if (tint or flashing) else None
        hit = canvas.create_rectangle(cx0 + 3, y + 3, cx0 + cw_ - 3, y + 10, fill=bgc, outline="", tags=tags) if tags else None
        ix, ww = cx0 + PAD, cw_ - 2 * PAD
        yy = y + GAP
        if flashing:
            lay["flash"] = (bgid, hit, base_bgc)
        sc = r.get("score")
        gl = r.get("graphic") or []
        gl = [gl] if isinstance(gl, dict) else gl
        tos = next((g_ for g_ in gl if g_["kind"] == "timeouts"), None)
        lp = next((g_ for g_ in gl if g_["kind"] == "lastplay"), None)
        gl = [g_ for g_ in gl if g_["kind"] not in ("timeouts", "lastplay")]
        info = r.get("info") or ""
        lw = ww
        if ui_state.get("layout", "scoreboard") == "scoreboard" and len(r.get("teams") or []) == 2 and r["state"] in ("pre", "in", "post"):
            yy, info = draw_scoreboard(r, cx0, cw_, yy, bgc, tags, gl, tos, info, lp, ce, ct)
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
                if ce and i == (ce["side"] if len(urls) == 2 else (0 if ce["side"] == 0 else -1)):
                    session["ring_center"] = (ix + lg_size / 2, yy + i * (lg_size + 2) + lg_size / 2, lg_size / 2)
            n_head = item_mark()
            _, h = ctext(tx, yy, r["name"], FONTS["name"], FG, width=text_w, tags=tags)
            y_head = yy
            yy += h
            if r["line"]:
                _, h = ctext(tx, yy, r["line"], FONTS["line"], DIM, width=text_w, tags=tags)
                yy += h
            if ce and ce.get("banner"):  # the banner covers the name and opponent for a few seconds
                by1 = max(yy, y_head + 40)
                fade_items(items_since(n_head), bgc, info_alpha(ce, ct))
                draw_banner(tx + text_w / 2, y_head, by1, text_w, ce, ct, bgc)
            if sc:
                yy = max(yy, y + GAP + 28)  # keep the lines below clear of the score
            bb = [g_ for g_ in gl if g_["kind"] == "baseball"]  # bases, count and batter/pitcher get their own row
            sx = tx  # the status line lines up with the name, beside the logo
            if r["state"] == "in":  # red LIVE pill in front of the clock
                bw = 34
                crisp_rr(tx, yy + 2, tx + bw, yy + 16, 3, LIVE_RED, tags)
                canvas.create_text(round(tx + bw / 2), round(yy + 9), text="LIVE", fill="#ffffff", font=FONTS["sec"], tags=tags)
                sx = tx + bw + 6
                if r.get("tv"):  # the channel(s) right beside the LIVE flag
                    sx += tv_badges(sx, yy + 2, r["tv"], tags, limit=2) + 8
            ys = yy  # top of the status row: the baseball panel starts here too
            sid, h = ctext(sx, yy, r.get("status") if sc else r["detail"], FONTS["detb"] if r["state"] == "in" else FONTS["line"],
                           COLORS.get(r["state"], FG), width=lw - (sx - ix), tags=tags)
            clock = r.get("clock") if r["state"] == "in" else None
            if clock:
                session["clock_items"].append((sid, canvas.itemcget(sid, "text"), clock))
                canvas.itemconfigure(sid, text=tick_clock(canvas.itemcget(sid, "text"), clock))
            yy += max(h, 18 if r["state"] == "in" else 0)
            if r.get("tv") and r["state"] == "pre":
                tv_badges(tx, yy + 2, r["tv"], tags)
                yy += 18
            elif r["state"] == "none" and r.get("next_tv"):  # a quiet team's next game
                tv_badges(tx, yy + 2, r["next_tv"], tags)
                yy += 18
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
            canvas.create_line(ix, yy + 5, ix + ww, yy + 5, fill=blend(bgc, FG, 0.12))  # a subtle divider above the next game
            _, h = ctext(ix, yy + 9, r["next"], FONTS["small"], DIM, width=ww, tags=tags)
            yy += 9 + h
            if r.get("next_tv"):  # where to watch it
                tv_badges(ix, yy + 1, r["next_tv"], tags, limit=3)
                yy += 17
        g = r.get("game")
        ctx = None
        if g and gkey(g) in session["expanded"]:
            key = "game:" + gkey(g)
            y0 = yy
            H = draw_details(ix, y0, ww, bgc, session["details"].get(gkey(g)), bool(r.get("win")) or r["state"] == "post", g)
            yy = y0 + H  # always drawn at full height; an animation only moves things afterwards
            if key in session["anims"]:
                cover = canvas.create_rectangle(cx0 - 1, yy + GAP, cx0 + cw_ + 1, yy + GAP + 3, fill=BG, outline="")
                ctx = {"key": key, "kind": "card", "H": H, "y0": y0, "cover": cover, "x0": cx0 - 1, "x1": cx0 + cw_ + 1,
                       "bg": bgid, "hit": hit, "geo": (cx0, y, cx0 + cw_), "dy": 0}
        bottom = yy + GAP
        if ce:
            bounds = (cx0 + 1, y + 1, cx0 + cw_ - 1, bottom - 1)
            lay["ring"] = (session["ring_center"], bounds) if session["ring_center"] else None
            if lay["ring"]:
                draw_rings(*session["ring_center"], ce, ct, base_bgc, bounds, lay["tag"])
            session["layers"][card_key(r)] = lay
            session["cur_layer"] = None
        border = None
        if clutch_of(r) or session["force_clutch"].get(card_key(r), 0) > _time.perf_counter():
            border = pid = canvas.create_polygon(rr_points(cx0 + 1, y + 1, cx0 + cw_ - 1, bottom - 1, 10), smooth=True, fill="",
                                                 outline="#fb923c", width=2)
            session["pulse_items"].append((pid, "#fb923c", bgc))
            start_pulse()
        if bgid:
            canvas.coords(bgid, *rr_points(cx0, y, cx0 + cw_, bottom, 10))
        if hit:
            canvas.coords(hit, cx0 + 3, y + 3, cx0 + cw_ - 3, bottom - 3)
        if ctx:
            ctx["bottom"] = bottom
            session["actx"] = ctx
        k_ = card_key(r)  # (the n-th card with this key, in case a game is listed twice)
        n_ = session["hseen"][k_] = session["hseen"].get(k_, -1) + 1
        hcover = canvas.create_rectangle(0, 0, 0, 0, fill=BG, outline="", state="hidden")  # hides content not yet eased into view
        session["hcards"].append({"k": (k_, n_), "top": y, "bottom": bottom, "x0": cx0, "x1": cx0 + cw_, "bg": bgid, "hit": hit,
                                  "border": border, "cover": hcover, "end": item_mark()})
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
            elif t == "legend":  # clinch marks under a league's standings
                _, h = ctext(x + 6, y + 6, n["text"], FONTS["small"], DIM, width=w - 12)
                y += 6 + h + 6
            elif t == "tabs":  # Overall / Conference / Division pills for one league's standings
                px = x + 6
                for key, label in n["options"]:
                    pw = text_width(FONTS["smallb"], label) + 20
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
                if n["fav"] or n.get("odd"):  # a favourite in green, otherwise every other row darker
                    canvas.create_polygon(rr_points(x + 2, y, x + w - 2, y + 18, 7), smooth=True, outline="",
                                          fill=blend(BG, "#34d399", 0.18) if n["fav"] else blend(BG, "#000000", 0.3))
                ctext(x + 24, y + 2, str(n["rank"]), FONTS["small"], DIM, anchor="ne")
                limit = x + w
                for k, val in enumerate(reversed(n["vals"])):
                    vid, _ = ctext(x + w - 8 - 40 * k, y + 2, val, FONTS["line"], FG if k == 0 else DIM, anchor="ne")
                    limit = min(limit, canvas.bbox(vid)[0])
                limit -= 8
                bold = n["fav"]
                mark = n.get("mark", "")
                if mark:  # the clinch mark sits right after the name
                    limit -= text_width(FONTS["smallb"], mark) + 6
                nid, _ = ctext(x + 32, y + 2, n["name"], FONTS["detb"] if bold else FONTS["line"], DIM if mark == "e" and not bold else FG)
                if canvas.bbox(nid)[2] > limit:  # always the full "City Name": shrink the font, then trim, to fit
                    canvas.itemconfigure(nid, font=FONTS["smallb"] if bold else FONTS["small"])
                    text = n["name"]
                    while canvas.bbox(nid)[2] > limit and len(text) > 4:
                        text = text[:-1]
                        canvas.itemconfigure(nid, text=text.rstrip() + "\u2026")
                if mark:
                    ctext(canvas.bbox(nid)[2] + 5, y + 4, mark, FONTS["smallb"], DIM if mark == "e" else "#34d399")
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
                legend = {}
                for g in groups:
                    children.append({"t": "sub", "text": g["name"], "headers": headers})
                    for rank, r in enumerate(g["rows"], start=1):
                        rec, cols = standing_cells(abbr, r["stats"])
                        fav = (league, r["abbr"].lower()) in favs or (league, r["id"]) in favs
                        mark = clinch_mark(r)
                        if mark:
                            legend[mark] = CLINCH_MARKS.get(abbr, {}).get(mark) or r["note"] or "Clinched"
                        children.append({"t": "srow", "rank": rank, "name": r["name"], "vals": [rec] + cols, "fav": fav, "odd": rank % 2 == 1,
                                         "mark": mark})
                if legend:  # what the marks mean, in ESPN's order
                    order = list(CLINCH_MARKS.get(abbr, {}))
                    keys = sorted(legend, key=lambda m: order.index(m) if m in order else len(order))
                    children.append({"t": "legend", "text": "   ".join(f"{m} \u2013 {legend[m]}".replace(" ", "\u00a0") for m in keys)})
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
                for k, r in enumerate(data):
                    fav = (cfg["league"], r["id"]) in favs or (cfg["league"], r["abbr"].lower()) in favs
                    children.append({"t": "srow", "rank": r["rank"], "name": r["name"], "vals": [r["record"], r["points"]], "fav": fav,
                                     "odd": k % 2 == 0})
            else:
                for g in data:
                    children.append({"t": "sub", "text": g["name"] or dict(options)[sel], "headers": ["Conf", "Ovr"]})
                    for rank, r in enumerate(g["rows"], start=1):
                        conf, cols = college_cells(r["stats"])
                        fav = (cfg["league"], r["id"]) in favs or (cfg["league"], r["abbr"].lower()) in favs
                        children.append({"t": "srow", "rank": rank, "name": r["name"], "vals": [conf] + cols, "fav": fav, "odd": rank % 2 == 1})
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
        session["pulse_items"].clear()
        session["layers"].clear()
        session["gcount"].clear()
        session["hcards"].clear()
        session["hseen"].clear()
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
        height_setup(cw, total)
        return total

    # ---- a card whose height changes between redraws (new lines, a badge...) eases to its new height ----
    HEIGHT_SECS = 0.35

    def height_now(st, now):
        p = (now - st["t0"]) / HEIGHT_SECS
        return st["v1"] if p >= 1 else st["v0"] + (st["v1"] - st["v0"]) * ease_io(p)

    def height_setup(cw, total):
        """After a full redraw: start easing each card whose height changed, and group the items below each card."""
        now, shows, recs = _time.perf_counter(), session["hshow"], session["hcards"]
        snap = session.pop("h_snap", False) or bool(session["anims"])
        live = {}
        for rec in recs:
            h = rec["bottom"] - rec["top"]
            st = shows.get(rec["k"])
            if st is None or snap:  # new card, or an expand/collapse is animating (or just animated) it already
                st = {"v0": h, "v1": h, "t0": now - HEIGHT_SECS}
            elif h != st["v1"]:
                st = {"v0": height_now(st, now), "v1": h, "t0": now}
            live[rec["k"]] = st
        session["hshow"] = live  # cards no longer shown are forgotten
        session["hgeo"] = {"cw": cw, "total": total, "moved": [0.0] * len(recs)}
        if not any(now - live[rec["k"]]["t0"] < HEIGHT_SECS for rec in recs):
            return
        for i, rec in enumerate(recs):  # everything drawn after card i (up to the end of card i + 1) moves with card i's change
            last = recs[i + 1]["end"] if i + 1 < len(recs) else item_mark()
            canvas.tk.eval(f"for {{set j {rec['end'] + 1}}} {{$j < {last}}} {{incr j}} {{{canvas._w} addtag hs{i} withtag $j}}")
        height_frame()
        if not session["h_on"]:
            session["h_on"] = True
            root.after(FRAME_MS, height_tick)

    def height_frame():
        """Place the cards for the current moment of their height easing. Returns whether any is still easing."""
        now, geo, recs = _time.perf_counter(), session.get("hgeo"), session["hcards"]
        if not geo or session["anims"] or len(geo["moved"]) != len(recs):
            return False
        offs, D, easing = [], 0.0, False
        for i, rec in enumerate(recs):
            st = session["hshow"].get(rec["k"])
            d = height_now(st, now) - (rec["bottom"] - rec["top"]) if st else 0.0
            easing = easing or (st is not None and now - st["t0"] < HEIGHT_SECS)
            offs.append((D, d))
            D += d
            canvas.move(f"hs{i}", 0, D - geo["moved"][i])
            geo["moved"][i] = D
        for rec, (above, d) in zip(recs, offs):  # the card itself: its background, border and click area end at the eased bottom
            top, bot = rec["top"] + above, rec["bottom"] + above + d
            if rec["bg"]:
                canvas.coords(rec["bg"], *rr_points(rec["x0"], top, rec["x1"], bot, 10))
            if rec["border"]:
                canvas.coords(rec["border"], *rr_points(rec["x0"] + 1, top + 1, rec["x1"] - 1, bot - 1, 10))
            if rec["hit"]:
                canvas.coords(rec["hit"], rec["x0"] + 3, top + 3, rec["x1"] - 3, bot - 3)
            if d < -0.5:  # growing: hide what lies below the eased bottom
                canvas.coords(rec["cover"], rec["x0"] - 1, bot, rec["x1"] + 1, rec["bottom"] + above + GAP)
                canvas.itemconfigure(rec["cover"], state="normal")
            else:
                canvas.itemconfigure(rec["cover"], state="hidden")
        canvas.configure(scrollregion=(0, 0, geo["cw"], geo["total"] + int(D)))
        return easing

    def height_tick():
        now = _time.perf_counter()
        try:
            easing = height_frame()
        except tk.TclError:
            easing = False
        if easing:
            root.after(frame_delay(now), height_tick)
        else:
            session["h_on"] = False

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
        if not user_sized["on"] and not view_tween["on"]:  # the window grows and shrinks with the content
            canvas.configure(height=max(1, min(session["total"] + int(dy), int(root.winfo_screenheight() * 0.7))))

    def finish_anim(key):
        session["h_snap"] = True  # the expand / collapse already eased the card's height: the next redraw must not again
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
            root.after(frame_delay(now), anim_step)
        else:
            session["looping"] = False
            fit()

    def start_anim(key, opening, from_px=None, on_done=None, dur=0.3):
        for other in [k for k in session["anims"] if k != key]:
            finish_anim(other)  # one animation at a time: jump the previous one to its end
        session["anims"][key] = {"t0": _time.perf_counter(), "dur": dur, "opening": opening, "from": from_px,
                                 "on_done": on_done}
        draw_all()  # drawn at full height; apply_frame() positions it for t = 0
        apply_frame()
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
        if k in session["opening"]:
            return
        session["opening"].add(k)
        session["games"][k] = g
        session["details"].pop(k, None)
        started = {"on": False}

        def begin():  # grow once to the full details, or to "Loading" when ESPN is slow
            if started["on"]:
                return
            started["on"] = True
            session["opening"].discard(k)
            session["expanded"].add(k)
            start_anim(key, True)

        def work():
            fetch_details(g)

            def arrived():
                if not started["on"]:
                    begin()
                elif k in session["expanded"]:  # grow from the "Loading" height to the full details
                    start_anim(key, True, from_px=session["vis"].get(key))
            root.after(0, arrived)
        threading.Thread(target=work, daemon=True).start()
        root.after(350, begin)

    loading = {"on": True, "phase": 0}

    def spin():
        """Spinner shown on the canvas until the first data arrives."""
        if not loading["on"]:
            return
        try:
            t0 = _time.perf_counter()
            cw = max(canvas.winfo_width(), MIN_BODY_W)
            ch = max(canvas.winfo_height(), 100)
            cx, cy, n = cw / 2, ch / 2 - 10, 8
            items = loading.get("items")
            if not items or not canvas.find_withtag("loading"):  # first frame (or the canvas was cleared): make the dots once
                items = loading["items"] = [canvas.create_oval(0, 0, 0, 0, outline="", tags="loading") for _ in range(n)] + [
                    canvas.create_text(0, 0, text="Loading...", fill=DIM, font=FONTS["line"], tags="loading")]
            for i in range(n):
                a_ = 2 * math.pi * i / n
                shade = ((i - t0 * 9) % n) / n  # the lead dot is brightest, the tail fades out; time-based, so smooth
                x, y = cx + 12 * math.cos(a_), cy + 12 * math.sin(a_)
                canvas.coords(items[i], x - 3, y - 3, x + 3, y + 3)
                canvas.itemconfigure(items[i], fill=blend(BG, FG, 0.15 + 0.85 * (1 - shade)))
            canvas.coords(items[n], cx, cy + 30)
            root.after(frame_delay(t0), spin)
        except tk.TclError:
            pass

    def render(results, pin_results, playoffs, leagues=()):
        loading["on"] = False
        detect_scores((results, pin_results, playoffs, leagues))
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
        ui_state["settings_open"] = True  # still open when the app quits or restarts: it opens again on the next start
        save_state(ui_state)

        def close():
            ui_state["settings_open"] = False
            save_state(ui_state)
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", close)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        dark_titlebar(win)
        tk.Label(win, text="Opacity", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, padx=16, pady=(16, 4), sticky="w")
        pct = tk.StringVar()
        box = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=PANEL, highlightcolor=DIM)  # entry with its own arrows
        box.grid(row=0, column=1, padx=16, pady=(16, 4), sticky="e")
        val = tk.Entry(box, width=4, textvariable=pct, justify="right", bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                       bd=0, highlightthickness=0, font=UI_FONT)
        val.pack(side="left", padx=(8, 2), ipady=3)
        arrows = tk.Canvas(box, width=22, height=26, bg=PANEL, highlightthickness=0, cursor="hand2")
        arrows.pack(side="left", padx=(0, 4))
        up = arrows.create_polygon(6, 11, 11, 5, 16, 11, fill=DIM, outline=DIM)
        down = arrows.create_polygon(6, 15, 11, 21, 16, 15, fill=DIM, outline=DIM)

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

        def step(d):
            try:
                n = int(float(pct.get().strip().rstrip("%")))
            except ValueError:
                n = scale.get()
            pct.set(str(max(30, min(100, n + d))))
            on_entry()
        for item, d in ((up, 1), (down, -1)):
            arrows.tag_bind(item, "<Enter>", lambda e, i=item: arrows.itemconfigure(i, fill=FG, outline=FG))
            arrows.tag_bind(item, "<Leave>", lambda e, i=item: arrows.itemconfigure(i, fill=DIM, outline=DIM))
            arrows.tag_bind(item, "<Button-1>", lambda e, d_=d: step(d_))
        val.bind("<Up>", lambda e: step(1))
        val.bind("<Down>", lambda e: step(-1))

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
        layout_choice = tk.StringVar(value=next((l for l, v in LAYOUT_CHOICES if v == ui_state.get("layout", "scoreboard")), "List"))

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
        tk.Label(win, text="Score animation", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=7, column=0, padx=16, pady=(6, 4), sticky="w")
        anim_choice = tk.StringVar(value=next((l for l, v in SCORE_ANIM_CHOICES if v == ui_state.get("score_anim", "pulse")), "Ripple"))

        def on_anim(label):
            ui_state["score_anim"] = dict(SCORE_ANIM_CHOICES)[label]
            save_state(ui_state)
        styled_option(win, anim_choice, [l for l, _ in SCORE_ANIM_CHOICES], command=on_anim, width=12).grid(
            row=7, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Animate", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=8, column=0, padx=16, pady=(6, 4), sticky="w")
        scope_choice = tk.StringVar(value="My teams only" if ui_state.get("anim_scope") == "mine" else "All games")

        def on_scope(label):
            ui_state["anim_scope"] = "mine" if label == "My teams only" else "all"
            save_state(ui_state)
        styled_option(win, scope_choice, ["All games", "My teams only"], command=on_scope, width=12).grid(
            row=8, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Sounds", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=9, column=0, padx=16, pady=(6, 4), sticky="w")
        sound_choice = tk.StringVar(value="On" if ui_state.get("sound", True) else "Muted")

        def on_sound(label):
            ui_state["sound"] = label == "On"
            save_state(ui_state)
        styled_option(win, sound_choice, ["On", "Muted"], command=on_sound, width=12).grid(
            row=9, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Animations", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=10, column=0, padx=16, pady=(6, 4), sticky="w")
        win_ref[0] = win
        tests = test_buttons(win)
        shown = [False]
        board_shown = [False]
        moved = [None]  # where the window was, if showing a side panel had to nudge it to stay on screen
        board = tk.Frame(win, bg=BG)  # one button per sound; plays even while muted

        def set_side(which):
            """Show the tests or the soundboard beside the options (None hides both); opening one closes the other."""
            shown[0], board_shown[0] = which == "tests", which == "board"
            panel = tests if shown[0] else board if board_shown[0] else None
            for p_ in (tests, board):
                if p_ is not panel:
                    p_.grid_remove()
            if panel is not None:  # the window grows to the right and stays where it is (nudged left only if it would leave the screen)
                panel.grid(row=0, column=2, rowspan=15, padx=(0, 16), pady=16, sticky="n")
                win.update_idletasks()
                l, _t, r, _b = screen_bounds()
                if win.winfo_x() + win.winfo_reqwidth() > r:
                    if not moved[0]:
                        moved[0] = (win.winfo_x(), win.winfo_y())
                    win.geometry(f"+{max(l, r - win.winfo_reqwidth())}+{win.winfo_y()}")
            elif moved[0]:  # put it back where it was
                win.geometry(f"+{moved[0][0]}+{moved[0][1]}")
                moved[0] = None
            test_btn.itemconfigure(2, text="Hide" if shown[0] else "Test...")
            board_btn.itemconfigure(2, text="Hide" if board_shown[0] else "Test...")
            ui_state["settings_tests"] = shown[0]
            ui_state["settings_sounds"] = board_shown[0]
            save_state(ui_state)

        def toggle_tests():
            set_side(None if shown[0] else "tests")
        test_btn = styled_button(win, "Test...", toggle_tests)
        test_btn.grid(row=10, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Team logos", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=11, column=0, padx=16, pady=(6, 4), sticky="w")

        def clear_logos():
            import shutil
            shutil.rmtree(LOGO_DIR, ignore_errors=True)
            logo_imgs.clear()
            logo_pending.clear()
            logo_done.clear()
            session["sig"] = None
            draw_all()  # redraws and downloads the logos again
            session["sig"] = compute_sig()
        styled_button(win, "Clear cache", clear_logos).grid(row=11, column=1, padx=16, pady=(6, 4), sticky="e")
        tk.Label(win, text="Soundboard", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=12, column=0, padx=16, pady=(6, 4), sticky="w")
        tk.Label(board, text="Soundboard", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, padx=4, pady=(0, 4), sticky="w")
        for i, (label, kind) in enumerate(SOUNDBOARD):
            styled_button(board, label, lambda k_=kind: play_sound(k_, force=True)).grid(row=1 + i // 2, column=i % 2, padx=4, pady=3, sticky="w")

        def toggle_board():
            set_side(None if board_shown[0] else "board")
        board_btn = styled_button(win, "Test...", toggle_board)
        board_btn.grid(row=12, column=1, padx=16, pady=(6, 4), sticky="e")
        styled_button(win, "Close", close).grid(row=14, column=1, padx=16, pady=(10, 16), sticky="e")
        win.update_idletasks()
        sp = ui_state.get("settings_pos")
        if isinstance(sp, list) and len(sp) == 2:  # where it was last time (kept on screen)
            sx, sy = clamp_pos(int(sp[0]), int(sp[1]), win.winfo_reqwidth(), win.winfo_reqheight())
        else:
            sx, sy = root.winfo_x() + 30, root.winfo_y() + 30
        win.geometry(f"+{sx}+{sy}")
        if ui_state.get("settings_tests"):  # a side panel was showing last time
            set_side("tests")
        elif ui_state.get("settings_sounds"):
            set_side("board")
        pos_save = {"id": None}

        def on_move(e):
            if e.widget is not win:
                return
            ui_state["settings_pos"] = [win.winfo_x(), win.winfo_y()]
            if pos_save["id"]:
                root.after_cancel(pos_save["id"])
            pos_save["id"] = root.after(400, lambda: save_state(ui_state))  # once the window stops moving
        win.update_idletasks()
        win.bind("<Configure>", on_move)

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
        elif h[0] == "boxside":  # box score: show the other team
            if session["box_side"].get(h[1], 0) != h[2]:
                session["box_side"][h[1]] = h[2]
                draw_all()
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
                    delay = FRAME_MS
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
    if ui_state.get("settings_open"):  # Settings was open when the app last closed or restarted
        root.after(300, settings_dialog)
    root.mainloop()


TEAM_COLORS = {"SF": {"color": "aa0000", "alternateColor": "b3995d"}, "DAL": {"color": "041e42", "alternateColor": "869397"},
               "NJ": {"color": "ce1126", "alternateColor": "000000"}, "BOS": {"color": "000000", "alternateColor": "fdb71a"},
               "SFG": {"color": "fd5a1e", "alternateColor": "27251f"}, "LAD": {"color": "005a9c", "alternateColor": "ffffff"},
               "ARS": {"color": "ef0107", "alternateColor": "ffffff"}, "CHE": {"color": "034694", "alternateColor": "ffffff"}}


def demo_data(o=None):
    """Fake live games for every sport, built through the real graphic/text code paths.
    `o` (the Settings dummy card's options) changes situations: lead, bases/outs/count/inning, down/distance/field/possession/clock..."""
    o = o or {}
    ordn = lambda n: {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")
    leads = {"football": {"tied": (21, 21), "close": (24, 21), "blowout": (35, 7)}, "baseball": {"tied": (3, 3), "close": (3, 2), "blowout": (9, 1)},
             "basketball": {"tied": (78, 78), "close": (78, 74), "blowout": (98, 70)}, "hockey": {"tied": (2, 2), "close": (2, 1), "blowout": (5, 1)},
             "soccer": {"tied": (1, 1), "close": (2, 1), "blowout": (4, 0)}}
    sc = lambda sport, dflt: leads[sport][o["lead"]] if o.get("lead") else dflt
    st = lambda n, v: {"name": n, "displayValue": str(v)}
    recs = {"SF": "4-1", "DAL": "3-2", "SFG": "85-77", "LAD": "98-64", "GS": "48-34", "BOS": "64-18", "NJ": "3-1-0",
            "ARS": "6-1-2", "CHE": "5-2-2"}
    team = lambda i, ha, a, score, stats=(): {"id": i, "homeAway": ha, "score": str(score), "statistics": list(stats),
                                              "records": [{"name": "overall", "summary": recs.get(a, "")}],
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
                "score": score, "status": status, "win": wbar, "next": next_line, "next_tv": "NBC \u00b7 Peacock" if next_line else "",
                "tv": {"nfl": "FOX", "mlb": "TBS", "nba": "ESPN \u00b7 ABC", "nhl": "TNT"}.get(league, "") if state in ("in", "pre") else "",
                "teams": comp_teams(comp, (comp.get("competitors") or [None])[0]) if state != "none" else [],
                "info": situation_text(sport, comp), "graphic": situation_graphic(sport, comp, league)}
    fa, fb = sc("football", (21, 17))
    offense, defense = ("SF", "DAL") if o.get("poss", "SF") == "SF" else ("DAL", "SF")
    fside, fyd = {"own": (offense, 25), "mid": (defense, 50), "red": (defense, 15), "goal": (defense, 3)}.get(o.get("field"), (defense, 38))
    nfl = {"competitors": [team("25", "away", "SF", fa), team("6", "home", "DAL", fb)],
           "situation": {"shortDownDistanceText": f"{ordn(o.get('down', 3))} & {o.get('dist', 4)}", "possession": "25" if offense == "SF" else "6",
                         "possessionText": f"{fside} {fyd}", "distance": o.get("dist", 4), "isRedZone": fside == defense and fyd <= 20,
                         "homeTimeouts": 2, "awayTimeouts": 3,
                         "lastPlay": {"text": "J. Purdy pass complete to G. Kittle for 12 yards to the DAL 38"}}}
    ba, bb = sc("baseball", (3, 2))
    bs = o.get("bases", (True, False, True))
    inning_txt = f"{o.get('half', 'Top')} {ordn(o.get('inning', 7))}"
    mlb = {"status": {"type": {"shortDetail": inning_txt}},
           "competitors": [team("1", "away", "SFG", ba), team("2", "home", "LAD", bb)],
           "situation": {"awayChallengesRemaining": 1, "homeChallengesRemaining": 2, "balls": o.get("balls", 1), "strikes": o.get("strikes", 2),
                         "outs": o.get("outs", 2), "onFirst": bs[0], "onSecond": bs[1], "onThird": bs[2],
                         "lastPlay": {"text": "Strike 2 swinging"}, "batter": {"athlete": {"shortName": "M. Chapman"}}, "pitcher": {"athlete": {"shortName": "T. Glasnow"}}}}
    na, nb = sc("basketball", (78, 74))
    nclk = o.get("clock", "5:12")
    nba = {"status": {"period": o.get("q", 3), "clock": int(nclk.split(":")[0]) * 60.0 + int(nclk.split(":")[1]), "displayClock": nclk},
           "situation": {"lastPlay": {"text": "S. Curry makes 26-foot three point jumper (A. Wiggins assists)"}},
           "competitors": [team("9", "away", "GS", na, [st("fouls", 9), st("rebounds", 31)]),
                           team("2", "home", "BOS", nb, [st("fouls", 12), st("rebounds", 28)])]}
    ha_, hb_ = sc("hockey", (2, 1))
    hclk = o.get("clock", "6:47")
    nhl = {"status": {"period": o.get("q", 2), "clock": int(hclk.split(":")[0]) * 60.0 + int(hclk.split(":")[1]), "displayClock": hclk},
           "situation": {"powerPlay": o.get("pp", True), "powerPlayTeam": "NJ",
                                                                  "lastPlay": {"text": "Shot on goal by J. Hughes, saved by J. Swayman"}},
           "competitors": [team("1", "away", "NJ", ha_, [st("shotsOnGoal", 24)]), team("2", "home", "BOS", hb_, [st("shotsOnGoal", 17)])]}
    sa, sb = sc("soccer", (1, 1))
    smin = o.get("min", 67)
    soc = {"status": {"displayClock": f"{smin}'"},
           "details": [{"scoringPlay": True, "clock": {"displayValue": "23'"}, "team": {"id": "1"}},
                       {"yellowCard": True, "clock": {"displayValue": "41'"}, "team": {"id": "2"}},
                       {"scoringPlay": True, "clock": {"displayValue": "55'"}, "team": {"id": "2"}},
                       *([{"redCard": True, "clock": {"displayValue": "62'"}, "team": {"id": "2"}}] if o.get("red", True) else [])],
           "competitors": [team("1", "home", "ARS", sa, [st("possessionPct", 61), st("totalShots", 12)]),
                           team("2", "away", "CHE", sb, [st("possessionPct", 39), st("totalShots", 6)])]}
    return [row("San Francisco 49ers (4-1)", "football", "nfl", "@ Dallas Cowboys (3-2)", f"{fa}-{fb}  Q{o.get('q', 3)} {o.get('clock', '5:12')}", nfl, tint="#aa0000", win=("SF", 62, "DAL", 38, "#b3995d", "#869397")),
            row("San Francisco Giants (85-77)", "baseball", "mlb", "@ Los Angeles Dodgers (98-64)", f"{ba}-{bb}  {inning_txt}", mlb, tint="#fd5a1e", win=("SFG", 58, "LAD", 42, "#fd5a1e", "#005a9c")),
            row("(4) Golden State Warriors (48-34)", "basketball", "nba", "@ (1) Boston Celtics (64-18)", f"{na}-{nb}  Q{o.get('q', 3)} {nclk}", nba, tint="#1d428a", win=("GS", 59, "BOS", 41, "#1d428a", "#007a33")),
            row("New Jersey Devils (3-1-0)", "hockey", "nhl", "@ Boston Bruins (2-2-0)", f"{ha_}-{hb_}  P{o.get('q', 2)} {hclk}", nhl, tint="#ce1126"),
            row("Arsenal (6-1-2)", "soccer", "eng.1", "vs Chelsea (5-2-2)", f"{sa}-{sb}  {smin}'", soc, tint="#ef0107"),
            row("#12 San Diego State Aztecs Football (7-2)", "football", "college-football", "vs Boise State (8-1)",
                "W 31-24  Final", {}, tint="#c41230", state="post",
                next_line="Next: @ #7 Boise State (8-1) \u00b7 Sat Oct 10 6:00 PM"),
            {"name": "Las Vegas Aces (30-14)", "state": "none", "line": "Last: vs New York Liberty (32-12) \u00b7 L 76-84  Final \u00b7 Sat Sep 19",
             "detail": "Season starts May 15, 2027", "info": "", "graphic": None},
            {"name": "Seattle Storm (20-24)", "state": "none", "line": "Last: @ Phoenix Mercury (27-17) \u00b7 L 70-81  Final \u00b7 Sat Sep 19",
             "detail": "Next: vs Chicago Sky \u00b7 Sat Oct 10 7:00 PM", "info": "", "graphic": None, "next_tv": "ESPN \u00b7 ABC"}]


def demo_leagues():
    def g(lg, name, state, detail, tint):
        m = re.match(r"^(\d+)-(\d+)\s+(.*)$", detail)
        return {"score": (m.group(1), m.group(2)) if m else None, "status": m.group(3) if m else detail,
                "name": name, "state": state, "line": "", "league": lg, "detail": detail,
                "tv": {"MLB": "TBS \u00b7 Peacock", "NFL": "FOX", "NBA": "ESPN \u00b7 ABC"}.get(lg, "") if state != "post" else "",
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
                sit_ = comp.get("situation") or {}
                counts_ = {k: v for k, v in sit_.items() if "imeout" in k or "hallenge" in k}
                if counts_:
                    print("  timeouts/challenges:", counts_, "| teams:", [(c.get("homeAway"), c.get("team", {}).get("abbreviation")) for c in comp.get("competitors", [])])
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
                print("   shows:", r and (r["name"], r["line"], r["detail"], r["next"], r.get("series")))
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
            print(f'  {r["name"]} | {r["line"]} | {r["detail"]} | series: {r.get("series")}')
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
