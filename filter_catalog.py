"""
filter_catalog.py — Filter rows from a catalog CSV using exclusion and/or inclusion patterns.

Usage:
    python3 filter_catalog.py <catalog.csv> [--exclusions exclusions.txt] [--inclusions inclusions.txt] [--output filtered.csv]

--exclusions: remove rows whose path matches any pattern
--inclusions: keep only rows whose path matches any pattern
Both can be used together.

If --output is omitted, overwrites the input file (after writing to a .tmp first).
"""

import argparse
import csv
import fnmatch
import os
import sys


def load_patterns(patterns_file, label):
    if not os.path.exists(patterns_file):
        print(f"{label} file not found: {patterns_file}", file=sys.stderr)
        sys.exit(1)
    patterns = []
    with open(patterns_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                patterns.append(line)
    return patterns


def matches(path, patterns):
    norm = path.replace(os.sep, '/')
    parts = [p for p in norm.split('/') if p]
    for pattern in patterns:
        pnorm = pattern.replace(os.sep, '/')
        if '/' in pnorm:
            if fnmatch.fnmatch(norm, pnorm):
                return True
            # Also match any file inside this directory path
            if norm.startswith(pnorm.rstrip('/') + '/'):
                return True
        else:
            for part in parts:
                if fnmatch.fnmatch(part, pnorm):
                    return True
    return False


def main():
    parser = argparse.ArgumentParser(description='Filter a catalog CSV by exclusion/inclusion patterns.')
    parser.add_argument('catalog', help='Input catalog CSV file.')
    parser.add_argument('--exclusions', default=None,
                        help='Exclusions file — remove rows matching these patterns.')
    parser.add_argument('--inclusions', default=None,
                        help='Inclusions file — keep only rows matching these patterns.')
    parser.add_argument('--output', default=None,
                        help='Output file (default: overwrite input).')
    args = parser.parse_args()

    if not args.exclusions and not args.inclusions:
        print("Specify --exclusions, --inclusions, or both.")
        sys.exit(1)

    exclusion_patterns = load_patterns(args.exclusions, 'Exclusions') if args.exclusions else []
    inclusion_patterns = load_patterns(args.inclusions, 'Inclusions') if args.inclusions else []

    if exclusion_patterns:
        print(f"Loaded {len(exclusion_patterns)} exclusion pattern(s).")
    if inclusion_patterns:
        print(f"Loaded {len(inclusion_patterns)} inclusion pattern(s).")

    output_file = args.output or args.catalog + '.tmp'

    kept = 0
    removed = 0
    with open(args.catalog, 'r', newline='', encoding='utf-8') as infile, \
         open(output_file, 'w', newline='', encoding='utf-8') as outfile:
        reader = csv.reader(infile)
        writer = csv.writer(outfile)

        header = next(reader)
        writer.writerow(header)
        full_path_index = header.index('FullPath')

        for row in reader:
            if len(row) <= full_path_index:
                writer.writerow(row)
                kept += 1
                continue
            path = row[full_path_index]
            if exclusion_patterns and matches(path, exclusion_patterns):
                removed += 1
                continue
            if inclusion_patterns and not matches(path, inclusion_patterns):
                removed += 1
                continue
            writer.writerow(row)
            kept += 1

    if args.output is None:
        os.replace(output_file, args.catalog)
        print(f"Kept {kept:,} rows, removed {removed:,} rows. Overwrote {args.catalog}.")
    else:
        print(f"Kept {kept:,} rows, removed {removed:,} rows. Written to {args.output}.")


if __name__ == '__main__':
    main()
