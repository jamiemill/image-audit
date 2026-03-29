import os
import argparse
import csv
import json
import shutil
import fnmatch
import logging
import warnings
from collections import defaultdict
from PIL import Image
import imagehash
try:
    from psd_tools import PSDImage as _PSDImage
except ImportError:
    _PSDImage = None
import exifread
from tqdm import tqdm
import concurrent.futures
from functools import partial
import multiprocessing
import hashlib

# Suppress exifread's verbose stderr logging (e.g. "no EXIF data found in PNG").
logging.getLogger('exifread').setLevel(logging.CRITICAL)

# Suppress PIL UserWarnings (e.g. "Corrupt EXIF data", "Truncated file").
# Real failures surface as exceptions and are caught + logged to the error CSV.
warnings.filterwarnings('ignore', category=UserWarning, module='PIL')

# Disable PIL's decompression bomb limit. The default (178M pixels) is too low
# for large TIFFs and PSDs in professional photography workflows. These are
# trusted local files, not user-uploaded content.
Image.MAX_IMAGE_PIXELS = None

# Pillow has a limit to prevent decompression bombs. Set it to a large but sane value.
Image.MAX_IMAGE_PIXELS = 500000000

QUICK_HASH_BYTES  = 65536         # 64KB read for fast duplicate detection
HASH_CHUNK_SIZE   = 65536         # 64KB chunks for streaming SHA256
SINGLE_READ_LIMIT = 50_000_000   # Files under 50 MB are loaded into memory once for both SHA256 and PIL

EXCLUSIONS_TEMPLATE = """\
# Exclusion patterns for scanner.py — one per line, # to comment.
#
# Patterns without a slash are matched against each directory name in the path.
#   iPhoto Library          <- excludes any folder named exactly this
#   *.photoslibrary         <- excludes any folder matching this glob
#
# Patterns with a slash are matched against the full path.
#   /Volumes/MyDrive/Old Backups/2003
#
# Common patterns (remove the leading # to enable):
# iPhoto Library
# *.photoslibrary
# .Spotlight-V100
# .Trashes
# .fseventsd
# System Volume Information
# $RECYCLE.BIN
# Thumbs.db
"""


def load_exclusions(exclusions_file):
    """Load exclusion patterns from file. Returns empty list if file doesn't exist."""
    if not os.path.exists(exclusions_file):
        return []
    patterns = []
    with open(exclusions_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                patterns.append(line)
    if patterns:
        print(f"Loaded {len(patterns)} exclusion pattern(s) from {exclusions_file}")
    return patterns


def load_inclusions(inclusions_file):
    """Load inclusion patterns from file. Returns empty list if file doesn't exist."""
    if not os.path.exists(inclusions_file):
        return []
    patterns = []
    with open(inclusions_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                patterns.append(line)
    if patterns:
        print(f"Loaded {len(patterns)} inclusion pattern(s) from {inclusions_file}")
    return patterns


def create_exclusions_template(exclusions_file):
    """Write a template exclusions file if one does not already exist."""
    if not os.path.exists(exclusions_file):
        with open(exclusions_file, 'w', encoding='utf-8') as f:
            f.write(EXCLUSIONS_TEMPLATE)
        print(f"Created exclusions template: {exclusions_file}  (edit to exclude directories)")


def is_excluded(path, patterns):
    """
    Returns True if path matches any exclusion pattern.
    Patterns without slashes are matched against each path component.
    Patterns with slashes are matched against the full normalised path.
    """
    if not patterns:
        return False
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


def get_psd_dimensions(f):
    """Read PSD/PSB image dimensions from the binary file header.
    PSD format: bytes 14-17 = height, bytes 18-21 = width (big-endian uint32).
    Works regardless of whether Photoshop 'Maximize Compatibility' was enabled."""
    f.seek(0)
    header = f.read(26)
    if len(header) < 26 or header[:4] != b'8BPS':
        raise ValueError("Not a valid PSD/PSB file")
    height = int.from_bytes(header[14:18], 'big')
    width  = int.from_bytes(header[18:22], 'big')
    return width, height


def get_exif_from_handle(f):
    """Extract EXIF metadata from an already-open file handle positioned at byte 0."""
    try:
        tags = exifread.process_file(f, details=False)

        def get_tag(name, default='N/A'):
            val = tags.get(name)
            return val.printable.strip() if val else default

        return {
            'DateTime':         get_tag('EXIF DateTimeOriginal', get_tag('Image DateTime')),
            'Copyright':        get_tag('Image Copyright'),
            'Artist':           get_tag('Image Artist'),
            'Software':         get_tag('Image Software'),
            'ImageDescription': get_tag('Image ImageDescription'),
            'Keywords':         get_tag('Iptc.Application2.Keywords', get_tag('XMP-dc:Subject')),
        }, None
    except Exception as e:
        return {k: 'N/A' for k in ['DateTime', 'Copyright', 'Artist', 'Software', 'ImageDescription', 'Keywords']}, \
               f"Could not read EXIF data: {e}"


def create_thumbnail(path, thumbnails_dir, sha256_hex):
    """Creates a thumbnail named by SHA256, skipping if it already exists."""
    thumbnail_path = os.path.join(thumbnails_dir, f"{sha256_hex}.webp")
    if os.path.exists(thumbnail_path):
        return True
    try:
        with Image.open(path) as img:
            img.thumbnail((256, 256), Image.Resampling.BILINEAR)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.save(thumbnail_path, "WEBP", quality=80, method=1)
        return True
    except Exception as pil_error:
        # For PSDs saved without Maximize Compatibility, PIL can't open them.
        # Detect PSD by magic bytes (works for both file paths and BytesIO).
        is_psd = False
        if isinstance(path, str):
            is_psd = os.path.splitext(path)[1].lower() in ('.psd', '.psb')
        elif hasattr(path, 'read'):
            path.seek(0)
            is_psd = path.read(4) == b'8BPS'
            path.seek(0)

        if is_psd:
            if _PSDImage is None:
                import sys
                print(f"\nThumbnail failed (PSD needs psd-tools): {path}"
                      f"\n  Run: pip install psd-tools", file=sys.stderr)
                return False
            try:
                if hasattr(path, 'seek'):
                    path.seek(0)
                psd = _PSDImage.open(path)
                img = psd.composite()
                if img is None:
                    raise ValueError("PSD composite returned None (no visible layers?)")
                img.thumbnail((256, 256), Image.Resampling.BILINEAR)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                img.save(thumbnail_path, "WEBP", quality=80, method=1)
                return True
            except Exception as e:
                import sys
                print(f"\nThumbnail failed (PSD): {path}\n  Reason: {e}", file=sys.stderr)
                return False

        import sys
        print(f"\nThumbnail failed: {path}\n  Reason: {pil_error}", file=sys.stderr)
        return False


def process_single_image_for_scan(full_path, identifier, min_size_kb, extensions):
    """
    Scan mode worker. Reads only the image header — no full file reads.
    SHA256 and perceptual hash are deferred to the thumbnail step.
    Returns (row_data, errors).
    """
    errors = []
    try:
        file_ext = os.path.splitext(full_path)[1].lower()
        if not file_ext or file_ext[1:] not in extensions:
            return None, []

        file_size_kb = os.path.getsize(full_path) / 1024
        if file_size_kb < min_size_kb:
            return None, []

        width, height = 0, 0
        exif_data = {k: 'N/A' for k in ['DateTime', 'Copyright', 'Artist', 'Software', 'ImageDescription', 'Keywords']}

        with open(full_path, 'rb') as f:
            # PIL lazy-loads: Image.open() reads only the file header to get dimensions.
            # No pixel data is decoded — typically 10-50KB, not the full file.
            try:
                with Image.open(f) as img:
                    width, height = img.size
            except Exception:
                # PIL can't open this file — common for PSDs saved without
                # Maximize Compatibility. Try reading dimensions from header.
                if file_ext[1:] in ('psd', 'psb'):
                    try:
                        width, height = get_psd_dimensions(f)
                    except Exception as e:
                        errors.append((full_path, f"Could not read PSD dimensions: {e}"))
                        return None, errors
                else:
                    errors.append((full_path, f"File format not recognized or image is corrupt"))
                    return None, errors

            # Rewind and pass the same open handle to exifread — avoids a second file open.
            f.seek(0)
            exif_data, exif_error = get_exif_from_handle(f)
            if exif_error:
                errors.append((full_path, exif_error))

        return [
            identifier, full_path, os.path.basename(full_path), file_ext[1:],
            f"{file_size_kb:.2f}",
            'N/A',  # PerceptualHash — computed in thumbnail step
            'N/A',  # SHA256 — computed in thumbnail step
            width, height,
            exif_data['DateTime'], exif_data['Copyright'], exif_data['Artist'],
            exif_data['Software'], exif_data['ImageDescription'], exif_data['Keywords'],
        ], errors

    except (IOError, OSError) as e:
        errors.append((full_path, f"IO/OS Error: {e}"))
        return None, errors


def _quick_key(path):
    """
    Returns (file_size_bytes, blake2b_of_first_64KB) for fast duplicate detection.
    Reading only 64KB catches virtually all duplicate photo files — the first 64KB of
    a JPEG contains the full EXIF header (capture time, GPS, camera) and the start of
    compressed image data, making accidental collision between different photos
    astronomically unlikely.
    Raises IOError/OSError on unreadable files.
    """
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        head = f.read(QUICK_HASH_BYTES)
    return (size, hashlib.blake2b(head, digest_size=16).hexdigest())


def process_unique_image(input_row, full_path_index, sha256_index, phash_index, thumbnails_dir, existing_thumbnails):
    """
    Thumbnail mode worker, called only for files that passed the quick-hash dedup check.
    Streams the full file to compute SHA256, creates a thumbnail, computes perceptual hash.
    existing_thumbnails is a frozenset of SHA256 hex strings loaded from disk at startup,
    used to skip thumbnail creation for images processed in prior runs without a per-file
    os.path.exists() call.
    """
    full_path = input_row[full_path_index]
    output_row = list(input_row)

    try:
        file_size = os.path.getsize(full_path)
        if file_size <= SINGLE_READ_LIMIT:
            # Small file: read once into memory, use for both SHA256 and PIL.
            # Avoids the second disk read that would otherwise be needed for PIL.
            with open(full_path, 'rb') as f:
                file_bytes = f.read()
            sha256_hex = hashlib.sha256(file_bytes).hexdigest()
            pil_source = __import__('io').BytesIO(file_bytes)
        else:
            # Large file (TIFFs, PSDs): stream for SHA256, then PIL re-opens from path.
            # The first read fills the OS page cache so the PIL re-open is typically free.
            sha256_hash = hashlib.sha256()
            with open(full_path, 'rb') as f:
                for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b''):
                    sha256_hash.update(chunk)
            sha256_hex = sha256_hash.hexdigest()
            pil_source = full_path
    except (IOError, OSError):
        output_row[sha256_index] = 'N/A'
        output_row[phash_index] = 'N/A'
        return output_row

    output_row[sha256_index] = sha256_hex
    thumbnail_path = os.path.join(thumbnails_dir, f"{sha256_hex}.webp")

    # Memory-first check (prior runs), then disk fallback (same-run SHA256 collisions)
    if sha256_hex not in existing_thumbnails and not os.path.exists(thumbnail_path):
        if not create_thumbnail(pil_source, thumbnails_dir, sha256_hex):
            output_row[phash_index] = 'N/A'
            return output_row

    try:
        with Image.open(thumbnail_path) as thumb_img:
            output_row[phash_index] = str(imagehash.dhash(thumb_img))
    except Exception:
        output_row[phash_index] = 'N/A'

    return output_row


def scan_mode(identifier, root_dir, catalog_file, min_size_kb, extensions, max_workers, exclusion_patterns=None, inclusion_patterns=None, fast=False):
    """Scans a directory, reads only image headers (fast), saves metadata to a CSV catalog.
    In fast mode, skips all file opens — records only path, size, and extension."""
    processed_paths = set()
    is_new_file = not os.path.exists(catalog_file) or os.path.getsize(catalog_file) == 0
    if not is_new_file:
        try:
            with open(catalog_file, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)
                fpi = header.index('FullPath')
                for row in reader:
                    if len(row) > fpi:
                        processed_paths.add(row[fpi])
            print(f"Found {len(processed_paths):,} files already in catalog.")
        except (IOError, StopIteration, ValueError) as e:
            print(f"Warning: could not read existing catalog, starting fresh. ({e})")
            is_new_file = True


    print("Walking directory tree...")
    all_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Prune excluded directories in-place — os.walk will not descend into them.
        if exclusion_patterns:
            dirnames[:] = [d for d in dirnames
                           if not is_excluded(os.path.join(dirpath, d), exclusion_patterns)]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            if exclusion_patterns and is_excluded(full_path, exclusion_patterns):
                continue
            if inclusion_patterns and not is_excluded(full_path, inclusion_patterns):
                continue
            # Pre-filter by extension and size here, not in the worker.
            # Files that fail these checks would otherwise never land in the catalog
            # or error log, making them invisible to resume and retried on every run.
            file_ext = os.path.splitext(filename)[1].lower()
            if not file_ext or file_ext[1:] not in extensions:
                continue
            try:
                size_kb = os.path.getsize(full_path) / 1024
                if size_kb < min_size_kb:
                    continue
            except OSError:
                continue
            all_files.append((full_path, size_kb))

    files_to_process = [(p, s) for p, s in all_files if p not in processed_paths]
    if not files_to_process:
        print("No new files to process.")
        return

    print(f"Files to process: {len(files_to_process):,}")

    output_dir = os.path.dirname(catalog_file)

    with open(catalog_file, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)

        if is_new_file:
            writer.writerow(['Identifier', 'FullPath', 'FileName', 'FileType', 'FileSizeKB',
                             'PerceptualHash', 'SHA256', 'Width', 'Height', 'DateTime',
                             'Copyright', 'Artist', 'Software', 'ImageDescription', 'Keywords'])
            csvfile.flush()

        if fast:
            for i, (full_path, size_kb) in enumerate(tqdm(files_to_process, desc="Scanning (fast)")):
                filename = os.path.basename(full_path)
                ext = os.path.splitext(filename)[1].lstrip('.').upper()
                writer.writerow([identifier, full_path, filename, ext, f"{size_kb:.1f}",
                                 'N/A', 'N/A', 'N/A', 'N/A', 'N/A',
                                 'N/A', 'N/A', 'N/A', 'N/A', 'N/A'])
                if i % 500 == 0:
                    csvfile.flush()
        else:
            error_log = os.path.join(output_dir, f"{identifier}_errors.csv")
            is_new_error = not os.path.exists(error_log) or os.path.getsize(error_log) == 0
            with open(error_log, 'a', newline='', encoding='utf-8') as errorfile:
                error_writer = csv.writer(errorfile)
                if is_new_error:
                    error_writer.writerow(['FullPath', 'Error'])
                    errorfile.flush()

                paths_only = [p for p, _ in files_to_process]
                with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
                    worker = partial(process_single_image_for_scan,
                                     identifier=identifier, min_size_kb=min_size_kb, extensions=extensions)
                    for i, (row_data, errors) in enumerate(
                            tqdm(executor.map(worker, paths_only),
                                 total=len(paths_only), desc="Scanning")):
                        if row_data:
                            writer.writerow(row_data)
                        for path, msg in errors:
                            error_writer.writerow([path, msg])
                        if i % 100 == 0:
                            csvfile.flush()
                            errorfile.flush()
                errorfile.flush()

        csvfile.flush()


def thumbnail_mode(catalog_file, output_catalog_file, thumbnails_dir, max_workers, yes=False):
    """
    Generates thumbnails and perceptual hashes, producing an enriched catalog.

    Three phases:
      1. Quick-hash every file (reads only the first 64KB) to detect duplicates cheaply.
         Only unique files proceed to phase 2; duplicates copy results from their canonical.
      2. Process unique files in parallel: stream full SHA256, create thumbnail, compute phash.
      3. Write duplicate rows with SHA256/phash copied from their canonical file.

    Supports resume: re-running will skip files already written to the output catalog.
    """
    os.makedirs(thumbnails_dir, exist_ok=True)

    # Load existing thumbnail SHA256s into memory — avoids a per-file os.path.exists()
    # call for thumbnails already created in prior runs.
    existing_thumbnails = frozenset(
        os.path.splitext(f)[0] for f in os.listdir(thumbnails_dir) if f.endswith('.webp')
    )
    print(f"Found {len(existing_thumbnails):,} existing thumbnails.")

    # Resume: skip files already written to the output catalog.
    # Also preload SHA256/phash for already-processed files so phase 3 can
    # find canonical data even if it was written in a prior interrupted run.
    processed_paths   = set()
    preloaded_results = {}  # FullPath -> output row (for phase 3 resume)
    is_new_output = not os.path.exists(output_catalog_file) or os.path.getsize(output_catalog_file) == 0
    if not is_new_output:
        try:
            with open(output_catalog_file, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                hdr = next(reader)
                fpi = hdr.index('FullPath')
                for row in reader:
                    if len(row) > fpi:
                        processed_paths.add(row[fpi])
                        preloaded_results[row[fpi]] = row
            print(f"Resuming: {len(processed_paths):,} already processed.")
        except Exception:
            is_new_output = True

    try:
        with open(catalog_file, 'r', newline='', encoding='utf-8') as infile:
            reader = csv.reader(infile)
            header = next(reader)
            full_path_index = header.index('FullPath')
            sha256_index    = header.index('SHA256')
            phash_index     = header.index('PerceptualHash')
            images_to_process = [
                r for r in reader
                if len(r) > full_path_index and r[full_path_index] not in processed_paths
            ]
    except (IOError, StopIteration, ValueError) as e:
        print(f"Error reading catalog: {e}")
        return

    if not images_to_process:
        print("No images to process.")
        return

    print(f"Found {len(images_to_process):,} images to process.")

    # --- Phase 1: Quick-hash all files to detect duplicates (reads only 64KB per file) ---
    # Checkpoint: if phase 1 completed in a prior run, reload its results from disk
    # rather than re-running 14+ hours of quick-hashing.
    checkpoint_file = output_catalog_file + '.phase1.json'
    unique_rows     = None
    duplicate_pairs = None

    if os.path.exists(checkpoint_file):
        print(f"\nFound phase 1 checkpoint — loading instead of re-running phase 1...")
        try:
            with open(checkpoint_file, 'r', encoding='utf-8') as f:
                cp = json.load(f)
            # Filter out rows already processed by a prior phase 2 run.
            unique_rows     = [r for r in cp['unique_rows']
                               if r[full_path_index] not in processed_paths]
            duplicate_pairs = [tuple(pair) for pair in cp['duplicate_pairs']]
            print(f"  Unique files remaining:  {len(unique_rows):,}")
            print(f"  Duplicate pairs:         {len(duplicate_pairs):,}")
        except Exception as e:
            print(f"  Warning: could not load checkpoint ({e}), re-running phase 1.")
            os.remove(checkpoint_file)
            unique_rows = duplicate_pairs = None

    if unique_rows is None:
        print(f"\nPhase 1: Quick-hashing {len(images_to_process):,} files to detect duplicates...")
        quick_hash_to_row = {}
        unique_rows       = []
        duplicate_pairs   = []

        for row in tqdm(images_to_process, desc="Quick-hashing"):
            path = row[full_path_index]
            try:
                qkey = _quick_key(path)
            except (IOError, OSError):
                unique_rows.append(row)
                continue

            if qkey not in quick_hash_to_row:
                quick_hash_to_row[qkey] = row
                unique_rows.append(row)
            else:
                duplicate_pairs.append((row, quick_hash_to_row[qkey]))

        print(f"  Unique files:          {len(unique_rows):,}")
        print(f"  Duplicates (skipped):  {len(duplicate_pairs):,}")

        # Save checkpoint so phase 1 is never repeated if phase 2 is interrupted.
        try:
            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump({'unique_rows': unique_rows,
                           'duplicate_pairs': [[d, c] for d, c in duplicate_pairs]}, f)
            print(f"  Phase 1 checkpoint saved: {checkpoint_file}")
        except Exception as e:
            print(f"  Warning: could not save checkpoint ({e}). Continuing without it.")

    # --- Space check before the expensive work ---
    THUMB_KB_ESTIMATE = 25
    # Upper bound: assume none of the unique files have existing thumbnails yet.
    # (We can't know for sure until we compute SHA256 in the worker.)
    est_new = max(0, len(unique_rows) - len(existing_thumbnails))
    est_storage_gb = est_new * THUMB_KB_ESTIMATE / (1024 * 1024)
    print(f"\nSpace estimate:")
    print(f"  Unique files to process:    {len(unique_rows):,}")
    print(f"  Existing thumbnails:        {len(existing_thumbnails):,}  (reused from prior runs)")
    print(f"  New thumbnails (est max):   ~{est_new:,}  ({_format_size(est_storage_gb)} at ~{THUMB_KB_ESTIMATE} KB each)")
    try:
        free_gb = shutil.disk_usage(thumbnails_dir).free / (1024 ** 3)
        if est_storage_gb > free_gb * 0.85:
            print(f"  Free disk space:            {free_gb:.1f} GB  ⚠  may not be enough")
            if not yes:
                try:
                    resp = input("\n  Proceed anyway? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    resp = 'n'
                if resp != 'y':
                    print("Aborted.")
                    return
        else:
            print(f"  Free disk space:            {free_gb:.1f} GB  ✓")
    except Exception:
        pass

    # --- Phase 2: Process unique files ---
    print(f"\nPhase 2: Thumbnailing {len(unique_rows):,} unique files...")
    # Seed with preloaded results so phase 3 can find canonicals processed in prior runs.
    results_by_path = dict(preloaded_results)

    with open(output_catalog_file, 'a', newline='', encoding='utf-8') as outfile:
        writer = csv.writer(outfile)
        if is_new_output:
            writer.writerow(header)

        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            worker = partial(process_unique_image,
                             full_path_index=full_path_index,
                             sha256_index=sha256_index,
                             phash_index=phash_index,
                             thumbnails_dir=thumbnails_dir,
                             existing_thumbnails=existing_thumbnails)

            for i, (input_row, output_row) in enumerate(
                    zip(unique_rows,
                        tqdm(executor.map(worker, unique_rows),
                             total=len(unique_rows), desc="Thumbnailing"))):
                writer.writerow(output_row)
                results_by_path[input_row[full_path_index]] = output_row
                if i % 100 == 0:
                    outfile.flush()

        # --- Phase 3: Write duplicate rows, copying SHA256/phash from their canonical ---
        if duplicate_pairs:
            print(f"\nPhase 3: Writing {len(duplicate_pairs):,} duplicate entries...")
            for dup_row, canonical_row in duplicate_pairs:
                out = list(dup_row)
                canonical = results_by_path.get(canonical_row[full_path_index])
                if canonical:
                    out[sha256_index] = canonical[sha256_index]
                    out[phash_index]  = canonical[phash_index]
                writer.writerow(out)
            outfile.flush()

    # Phase 3 complete — checkpoint no longer needed.
    if os.path.exists(checkpoint_file):
        os.remove(checkpoint_file)
        print(f"Phase 1 checkpoint removed.")


def _format_size(size_gb):
    if size_gb < 1:
        return f"~{size_gb * 1024:.0f} MB"
    return f"~{size_gb:.1f} GB"


def _format_duration(minutes):
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"~{int(minutes)} min"
    hours = minutes / 60
    if hours < 24:
        return f"~{hours:.1f} hrs"
    return f"~{hours / 24:.1f} days"


def estimate_mode(root_dir, min_size_kb, extensions, exclusion_patterns=None, inclusion_patterns=None):
    """
    Zero-read pre-flight analysis using only filesystem metadata (no file content reads).
    Walks the directory, counts candidate image files, estimates duplicates by file size,
    and gives tight time estimates based on actual extension breakdown.
    """
    # PIL decode time estimates per file by extension (seconds).
    # JPEGs are fast (lossy compressed, hardware-accelerated paths in libjpeg).
    # TIFFs and PSDs can be very slow — an uncompressed 200MP TIFF may take 10–30 s.
    PIL_SECS = {
        'jpg': 0.3, 'jpeg': 0.3,
        'png':  1.0,
        'tif':  6.0, 'tiff': 6.0,
        'psd': 10.0,
    }
    PIL_SECS_DEFAULT = 1.0

    print("Walking directory (metadata only — no file content reads)...")
    all_files = []  # (size_bytes, ext)
    for dirpath, dirnames, filenames in os.walk(root_dir):
        if exclusion_patterns:
            dirnames[:] = [d for d in dirnames
                           if not is_excluded(os.path.join(dirpath, d), exclusion_patterns)]
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if not ext or ext[1:] not in extensions:
                continue
            path = os.path.join(dirpath, filename)
            if exclusion_patterns and is_excluded(path, exclusion_patterns):
                continue
            if inclusion_patterns and not is_excluded(path, inclusion_patterns):
                continue
            try:
                size = os.path.getsize(path)
                if size / 1024 >= min_size_kb:
                    all_files.append((size, ext[1:]))
            except OSError:
                pass

    total = len(all_files)
    if total == 0:
        print("No candidate image files found.")
        return

    all_sizes   = [s for s, _ in all_files]
    total_gb    = sum(all_sizes) / (1024 ** 3)
    avg_mb      = (total_gb * 1024) / total

    # Extension breakdown
    ext_counts = defaultdict(int)
    for _, ext in all_files:
        ext_counts[ext] += 1

    # Group by exact file size as a duplicate proxy.
    size_counts = defaultdict(int)
    for s, _ in all_files:
        size_counts[s] += 1
    unique_sizes = len(size_counts)
    likely_dupes = sum(c - 1 for c in size_counts.values() if c > 1)

    # Total data volume of unique files (each unique size counted once).
    unique_total_gb = sum(size_counts.keys()) / (1024 ** 3)

    # Weighted PIL time based on actual extension mix.
    # Scale to unique_sizes since only unique files get thumbnailed.
    total_pil_secs = sum(PIL_SECS.get(ext, PIL_SECS_DEFAULT) * count
                         for ext, count in ext_counts.items())
    avg_pil_secs   = total_pil_secs / total
    est_pil_min    = avg_pil_secs * unique_sizes / 60

    # Thumbnail storage: one 256×256 WebP per unique file, ~25 KB each.
    THUMB_KB_ESTIMATE = 25
    est_thumb_gb = unique_sizes * THUMB_KB_ESTIMATE / (1024 * 1024)

    print(f"\n{'=' * 54}")
    print(f"  Total candidate image files:  {total:>10,}")
    print(f"  Total size on disk:           {total_gb:>9.1f} GB")
    print(f"  Average file size:            {avg_mb:>9.1f} MB")
    print(f"  File types found:")
    for ext in sorted(ext_counts, key=lambda e: -ext_counts[e]):
        print(f"    .{ext:<8}  {ext_counts[ext]:>8,} files")
    print(f"{'=' * 54}")
    print(f"  Duplicate estimate (by file size — no file reads):")
    print(f"    Unique file sizes:          {unique_sizes:>10,}")
    print(f"    Likely duplicates:          {likely_dupes:>10,}  ({100 * likely_dupes / total:.1f}%)")
    print(f"    Estimated unique images:    {unique_sizes:>10,} – {total:>10,}")
    print(f"    Data to read (unique only): {unique_total_gb:>9.1f} GB")
    print(f"{'=' * 54}")
    print(f"  Thumbnail storage estimate:")
    print(f"    ~{unique_sizes:,} thumbnails × ~{THUMB_KB_ESTIMATE} KB  =  {_format_size(est_thumb_gb)}")
    try:
        free_gb = shutil.disk_usage('.').free / (1024 ** 3)
        marker  = "✓" if free_gb > est_thumb_gb * 1.5 else "⚠  may be tight"
        print(f"    Current free disk space:    {free_gb:>9.1f} GB  {marker}")
    except Exception:
        pass
    print(f"{'=' * 54}")

    # Time estimates for a spinning drive, single worker (--hdd).
    #
    # Both scan and quick-hash are seek-dominated (~10ms/file on HDD, read time negligible).
    # Scan reads PIL header + exifread (~10–50 KB); quick-hash reads 64 KB — essentially equal.
    # Same range used for both: ~50–120 files/sec.
    scan_fast  = total / 120
    scan_slow  = total / 50
    qhash_fast = total / 120
    qhash_slow = total / 50
    #
    # Thumbnail phase 2: unique files only.
    #   I/O: full file read for SHA256 at ~100 MB/s.
    #   PIL: weighted by actual extension mix (computed above).
    #   ±30% on each to account for real-world variability.
    io_min     = (unique_total_gb * 1024) / 100 / 60
    thumb_fast = io_min + est_pil_min * 0.7
    thumb_slow = io_min * 1.3 + est_pil_min * 1.3

    print(f"\n  Estimated times on a spinning drive (--hdd mode):")
    print(f"    estimate:            seconds (this step)")
    print(f"    scan:                {_format_duration(scan_fast)} – {_format_duration(scan_slow)}")
    print(f"    thumbnail phase 1:   {_format_duration(qhash_fast)} – {_format_duration(qhash_slow)}  (quick-hash, 64 KB/file)")
    print(f"    thumbnail phase 2:   {_format_duration(thumb_fast)} – {_format_duration(thumb_slow)}  ({unique_sizes:,} unique files, {unique_total_gb:.1f} GB to read)")
    print(f"\n  Use --hdd on scan and thumbnail commands for spinning drives.")
    print(f"{'=' * 54}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Scan drives for images, generate thumbnails, and build a searchable catalog.")
    parser.add_argument('--output-dir', type=str, default='output', help='Directory for all output files.')
    subparsers = parser.add_subparsers(dest='mode', required=True)

    # estimate
    est = subparsers.add_parser('estimate',
        help='Pre-flight: count files, estimate duplicates and processing time. No file reads.')
    est.add_argument('--directory', type=str, required=True, help='Directory to analyse.')
    est.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB.')
    est.add_argument('--extensions', type=str, default='jpg,jpeg,png,tiff,tif,psd',
                     help='Comma-separated extensions to count.')
    est.add_argument('--exclusions', type=str, default='exclusions.txt',
                     help='Path to exclusions file (default: exclusions.txt).')
    est.add_argument('--inclusions', type=str, default=None,
                     help='Path to inclusions file. If set, only matching files are counted.')

    # scan
    scan_p = subparsers.add_parser('scan',
        help='Scan a directory and build a metadata catalog (fast: reads image headers only).')
    scan_p.add_argument('identifier', type=str, help='Unique name for this scan.')
    scan_p.add_argument('--directory', type=str, required=True)
    scan_p.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB.')
    scan_p.add_argument('--extensions', type=str, default='jpg,jpeg,png,tiff,tif,psd')
    scan_p.add_argument('--max-workers', type=int, default=None,
                        help='Worker processes (default: CPU count). Ignored when --hdd is set.')
    scan_p.add_argument('--hdd', action='store_true',
                        help='Optimise for spinning drives: forces a single worker to avoid seek contention.')
    scan_p.add_argument('--exclusions', type=str, default='exclusions.txt',
                        help='Path to exclusions file (default: exclusions.txt).')
    scan_p.add_argument('--inclusions', type=str, default=None,
                        help='Path to inclusions file. If set, only matching files are scanned.')
    scan_p.add_argument('--fast', action='store_true',
                        help='Skip file opens entirely: record only path, size, and extension. No dimensions or EXIF.')

    # thumbnail
    thumb_p = subparsers.add_parser('thumbnail',
        help='Generate thumbnails and hashes from a catalog. Deduplicates and supports resume.')
    thumb_p.add_argument('identifier', type=str)
    thumb_p.add_argument('--max-workers', type=int, default=None,
                         help='Worker processes (default: CPU count). Ignored when --hdd is set.')
    thumb_p.add_argument('--hdd', action='store_true',
                         help='Optimise for spinning drives: forces a single worker.')
    thumb_p.add_argument('--yes', action='store_true',
                         help='Skip the disk-space confirmation prompt (for unattended runs).')

    args = parser.parse_args()
    output_dir     = args.output_dir
    extensions_set = set(args.extensions.lower().split(',')) if hasattr(args, 'extensions') else set()
    exclusions     = load_exclusions(args.exclusions) if hasattr(args, 'exclusions') else []
    inclusions     = load_inclusions(args.inclusions) if hasattr(args, 'inclusions') and args.inclusions else []

    if args.mode == 'estimate':
        create_exclusions_template(args.exclusions)
        estimate_mode(args.directory, args.min_size, extensions_set,
                      exclusion_patterns=exclusions, inclusion_patterns=inclusions)

    elif args.mode == 'scan':
        max_workers  = 1 if args.hdd else args.max_workers
        catalog_file = os.path.join(output_dir, f"{args.identifier}.csv")
        os.makedirs(output_dir, exist_ok=True)
        create_exclusions_template(args.exclusions)
        print(f"Scan '{args.identifier}'  →  {args.directory}")
        if args.hdd:
            print("HDD mode: single worker, sequential disk access.")
        if args.fast:
            print("Fast mode: skipping file opens, no dimensions or EXIF.")
        if inclusions:
            print(f"Inclusion mode: only processing files matching {len(inclusions)} pattern(s).")
        scan_mode(args.identifier, args.directory, catalog_file, args.min_size, extensions_set,
                  max_workers, exclusion_patterns=exclusions, inclusion_patterns=inclusions, fast=args.fast)
        print(f"\nScan complete. Catalog: {catalog_file}")

    elif args.mode == 'thumbnail':
        max_workers      = 1 if args.hdd else args.max_workers
        thumbnails_dir   = os.path.join(output_dir, 'thumbnails')
        catalog_file     = os.path.join(output_dir, f"{args.identifier}.csv")
        final_catalog    = os.path.join(output_dir, f"{args.identifier}_final.csv")
        print(f"Thumbnailing '{args.identifier}'")
        if args.hdd:
            print("HDD mode: single worker, sequential disk access.")
        thumbnail_mode(catalog_file, final_catalog, thumbnails_dir, max_workers, yes=args.yes)
        print(f"\nComplete. Enriched catalog: {final_catalog}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
