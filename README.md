# Image Audit

This script provides a multi-step process to scan drives for images, generate metadata and thumbnails, and browse the results.

## Step 1: Scan a Directory

This step scans a directory to find images and record their metadata into a catalog file. This initial scan is optimized for speed by deferring the calculation of perceptual hashes.

Provide a unique `identifier` for your scan. This will be used to name your output files.

    python3 scanner.py scan <your_identifier> --directory /path/to/your/drive

This will create `output/<your_identifier>.csv`.

## Step 2: Generate Thumbnails and Hashes

This step reads the catalog generated in Step 1, creates a thumbnail for each image, calculates a perceptual hash from the thumbnail, and saves the result to a new, final catalog file.

    python3 scanner.py thumbnail <your_identifier>

This will read `output/<your_identifier>.csv` and create:
-   `output/<your_identifier>_final.csv` (the enriched catalog)
-   Thumbnails inside `output/thumbnails/`

*You can use the optional `--output-dir` flag on either command to change the location of the `output` directory.*

## Step 3: Browse the Results

Before running the browser, you need to tell it which data file to load.

1.  Open `config.json`.
2.  Make sure the `csv_files` property points to your new, final catalog file.

    ```json
    {
      "csv_files": ["output/your_identifier_final.csv"]
    }
    ```

Now, you can start the local web server to view the browser interface.

    python3 -m http.server

Then open `http://localhost:8000/browser.html` in your web browser.

## Build for Distribution

    pyinstaller --onefile scanner.py
