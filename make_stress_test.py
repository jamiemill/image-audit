"""
Generates a large synthetic CSV for browser stress testing.
Repeats the real data N times with varied identifiers and paths.
SHA256 values are kept intact so real thumbnails load.

Usage:
    python3 make_stress_test.py            # ~200K rows (default)
    python3 make_stress_test.py 50000      # custom row count

Clean up:
    rm output/stress_test.csv
    (then restore config.json via git)
"""
import csv
import sys
import json
import os

SOURCE_CSV  = "output/test15mar_final.csv"
OUTPUT_CSV  = "output/stress_test.csv"
CONFIG_FILE = "config.json"
TARGET_ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 200_000

with open(SOURCE_CSV, newline='', encoding='utf-8') as f:
    reader = csv.reader(f)
    header = next(reader)
    source_rows = list(reader)

real_count = len(source_rows)
repeats    = (TARGET_ROWS + real_count - 1) // real_count  # ceiling division
drive_names = [f"Lacie-10TB-{chr(65+i)}" for i in range(26)]  # Drive-A … Drive-Z

identifier_idx = header.index('Identifier')
fullpath_idx   = header.index('FullPath')

written = 0
with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
    writer = csv.writer(f)
    writer.writerow(header)
    for rep in range(repeats):
        drive = drive_names[rep % len(drive_names)]
        for row in source_rows:
            out = list(row)
            out[identifier_idx] = drive
            out[fullpath_idx]   = f"/Volumes/{drive}{row[fullpath_idx]}"
            writer.writerow(out)
            written += 1
            if written >= TARGET_ROWS:
                break
        if written >= TARGET_ROWS:
            break

print(f"Written {written:,} rows to {OUTPUT_CSV}")

with open(CONFIG_FILE, 'w') as f:
    json.dump({"csv_files": [OUTPUT_CSV]}, f, indent=2)
print(f"Updated {CONFIG_FILE} to use {OUTPUT_CSV}")
print(f"\nRestore with:  git checkout config.json && rm {OUTPUT_CSV}")
