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
    return {"name": name, "state": state, "line": line, "detail": detail}


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
        return json.load(r).get("events", [])


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
                return {"name": pin["label"], "state": s[0], "line": s[1], "detail": s[2]}
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

    header = tk.Label(root, text="My Teams", bg=BG, fg=DIM, font=("Segoe UI", 9, "bold"), anchor="w")
    header.pack(fill="x", padx=12, pady=(8, 2))
    body = tk.Frame(root, bg=BG)
    body.pack(fill="both", padx=12, pady=(0, 10))

    topmost = tk.BooleanVar(value=True)
    pins = load_pinned()

    def render(results, pin_results):
        for w in body.winfo_children():
            w.destroy()
        if pin_results:
            tk.Label(body, text="TRACKED GAMES", bg=BG, fg=DIM, font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", pady=(0, 2))
        if not (pin_results or results):
            tk.Label(body, text="No games in the next 7 days", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w")
        for r in pin_results + results:
            row = tk.Frame(body, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=r["name"], bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x")
            tk.Label(row, text=r["line"], bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x")
            tk.Label(row, text=r["detail"], bg=BG, fg=COLORS.get(r["state"], FG),
                     font=("Segoe UI", 9, "bold" if r["state"] == "in" else "normal"), anchor="w").pack(fill="x")
        header.config(text="My Teams · " + datetime.now().strftime("%I:%M %p").lstrip("0"))

    def refresh():
        def work():
            res, pres = fetch_all(entries), fetch_pinned(list(pins))
            root.after(0, lambda: render(res, pres))
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
    if "--find" in sys.argv:  # --find "san diego state" [--add]
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
        for r in fetch_pinned(load_pinned()) + fetch_all(load_config()["teams"]):
            print(f'{r["name"]:<28} {r["line"]:<10} {r["detail"]}')
    else:
        run_gui()
