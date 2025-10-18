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
    """Creates a high-quality, aspect-ratio-preserved thumbnail, skipping if it already exists."""
    thumbnail_path = os.path.join(thumbnails_dir, f"{hash_str}.jpg")
    if os.path.exists(thumbnail_path):
        return True
    try:
        img = Image.open(path)
        img.thumbnail((512, 512))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.save(thumbnail_path, "JPEG", quality=85)
        return True
    except Exception:
        return False

def process_single_image_for_scan(full_path, identifier, min_size_kb, extensions):
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
            with Image.open(full_path) as img:
                width, height = img.size
        except Exception:
            return None

        exif_data = get_exif_data(full_path)

        return [
            identifier,
            full_path,
            os.path.basename(full_path),
            file_ext[1:],
            f"{file_size_kb:.2f}",
            'N/A',  # PerceptualHash placeholder
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

def create_thumbnail_and_phash(input_row, full_path_index, sha256_index, phash_index, thumbnails_dir):
    """Worker function to create a thumbnail and then generate a perceptual hash from it."""
    full_path = input_row[full_path_index]
    sha256_hex = input_row[sha256_index]

    if not create_thumbnail(full_path, thumbnails_dir, sha256_hex):
        input_row[phash_index] = 'N/A'
        return input_row

    thumbnail_path = os.path.join(thumbnails_dir, f"{sha256_hex}.jpg")
    try:
        with Image.open(thumbnail_path) as thumb_img:
            perceptual_hash_val = imagehash.dhash(thumb_img)
            perceptual_hash_str = str(perceptual_hash_val)
    except Exception:
        perceptual_hash_str = 'N/A'

    input_row[phash_index] = perceptual_hash_str
    return input_row

def scan_mode(identifier, root_dir, catalog_file, min_size_kb, extensions):
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
                'Identifier', 'FullPath', 'FileName', 'FileType', 'FileSizeKB', 'PerceptualHash', 'SHA256',
                'Width', 'Height', 'DateTime', 'Copyright', 'Artist', 'Software',
                'ImageDescription', 'Keywords'
            ]
            writer.writerow(header)
            csvfile.flush()

        with concurrent.futures.ProcessPoolExecutor() as executor:
            worker_func = partial(process_single_image_for_scan,
                                  identifier=identifier,
                                  min_size_kb=min_size_kb,
                                  extensions=extensions)
            
            results = executor.map(worker_func, files_to_process)
            
            for row_data in tqdm(results, total=len(files_to_process), desc="Scanning New Images"):
                if row_data:
                    writer.writerow(row_data)
                    csvfile.flush()

def thumbnail_mode(catalog_file, output_catalog_file, thumbnails_dir):
    """Generates thumbnails and perceptual hashes, creating a new, enriched catalog file."""
    os.makedirs(thumbnails_dir, exist_ok=True)

    try:
        with open(catalog_file, 'r', newline='', encoding='utf-8') as infile, \
             open(output_catalog_file, 'w', newline='', encoding='utf-8') as outfile:

            reader = csv.reader(infile)
            writer = csv.writer(outfile)

            header = next(reader)
            writer.writerow(header)

            full_path_index = header.index('FullPath')
            sha256_index = header.index('SHA256')
            phash_index = header.index('PerceptualHash')

            images_to_process = list(reader)
            if not images_to_process:
                print("No images found in the catalog file.")
                return

            print(f"Found {len(images_to_process)} images to process.")

            with concurrent.futures.ProcessPoolExecutor() as executor:
                worker_func = partial(create_thumbnail_and_phash,
                                      full_path_index=full_path_index,
                                      sha256_index=sha256_index,
                                      phash_index=phash_index,
                                      thumbnails_dir=thumbnails_dir)

                results = executor.map(worker_func, images_to_process)

                for output_row in tqdm(results, total=len(images_to_process), desc="Generating Thumbnails and Hashes"):
                    writer.writerow(output_row)

    except (IOError, StopIteration, ValueError) as e:
        print(f"Error: Could not read catalog file. {e}")

def main():
    parser = argparse.ArgumentParser(description="Scan for images and generate thumbnails.")
    parser.add_argument('--output-dir', type=str, default='output', help='The directory to store all output files.')
    subparsers = parser.add_subparsers(dest='mode', required=True, help='The mode to run in.')

    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan a directory to find files and create a catalog.')
    scan_parser.add_argument('identifier', type=str, help='A unique name for this scan, used to generate filenames.')
    scan_parser.add_argument('--directory', type=str, required=True, help='The root directory to scan.')
    scan_parser.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB to process.')
    scan_parser.add_argument('--extensions', type=str, default='jpg,jpeg,png,tiff,tif,psd', help='Comma-separated image extensions to scan.')

    # Thumbnail command
    thumb_parser = subparsers.add_parser('thumbnail', help='Generate thumbnails and perceptual hashes from a catalog.')
    thumb_parser.add_argument('identifier', type=str, help='The unique name for the scan, used to find the input catalog.')

    args = parser.parse_args()

    # --- Path Generation ---
    output_dir = args.output_dir
    thumbnails_dir = os.path.join(output_dir, 'thumbnails')
    catalog_file = os.path.join(output_dir, f"{args.identifier}.csv")
    final_catalog_file = os.path.join(output_dir, f"{args.identifier}_final.csv")
    
    os.makedirs(thumbnails_dir, exist_ok=True)

    extensions_set = set(args.extensions.lower().split(',')) if 'extensions' in args else set()

    if args.mode == 'scan':
        print(f"Starting scan '{args.identifier}'")
        print(f"Scanning directory: {args.directory}")
        print(f"Outputting catalog to: {catalog_file}")

        scan_mode(args.identifier, args.directory, catalog_file, args.min_size, extensions_set)
        
        print("\nScan complete.")
        print(f"Catalog file created at: {catalog_file}")

    elif args.mode == 'thumbnail':
        print(f"Generating thumbnails and hashes for scan '{args.identifier}'")
        print(f"Reading from: {catalog_file}")
        print(f"Thumbnails will be saved in: {thumbnails_dir}")
        print(f"Enriched catalog will be saved to: {final_catalog_file}")
        
        thumbnail_mode(catalog_file, final_catalog_file, thumbnails_dir)
        
        print("\nThumbnail and hash generation complete.")
        print(f"Enriched catalog file created at: {final_catalog_file}")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()