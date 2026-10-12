# EzyGamers GameStreamer

Plays web games automatically in Chromium and (from Phase 4) streams them to
YouTube Live in 9:16. The games themselves are never modified. They are driven
from outside, the way a player would drive them.

## Status

| Phase | What | Status |
|---|---|---|
| 1 | Analyze SumLink, prove it can be automated | Done (probe passes 10/10) |
| 2 | Continuous bot with modes, stats, events | Done |
| 3 | 9:16 stream view with overlay | Done |
| 4 | FFmpeg → YouTube Live | Done |
| 5 | Watchdog, automatic recovery, 24/7, Level-100 marathon, recordings | Done |
| 6–10 | Logging extras, content events, clips, multi-game, dashboard | Planned |

## 24/7 quick start

```powershell
cd "C:\path\to\Youtube-Streaming"
start_247.bat            # YouTube + recordings, Level-100 marathons forever
start_247.bat record     # recordings only, no YouTube
```

To stop, close the window.

**What it does:**

- **Level-100 marathons:** the bot plays **Level 100** mode. Every 10 levels it shows "🔥 10 LEVELS DONE! 90 TO GO". When level 100 is cleared it shows "🏆 LEVEL 100 COMPLETE!", saves the record, pauses 10 s and starts again at level 1. The bot plays about 0.8 s per move (marathon timing in `games/sumlink/config.json`), so a marathon takes about 75 min. In simulation the bot reached level 100 in 20 of 20 runs.
- **Recordings,** in 1-hour files:
  - `recordings\overlay\overlay_*.mp4`: exactly what's on the stream (1080×1920).
  - `recordings\clean\game_*.mp4`: only the game, with no overlay (about 780×1300).
  - Each hour is about 320 MB for the overlay video and 210 MB for the clean video, about 13 GB per day.
- **Disk space:** the oldest recordings are deleted automatically once the folder passes 60 GB or the disk has less than 20 GB free (`output.recordings` in `config/stream.json`).
- **Sleep:** the PC is kept awake while the streamer runs.
- **Watchdog:** checks everything every 2 s.

| Problem | What happens |
|---|---|
| Game over | New run starts after 3 s |
| Game frozen (40 moves without progress) | Game page reloaded, run continues |
| Bot hung (no move for 3 min) | Browser restarted, run **resumed** from snapshot |
| Chromium crash / no frames for 30 s | Browser restarted, run **resumed** (same level and score) |
| Moves get slower than normal (1.6×), or browser running 60 min | Browser refreshed right after a new level appears; run **resumed** (not counted as a failure) |
| Browser crash during the "stage cleared" moment | Run resumed on the next level (the game would otherwise wait on an empty board) |
| YouTube / FFmpeg disconnect or stall | FFmpeg restarted (reconnect); recordings keep going |
| Recorder FFmpeg exits | Restarted (starts a new file) |
| Python itself exits | `start_247.bat` restarts it after 30 s |

- **Restart limits:** restarts back off (5, 15, 30, 60, 120 s). More than the limit within a few minutes triggers a 5-minute cooldown, so it never gets stuck in a rapid restart loop.
- **Stream stays connected during browser restarts:** the last frame keeps being sent.
- **Status:** `data\status.json` shows live status, updated every 5 s, for the dashboard. `logs\streamer.log` has a heartbeat line every 5 minutes.

**Start automatically when you log in to Windows (optional):**

```powershell
schtasks /Create /TN "EzyGamers Streamer" /SC ONLOGON /RL LIMITED /TR "\"C:\path\to\Youtube-Streaming\start_247.bat\""
```

To remove it: `schtasks /Delete /TN "EzyGamers Streamer" /F`

**Things to set in Windows yourself for 24/7:**

- Keep the laptop plugged in.
- Set "When I close the lid" to *Do nothing* (Control Panel → Power Options).
- Turn off automatic restarts for Windows Update during your streaming hours (active hours).
- YouTube keeps only the first 12 hours of a live stream as a replay, so the local recordings are your full archive.

## Setup (Windows, PowerShell)

Needs Python 3.11+ and FFmpeg (`winget install Gyan.FFmpeg`).

```powershell
git clone https://github.com/ezygamer/Youtube-Streaming.git
cd Youtube-Streaming
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env      # then put your YouTube stream key in .env (never commit it)
```

Optional background music: put an MP3 you have the rights to at `assets\music.mp3` (not committed).

## Run the bot

```powershell
.venv\Scripts\python run_bot.py                         # SumLink, Normal mode, visible window, forever
.venv\Scripts\python run_bot.py --mode high_score
.venv\Scripts\python run_bot.py --mode speed_run --runs 1
.venv\Scripts\python run_bot.py --mode challenge --headless
.venv\Scripts\python run_bot.py --mode endless --duration 3600   # stop after 1 hour
```

### Stream view (1080×1920)

```powershell
.venv\Scripts\python run_bot.py --stream --preview-scale 0.45              # watch the 9:16 scene on a normal monitor
.venv\Scripts\python run_bot.py --stream --mode challenge --preview-scale 0.45
.venv\Scripts\python run_bot.py --stream --headless                         # no window (for streaming)
```

- **Layout:** the scene is one 1080×1920 page (`stream/overlay/`, plain HTML/CSS/JS). The unmodified game runs in an iframe in the middle, scaled to fit, and the bot plays inside that iframe.
- **Live data:** Python pushes score, stage, stage progress, combo, mode, record, run timer and challenge status twice a second (`core/overlay.py`).
- **Pop-up messages:** bot events (new high score, stage up, combos, game over, challenge done or failed, speed mode) show as temporary banners.
- **Customizing:** the title, tagline, footer text and every message are set in `config/stream.json`. Messages can differ per mode, and an empty text disables a message.
- **`--preview-scale`** only shrinks the window on your screen. The page and its screenshots stay a full 1080×1920.

### Recording and going live (FFmpeg)

Install FFmpeg once with `winget install Gyan.FFmpeg`. Open a new terminal afterwards so it's on PATH; the code also finds the winget folder by itself, or you can set `FFMPEG_PATH` in `.env`.

```powershell
# 1. Test recording (nothing goes live). Saves recordings\stream_YYYYMMDD_HHMMSS.mp4
.venv\Scripts\python run_bot.py --output file --headless --duration 120

# 2. Go live. First: copy .env.example .env, then set YOUTUBE_STREAM_KEY
.venv\Scripts\python run_bot.py --output youtube --headless --mode high_score
```

How it works:

- **Capture:** the 1080×1920 scene is captured from Chromium (via the DevTools screencast), so no window or screen capture is needed.
- **Writer thread:** a dedicated thread feeds the latest frame to FFmpeg at a constant 30 fps.
- **Encoding:** H.264 at 6 Mbit/s with a keyframe every 2 s, plus AAC 128 kbit/s stereo. The output is a fragmented MP4 file, or FLV over RTMP for YouTube. All of this is set under `output` in `config/stream.json`.
- **Stream key:** read only from `.env` or environment variables. It's replaced with `****` in everything logged, including FFmpeg's own messages.
- **Health:** the log prints a line every 30 s, e.g. `Stream: sending 30.0 fps (target 30), browser repaints 57/s, 4.4 Mbit/s`. This is a warning if the send rate drops below 90%.
- **If FFmpeg stops:** the bot shuts down cleanly. Automatic restart and reconnect come in Phase 5.

**Audio:**

- The default is a silent track; YouTube expects an audio track to be present.
- For background music, set `"audio": {"source": "file", "file": "assets/music.mp3"}` and use music you have the rights to.
- **Game sound effects are not captured.** Chromium's audio can't be captured headlessly. The workarounds need a system-wide loopback device ("Stereo Mix" or a virtual audio cable), which would also pick up every other sound on the PC, so I left it out rather than ship something fragile.

**Hardware encoding:**

- libx264 on the CPU is the default. It measured about 10× real time on this machine, which is plenty.
- **NVIDIA NVENC:** to move encoding to the RTX 3050, update the NVIDIA driver (FFmpeg 9 needs NVENC API 13.1; the current driver has 13.0). Then set `"codec": "h264_nvenc"` in `config/stream.json`.
- **Intel Quick Sync:** `"codec": "h264_qsv"` works today.

Press Ctrl+C to stop. The bot saves the current run before exiting; press Ctrl+C twice to force quit.

### Modes

Modes are defined in `games/sumlink/config.json` under `modes` and are all editable.

| Mode | Strategy | On game over | Ends a run when |
|---|---|---|---|
| `marathon` | look-ahead | restart | level 100 cleared: celebration, then a fresh run (the 24/7 default) |
| `normal` | first pair (the game's own hint move) | restart | game over |
| `high_score` | look-ahead (rollout) | restart | game over |
| `speed_run` | look-ahead, shorter delays | restart | `goal` reached (default: stage 6), which records the time |
| `endless` | first pair | presses the game's **Try Again** (score kept) | never |
| `challenge` | look-ahead | restart | challenge completed or failed (time limit). Challenges rotate |

Strategies (`games/sumlink/strategies.py`) are deterministic, with no AI:

- **first_pair:** plays the first valid match, which is the same move the game's Hint shows.
- **rollout:** for each possible match, plays the rest of the stage out in a simulation and picks the match that clears the stage or makes the most matches.

Offline benchmark (`tools\bench_sumlink.py`, 30 runs capped at stage 20):

| Strategy | Average stage | Average score | Reached the cap |
|---|---|---|---|
| first_pair | 9.4 | 2,318 | 3/30 |
| rollout | 21.0 | 8,130 | 30/30 |

### Output

- **`logs/game.log`:** "Game started", "Stage 5 reached", "Combo x20!", "New high score", "Game over: score …", "Challenge completed …".
- **`logs/streamer.log`:** everything. **`logs/errors.log`:** warnings and errors.
- **`data/stats.db`** (SQLite):
  - `runs` table: one row per run, with score, max stage, moves, max combo, end reason and duration.
  - `events` table: notable moments, kept for the future clip system.
- **High scores** are tracked per game and mode in `stats.db`. The game's own "Best" resets whenever Chromium starts fresh, so it isn't used.

### Error handling in the bot loop

- **A move fails:** the bot logs it, waits (1, 2, 5, 10 or 30 s) and reloads the page. The game restores itself from its saved state, so the run continues.
- **5 consecutive failures:** the bot stops with exit code 2. Restarting the whole process is the watchdog's job (Phase 5).
- **40 moves with no score or stage change:** treated as a frozen game, and the page is reloaded.

## Checks

```powershell
# Phase 1 probe: opens SumLink, plays a bit, checks score/game-over/restart.
.venv\Scripts\python tools\probe_sumlink.py              # watch it in a window
.venv\Scripts\python tools\probe_sumlink.py --headless

# Phase 3 check: renders the stream scene, verifies layout, overlay values, messages
.venv\Scripts\python tools\check_stream_view.py --mode challenge

# Phase 5 checks: watchdog recovery (kills Chromium/FFmpeg on purpose), and the level-100 ending
.venv\Scripts\python tools\check_recovery.py
.venv\Scripts\python tools\check_level100.py

# Unit tests: rule port vs. the game's real JS, plus bot-runner flow with a fake game
.venv\Scripts\python -m pytest -q tests

# Strategy benchmark (offline, no browser; rollout is slow, ~8 min for 30 runs)
.venv\Scripts\python tools\bench_sumlink.py --runs 30 --max-stage 20
```

Screenshots go to `probe_output/`. Logs go to `logs/streamer.log`, `logs/game.log` and `logs/errors.log`.

To regenerate the rule fixtures from the SumLink source:

```powershell
node tools\gen_sumlink_rules_fixtures.mjs "..\Sumlink-React-Web-1Dec-main\Sumlink-React-Web-1Dec-main\src\utils\gameLogic.js" 400
```

## Layout

```
run_bot.py            entry point (CLI)
start_247.bat         24/7 launcher (restarts the program if it ever exits)
config/stream.json    stream scene size, overlay texts, event messages
stream/overlay/       the 1080x1920 scene: index.html, overlay.css, overlay.js
core/                 reusable, game-agnostic
  capture.py          Chromium screencast -> latest JPEG frame
  ffmpeg_output.py    FFmpeg process, constant-rate writer thread, key redaction
  stream_pipeline.py  capture + FFmpeg + health logging
  app.py              the application + watchdog (supervises bot, browser, outputs)
  watchdog.py         restart policies (backoff, cooldown)
  recordings.py       overlay/clean recording outputs + disk janitor
  keep_awake.py       stops Windows sleeping while running
  overlay.py          pushes bot status and event messages into the overlay page
  game_view.py        game as the whole page (DirectView) or inside the overlay iframe (StreamView)
  bot_runner.py       main loop: runs, modes, combo, goals, events, error recovery
  game_adapter.py     interface every game implements (open/read_state/is_game_over/restart/step)
  events.py           event bus (overlay/clips will subscribe later)
  event_log.py        events -> readable lines in game.log
  stats_db.py         SQLite: runs, high scores, events
  config.py           loads games/<id>/config.json, adapters, .env
  browser.py          Playwright/Chromium session, blocks analytics requests
  game_server.py      local HTTP server for SPA games (falls back to index.html)
  logging_setup.py    streamer.log, game.log, errors.log (rotating)
games/sumlink/
  build/              unmodified copy of build/ from Downloads\Sumlink-React-Web-1Dec-main.zip
  config.json         game settings, timings, modes, challenges
  rules.py            Python port of the game's rules (matching, compaction, add row, board generation)
  strategies.py       first_pair and rollout move selection
  adapter.py          reads state, clicks cells, detects game over, restarts, Try Again
tools/                probe, strategy benchmark, fixture generator
tests/                rule tests vs. real JS fixtures; bot-runner tests with a fake game
```

## SumLink analysis (Phase 1)

- **The game:** a 9-column grid of 108 cells. Match two numbers that are equal or sum to 10, with a clear path between them: same row, column or diagonal with no active cells between, or adjacent in reading order. Each match scores +10, and fully cleared rows are removed. "+" (Add row) re-appends the remaining numbers when no match exists, up to 5 times per stage. Clearing the board advances the stage automatically, and the score carries over.
- **Controls:** DOM clicks only. Cells are `#main-game-grid > div`, where index `i` is row `i//9`, column `i%9`. The buttons are `#add-row-btn` and `#hint-btn`. There's no keyboard or canvas input.
- **State:** the game saves `localStorage.gameState` (board, score, stage, addRowsLeft, hintsLeft) and `localStorage.bestScore` on every change. The adapter reads this instead of scraping the screen.
- **Game Over:** the "Out of Moves!" popup appears when there's no valid pair and `addRowsLeft == 0`. The adapter checks both the popup and the rule.
- **Restart:** delete `gameState` and reload, which gives stage 1 with score 0. Don't use "Try Again": it keeps the accumulated score. Don't use `window.resetGame()`: it also wipes `bestScore`.
- **First-run screens:** the language picker and tutorial are skipped by seeding `gameLanguage`, `tutorialCompleted` and `currentScreen` before the page loads.
- **Quirks:**
  - **Stale save after a match:** the game briefly re-saves the pre-match board and writes the new score about 400 ms later. The adapter therefore waits for the score to rise rather than for the state to go quiet.
  - **Routing:** this build uses normal URL paths (`/english`), so it is served by a local server that sends `index.html` for every path.
  - **Analytics:** this build has its tracking code commented out. The bot browser still blocks Google Analytics, Facebook and PostHog in case a future build turns them on.
  - **Sounds:** they are loaded from external URLs (googleapis, raw.githubusercontent), so game audio needs an internet connection.
- **Not in the game:** combo and an explicit level-progress value. Phase 2 will compute these in the bot.
