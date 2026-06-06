"""
merge_results.py — 合并多个 CSV，按 player_id 去重
Usage: python merge_results.py <file1.csv> <file2.csv> [file3.csv ...]

Example:
  python merge_results.py efootball_players.csv efootball_players_ec2.csv
  → efootball_players_final.csv

Later file wins when player_id duplicates (keeps the last occurrence).
"""

import sys
import csv
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(_DIR, "efootball_players_final.csv")

ALL_FIELDS = [
    "player_id",
    "player_name", "squad_number", "team", "league", "nationality", "region",
    "height", "weight", "age", "foot", "max_level", "rating_grade", "position",
    "overall_rating", "offensive_awareness", "defensive_awareness",
    "ball_control", "dribbling", "tight_possession", "low_pass", "lofted_pass",
    "finishing", "heading", "set_piece_taking", "curl",
    "tackling", "aggression", "defensive_engagement",
    "gk_awareness", "gk_catching", "gk_parrying", "gk_reflexes", "gk_reach",
    "speed", "acceleration", "kicking_power", "jumping", "physical_contact",
    "balance", "stamina",
    "weak_foot_usage", "weak_foot_accuracy", "form", "injury_resistance",
    "primary_playing_style", "player_skills", "ai_playing_styles",
]


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python merge_results.py <file1.csv> <file2.csv> [...]")
        sys.exit(1)

    input_files = sys.argv[1:]
    for f in input_files:
        if not os.path.exists(f):
            print(f"Error: file not found: {f}")
            sys.exit(1)

    rows: dict = {}   # player_id → row dict
    total_read = 0

    for path in input_files:
        file_count = 0
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pid = row.get("player_id", "").strip()
                if pid:
                    rows[pid] = row
                    file_count += 1
                    total_read += 1
        print(f"  {path}: {file_count} rows")

    merged = list(rows.values())
    merged.sort(key=lambda r: int(r["player_id"]) if r["player_id"].isdigit() else 0)

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in merged:
            writer.writerow({k: row.get(k, "") for k in ALL_FIELDS})

    print()
    print(f"Total rows read : {total_read}")
    print(f"After dedup     : {len(merged)}")
    print(f"Duplicates removed: {total_read - len(merged)}")
    print(f"Output → {OUTPUT}")


if __name__ == "__main__":
    main()
