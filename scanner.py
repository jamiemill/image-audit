
import os
import argparse
import csv
from datetime import datetime
from PIL import Image, ExifTags
import imagehash
import exifread
from tqdm import tqdm

# Pillow has a limit to prevent decompression bombs. Set it to a large but sane value. 
Image.MAX_IMAGE_PIXELS = 500000000 

def get_exif_data(path):
    """Extracts key EXIF data from an image file using exifread."""
    try:
        with open(path, 'rb') as f:
            tags = exifread.process_file(f, details=False)
            return {
                'DateTime': tags.get('EXIF DateTimeOriginal', tags.get('Image DateTime', 'N/A')).printable,
                'CameraModel': tags.get('Image Model', 'N/A').printable,
                'LensModel': tags.get('EXIF LensModel', 'N/A').printable,
                'Artist': tags.get('Image Artist', 'N/A').printable,
                'Copyright': tags.get('Image Copyright', 'N/A').printable,
            }
    except Exception:
        return {
            'DateTime': 'N/A',
            'CameraModel': 'N/A',
            'LensModel': 'N/A',
            'Artist': 'N/A',
            'Copyright': 'N/A',
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

def scan_directory(root_dir, output_tsv, thumbnails_dir, min_size_kb, extensions):
    """Scans a directory, processes images, and logs data."""
    
    # Ensure output directories exist
    os.makedirs(thumbnails_dir, exist_ok=True)

    # Get a list of all files to process to have a total for tqdm
    files_to_process = []
    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            files_to_process.append(os.path.join(dirpath, filename))

    # Open the TSV file and write the header
    with open(output_tsv, 'w', newline='', encoding='utf-8') as tsvfile:
        writer = csv.writer(tsvfile, delimiter='\t')
        header = [
            'FullPath', 'FileName', 'FileType', 'FileSizeKB', 
            'PerceptualHash', 'DateTime', 'CameraModel', 'LensModel', 
            'Artist', 'Copyright'
        ]
        writer.writerow(header)

        # Process files with a progress bar
        for full_path in tqdm(files_to_process, desc="Scanning Images"):
            try:
                # 1. Filter by extension
                file_ext = os.path.splitext(full_path)[1].lower()
                if not file_ext or file_ext[1:] not in extensions:
                    continue

                # 2. Filter by size
                file_size_kb = os.path.getsize(full_path) / 1024
                if file_size_kb < min_size_kb:
                    continue

                # 3. Generate Perceptual Hash
                try:
                    # Use Pillow to open for hashing, as it supports more formats robustly
                    img_for_hash = Image.open(full_path)
                    hash_val = imagehash.dhash(img_for_hash)
                    hash_str = str(hash_val)
                except Exception:
                    # If hashing fails, we can't process this file
                    continue
                
                # 4. Create Thumbnail
                if not create_thumbnail(full_path, thumbnails_dir, hash_str):
                    # If thumbnail creation fails, skip logging this file
                    continue

                # 5. Get EXIF data
                exif_data = get_exif_data(full_path)

                # 6. Write to TSV
                row = [
                    full_path,
                    os.path.basename(full_path),
                    file_ext[1:],
                    f"{file_size_kb:.2f}",
                    hash_str,
                    exif_data['DateTime'],
                    exif_data['CameraModel'],
                    exif_data['LensModel'],
                    exif_data['Artist'],
                    exif_data['Copyright']
                ]
                writer.writerow(row)

            except (IOError, OSError):
                # Handle cases like broken symlinks or permission errors gracefully
                continue

def main():
    parser = argparse.ArgumentParser(description="Recursively scan a directory for images, extract metadata, and create thumbnails.")
    parser.add_argument('--directory', type=str, required=True, help='The root directory to scan.')
    parser.add_argument('--output-tsv', type=str, required=True, help='Path to the output TSV log file.')
    parser.add_argument('--thumbnails-dir', type=str, required=True, help='Directory to store generated thumbnails.')
    parser.add_argument('--min-size', type=int, default=100, help='Minimum file size in KB to process (default: 100).')
    parser.add_argument('--extensions', type=str, default='jpg,jpeg,png,heic,tiff,tif,cr2,nef,arw,orf,rw2,pef,dng', help='Comma-separated list of image extensions to scan (e.g., jpg,png,nef).')
    
    args = parser.parse_args()
    
    # Convert comma-separated extensions string to a set for faster lookups
    extensions_set = set(args.extensions.lower().split(','))
    
    print(f"Starting scan in: {args.directory}")
    print(f"Allowed extensions: {', '.join(extensions_set)}")
    print(f"Minimum size: {args.min_size} KB")
    print(f"Logging to: {args.output_tsv}")
    print(f"Thumbnails in: {args.thumbnails_dir}")

    scan_directory(args.directory, args.output_tsv, args.thumbnails_dir, args.min_size, extensions_set)
    
    print("\nScan complete.")
    print(f"Log file created at: {args.output_tsv}")
    print(f"Thumbnails saved in: {args.thumbnails_dir}")

if __name__ == "__main__":
    main()
