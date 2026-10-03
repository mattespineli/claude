# Sports Widget (Windows)

Small always-on-top desktop widget showing live score / next game / last result for your teams.
Data: ESPN public API. Requires Python 3.8+ (tkinter is included in the python.org installer). No other packages.

## Run
Double-click `run.bat`, or `python sports_widget.py`. `python sports_widget.py --print` prints to the console.

Only teams with a live game or a game in the next 7 days are shown.

## Choose teams
Edit `teams.json`. Each entry: `sport`, `league`, and ESPN `team` abbreviation or numeric id (SDSU is `21` in every college league), e.g.
`basketball/nba/lal`, `football/nfl/dal`, `baseball/mlb/nyy`, `hockey/nhl/bos`, `soccer/eng.1/arsenal`.

## Use
Drag to move. Right-click: track a game, untrack, refresh, toggle always-on-top, quit.
Auto-start: put a shortcut to `run.bat` in `shell:startup`.

## Track a game on demand
Right-click → **Track a game...**, pick a league and date (YYYYMMDD, defaults to today), select one or more games, click *Track selected*.
Tracked games show at the top and persist in `pinned.json`. Right-click → **Untrack a game...** to remove.

## Find a team's ESPN id
`python sports_widget.py --find "san diego state"` searches every college league and prints matches;
add `--add` to append them to `teams.json`. Leagues where the school has no team are simply not listed.

Playoffs section: live or same-day postseason games in NBA, NFL, MLB, WNBA and NHL appear automatically; hidden when none.

Live games also show sport-specific info when ESPN provides it: down & distance, possession and red zone (football); count, outs and runners (baseball).
