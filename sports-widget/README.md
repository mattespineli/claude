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
Resize with the grip in the bottom-right corner (double-click it to auto-fit again). A scrollbar appears when content is taller than the window; the mouse wheel scrolls.
Auto-start: put a shortcut to `run.bat` in `shell:startup`.

## Track a game on demand
Right-click → **Track a game...**, pick a league and date (YYYYMMDD, defaults to today), select one or more games, click *Track selected*.
Tracked games show at the top and persist in `pinned.json`. Right-click → **Untrack a game...** to remove.

## Find a team's ESPN id
`python sports_widget.py --find "san diego state"` searches every college league and prints matches;
add `--add` to append them to `teams.json`. Leagues where the school has no team are simply not listed.

Playoffs section: live and same-day postseason games in NBA, NFL, MLB, WNBA and NHL, plus the latest result per matchup from the last 7 days; hidden when none.

Live games also show sport-specific info when ESPN provides it: down & distance, possession, red zone (football); count, outs, runners, batter/pitcher (baseball); power play and shots (hockey); fouls, rebounds, turnovers (basketball); possession, shots, corners, fouls, cards (soccer).
`python sports_widget.py --debug-live` shows what ESPN sends for live games right now.
**Leagues** lists today's regular MLB, NFL, NBA and WNBA games, grouped by league (leagues with a live game start open).

Playoffs has a **Live** section (opens automatically whenever a game goes live), plus collapsible **Upcoming Today** and **Previous** sections, each grouped by league. Your choices are saved in `state.json`.

Click the Full/Live/Title button in the header to switch between all games, live games only, and title only.

Two tabs: **Games** (everything above) and **Standings** (NFL, NBA, NHL, MLB, WNBA; your teams are highlighted). Cards show the current score large at the top right, and live games show a win-probability bar until you expand them.

While the first data loads, a spinner is shown instead of a blank window. A team shows its last game if it was within the past two weeks.

The gear button in the header opens the same menu as a right-click.

The window remembers its position (and size, if you resized it) between runs.

Switching views (Full/Live/Title) animates the window height and the circle icon fills smoothly.

Use the refresh button (left of the view button) to refresh immediately. Everything that expands or collapses animates smoothly.

Click a game to expand it (win probability, recent plays, scoring, team stats from ESPN); click again to collapse.

While any game is live the widget refreshes faster (default 15 seconds; change under Settings → While games are live).

Right-click a game → **Open game on ESPN** opens its ESPN page.

Right-click → **Update & Restart** runs `git pull` and relaunches the widget (needs Git on PATH; shows the error if the pull fails). **Restart** relaunches without pulling.

Right-click → **Settings...** to adjust opacity (30-100%, slider or typed value) and refresh cadence (15 seconds to 15 minutes). Live MLB games show a base diamond with outs; live NFL games show a field-position strip (ball, first-down line, red zone).

## Live-game graphics
Football: field-position strip. Baseball: base diamond plus ball/strike/out dots. Basketball/hockey: period progress bar. Hockey: shots-on-goal comparison. Soccer: match timeline (goals, cards) and possession bar.
Preview them with fake data: `python sports_widget.py --demo` (see `demo.png`).
