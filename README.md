# CMS Hospital Dataset Downloader

Downloads all datasets tagged under the **Hospitals** theme from the [CMS Provider Data metastore](https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items), normalizes CSV column headers to `snake_case`, and saves the files locally. Designed to run daily : only files that have changed since the last run are re-downloaded.

## Requirements

- Python 3.9+
- `requests` and `urllib3` (see `requirements.txt`)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Copy `env.cms` to a file of your choice (e.g. `.env`, `my_config`, anything works), fill in the values, then export before running:

```bash
cp env.cms .env                 # or any name you prefer
export $(cat .env | grep -v '^#' | xargs)   # Windows: set each variable in System Environment Variables
```

## Configuration

Every setting can be supplied as a CLI flag or an environment variable. CLI flags take precedence when both are provided. All values are required — the script exits with a clear error if any are missing.

| CLI Flag | Env Variable | Description |
|---|---|---|
| `--metastore-url` | `METASTORE_URL` | CMS metastore API endpoint |
| `--output-dir` | `OUTPUT_DIR` | Directory to write CSV files |
| `--metadata-file` | `METADATA_FILE` | Run state tracking file |
| `--workers` | `WORKERS` | Parallel download threads |
| `--max-retries` | `MAX_RETRIES` | Retry attempts on failed requests |
| `--retry-backoff` | `RETRY_BACKOFF` | Backoff multiplier between retries (seconds) |
| `--log-level` | `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `--log-file` | `LOG_FILE` | Log file path |

## Usage

```bash
# Standard daily run using env vars
python cms_pipeline.py

# Override specific values with CLI flags
python cms_pipeline.py --output-dir /data/cms --workers 4

# Re-download everything regardless of what's changed
python cms_pipeline.py --force

# Run entirely from CLI flags, no env file needed
python cms_pipeline.py \
  --metastore-url https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items \
  --output-dir hospital_data \
  --metadata-file run_metadata.json \
  --workers 8 \
  --max-retries 3 \
  --retry-backoff 1.0 \
  --log-level INFO \
  --log-file cms_pipeline.log
```

## Scheduling

**Linux/macOS** : run at 6 AM daily:
```
0 6 * * * /path/to/venv/bin/python /path/to/cms_hospital_downloader.py
```

**Windows** : open Task Scheduler, create a Basic Task, set the trigger to Daily, and point the action at `python cms_hospital_downloader.py` in the project directory.

## Output

```
hospital_data/
  Complications_and_Deaths_-_Hospital.csv
  Patient_survey__HCAHPS__-_Hospital.csv
  Hospital_General_Information.csv
  ...

run_metadata.json    # tracks last run time and per-file modified dates
cms_downloader.log   # execution log
```

All CSV headers are converted to `snake_case` on download:

| Original | Normalized |
|---|---|
| `Patients' rating of the facility linear mean score` | `patients_rating_of_the_facility_linear_mean_score` |
| `HCAHPS Answer Percent` | `hcahps_answer_percent` |
| `# of Completed Surveys` | `of_completed_surveys` |

## How it works

1. Fetches the full CMS metastore catalog and filters for datasets where `theme == "Hospitals"` (currently ~76 datasets).
2. Collects all CSV distribution URLs across those datasets.
3. Checks `run_metadata.json` to see which files have been modified since the last run : unchanged files are skipped.
4. Downloads remaining files in parallel using a thread pool.
5. Replaces the header row in each CSV with snake_case column names.
6. Saves files to `hospital_data/` and updates the metadata for the next run.
