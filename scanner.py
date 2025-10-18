import os
import argparse
import csv
from datetime import datetime
from PIL import Image, ExifTags
import imagehash
import exifread
from tqdm import tqdm
import concurrent.futures
from functools import partial
import multiprocessing
import hashlib

# Pillow has a limit to prevent decompression bombs. Set it to a large but sane value.
Image.MAX_IMAGE_PIXELS = 500000000

def get_exif_data(path):
    """Extracts a curated list of useful, identifying metadata from an image file."""
    try:
        with open(path, 'rb') as f:
            tags = exifread.process_file(f, details=False)

            def get_tag(tag_name, default='N/A'):
                val = tags.get(tag_name)
                return val.printable.strip() if val else default

            return {
                'DateTime': get_tag('EXIF DateTimeOriginal', get_tag('Image DateTime')),
                'Copyright': get_tag('Image Copyright'),
                'Artist': get_tag('Image Artist'),
                'Software': get_tag('Image Software'),
                'ImageDescription': get_tag('Image ImageDescription'),
                'Keywords': get_tag('Iptc.Application2.Keywords', get_tag('XMP-dc:Subject')),
            }
    except Exception:
        return {
            'DateTime': 'N/A',
            'Copyright': 'N/A',
            'Artist': 'N/A',
            'Software': 'N/A',
            'ImageDescription': 'N/A',
            'Keywords': 'N/A',
        }

def create_thumbnail(path, thumbnails_dir, hash_str):
    """Creates a high-quality, aspect-ratio-preserved thumbnail."""
    try:
        img = Image.open(path)
        img.thumbnail((512, 512))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        thumbnail_path = os.path.join(thumbnails_dir, f"{hash_str}.jpg")
        img.save(thumbnail_path, "JPEG", quality=85)
        return True
    except Exception:
        return False

def process_single_image_for_scan(full_path, drivename, min_size_kb, extensions):
    """
    Worker function to process a single image file for the scan mode.
    Returns a data row for the CSV on success, or None on failure/skip.
    """
    try:
        file_ext = os.path.splitext(full_path)[1].lower()
        if not file_ext or file_ext[1:] not in extensions:
            return None

        file_size_kb = os.path.getsize(full_path) / 1024
        if file_size_kb < min_size_kb:
            return None

        sha256_hash = hashlib.sha256()
        with open(full_path, 'rb') as f:
            for byte_block in iter(lambda: f.read(4096), b''):
                sha256_hash.update(byte_block)
        sha256_hex = sha256_hash.hexdigest()

        width, height = 0, 0
        try:
            img_for_hash = Image.open(full_path)
            width, height = img_for_hash.size
            perceptual_hash_val = imagehash.dhash(img_for_hash)
            perceptual_hash_str = str(perceptual_hash_val)
        except Exception:
            return None

        exif_data = get_exif_data(full_path)

        return [
            drivename,
            full_path,
            os.path.basename(full_path),
            file_ext[1:],
            f"{file_size_kb:.2f}",
            perceptual_hash_str,
            sha256_hex,
            width,
            height,
            exif_data['DateTime'],
            exif_data['Copyright'],
            exif_data['Artist'],
            exif_data['Software'],
            exif_data['ImageDescription'],
            exif_data['Keywords'],
        ]
    except (IOError, OSError):
        return None

def scan_mode(drivename, root_dir, catalog_file, min_size_kb, extensions):
    """Scans a directory, gathers image metadata, and saves it to a CSV catalog."""
    processed_paths = set()
    is_new_file = not os.path.exists(catalog_file) or os.path.getsize(catalog_file) == 0
    if not is_new_file:
        try:
            with open(catalog_file, 'r', newline='', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                header = next(reader)
                full_path_index = header.index('FullPath')
                for row in reader:
                    if len(row) > full_path_index:
                        processed_paths.add(row[full_path_index])
            print(f"Found {len(processed_paths)} files already in the catalog.")
        except (IOError, StopIteration, ValueError) as e:
            print(f"Warning: Could not read existing catalog file. Starting fresh. Error: {e}")
            is_new_file = True

    all_files_in_dir = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            all_files_in_dir.append(os.path.join(dirpath, filename))
    
    files_to_process = [p for p in all_files_in_dir if p not in processed_paths]

    if not files_to_process:
        print("No new files to process.")
        return

    print(f"Total files to process: {len(files_to_process)}")

    with open(catalog_file, 'a', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        
        if is_new_file:
            header = [
                'DriveName', 'FullPath', 'FileName', 'FileType', 'FileSizeKB', 'PerceptualHash', 'SHA256',
                'Width', 'Height', 'DateTime', 'Copyright', 'Artist', 'Software',
                'ImageDescription', 'Keywords'
            ]
            writer.writerow(header)
            csvfile.flush()

        with concurrent.futures.ProcessPoolExecutor() as executor:
            worker_func = partial(process_single_image_for_scan,
                                  drivename=drivename,
                                  min_size_kb=min_size_kb,
                                  extensions=extensions)
            
            results = executor.map(worker_func, files_to_process)
            
            for row_data in tqdm(results, total=len(files_to_process), desc="Scanning New Images"):
                if row_data:
                    writer.writerow(row_data)
                    csvfile.flush()

def thumbnail_mode(catalog_file, thumbnails_dir):
    """Generates thumbnails for all images listed in the catalog file."""
    os.makedirs(thumbnails_dir, exist_ok=True)

    try:
        with open(catalog_file, 'r', newline='', encoding='utf-8') as csvfile:
            reader = csv.reader(csvfile)
            header = next(reader)
            full_path_index = header.index('FullPath')
            sha256_index = header.index('SHA256')
            
            images_to_thumbnail = list(reader)
            if not images_to_thumbnail:
                print("No images found in the catalog file.")
                return

            print(f"Found {len(images_to_thumbnail)} images to thumbnail.")

            paths = [row[full_path_index] for row in images_to_thumbnail]
            hashes = [row[sha256_index] for row in images_to_thumbnail]

            with concurrent.futures.ProcessPoolExecutor() as executor:
                from itertools import repeat
                results = executor.map(create_thumbnail, paths, repeat(thumbnails_dir), hashes)

                for _ in tqdm(results, total=len(images_to_thumbnail), desc="Generating Thumbnails"):
                    pass

    except (IOError, StopIteration, ValueError) as e:
        print(f"Error: Could not read catalog file. {e}")

def main():
    parser = argparse.ArgumentParser(description="Scan for images and generate thumbnails in two steps.")
    parser.add_argument('--mode', type=str, choices=['scan', 'thumbnail'], default='scan', help='The mode to run in: "scan" to find files and create a catalog, or "thumbnail" to generate thumbnails from a catalog.')
    parser.add_argument('--catalog-file', type=str, default='catalog.csv', help='Path to the catalog CSV file.')
    parser.add_argument('--drivename', type=str, help='A name for the drive being scanned (required for "scan" mode).')
    parser.add_argument('--directory', type=str, help='The root directory to scan (required for "scan" mode).')
    parser.add_argument('--thumbnails-dir', type=str, help='Directory to store thumbnails (required for "thumbnail" mode).')
    parser.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB (for "scan" mode).')
    parser.add_argument('--extensions', type=str, default='jpg,jpeg,png,tiff,tif,psd', help='Comma-separated image extensions to scan.')

    args = parser.parse_args()
    extensions_set = set(args.extensions.lower().split(','))

    if args.mode == 'scan':
        if not all([args.drivename, args.directory]):
            parser.error("--drivename and --directory are required for 'scan' mode.")
        
        print(f"Starting scan on drive: {args.drivename}")
        print(f"Scanning directory: {args.directory}")
        print(f"Allowed extensions: {', '.join(extensions_set)}")
        print(f"Minimum size: {args.min_size} KB")
        print(f"Cataloging to: {args.catalog_file}")

        scan_mode(args.drivename, args.directory, args.catalog_file, args.min_size, extensions_set)
        
        print("\nScan complete.")
        print(f"Catalog file created at: {args.catalog_file}")

    elif args.mode == 'thumbnail':
        if not args.thumbnails_dir:
            parser.error("--thumbnails-dir is required for 'thumbnail' mode.")

        print(f"Generating thumbnails from: {args.catalog_file}")
        print(f"Thumbnails will be saved in: {args.thumbnails_dir}")
        
        thumbnail_mode(args.catalog_file, args.thumbnails_dir)
        
        print("\nThumbnail generation complete.")
        print(f"Thumbnails saved in: {args.thumbnails_dir}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()