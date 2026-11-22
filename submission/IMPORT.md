# Importing the submission into Google Sheets

Six CSV files in this folder become the six tabs of ONE Google Sheet.

## Steps (repeat for each CSV)
1. Create a new, empty Google Sheet (or open the one you are building).
2. **File → Import**.
3. **Upload** and choose the CSV file.
4. Import location: **Insert new sheet(s)**.
5. Separator type: **Detect automatically** (or Comma).
6. **UNCHECK "Convert text to numbers, dates and formulas"** — this keeps
   IDs, timestamps and leading zeros exactly as written.
7. Click **Import data**.
8. Rename the new tab to match the file name (see the exact names below).

## Exact tab names (in this order)
- `Startups`  ← `Startups.csv`
- `Products`  ← `Products.csv`
- `Research Papers`  ← `Research Papers.csv`
- `Jobs`  ← `Jobs.csv`
- `News`  ← `News.csv`
- `Entity Mapping Log`  ← `Entity Mapping Log.csv`

## Share the sheet
1. Click **Share** (top right).
2. Under **General access**, choose **Anyone with the link**.
3. Set the role to **Viewer**.
4. Click **Copy link** and submit that link.

The matching `.jsonl` files hold the full, untruncated records (CSV text
cells are capped at 45,000 characters for the Google Sheets cell limit).

The **Entity Mapping Log** tab contains only real mappings derived from
the live database (raw name → canonical). The resolver's synthetic test
corpus (expected vs actual, with accuracy) is kept separately in
`docs/entity_resolution_eval.csv` / `docs/entity_resolution_eval.md`.
