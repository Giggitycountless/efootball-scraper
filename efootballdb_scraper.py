"""
eFootball DB Scraper — efootballdb.com 公开 JSON API 版
  - 数据源 https://api.efootballdb.com/api/2022/ （无需登录/token/cookie）
  - 列表接口 players?variation=none 每页 50 人、每人 181 个原始字段，约 900 页覆盖全库
  - 字段映射：惯用脚/逆足/状态/伤病/打法风格/状态箭头/技能/AI 风格/联赛/地区/加成
  - 自算 OVR（移植自 efootballdb 前端公式，并修正其逆足精度 +1 偏移，已与 pesdb 对拍）
  - 断点续爬：每页写入 JSONL + 进度文件；导出时按 player_id 去重
Usage:
  python efootballdb_scraper.py                     # 爬列表 → 导出 CSV/JSON
  python efootballdb_scraper.py --start 1 --end 400  # 只爬这个页码范围（多机分工）
  python efootballdb_scraper.py --export-only        # 只用已爬数据重新导出
  python efootballdb_scraper.py --restart            # 清空进度与已爬数据，从头开始
"""

import argparse
import csv, json, math, os, random, sys, time

from curl_cffi.requests import Session as CurlSession

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.progress import (Progress, BarColumn, MofNCompleteColumn, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

_console = Console(legacy_windows=False)

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
API_BASE     = "https://api.efootballdb.com/api/2022/"
# Without this the API silently filters to ~58% of cards and drops
# variation_details (pack name) and nationality_b from each row
LIST_PARAMS  = {"variation": "none"}
REQ_INTERVAL = 1.5                     # seconds between requests, plus random jitter below
REQ_JITTER   = 1.0
MAX_RETRIES  = 6
IMPERSONATE  = "chrome120"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.efootballdb.com",
    "Referer": "https://www.efootballdb.com/",
}

_DIR            = os.path.dirname(os.path.abspath(__file__))
ROWS_FILE       = os.path.join(_DIR, "efootballdb_rows.jsonl")
PROGRESS_FILE   = os.path.join(_DIR, "efootballdb_progress.json")
OUTPUT_CSV      = os.path.join(_DIR, "efootballdb_players.csv")
OUTPUT_JSON     = os.path.join(_DIR, "efootballdb_players.json")


# ─────────────────────────────────────────────
# Field mappings
# ─────────────────────────────────────────────
POSITIONS = ["gk", "cb", "lb", "rb", "dmf", "cmf", "lmf", "rmf", "amf", "lwf", "rwf", "ss", "cf"]

# API's internal (PES) stat names → the names used in efootball_players.csv
STAT_FIELDS = {
    "attacking_prowess":    "offensive_awareness",
    "ball_control":         "ball_control",
    "dribbling":            "dribbling",
    "tight_possession":     "tight_possession",
    "low_pass":             "low_pass",
    "lofted_pass":          "lofted_pass",
    "finishing":            "finishing",
    "header":               "heading",
    "place_kicking":        "set_piece_taking",
    "swerve":               "curl",
    "speed":                "speed",
    "explosive_power":      "acceleration",
    "kicking_power":        "kicking_power",
    "jump":                 "jumping",
    "physical_contact":     "physical_contact",
    "body_control":         "balance",
    "stamina":              "stamina",
    "defensive_prowess":    "defensive_awareness",
    "ball_winning":         "tackling",
    "aggression":           "aggression",
    "defensive_engagement": "defensive_engagement",
    "goalkeeping":          "gk_awareness",
    "catching":             "gk_catching",
    "clearing":             "gk_parrying",
    "reflexes":             "gk_reflexes",
    "coverage":             "gk_reach",
}

# The site's own label table (common:playing-style-N). Code 4 = Prolific Winger
# was cross-checked against pesdb; anything newer falls back to "Unknown (N)".
PLAYING_STYLES = {
    0: "None", 1: "Goal Poacher", 2: "Dummy Runner", 3: "Fox in the Box",
    4: "Prolific Winger", 5: "Classic No. 10", 6: "Hole Player", 7: "Box-to-Box",
    8: "Anchor Man", 9: "The Destroyer", 10: "Extra Frontman", 11: "Offensive Full-back",
    12: "Defensive Full-back", 13: "Deep-Lying Forward", 14: "Creative Playmaker",
    15: "Build Up", 16: "Offensive Goalkeeper", 17: "Defensive Goalkeeper",
    18: "Roaming Flank", 19: "Cross Specialist", 20: "Orchestrator",
    21: "Full-back Finisher", 22: "Target Man",
}

# The API stores these one step higher than the in-game scale (verified against
# pesdb: weak_foot_usage 3 = "Rarely", form 4 = "Unwavering", injury 2 = "Low").
# efootballdb's own frontend labels them without the shift, which is wrong.
WEAK_FOOT_USAGE    = {1: "Almost Never", 2: "Rarely", 3: "Occasionally", 4: "Regularly"}
WEAK_FOOT_ACCURACY = {1: "Low", 2: "Medium", 3: "High", 4: "Very High"}
FORM               = {1: "Inconsistent", 2: "Standard", 3: "Unwavering"}
INJURY_RESISTANCE  = {1: "Low", 2: "Medium", 3: "High"}
CODE_OFFSET        = 1

CONDITION = {0: "E", 1: "D", 2: "C", 3: "B", 4: "A"}   # weekly_form arrow
FOOT      = {0: "Right foot", 1: "Left foot"}
REGION    = {1: "Asia-Oceania", 2: "Europe", 3: "Africa", 4: "North & Central America",
             5: "South America", 6: "Asia-Oceania"}    # continent_id, pesdb's grouping

SKILLS = {
    "scissors_feint": "Scissors Feint", "double_touch": "Double Touch",
    "flip_flap": "Flip Flap", "marseille_turn": "Marseille Turn", "sombrero": "Sombrero",
    "cross_over_turn": "Chop Turn", "cut_behind_turn": "Cut Behind & Turn",
    "scotch_move": "Scotch Move", "tap_trick": "Tap Trick",
    "step_on_skill_control": "Sole Control", "momentum_dribbling": "Momentum Dribbling",
    "acceleration_burst": "Acceleration Burst", "magnetic_feet": "Magnetic Feet",
    "heading": "Heading", "bullet_header": "Bullet Header",
    "long_range_drive": "Long-Range Curler", "blitz_curler": "Blitz Curler",
    "chip_shot_control": "Chip Shot Control", "knuckle_shot": "Knuckle Shot",
    "dipping_shots": "Dipping Shot", "rising_shots": "Rising Shot",
    "long_range_shooting": "Long Range Shooting", "low_screamer": "Low Screamer",
    "acrobatic_finishing": "Acrobatic Finishing", "heel_trick": "Heel Trick",
    "first_time_shot": "First-time Shot", "phenomenal_finishing": "Phenomenal Finishing",
    "willpower": "Willpower", "one_touch_pass": "One-touch Pass",
    "through_passing": "Through Passing", "weighted_pass": "Weighted Pass",
    "pinpoint_crossing": "Pinpoint Crossing", "edged_crossing": "Edged Crossing",
    "outside_curler": "Outside Curler", "rabona": "Rabona", "no_look_pass": "No Look Pass",
    "game_changing_pass": "Game-changing Pass", "visionary_pass": "Visionary Pass",
    "phenomenal_pass": "Phenomenal Pass", "low_lofted_pass": "Low Lofted Pass",
    "low_punt_trajectory": "GK Low Punt", "gk_high_punt": "GK High Punt",
    "long_throw": "Long Throw", "gk_long_throw": "GK Long Throw",
    "penalty_specialist": "Penalty Specialist", "gk_penalty_saver": "GK Penalty Saver",
    "gk_directing_defense": "GK Directing Defence", "gk_spirit_roar": "GK Spirit Roar",
    "gamesmanship": "Gamesmanship", "man_marking": "Man Marking", "track_back": "Track Back",
    "interception": "Interception", "blocker": "Blocker",
    "aerial_superiority": "Aerial Superiority", "sliding_tackle": "Sliding Tackle",
    "long_reach_tackle": "Long-reach Tackle", "fortress": "Fortress",
    "acrobatic_clear": "Acrobatic Clearance", "aerial_fort": "Aerial Fort",
    "shadow_hunt": "Shadow Hunt", "captaincy": "Captaincy", "attack_trigger": "Attack Trigger",
    "super_sub": "Super-sub", "fighting_spirit": "Fighting Spirit",
    "attacking_surge": "Attacking Surge",
    # Placeholders the site itself hasn't named yet — kept under their raw key
    "new_skill_6": "new_skill_6", "new_skill_8": "new_skill_8",
    "new_skill_a_660": "new_skill_a_660", "new_skill_b_660": "new_skill_b_660",
    "new_skill_c_660": "new_skill_c_660", "new_skill_d_660": "new_skill_d_660",
    "new_skill_e_660": "new_skill_e_660",
}

AI_STYLES = {
    "trickster": "Trickster", "mazing_run": "Mazing Run", "speeding_bullet": "Speeding Bullet",
    "incisive_run": "Incisive Run", "long_ball_expert": "Long Ball Expert",
    "early_cross": "Early Cross", "long_ranger": "Long Ranger",
}

# Booster objects reuse the stat names, plus a couple of their own spellings
BOOSTER_STAT_LABELS = {
    **{k: v.replace("_", " ").title() for k, v in STAT_FIELDS.items()},
    "tackling": "Tackling", "defensive_awareness": "Defensive Awareness",
}

# Fields we read or deliberately drop — anything else gets reported once so
# new API fields (e.g. a newly named skill) don't go unnoticed.
_HANDLED = (set(STAT_FIELDS) | set(SKILLS) | set(AI_STYLES) | set(POSITIONS) | {
    "pes_id", "base_pes_id", "variation", "variation_details", "card_type", "current_club_id",
    "club_team", "national_team", "nationality_a", "nationality_b", "player_name", "shirt_name", "japanese_name",
    "chinese_name", "age", "height", "weight", "strong_foot", "weekly_form", "main_position",
    "main_position_text", "playing_style", "playing_attitude", "max_level", "form",
    "weak_foot_usage", "weak_foot_accuracy", "injury_resistance", "booster", "booster2",
    "booster3", "iconic", "market_value", "sell_gp", "gp_price", "coin_price",
})
_IGNORED = {
    "fake_version", "mobile_version", "competition_group", "level", "exp_total",
    "exp_current_level", "exp_to_next_level", "customize_point", "gp_price_base",
    "can_use_gp", "default_rarity", "contract_expiry_date", "price", "youth_club_id",
    "loan_from_club_id", "loan_expiry_date", "hidden_player", "national_shirt_name",
    "strong_hand", "star_player", "free_kick_motion", "goal_celebration", "won_ballon_dor",
    "possession_game", "quick_counter", "long_ball_counter", "out_wide", "long_ball",
}
_unknown_fields: set = set()


# ─────────────────────────────────────────────
# Overall rating
# ─────────────────────────────────────────────
# Weight table and proficiency multipliers lifted from efootballdb's frontend
# (get_rating_2022). Columns follow POSITIONS order.
OVR_WEIGHTS = {
    "height":               [186, 136,  49,  49,  61,  37,  12,  12,  37,  49,  49,  62,  99],
    "attacking_prowess":    [  0,  14,  61,  61,  61,  98,  98,  98, 171, 159, 159, 173, 210],
    "ball_control":         [ 13,  27,  86,  86, 122, 171, 171, 171, 196, 159, 159, 210, 123],
    "dribbling":            [  0,  14,  61,  61,  37,  98, 110, 122, 122, 159, 159, 123,  62],
    "tight_possession":     [  0,   0,  37,  37,  24,  49,  73,  61,  73,  86,  86,  86,  37],
    "low_pass":             [ 27,  41,  61,  61, 122, 208, 135, 135, 196,  73,  73,  99,  37],
    "lofted_pass":          [ 40,  68, 147, 147, 122, 159, 196, 196, 159,  98,  98,  74,  12],
    "finishing":            [  0,  27,  24,  24,  37,  73,  86,  86, 184, 159, 159, 284, 358],
    "place_kicking":        [  0,  14,  24,  24,  12,  12,  24,  24,  12,  12,  12,  12,  12],
    "swerve":               [  0,  14,  24,  24,  12,  12,  24,  24,  12,  12,  12,  12,  12],
    "header":               [  0,  55,  24,  24,  61,  24,  12,  12,  24,  24,  24,  25,  62],
    "defensive_prowess":    [ 13, 286, 147, 147, 220,  86,  49,  49,  24,  12,  12,   0,   0],
    "ball_winning":         [  0, 191,  86,  86, 122,  86,  24,  24,  24,  12,  12,  12,  12],
    "aggression":           [  0,  82,  37,  37,  98,  37,  12,  12,  12,  12,  12,  12,  12],
    "kicking_power":        [ 53,  27,  24,  24,  49,  73,  24,  24,  73,  61,  61,  99, 123],
    "speed":                [ 13, 136, 220, 220,  61,  61, 196, 196,  98, 220, 220,  86,  99],
    "explosive_power":      [ 40, 150, 184, 184,  61,  86, 159, 159,  86, 159, 159,  99, 123],
    "physical_contact":     [ 80, 204,  98,  98, 122,  49,  24,  24,  24,  37,  37,  37,  86],
    "body_control":         [  0,   0,  24,  24,  12,  24,  61,  61,  24,  73,  73,  74,  86],
    "jump":                 [133, 109,  37,  37,  37,  12,  12,  12,  12,  24,  24,  37,  62],
    "goalkeeping":          [279,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0],
    "catching":             [226,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0],
    "coverage":             [226,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0],
    "reflexes":             [173,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0],
    "clearing":             [173,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0,   0],
    "stamina":              [  0,  68, 196, 196, 196, 196, 147, 147,  86,  49,  49,  49,  37],
    "weak_foot_accuracy":   [  4,   4,   4,   4,   4,   4,   4,   4,   4,   4,   4,   4,   4],
    "defensive_engagement": [  0,  14,  24,  24,  24,  24,  24,  24,  24,  24,  24,  12,  12],
}

# Stat → multiplier row; each row is indexed by the player's proficiency (0/1/2)
# at the position being rated, so off-position ratings drop.
_PROFICIENCY_MULT = {0: [.8, .92, 1], 1: [.81999999, .94, 1], 2: [.83999997, .95999998, 1]}
_STAT_MULT_ROW = {
    "attacking_prowess": 1, "ball_control": 0, "dribbling": 0, "tight_possession": 1,
    "low_pass": 0, "lofted_pass": 0, "finishing": 0, "place_kicking": 0, "swerve": 0,
    "header": 0, "defensive_prowess": 1, "ball_winning": 0, "aggression": 1,
    "kicking_power": 1, "speed": 0, "explosive_power": 1, "physical_contact": 1,
    "body_control": 2, "jump": 2, "goalkeeping": 0, "stamina": 1, "coverage": 1,
    "clearing": 1, "reflexes": 1, "catching": 1, "defensive_engagement": 1,
}


def overall_rating(p: dict, pos: int) -> int:
    """Level-1 rating at position index `pos`, normal condition, no boosters.

    Matches pesdb's "Overall Rating" once weak_foot_accuracy is shifted back to
    the 1-4 scale — efootballdb's frontend skips that and reads ~1 point high.
    """
    proficiency = p.get(POSITIONS[pos]) or 0
    total = min(p["height"] - 136, 95) * OVR_WEIGHTS["height"][pos]
    for stat, row in _STAT_MULT_ROW.items():
        v = math.floor(p[stat] * _PROFICIENCY_MULT[row][proficiency])
        v = max(min(v, 99) - 25, 0)
        total += min(v, 95) * OVR_WEIGHTS[stat][pos]
    wfa = p["weak_foot_accuracy"] - CODE_OFFSET
    total += min(math.floor((wfa - 1) * 59 / 3 + 40) - 25, 95) * OVR_WEIGHTS["weak_foot_accuracy"][pos]
    return math.floor((total + 500) / 1000)


# ─────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────
_session = CurlSession(impersonate=IMPERSONATE)
_last_request = [0.0]


def _pace() -> None:
    wait = REQ_INTERVAL + random.uniform(0, REQ_JITTER) - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def get_json(path: str, params: dict = None):
    for attempt in range(MAX_RETRIES):
        _pace()
        backoff = min(5 * 2 ** attempt, 300)
        try:
            r = _session.get(API_BASE + path, params=params, headers=HEADERS, timeout=30)
        except Exception as exc:
            _console.print(f"[yellow]{type(exc).__name__}[/yellow] {path} — retry in {backoff}s")
            time.sleep(backoff)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = max(float(r.headers.get("Retry-After") or 0), backoff)
            _console.print(f"[yellow]HTTP {r.status_code}[/yellow] {path} — retry in {wait:.0f}s")
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {r.status_code} for {path}: {r.text[:200]}")
    raise RuntimeError(f"giving up on {path} after {MAX_RETRIES} attempts")


# ─────────────────────────────────────────────
# Row flattening
# ─────────────────────────────────────────────
def _label(code, table: dict, offset: int = 0) -> str:
    if code is None:
        return ""
    return table.get(code - offset, f"Unknown ({code})")


def _team_name(team) -> str:
    return (team or {}).get("english_name") or ""


def _league(team, competitions: dict) -> str:
    entries = (team or {}).get("competition_entries") or []
    names = [competitions.get(e.get("competition_id"), "") for e in entries]
    # A club is entered in its league plus cups; the league is the entry that
    # isn't a cup. National teams only have cups, so they get no league.
    for name in names:
        if name and not any(w in name for w in ("Cup", "CUP", "Champions League", "Qualifiers",
                                                "Championship")):
            return name
    return ""


def _boosters(p: dict) -> str:
    parts = []
    for key in ("booster", "booster2", "booster3"):
        b = p.get(key)
        if not b:
            continue
        stats = [BOOSTER_STAT_LABELS[k] for k in BOOSTER_STAT_LABELS
                 if isinstance(b.get(k), int) and b.get(k) > 0]
        parts.append(f"+{b.get('up_point', 0)} " + "/".join(stats))
    return "; ".join(parts)


def flatten(p: dict, competitions: dict) -> dict:
    _unknown_fields.update(k for k in p if k not in _HANDLED and k not in _IGNORED)

    club_team, national_team = p.get("club_team") or {}, p.get("national_team") or {}
    nationality = p.get("nationality_a") or {}
    pack = p.get("variation_details") or {}
    main_pos = p.get("main_position")

    row = {
        "player_id":             p["pes_id"],
        "base_player_id":        p.get("base_pes_id"),
        "player_name":           p.get("player_name") or "",
        "shirt_name":            p.get("shirt_name") or "",
        "name_ja":               p.get("japanese_name") or "",
        "name_zh":               p.get("chinese_name") or "",
        "card_type":             p.get("card_type"),
        "variation":             p.get("variation"),
        "featured_pack":         pack.get("name") or ("Original" if not p.get("card_type") else ""),
        "release_date":          pack.get("release_date") or "",
        "position":              p.get("main_position_text") or "",
        "positions_proficient":  "/".join(x.upper() for x in POSITIONS if p.get(x) == 2),
        "positions_secondary":   "/".join(x.upper() for x in POSITIONS if p.get(x) == 1),
        "team":                  _team_name(p.get("current_club_id")),
        "club":                  _team_name(club_team.get("team")),
        "league":                _league(club_team.get("team") or p.get("current_club_id"), competitions),
        "squad_number":          club_team.get("shirt_number") or "",
        "national_team":         _team_name(national_team.get("team")),
        "squad_number_national": national_team.get("shirt_number") or "",
        "nationality":           nationality.get("english_name") or "",
        "second_nationality":    (p.get("nationality_b") or {}).get("english_name") or "",
        "region":                REGION.get(nationality.get("continent_id"), ""),
        "height":                p.get("height"),
        "weight":                p.get("weight"),
        "age":                   p.get("age"),
        "foot":                  _label(p.get("strong_foot"), FOOT),
        "condition":             _label(p.get("weekly_form"), CONDITION),
        "max_level":             p.get("max_level"),
        "overall_rating":        overall_rating(p, main_pos) if main_pos is not None else "",
    }
    for i, pos in enumerate(POSITIONS):
        row[f"rating_as_{pos}"] = overall_rating(p, i)
    for api_key, col in STAT_FIELDS.items():
        row[col] = p.get(api_key)
    row.update({
        "weak_foot_usage":    _label(p.get("weak_foot_usage"), WEAK_FOOT_USAGE, CODE_OFFSET),
        "weak_foot_accuracy": _label(p.get("weak_foot_accuracy"), WEAK_FOOT_ACCURACY, CODE_OFFSET),
        "form":               _label(p.get("form"), FORM, CODE_OFFSET),
        "injury_resistance":  _label(p.get("injury_resistance"), INJURY_RESISTANCE, CODE_OFFSET),
        "playing_style":      _label(p.get("playing_style"), PLAYING_STYLES),
        "playing_style_id":   p.get("playing_style"),
        "playing_attitude":   p.get("playing_attitude"),   # raw code, no confirmed labels
        "player_skills":      "; ".join(name for k, name in SKILLS.items() if p.get(k)),
        "ai_playing_styles":  "; ".join(name for k, name in AI_STYLES.items() if p.get(k)),
        "boosters":           _boosters(p),
        "iconic":             int(bool(p.get("iconic"))),
        "market_value":       p.get("market_value"),
        "sell_gp":            p.get("sell_gp"),
        "gp_price":           p.get("gp_price"),
        "coin_price":         p.get("coin_price"),
    })
    for pos in POSITIONS:
        row[f"proficiency_{pos}"] = p.get(pos)
    for k in SKILLS:
        row[f"s_{k}"] = int(bool(p.get(k)))
    for k in AI_STYLES:
        row[f"p_{k}"] = int(bool(p.get(k)))
    return row


# ─────────────────────────────────────────────
# Progress I/O
# ─────────────────────────────────────────────
def _load_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _progress_bar() -> Progress:
    return Progress(TextColumn("[bold cyan]{task.description}"), BarColumn(),
                    MofNCompleteColumn(), TimeElapsedColumn(), TextColumn("eta"),
                    TimeRemainingColumn(), console=_console)


# ─────────────────────────────────────────────
# Phase 1: list pages
# ─────────────────────────────────────────────
def scrape_pages(page_start: int, page_end: int = None) -> None:
    progress = _load_json(PROGRESS_FILE, {"pages_done": []})
    done = set(progress["pages_done"])

    competitions = {c["pes_id"]: c.get("competition_name") or ""
                    for c in get_json("competitions")["data"]}

    first = get_json("players", {**LIST_PARAMS, "page": page_start})
    last_page = first["meta"]["last_page"]
    progress.update(last_page=last_page, total=first["meta"]["total"])
    page_end = min(page_end or last_page, last_page)
    _console.print(f"API reports [bold]{progress['total']}[/bold] players over "
                   f"{last_page} pages; scraping pages {page_start}-{page_end}")

    todo = [n for n in range(page_start, page_end + 1) if n not in done]
    with open(ROWS_FILE, "a", encoding="utf-8") as out, _progress_bar() as bar:
        task = bar.add_task("pages", total=page_end - page_start + 1,
                            completed=page_end - page_start + 1 - len(todo))
        for n in todo:
            data = first if n == page_start else get_json("players", {**LIST_PARAMS, "page": n})
            for p in data["data"]:
                out.write(json.dumps(flatten(p, competitions), ensure_ascii=False) + "\n")
            out.flush()
            done.add(n)
            progress["pages_done"] = sorted(done)
            _save_json(PROGRESS_FILE, progress)
            bar.advance(task)


# ─────────────────────────────────────────────
# Phase 2: export
# ─────────────────────────────────────────────
def export() -> None:
    if not os.path.exists(ROWS_FILE):
        _console.print("[red]Nothing scraped yet[/red]")
        return

    rows = {}
    with open(ROWS_FILE, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                rows[row["player_id"]] = row   # a resumed page may have been written twice

    out = list(rows.values())
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    progress = _load_json(PROGRESS_FILE, {})
    total, last_page = progress.get("total"), progress.get("last_page")
    missing_pages = (last_page or 0) - len(progress.get("pages_done", []))
    _console.print(f"\n[green]Done![/green] {len(out)} players → {OUTPUT_CSV}\n"
                   f"                       → {OUTPUT_JSON}")
    if missing_pages > 0:
        _console.print(f"[yellow]  {missing_pages} of {last_page} pages not scraped yet "
                       f"(rerun to resume)[/yellow]")
    elif total and len(out) < total:
        _console.print(f"[yellow]  {total - len(out)} fewer than the API's total ({total}) — "
                       f"the list likely shifted mid-run (rerun with --restart)[/yellow]")
    if _unknown_fields:
        _console.print(f"[yellow]  Unmapped API fields (not exported): "
                       f"{', '.join(sorted(_unknown_fields))}[/yellow]")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
def main() -> None:
    global REQ_INTERVAL
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="first list page (inclusive)")
    parser.add_argument("--end", type=int, default=None, help="last list page (inclusive, default: last)")
    parser.add_argument("--interval", type=float, default=REQ_INTERVAL,
                        help=f"base seconds between requests (default {REQ_INTERVAL})")
    parser.add_argument("--export-only", action="store_true", help="only rebuild CSV/JSON from scraped rows")
    parser.add_argument("--restart", action="store_true", help="discard progress and scraped rows first")
    args = parser.parse_args()
    REQ_INTERVAL = args.interval

    if args.restart:
        for path in (ROWS_FILE, PROGRESS_FILE):
            if os.path.exists(path):
                os.remove(path)

    if not args.export_only:
        scrape_pages(args.start, args.end)
    export()


if __name__ == "__main__":
    main()
