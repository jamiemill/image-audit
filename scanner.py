
import os
import argparse
import csv
from datetime import datetime
from PIL import Image, ExifTags
import imagehash
import exifread
from tqdm import tqdm
import multiprocessing

# Pillow has a limit to prevent decompression bombs. Set it to a large but sane value. 
Image.MAX_IMAGE_PIXELS = 500000000 

def get_exif_data(path):
    """Extracts a curated list of useful, identifying metadata from an image file."""
    try:
        with open(path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
            
            # Helper to get a clean, printable value from a tag.
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
        # The thumbnail method preserves aspect ratio. It modifies the image in-place.
        img.thumbnail((512, 512))
        
        # Convert to RGB if it's not, which is necessary for saving as JPEG. 
        if img.mode not in ('RGB', 'L'): # L is for grayscale
             img = img.convert('RGB')

        thumbnail_path = os.path.join(thumbnails_dir, f"{hash_str}.jpg")
        img.save(thumbnail_path, "JPEG", quality=85)
        return True
    except Exception:
        # This can happen for corrupted files or unsupported formats that sneak by the extension filter.
        return False

def scan_directory(drivename, root_dir, output_tsv, thumbnails_dir, min_size_kb, extensions):
    """Scans a directory, processes images, and logs data idempotently."""
    
    os.makedirs(thumbnails_dir, exist_ok=True)

    # --- Idempotency: Load already processed file paths ---
    processed_paths = set()
    is_new_file = not os.path.exists(output_tsv) or os.path.getsize(output_tsv) == 0
    if not is_new_file:
        try:
            with open(output_tsv, 'r', newline='', encoding='utf-8') as tsvfile:
                reader = csv.reader(tsvfile, delimiter='\t')
                header = next(reader) # Skip header
                full_path_index = header.index('FullPath')
                for row in reader:
                    if len(row) > full_path_index:
                        processed_paths.add(row[full_path_index])
            print(f"Found {len(processed_paths)} already processed files in the log.")
        except (IOError, StopIteration, ValueError) as e:
            print(f"Warning: Could not read existing log file. Starting fresh. Error: {e}")
            is_new_file = True # Treat as a new file if reading fails

    # --- File Discovery ---
    all_files_in_dir = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            all_files_in_dir.append(os.path.join(dirpath, filename))
    
    # Filter out files that have already been processed
    files_to_process = [p for p in all_files_in_dir if p not in processed_paths]

    if not files_to_process:
        print("No new files to process.")
        return

    print(f"Total files to process: {len(files_to_process)}")

    # --- Processing Loop ---
    with open(output_tsv, 'a', newline='', encoding='utf-8') as tsvfile:
        writer = csv.writer(tsvfile, delimiter='\t')
        
        if is_new_file:
            header = [
                'DriveName', 'FullPath', 'FileName', 'FileType', 'FileSizeKB', 'PerceptualHash', 
                'Width', 'Height', 'DateTime', 'Copyright', 'Artist', 'Software', 
                'ImageDescription', 'Keywords'
            ]
            writer.writerow(header)
            tsvfile.flush()

        for full_path in tqdm(files_to_process, desc="Scanning New Images"):
            try:
                file_ext = os.path.splitext(full_path)[1].lower()
                if not file_ext or file_ext[1:] not in extensions:
                    continue

                file_size_kb = os.path.getsize(full_path) / 1024
                if file_size_kb < min_size_kb:
                    continue

                width, height = 0, 0
                try:
                    img_for_hash = Image.open(full_path)
                    width, height = img_for_hash.size
                    hash_val = imagehash.dhash(img_for_hash)
                    hash_str = str(hash_val)
                except Exception:
                    continue
                
                if not create_thumbnail(full_path, thumbnails_dir, hash_str):
                    continue

                exif_data = get_exif_data(full_path)

                row = [
                    drivename,
                    full_path,
                    os.path.basename(full_path),
                    file_ext[1:],
                    f"{file_size_kb:.2f}",
                    hash_str,
                    width,
                    height,
                    exif_data['DateTime'],
                    exif_data['Copyright'],
                    exif_data['Artist'],
                    exif_data['Software'],
                    exif_data['ImageDescription'],
                    exif_data['Keywords'],
                ]
                writer.writerow(row)
                tsvfile.flush() # Flush after each write for safety

            except (IOError, OSError):
                continue

def main():
    parser = argparse.ArgumentParser(description="Recursively scan a directory for images, extract metadata, and create thumbnails.")
    parser.add_argument('--drivename', type=str, required=True, help='A name to identify the drive being scanned, e.g., \'WD_BLACK_1\'.')
    parser.add_argument('--directory', type=str, required=True, help='The root directory to scan.')
    parser.add_argument('--output-tsv', type=str, required=True, help='Path to the output TSV log file.')
    parser.add_argument('--thumbnails-dir', type=str, required=True, help='Directory to store generated thumbnails.')
    parser.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB to process (default: 100).')
    parser.add_argument('--extensions', type=str, default='jpg,jpeg,png,heic,tiff,tif,cr2,nef,arw,orf,rw2,pef,dng,psd', help='Comma-separated list of image extensions to scan (e.g., jpg,png,nef).')
    
    args = parser.parse_args()
    
    extensions_set = set(args.extensions.lower().split(','))
    
    print(f"Starting scan on drive: {args.drivename}")
    print(f"Scanning directory: {args.directory}")
    print(f"Allowed extensions: {', '.join(extensions_set)}")
    print(f"Minimum size: {args.min_size} KB")
    print(f"Logging to: {args.output_tsv}")
    print(f"Thumbnails in: {args.thumbnails_dir}")

    scan_directory(args.drivename, args.directory, args.output_tsv, args.thumbnails_dir, args.min_size, extensions_set)
    
    print("\nScan complete.")
    print(f"Log file created at: {args.output_tsv}")
    print(f"Thumbnails saved in: {args.thumbnails_dir}")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
