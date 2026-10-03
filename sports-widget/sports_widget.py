"""Always-on-top desktop widget that tracks favorite sports teams.

Uses only the Python standard library (tkinter + urllib) and ESPN's public
JSON endpoints. Edit teams.json to choose teams.

Drag to move, right-click for menu (refresh / always-on-top / quit).
"""
import json
import os
import sys
import threading
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


def situation_text(sport, comp):
    """Sport-specific live info (football down/possession, baseball count/runners)."""
    sit = comp.get("situation") or {}
    if not sit:
        return ""
    if sport == "football":
        parts = [sit.get("shortDownDistanceText") or sit.get("downDistanceText")]
        poss = str(sit.get("possession", ""))
        for c in comp.get("competitors", []):
            if poss and str(c.get("id", c.get("team", {}).get("id", ""))) == poss:
                parts.append(f'{c.get("team", {}).get("abbreviation", "")} ball')
        if sit.get("possessionText"):
            parts.append(sit["possessionText"])
        if sit.get("isRedZone"):
            parts.append("Red zone")
        return " · ".join(p for p in parts if p)
    if sport == "baseball":
        if "balls" not in sit and "outs" not in sit:
            return ""
        bases = [n for n, k in (("1st", "onFirst"), ("2nd", "onSecond"), ("3rd", "onThird")) if sit.get(k)]
        return " · ".join([f'{sit.get("balls", 0)}-{sit.get("strikes", 0)}, {sit.get("outs", 0)} out',
                           "Runners: " + ", ".join(bases) if bases else "Bases empty"])
    return ""


def live_info(entry, event):
    """Fetch the scoreboard entry for a live game (schedule data lacks situation)."""
    today = datetime.now().astimezone().date()
    rng = f"{(today - timedelta(days=1)):%Y%m%d}-{today:%Y%m%d}"
    try:
        for e in fetch_scoreboard(entry["sport"], entry["league"], rng):
            if str(e.get("id")) == str(event.get("id")) and e.get("competitions"):
                return situation_text(entry["sport"], e["competitions"][0])
    except Exception:
        pass
    return ""


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
    info = live_info(entry, event) if state == "in" else ""
    return {"name": name, "state": state, "line": line, "detail": detail, "info": info}


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
                   "detail": detail, "_date": e.get("date", ""),
                   "info": situation_text(sport, comp) if state == "in" else ""}
            key = (league, frozenset(str(c.get("team", {}).get("id", c.get("id", ""))) for c in comp.get("competitors", [])))
            cur = best.get(key)
            # lower priority number wins; within completed games the most recent wins
            if cur is None or prio < cur[0] or (prio == cur[0] == 2 and row["_date"] > cur[1]):
                best[key] = (prio, row["_date"], row)
    rows = sorted((v for v in best.values()), key=lambda v: (v[0], v[1] if v[0] < 2 else ""))
    done = sorted((v for v in rows if v[0] == 2), key=lambda v: v[1], reverse=True)
    return [v[2] for v in rows if v[0] < 2] + [v[2] for v in done]


def load_pinned():
    try:
        with open(PINNED, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_pinned(pins):
    with open(PINNED, "w", encoding="utf-8") as f:
        json.dump(pins, f, indent=2)


def fetch_scoreboard(sport, league, date):
    url = SCOREBOARD.format(sport=sport, league=league, date=date)
    req = urllib.request.Request(url, headers={"User-Agent": "sports-widget/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.load(r)
    # Some responses only carry the season at league level; copy it onto events.
    lg_season = (data.get("leagues") or [{}])[0].get("season", {})
    events = data.get("events", [])
    for e in events:
        e.setdefault("season", lg_season if lg_season.get("type") else data.get("season", {}))
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
                        "info": situation_text(pin["sport"], e["competitions"][0]) if s[0] == "in" else ""}
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
    interval = int(cfg.get("refresh_seconds", 60)) * 1000

    BG, FG, DIM = "#1e1e24", "#f2f2f2", "#9aa0a6"
    COLORS = {"in": "#34d399", "pre": DIM, "post": FG, "none": DIM, "err": "#f87171"}

    root = tk.Tk()
    root.title("Sports")
    root.configure(bg=BG)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.95)
    except tk.TclError:
        pass
    root.geometry("+40+40")

    header = tk.Label(root, text="Sports Tracker", bg=BG, fg=DIM, font=("Segoe UI", 9, "bold"), anchor="w")
    header.pack(fill="x", padx=12, pady=(8, 2))
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", padx=12, pady=(0, 10))

    topmost = tk.BooleanVar(value=True)
    pins = load_pinned()

    def section(title):
        tk.Label(body, text=title, bg=BG, fg=DIM, font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", pady=(6, 2))

    def add_rows(rows):
        for r in rows:
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=r["name"], bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(row, text=r["line"], bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            tk.Label(row, text=r["detail"], bg=BG, fg=COLORS.get(r["state"], FG),
                     font=("Segoe UI", 9, "bold" if r["state"] == "in" else "normal"), anchor="w").pack(fill="x")
            if r.get("info"):
                tk.Label(row, text=r["info"], bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x")

    def render(results, pin_results, playoffs):
        for w in body.winfo_children():
            w.destroy()
        if pin_results:
            section("TRACKED GAMES")
            add_rows(pin_results)
        if results:
            section("MY TEAMS")
            add_rows(results)
        elif not (pin_results or playoffs):
            tk.Label(body, text="No games in the next 7 days", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        if playoffs:
            section("PLAYOFFS")
            add_rows(playoffs)
        header.config(text="Sports Tracker · " + datetime.now().strftime("%I:%M %p").lstrip("0"))

    def refresh():
        def work():
            res, pres, po = fetch_all(entries), fetch_pinned(list(pins)), playoff_games()
            root.after(0, lambda: render(res, pres, po))
        threading.Thread(target=work, daemon=True).start()

    def tick():
        refresh()
        root.after(interval, tick)

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
    def start(e): drag["x"], drag["y"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()
    def move(e): root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")
    for w in (root, header, body):
        w.bind("<Button-1>", start)
        w.bind("<B1-Motion>", move)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Track a game...", command=track_dialog)
    menu.add_command(label="Untrack a game...", command=lambda: untrack_menu(menu_pos["e"]))
    menu.add_command(label="Refresh", command=refresh)
    menu.add_checkbutton(label="Always on top", variable=topmost,
                         command=lambda: root.attributes("-topmost", topmost.get()))
    menu.add_separator()
    menu.add_command(label="Quit", command=root.destroy)
    menu_pos = {}
    def popup(e):
        menu_pos["e"] = e
        menu.tk_popup(e.x_root, e.y_root)
    root.bind("<Button-3>", popup)
    for w in (header, body):
        w.bind("<Button-3>", popup)

    try:
        round_corners(root)
    except Exception:
        pass  # cosmetic only
    tick()
    root.mainloop()


if __name__ == "__main__":
    if "--debug-playoffs" in sys.argv:
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
