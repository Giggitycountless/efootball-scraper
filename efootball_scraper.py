"""
eFootball Player Scraper — 单阶段列表页版（极速）
  - 利用 pesdb.net 的列选择器 cookie，列表页直接输出全部 118 列数据
  - 只需爬 1177 个列表页，完全跳过 37,650 个详情页
  - curl_cffi TLS 指纹伪装 + BrowserProfile 轮换 + ProxyPool
  - Perlin 噪声限速 + ThrottleGate 熔断 + 断点续爬
Usage:
  python efootball_scraper.py
  python efootball_scraper.py --start 701 --end 1013   # only scrape this page range
                                                          # (for splitting work across machines)
"""

import argparse
import sys, csv, json, time, os, re, random, threading, collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from curl_cffi.requests import Session as CurlSession
import noise

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.console import Console
from rich import box

_console = Console(legacy_windows=False, force_terminal=True)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
WORKERS      = 3
REQ_INTERVAL = 1.5
MAX_PAGES    = 1325  # confirmed via binary search on 2026-08-15 (detect_max_pages() below
                      # can re-verify this later when the site isn't throttling so hard)
SAVE_EVERY   = 50

BASE_URL = "https://pesdb.net/efootball/"
_DIR          = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(_DIR, "efootball_progress_v2.json")
OUTPUT_FILE   = os.path.join(_DIR, "efootball_players.csv")
PROXY_FILE    = os.path.join(_DIR, "proxies.txt")

# All 120 columns we request from the list view
_COLS_PARAM = (
    "id,club_number,national_number,pos,name,club_team,league,region,nationality,"
    "height,weight,age,foot,condition,max_level,overall_rating,"
    "offensive_awareness,ball_control,dribbling,tight_possession,low_pass,lofted_pass,"
    "finishing,heading,set_piece_taking,curl,speed,acceleration,kicking_power,jumping,"
    "physical_contact,balance,stamina,defensive_awareness,tackling,aggression,"
    "defensive_engagement,gk_awareness,gk_catching,gk_parrying,gk_reflexes,gk_reach,"
    "weak_foot_usage,weak_foot_accuracy,form,injury_resistance,"
    "playing_style,playing_style_2,"
    "S01,S02,S03,S04,S05,S06,S07,S08,S09,S10,S11,S12,S13,S14,S15,S16,"
    "S17,S18,S19,S20,S21,S22,S23,S24,S25,S26,S27,S28,S29,S30,S31,S32,"
    "S33,S34,S35,S36,S37,S38,S39,S40,S41,S42,S43,S44,S45,S46,S47,S48,"
    "S49,S50,S51,S52,S53,S54,S55,S56,S57,S58,S59,S60,S61,S62,S63,S64,S65,"
    "P01,P02,P03,P04,P05,P06,P07"
)

# S01–S65 skill names, scraped directly from pesdb's own filter-form/column
# labels (the previous hand-typed list omitted "Tap Trick" at S09, shifting
# every name after it by one slot through S64, and never requested S65 at
# all — "Attacking Surge" lives there, outside the S01-S64 range entirely.
# Verified against https://pesdb.net/efootball/ search form on 2026-08-15)
SKILL_NAMES = [
    "Scissors Feint", "Double Touch", "Flip Flap", "Marseille Turn", "Sombrero",
    "Chop Turn", "Cut Behind & Turn", "Scotch Move", "Tap Trick", "Sole Control",
    "Momentum Dribbling", "Acceleration Burst", "Magnetic Feet",
    "Heading", "Bullet Header", "Long-Range Curler", "Blitz Curler",
    "Chip Shot Control", "Knuckle Shot", "Dipping Shot", "Rising Shot",
    "Long Range Shooting", "Low Screamer", "Acrobatic Finishing", "Heel Trick",
    "First-time Shot", "Phenomenal Finishing", "Willpower", "One-touch Pass",
    "Through Passing", "Weighted Pass", "Pinpoint Crossing", "Edged Crossing",
    "Outside Curler", "Rabona", "No Look Pass", "Game-changing Pass",
    "Visionary Pass", "Phenomenal Pass", "Low Lofted Pass",
    "GK Low Punt", "GK High Punt", "Long Throw", "GK Long Throw",
    "Penalty Specialist", "GK Penalty Saver", "GK Directing Defence", "GK Spirit Roar",
    "Gamesmanship", "Man Marking", "Track Back", "Interception", "Blocker",
    "Aerial Superiority", "Sliding Tackle", "Long-reach Tackle", "Fortress",
    "Acrobatic Clearance", "Aerial Fort", "Shadow Hunt", "Captaincy",
    "Attack Trigger", "Super-sub", "Fighting Spirit", "Attacking Surge",
]

AI_STYLE_NAMES = [
    "Trickster", "Mazing Run", "Speeding Bullet", "Incisive Run",
    "Long Ball Expert", "Early Cross", "Long Ranger",
]

# Final CSV columns
ALL_FIELDS = [
    "player_id", "squad_number", "squad_number_national", "position", "player_name",
    "team", "league", "region", "nationality", "height", "weight", "age", "foot",
    "rating_grade", "max_level", "overall_rating",
    "offensive_awareness", "ball_control", "dribbling", "tight_possession",
    "low_pass", "lofted_pass", "finishing", "heading", "set_piece_taking", "curl",
    "speed", "acceleration", "kicking_power", "jumping", "physical_contact",
    "balance", "stamina", "defensive_awareness", "tackling", "aggression",
    "defensive_engagement", "gk_awareness", "gk_catching", "gk_parrying",
    "gk_reflexes", "gk_reach",
    "weak_foot_usage", "weak_foot_accuracy", "form", "injury_resistance",
    "primary_playing_style", "secondary_playing_style", "player_skills", "ai_playing_styles",
]


# ─────────────────────────────────────────────
# Browser profiles
# ─────────────────────────────────────────────
_PROFILES = [
    ("chrome136", "en-US,en;q=0.9"),
    ("chrome124", "en-GB,en;q=0.9,de;q=0.7"),
    ("chrome120", "en-US,en;q=0.8,zh-CN;q=0.5,zh;q=0.3"),
    ("chrome116", "ja,en-US;q=0.9,en;q=0.8"),
    ("firefox135", "en-US,en;q=0.9"),
    ("firefox133", "en-GB,en;q=0.8,fr;q=0.3"),
]


class BrowserProfile:
    def __init__(self):
        self._lock    = threading.Lock()
        self._current = random.choice(_PROFILES)

    def rotate(self) -> None:
        with self._lock:
            others = [p for p in _PROFILES if p[0] != self._current[0]]
            self._current = random.choice(others)

    @property
    def impersonate(self) -> str:
        return self._current[0]

    @property
    def accept_language(self) -> str:
        return self._current[1]

    @property
    def name(self) -> str:
        return self._current[0]


_browser_profile = BrowserProfile()


# ─────────────────────────────────────────────
# Proxy pool
# ─────────────────────────────────────────────
class ProxyPool:
    def __init__(self, path: str = PROXY_FILE):
        self._proxies: list = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self._proxies = [
                    ln.strip() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")
                ]
        self._idx  = 0
        self._lock = threading.Lock()

    def next(self) -> dict | None:
        if not self._proxies:
            return None
        with self._lock:
            proxy = self._proxies[self._idx % len(self._proxies)]
            self._idx += 1
        return {"http": proxy, "https": proxy}

    @property
    def count(self) -> int:
        return len(self._proxies)


_proxy_pool = ProxyPool()


# ─────────────────────────────────────────────
# Rate limiter
# ─────────────────────────────────────────────
class HumanizedRateLimiter:
    def __init__(self, base: float):
        self._lock       = threading.Lock()
        self._base       = base
        self._mult       = 1.0
        self._last       = 0.0
        self._t          = random.uniform(0, 500)
        self._req_n      = 0
        self._next_think = random.randint(35, 90)
        self._o1 = random.uniform(0, 1000)
        self._o2 = random.uniform(0, 1000)
        self._o3 = random.uniform(0, 1000)

    @property
    def current_interval(self) -> float:
        return self._base * self._mult

    def set_interval(self, v: float) -> None:
        with self._lock:
            self._mult = v / self._base

    def wait(self) -> None:
        with self._lock:
            self._req_n += 1
            n1 = noise.pnoise1(self._t * 0.06 + self._o1) * 0.25
            n2 = noise.pnoise1(self._t * 0.35 + self._o2) * 0.15
            n3 = noise.pnoise1(self._t * 2.00 + self._o3) * 0.08
            nm = max(0.4, 1.0 + n1 + n2 + n3)
            interval = self._base * self._mult * nm
            self._t += 0.1 + random.uniform(-0.015, 0.015)
            think = 0.0
            if self._req_n >= self._next_think:
                think             = random.uniform(1.5, 5.0)
                self._next_think += random.randint(35, 90)
            gap = interval + think - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


_rate = HumanizedRateLimiter(REQ_INTERVAL)


# ─────────────────────────────────────────────
# Throttle gate
# ─────────────────────────────────────────────
class ThrottleGate:
    COOLDOWNS = [20, 40, 60, 90, 120]

    def __init__(self):
        self._lock      = threading.Lock()
        self._open      = threading.Event()
        self._open.set()
        self._resume_at = 0.0
        self._trips     = 0

    def trip(self, retry_after: float = 0.0) -> None:
        with self._lock:
            self._trips += 1
            cd  = max(retry_after, self.COOLDOWNS[min(self._trips - 1, len(self.COOLDOWNS) - 1)])
            end = time.monotonic() + cd
            if end > self._resume_at:
                self._resume_at = end
                self._open.clear()
                threading.Thread(target=self._reopen_after, args=(cd,), daemon=True).start()

    def _reopen_after(self, s: float) -> None:
        time.sleep(s)
        with self._lock:
            if time.monotonic() >= self._resume_at - 0.5:
                _browser_profile.rotate()
                self._open.set()

    def wait(self) -> None:
        self._open.wait()

    @property
    def is_closed(self) -> bool:
        return not self._open.is_set()

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._resume_at - time.monotonic())

    @property
    def trip_count(self) -> int:
        return self._trips


_gate = ThrottleGate()


# ─────────────────────────────────────────────
# Reliability tracker
# ─────────────────────────────────────────────
class ReliabilityTracker:
    _THROTTLE = frozenset({429, 503})
    _BACKOFF  = 1.6
    _RECOVER  = 0.95
    _MAX_MULT = 3.0

    def __init__(self, rl: HumanizedRateLimiter):
        self._lock   = threading.Lock()
        self._rl     = rl
        self._ok     = 0
        self._slow   = False
        self.trips   = 0
        self.errors: collections.Counter = collections.Counter()

    def record_ok(self) -> None:
        with self._lock:
            self._ok += 1
            if self._slow and self._ok % 100 == 0:
                new = max(REQ_INTERVAL, self._rl.current_interval * self._RECOVER)
                self._rl.set_interval(new)
                if new <= REQ_INTERVAL * 1.05:
                    self._slow = False

    def record_error(self, status: int, retry_after: float = 0.0) -> None:
        with self._lock:
            self.errors[status] += 1
            self._ok = 0
            if status in self._THROTTLE:
                self.trips += 1
                self._slow = True
                self._rl.set_interval(
                    min(REQ_INTERVAL * self._MAX_MULT,
                        self._rl.current_interval * self._BACKOFF)
                )
        if status in self._THROTTLE:
            _gate.trip(retry_after)

    def snapshot(self) -> dict:
        with self._lock:
            err = "  ".join(f"HTTP {c}×{n}" for c, n in sorted(self.errors.items()))
            return {
                "slow":     self._slow,
                "trips":    self.trips,
                "errors":   err or "none",
                "interval": self._rl.current_interval,
            }


_reliability = ReliabilityTracker(_rate)


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────
class Dashboard:
    def __init__(self):
        self._lock   = threading.Lock()
        self.phase   = "Starting"
        self.done    = 0
        self.total   = MAX_PAGES
        self.rows_written = 0
        self.recent: collections.deque = collections.deque(maxlen=6)
        self._req_times: collections.deque = collections.deque(maxlen=80)
        self.start   = time.monotonic()

    def record_request(self) -> None:
        with self._lock:
            self._req_times.append(time.monotonic())

    def record_page(self, n_rows: int) -> None:
        with self._lock:
            self.done += 1
            self.rows_written += n_rows

    def record_event(self, markup: str) -> None:
        with self._lock:
            self.recent.append(markup)

    def set_phase(self, label: str) -> None:
        with self._lock:
            self.phase = label

    def _rps(self) -> float:
        now = time.monotonic()
        ts  = [t for t in self._req_times if now - t <= 10]
        if len(ts) < 2:
            return 0.0
        span = ts[-1] - ts[0]
        return (len(ts) - 1) / span if span > 0 else 0.0

    @staticmethod
    def _bar(done: int, total: int, w: int = 24) -> tuple:
        pct    = done / total if total else 0
        filled = int(pct * w)
        return "█" * filled + "░" * (w - filled), pct * 100

    @staticmethod
    def _eta(rem: int, rps: float) -> str:
        if rps <= 0 or rem <= 0:
            return ""
        h, r = divmod(int(rem / rps), 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"

    def render(self):
        with self._lock:
            elapsed   = time.monotonic() - self.start
            h, r      = divmod(int(elapsed), 3600)
            m, s      = divmod(r, 60)
            rps       = self._rps()
            bar, pct  = self._bar(self.done, self.total)
            eta       = self._eta(self.total - self.done, rps)
            done      = self.done
            total     = self.total
            rows      = self.rows_written
            phase     = self.phase
            recent    = list(self.recent)

        rel  = _reliability.snapshot()
        gate = _gate.is_closed
        cd   = _gate.cooldown_remaining
        tc   = _gate.trip_count

        tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=True)
        tbl.add_column("label", style="bold cyan", no_wrap=True, width=22)
        tbl.add_column("value", overflow="fold")

        tbl.add_row(
            "Pages",
            f"[{bar}] [bold]{done}[/bold]/{total}  [green]{pct:.0f}%[/green]  "
            f"Rows written: [bold]{rows}[/bold]"
            + (f"  ETA [yellow]{eta}[/yellow]" if eta else ""),
        )
        tbl.add_row("", "")

        speed = "[dim]PAUSED[/dim]" if gate else f"[bold green]{rps:.1f}[/bold green] req/s"
        tbl.add_row(
            "Speed",
            f"{speed}  [dim]~{rel['interval']:.2f}s/req  ·  "
            f"{h:02d}:{m:02d}:{s:02d} elapsed[/dim]",
        )
        tbl.add_row(
            "Identity",
            f"[cyan]{_browser_profile.name}[/cyan]  "
            + (f"[dim]{_proxy_pool.count} proxies[/dim]"
               if _proxy_pool.count else "[dim]direct[/dim]"),
        )
        tbl.add_row("", "")

        if gate:
            cd_h, cd_r = divmod(int(cd), 60)
            cd_s = f"{cd_h}m {cd_r:02d}s" if cd_h else f"{cd_r}s"
            th_val = (
                f"[bold red]COOLING DOWN[/bold red]  "
                f"[yellow]{cd_s} remaining[/yellow]  "
                f"[dim]trip #{tc}  ·  will rotate identity[/dim]"
            )
        elif rel["trips"] > 0:
            th_val = f"[green]OK — gate open[/green]  [dim]{rel['trips']} past trips[/dim]"
        else:
            th_val = "[green]OK — no throttling[/green]"
        tbl.add_row("  Throttle", th_val)
        tbl.add_row("  HTTP errors", rel["errors"])

        if recent:
            tbl.add_row("", "")
            for line in recent:
                tbl.add_row("", Text.from_markup(f"  {line}"))

        border = "bright_red" if gate else "yellow" if rel["slow"] else "bright_blue"
        return Panel(
            tbl,
            title=f"[bold bright_blue]eFootball Scraper[/bold bright_blue]  [dim]— {phase}[/dim]",
            border_style=border,
        )


_dash = Dashboard()


# ─────────────────────────────────────────────
# Per-thread session with columns cookie
# ─────────────────────────────────────────────
_tls = threading.local()


def _get_session() -> CurlSession:
    """Return (or create) a per-thread CurlSession with the columns cookie set."""
    imp = _browser_profile.impersonate
    session  = getattr(_tls, "session", None)
    prev_imp = getattr(_tls, "impersonate", None)
    if session is None or prev_imp != imp:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        session = CurlSession(impersonate=imp)
        # Set columns cookie so the list page returns all 118 columns
        import urllib.parse
        session.cookies.set(
            "columns",
            urllib.parse.quote(_COLS_PARAM, safe=""),
            domain="pesdb.net",
        )
        _tls.session   = session
        _tls.impersonate = imp
    return session


# ─────────────────────────────────────────────
# HTTP helper
# ─────────────────────────────────────────────
def fetch_html(url: str, retries: int = 3):
    for attempt in range(retries):
        _gate.wait()
        _rate.wait()
        _dash.record_request()
        proxy   = _proxy_pool.next()
        session = _get_session()
        try:
            r = session.get(
                url,
                headers={"Accept-Language": _browser_profile.accept_language,
                         "Referer": BASE_URL},
                proxies=proxy,
                timeout=30,
            )
            if r.status_code >= 400:
                ra = float(r.headers.get("Retry-After") or 0)
                _reliability.record_error(r.status_code, ra)
                if r.status_code not in (429, 503) and attempt < retries - 1:
                    time.sleep(1.5 ** attempt)
                if attempt == retries - 1:
                    _dash.record_event(f"[red]HTTP {r.status_code}[/red] {url[-40:]}")
                continue
            _reliability.record_ok()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:60]}"
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
            else:
                _dash.record_event(f"[red]ERR[/red] {url[-35:]} [dim]{msg}[/dim]")
    return None


# ─────────────────────────────────────────────
# Page parser — extracts rows from 118-col table
# ─────────────────────────────────────────────
_HDR_MAP = {
    "ID": "player_id", "Squad Number": "squad_number",
    "Squad Number National": "squad_number_national",
    "Position": "position", "Player Name": "player_name",
    "Team Name": "team", "League": "league", "Region": "region",
    "Nationality": "nationality", "Height": "height", "Weight": "weight",
    "Age": "age", "Foot": "foot", "Rating": "rating_grade",
    "Maximum Level": "max_level", "Overall Rating": "overall_rating",
    "Offensive Awareness": "offensive_awareness", "Ball Control": "ball_control",
    "Dribbling": "dribbling", "Tight Possession": "tight_possession",
    "Low Pass": "low_pass", "Lofted Pass": "lofted_pass",
    "Finishing": "finishing", "Heading": "heading",
    "Set Piece Taking": "set_piece_taking", "Curl": "curl",
    "Speed": "speed", "Acceleration": "acceleration",
    "Kicking Power": "kicking_power", "Jumping": "jumping",
    "Physical Contact": "physical_contact", "Balance": "balance",
    "Stamina": "stamina", "Defensive Awareness": "defensive_awareness",
    "Tackling": "tackling", "Aggression": "aggression",
    "Defensive Engagement": "defensive_engagement",
    "GK Awareness": "gk_awareness", "GK Catching": "gk_catching",
    "GK Parrying": "gk_parrying", "GK Reflexes": "gk_reflexes",
    "GK Reach": "gk_reach",
    "Weak Foot Usage": "weak_foot_usage",
    "Weak Foot Accuracy": "weak_foot_accuracy",
    "Form": "form", "Injury Resistance": "injury_resistance",
    "Playing Style": "primary_playing_style",
    "Playing Style (Def)": "secondary_playing_style",
}


def parse_list_page(soup) -> list[dict]:
    """Parse the main data table and return a list of player row dicts."""
    tables = soup.find_all("table")
    if not tables:
        return []

    table   = tables[0]
    headers = [th.get_text(strip=True) for th in table.find_all("th")]
    if len(headers) < 10:
        return []

    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) != len(headers):
            continue

        row = {f: "" for f in ALL_FIELDS}
        skill_yes = []
        ai_yes    = []

        for i, hdr in enumerate(headers):
            val = cells[i]
            if hdr in _HDR_MAP:
                row[_HDR_MAP[hdr]] = val
            elif re.match(r"S(\d+)", hdr):
                idx = int(re.match(r"S(\d+)", hdr).group(1)) - 1
                if val == "Yes" and idx < len(SKILL_NAMES):
                    skill_yes.append(SKILL_NAMES[idx])
            elif re.match(r"P(\d+)", hdr):
                idx = int(re.match(r"P(\d+)", hdr).group(1)) - 1
                if val == "Yes" and idx < len(AI_STYLE_NAMES):
                    ai_yes.append(AI_STYLE_NAMES[idx])

        row["player_skills"]    = "; ".join(skill_yes)
        row["ai_playing_styles"] = "; ".join(ai_yes)

        if row.get("player_id") and row.get("overall_rating"):
            rows.append(row)

    return rows


# ─────────────────────────────────────────────
# Auto-detect true page count (pesdb's roster keeps growing)
# ─────────────────────────────────────────────
def _page_has_rows(page_num: int) -> bool:
    soup = fetch_html(f"{BASE_URL}?all=1&page={page_num}", retries=3)
    if soup is None:
        return False
    tables = soup.find_all("table")
    if not tables:
        return False
    return len(tables[0].find_all("tr")) > 1


def detect_max_pages(known_good: int = MAX_PAGES, hard_cap: int = 5000) -> int:
    """Binary-search the last page that still has player rows.

    MAX_PAGES used to be a hardcoded constant, which silently went stale as
    pesdb added new players — new pages beyond the constant were never
    fetched, and any playing-style/skill only used by those newer players
    looked completely absent from the CSV. Re-detecting this on every run
    means the scraper always covers the full roster.
    """
    lo = known_good if _page_has_rows(known_good) else 1

    hi = lo
    step = max(lo, 1)
    while hi < hard_cap and _page_has_rows(hi + step):
        hi += step
        step *= 2
    hi = min(hi + step, hard_cap)
    if not _page_has_rows(hi):
        while hi > lo and not _page_has_rows(hi):
            hi -= 1
    else:
        lo = hi

    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _page_has_rows(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


# ─────────────────────────────────────────────
# Progress I/O
# ─────────────────────────────────────────────
_prog_lock = threading.Lock()


def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            p = json.load(f)
        p["pages_done"] = set(p.get("pages_done", []))
        return p
    return {"pages_done": set()}


def save_progress(p: dict) -> None:
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pages_done": sorted(p["pages_done"])}, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)


# ─────────────────────────────────────────────
# Main scrape loop
# ─────────────────────────────────────────────
def scrape(progress: dict, page_start: int = 1, page_end: int = None) -> None:
    page_end = page_end or MAX_PAGES
    pages_todo = [p for p in range(page_start, page_end + 1) if p not in progress["pages_done"]]
    _dash.done  = len([p for p in progress["pages_done"] if page_start <= p <= page_end])
    _dash.total = page_end - page_start + 1
    _dash.set_phase(f"Scraping pages {page_start}-{page_end}")

    if not pages_todo:
        _dash.record_event("[green]All pages already done![/green]")
        return

    write_header = not os.path.exists(OUTPUT_FILE)
    csv_lock     = threading.Lock()
    save_counter = [0]

    csv_file = open(OUTPUT_FILE, "a", newline="", encoding="utf-8")
    writer   = csv.DictWriter(csv_file, fieldnames=ALL_FIELDS, extrasaction="ignore")
    if write_header:
        writer.writeheader()

    def fetch_page(page_num: int) -> tuple:
        url  = f"{BASE_URL}?all=1&page={page_num}"
        soup = fetch_html(url)
        rows = parse_list_page(soup) if soup else []
        return page_num, rows

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(fetch_page, p): p for p in pages_todo}
            for fut in as_completed(futs):
                page_num, rows = fut.result()
                with csv_lock:
                    for row in rows:
                        writer.writerow(row)
                    csv_file.flush()
                    progress["pages_done"].add(page_num)
                    _dash.record_page(len(rows))
                    save_counter[0] += 1
                    if rows:
                        _dash.record_event(
                            f"[green]p{page_num}[/green] → {len(rows)} players"
                        )
                    if save_counter[0] % SAVE_EVERY == 0:
                        with _prog_lock:
                            save_progress(progress)
    finally:
        csv_file.close()

    save_progress(progress)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
def main() -> None:
    # Auto-detection (detect_max_pages()) is available but not called here by
    # default — under heavy throttling its own probe requests can take many
    # minutes. Run it manually and update MAX_PAGES above when you suspect
    # the site has grown again.
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="first page to scrape (inclusive)")
    parser.add_argument("--end", type=int, default=None, help="last page to scrape (inclusive, default MAX_PAGES)")
    args = parser.parse_args()
    page_start, page_end = args.start, args.end or MAX_PAGES

    progress = load_progress()
    _dash.done = len([p for p in progress["pages_done"] if page_start <= p <= page_end])
    _dash.total = page_end - page_start + 1

    with Live(_dash.render(), refresh_per_second=4, screen=False,
              console=_console) as live:
        _stop = threading.Event()

        def _refresh():
            while not _stop.is_set():
                live.update(_dash.render())
                time.sleep(0.25)

        t = threading.Thread(target=_refresh, daemon=True)
        t.start()
        try:
            scrape(progress, page_start, page_end)
        finally:
            _stop.set()
            live.update(_dash.render())

    rel = _reliability.snapshot()
    print(f"\nDone!  {_dash.rows_written} players written → {OUTPUT_FILE}")
    if rel["trips"]:
        print(f"  Throttle trips: {rel['trips']}")


if __name__ == "__main__":
    main()
