# Image Audit

Scans drives for images, generates thumbnails and metadata, and provides a browser interface for searching the results.

## Step 0: Install

    pip3 install -r requirements.txt

If you're using a virtual environment:

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

---

## Step 1: Estimate (optional but recommended)

Before committing to a long scan, run `estimate` to get a quick picture of what's on the drive — how many files, likely duplicates, estimated thumbnail storage, and rough time estimates. This does **zero file content reads** and completes in seconds.

    python3 scanner.py estimate --directory /path/to/your/drive

Example output:

    Total candidate image files:       170,432
    Total size:                            850 GB
    Average file size:                     5.1 MB
    File types found:
      .jpg        142,000 files
      .tif         20,000 files
      .psd          8,432 files
    Unique file sizes:                  95,000
    Likely duplicates:                  75,432  (44.3%)
    Estimated thumbnail storage:         ~2.3 GB
    Current free disk space:            45.1 GB  ✓

    Estimated times on a spinning drive (--hdd mode):
      scan:                ~28 min – ~1.6 hrs
      thumbnail phase 1:   ~28 min – ~57 min
      thumbnail phase 2:   ~3.2 hrs – ~5.5 hrs  (95,000 unique files)

Re-run `estimate` after editing `exclusions.txt` to get a revised count and time estimate for only the files that will actually be processed.

---

## Step 2: Excluding directories

On first run, `estimate` or `scan` creates `exclusions.txt` in the current directory. Edit it to skip directories you don't want to scan (e.g. application libraries, system folders, old backups).

    # match by directory name anywhere in the tree
    iPhoto Library
    *.photoslibrary
    .Spotlight-V100

    # match by full path
    /Volumes/Lacie 10TB/Archive/Old Backups

Excluded directories are pruned during the directory walk, so their contents are never opened or counted at all. Both `estimate` and `scan` use the same `exclusions.txt`.

Use `--exclusions /path/to/file` to point to a different exclusions file.

---

## Step 3: Scan

Scans the directory, reads only image headers (dimensions + EXIF), and writes a catalog CSV. No thumbnailing yet. Supports resume — safe to interrupt and re-run.

    python3 scanner.py scan <identifier> --directory /path/to/your/drive

**For spinning hard drives, always add `--hdd`** to force single-worker sequential access. Multiple workers thrash the disk head and can make things dramatically slower.

    python3 scanner.py scan <identifier> --directory /path/to/your/drive --hdd

This creates `output/<identifier>.csv`.

---

## Step 4: Thumbnail

Reads the scan catalog, deduplicates files, creates thumbnails, computes perceptual hashes, and writes an enriched catalog. Supports resume — safe to interrupt and re-run.

    python3 scanner.py thumbnail <identifier>
    python3 scanner.py thumbnail <identifier> --hdd   # for spinning drives

**How it works — three phases:**

**Phase 1 — Quick dedup check.** Reads only the first 64KB of every file and hashes that together with the file size. This is enough to reliably identify duplicates (the first 64KB of a photo contains the full EXIF header — capture time, GPS, camera model — and the start of compressed image data; two different photos will essentially never match). Crucially, this costs the same for all file sizes: a 500MB TIFF and a 5MB JPEG both require one seek plus a tiny read. Files with matching quick-hashes are flagged as duplicates and excluded from phase 2.

**Phase 2 — Process unique files only.** For each file that passed the dedup check: streams the full file to compute SHA256, decodes the image with PIL to create a 256×256 thumbnail, and computes a perceptual hash from the thumbnail. Duplicate files never reach this phase — their SHA256 and perceptual hash are simply copied from whichever copy was processed. This means the expensive work (full file reads, PIL decode) only happens once per unique image, regardless of how many copies exist on the drive.

**Phase 3 — Write duplicate entries.** Writes a catalog row for every duplicate file, copying the SHA256 and perceptual hash from the canonical file processed in phase 2. This ensures every file on the drive has an entry in the final catalog, even if its thumbnail was not created separately.

Before phase 2 starts, the script prints a disk space estimate and warns if space looks tight. Add `--yes` to skip this prompt for unattended runs.

This creates:
- `output/<identifier>_final.csv` — the enriched catalog
- `output/thumbnails/` — one `.webp` thumbnail per unique image (named by SHA256)

---

## Step 5: Browse

1. Open `config.json` and point `csv_files` at your final catalog:

    ```json
    {
      "csv_files": ["output/your_identifier_final.csv"]
    }
    ```

2. Start the server:

        python3 -m http.server

3. Open `http://localhost:8000/browser.html`

---

## Options

| Flag | Commands | Description |
|------|----------|-------------|
| `--hdd` | scan, thumbnail | Optimise for spinning drives: forces single worker to avoid seek contention |
| `--max-workers N` | scan, thumbnail | Override worker count (default: CPU count; ignored if `--hdd` is set) |
| `--yes` | thumbnail | Skip the disk-space confirmation prompt |
| `--exclusions FILE` | estimate, scan | Path to exclusions file (default: `exclusions.txt`) |
| `--min-size KB` | estimate, scan | Minimum file size to include (default: 100 KB) |
| `--extensions` | estimate, scan | Comma-separated extensions (default: `jpg,jpeg,png,tiff,tif,psd`) |
| `--output-dir` | all | Change the output directory (default: `output/`) |

---

## Build for Distribution

    pyinstaller --onefile scanner.py
