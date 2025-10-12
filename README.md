# Drive image scanner

## run

    python3 scanner.py \
    --drivename "WD_BLACK_1" \
    --directory /path/to/your/hard_drive \
    --output-tsv scanned-media/output.tsv \
    --thumbnails-dir scanned-media/thumbnails

## build for distro

    pyinstaller --onefile scanner.py
