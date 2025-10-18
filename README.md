# Image Audit

This script provides a two-step process to first catalog images from a drive and then generate thumbnails for them.

## Step 1: Scan and Catalog

This step scans a directory for images and creates a `catalog.csv` file with their metadata. You can review and edit this file before generating thumbnails.

    python3 scanner.py --mode scan \
    --drivename "WD_BLACK_1" \
    --directory /path/to/your/hard_drive \
    --catalog-file scanned-media/catalog.csv

## Step 2: Generate Thumbnails

This step reads the `catalog.csv` file and generates thumbnails for all the images listed in it.

    python3 scanner.py --mode thumbnail \
    --catalog-file scanned-media/catalog.csv \
    --thumbnails-dir scanned-media/thumbnails

## build for distro

    pyinstaller --onefile scanner.py

## browser

    python3 -m http.server
