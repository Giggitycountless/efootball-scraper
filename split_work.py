"""
split_work.py — 把 Phase 1 收集的 IDs 切分成 N 个分片
Usage: python split_work.py <total_shards>

Example (2台机器):
  python split_work.py 2
  → progress_shard_0.json  (本机用)
  → progress_shard_1.json  (EC2 用，scp 传过去后改名为 efootball_progress.json)

每个分片文件保留完整的 pages_done（避免重跑 Phase 1），
all_ids 为该分片应处理的 ID 列表，详细记录为空（从头开始）。
"""

import sys
import json
import os

_DIR           = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE  = os.path.join(_DIR, "efootball_progress.json")


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("Usage: python split_work.py <total_shards>")
        print("Example: python split_work.py 2")
        sys.exit(1)

    total = int(sys.argv[1])
    if total < 2:
        print("total_shards must be >= 2 (use 1 for single-machine, no split needed)")
        sys.exit(1)

    if not os.path.exists(PROGRESS_FILE):
        print(f"Error: {PROGRESS_FILE} not found. Run Phase 1 first.")
        sys.exit(1)

    with open(PROGRESS_FILE, encoding="utf-8") as f:
        progress = json.load(f)

    all_ids   = progress.get("all_ids", [])
    pages_done = progress.get("pages_done", [])

    if not all_ids:
        print("Error: all_ids is empty. Run Phase 1 first.")
        sys.exit(1)

    print(f"Total IDs: {len(all_ids)}")
    print(f"Splitting into {total} shards (step-slice, interleaved)...")
    print()

    # Step-slice: shard i gets indices i, i+total, i+2*total, ...
    # Mirrors what the scraper does with all_ids[SHARD_INDEX::SHARD_TOTAL]
    for i in range(total):
        shard_ids = all_ids[i::total]
        shard = {
            "pages_done":   pages_done,      # keep so shard machines skip Phase 1
            "all_ids":      shard_ids,
            "detailed_ids": [],              # each machine starts fresh for its shard
        }
        out_file = os.path.join(_DIR, f"progress_shard_{i}.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(shard, f, indent=2)
        print(f"  Shard {i}: {len(shard_ids)} IDs → {out_file}")

    print()
    print("Next steps:")
    print("  Machine 0 (local):  copy progress_shard_0.json → efootball_progress.json")
    print("                      set SHARD_INDEX=0, SHARD_TOTAL=2 in scraper")
    print("  Machine 1 (EC2):    scp progress_shard_1.json → efootball_progress.json")
    print("                      set SHARD_INDEX=1, SHARD_TOTAL=2 in scraper")
    print()
    print("  When both done, run:  python merge_results.py <file1.csv> <file2.csv>")


if __name__ == "__main__":
    main()
