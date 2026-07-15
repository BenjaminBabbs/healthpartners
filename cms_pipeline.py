#!/usr/bin/env python3
"""
Download all CMS "Hospitals" datasets to local CSVs.

Tracks run state in run_metadata.json so only changed files are re-fetched.
Scheduled to run daily.

Author: Benjamin Bassey
See README.md for setup instructions and env.cms for required environment variables.
"""

import csv
import io
import json
import logging
import os
import re
import sys
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Logger is declared here so all functions can use it.
# Handlers are not attached until main() resolves the log config.
log = logging.getLogger(__name__)


def resolve_config(args):
    log.info("Resolving configuration from CLI args and environment variables...")

    # CLI args take precedence over env vars; fail if neither provides a value.
    def pick(arg_value, env_key):
        return arg_value if arg_value is not None else os.getenv(env_key)

    resolved = {
        "metastore_url":  pick(args.metastore_url,  "METASTORE_URL"),
        "output_dir":     pick(args.output_dir,      "OUTPUT_DIR"),
        "metadata_file":  pick(args.metadata_file,   "METADATA_FILE"),
        "workers":        pick(args.workers,          "WORKERS"),
        "max_retries":    pick(args.max_retries,      "MAX_RETRIES"),
        "retry_backoff":  pick(args.retry_backoff,    "RETRY_BACKOFF"),
    }

    missing = [key for key, value in resolved.items() if value is None]
    if missing:
        readable = ", ".join(missing)
        raise EnvironmentError(f"Missing required config: {readable} — set via CLI flag or env var")

    log.info("Configuration resolved successfully")
    return {
        "metastore_url":  resolved["metastore_url"],
        "output_dir":     Path(resolved["output_dir"]),
        "metadata_file":  Path(resolved["metadata_file"]),
        "workers":        int(resolved["workers"]),
        "max_retries":    int(resolved["max_retries"]),
        "retry_backoff":  float(resolved["retry_backoff"]),
    }


def create_session(max_retries, retry_backoff):
    log.info(f"Creating HTTP session with {max_retries} retries and {retry_backoff}s backoff")
    session = requests.Session()
    retry_policy = Retry(
        total=max_retries,
        backoff_factor=retry_backoff,
        status_forcelist=[429, 500, 502, 503, 504],  # rate limit + server errors
    )
    session.mount("https://", HTTPAdapter(max_retries=retry_policy))
    return session


def to_snake_case(column_name):
    # drop apostrophes before replacing other special chars so "Patients'" becomes "Patients" not "Patients_"
    column_name = re.sub(r"[''`\u2018\u2019]", "", column_name.strip())
    column_name = re.sub(r"[^\w\s]", " ", column_name)
    column_name = re.sub(r"[\s_]+", "_", column_name.strip()).strip("_")
    result = column_name.lower()
    log.debug(f"Converted column name to snake_case: {result}")
    return result


def normalize_headers(text):
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        log.warning("File appears to be empty — skipping header normalization")
        return text

    snake = [to_snake_case(h) for h in headers]
    log.debug(f"Normalized {len(snake)} column headers to snake_case")

    out = io.StringIO()
    csv.writer(out).writerow(snake)
    first_line = out.getvalue().rstrip("\r\n")

    # Preserve the original file's line endings instead of re-serializing the whole file
    cut = text.index("\n") if "\n" in text else len(text)
    return first_line + text[cut:]


def get_hospital_datasets(session, metastore_url):
    log.info("Fetching CMS dataset catalog...")
    try:
        response = session.get(metastore_url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Failed to fetch dataset catalog: {e}")
        return []

    datasets = []
    for item in response.json():
        # theme can come back as a string, a list of strings, or a list of {"data": "..."} dicts
        themes = item.get("theme", [])
        if not isinstance(themes, list):
            themes = [themes]
        themes = [t["data"] if isinstance(t, dict) else t for t in themes]
        if any("hospitals" in str(t).lower() for t in themes):
            datasets.append(item)

    log.info(f"Found {len(datasets)} Hospital datasets")
    return datasets


def get_csv_urls(dataset):
    title = dataset.get("title", "unknown")
    dataset_id = dataset.get("identifier", "")
    fallback_modified = dataset.get("modified", "")
    urls = []

    for distribution in dataset.get("distribution", []):
        distribution_data = distribution.get("data", distribution) if isinstance(distribution, dict) else {}
        url = distribution_data.get("downloadURL", "") or ""
        media_type = distribution_data.get("mediaType", "") or ""
        modified = distribution_data.get("modified", fallback_modified) or fallback_modified

        if url and ("csv" in media_type.lower() or url.lower().endswith(".csv")):
            urls.append({"url": url, "modified": modified, "title": title, "id": dataset_id})

    log.debug(f"Found {len(urls)} CSV distributions for dataset: {title}")
    return urls


def download_file(dist, output_dir, known, force, session):
    url = dist["url"]
    modified = dist.get("modified", "")
    title = dist["title"]

    if not force and url in known:
        prev = known[url].get("modified", "")
        if prev and modified and modified <= prev:
            log.info(f"skip {title}")
            return None

    safe = re.sub(r"[^\w\-]", "_", title)[:100].strip("_")
    dest = output_dir / f"{safe}.csv"

    try:
        response = session.get(url, timeout=120)
        response.raise_for_status()
        text = response.content.decode(response.apparent_encoding or "utf-8", errors="replace")
    except requests.RequestException as e:
        log.error(f"Failed to download {title}: {e}")
        return None

    try:
        text = normalize_headers(text)
    except Exception as e:
        log.warning(f"Header normalization failed for {title}: {e}")

    try:
        dest.write_text(text, encoding="utf-8")
    except OSError as e:
        log.error(f"Failed to write {dest}: {e}")
        return None

    log.info(f"Saved {dest.name}")

    return {
        "url": url,
        "modified": modified,
        "file": str(dest),
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "title": title,
    }


def main():
    parser = ArgumentParser(
        description="Download CMS Hospital datasets.",
        epilog="All flags are optional if the corresponding env var is set. CLI flags take precedence.",
    )
    parser.add_argument("--metastore-url",  default=None, help="CMS metastore API endpoint (env: METASTORE_URL)")
    parser.add_argument("--output-dir",     default=None, help="Directory to write CSV files (env: OUTPUT_DIR)")
    parser.add_argument("--metadata-file",  default=None, help="Path to run state tracking file (env: METADATA_FILE)")
    parser.add_argument("--workers",        type=int,   default=None, help="Parallel download threads (env: WORKERS)")
    parser.add_argument("--max-retries",    type=int,   default=None, help="Retry attempts on failed requests (env: MAX_RETRIES)")
    parser.add_argument("--retry-backoff",  type=float, default=None, help="Backoff multiplier between retries in seconds (env: RETRY_BACKOFF)")
    parser.add_argument("--log-level",      default=None, help="Logging level: DEBUG, INFO, WARNING, ERROR (env: LOG_LEVEL)")
    parser.add_argument("--log-file",       default=None, help="Log file path (env: LOG_FILE)")
    parser.add_argument("--force",          action="store_true", help="Re-download everything regardless of modification date")
    args = parser.parse_args()

    # Resolve log settings before anything else so we can attach handlers.
    log_level = args.log_level or os.getenv("LOG_LEVEL")
    log_file = args.log_file or os.getenv("LOG_FILE")
    missing_log = [label for label, value in [("--log-level / LOG_LEVEL", log_level), ("--log-file / LOG_FILE", log_file)] if not value]
    if missing_log:
        sys.exit(f"Missing required config: {', '.join(missing_log)}")

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s %(levelname)s [%(module)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )

    config = resolve_config(args)

    output_dir = config["output_dir"]
    metadata_file = config["metadata_file"]
    output_dir.mkdir(parents=True, exist_ok=True)

    session = create_session(config["max_retries"], config["retry_backoff"])

    metadata = {}
    if metadata_file.exists():
        try:
            metadata = json.loads(metadata_file.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Couldn't read metadata file, starting fresh")

    # if --force, treat everything as new regardless of what's in the metadata
    known = {} if args.force else metadata.get("files", {})

    datasets = get_hospital_datasets(session, config["metastore_url"])
    if not datasets:
        log.error("No datasets found — check the API or --metastore-url")
        sys.exit(1)

    all_dists = []
    for dataset in datasets:
        all_dists.extend(get_csv_urls(dataset))

    log.info(f"Evaluating {len(all_dists)} files across {len(datasets)} datasets")

    downloaded = {}
    skipped = 0

    with ThreadPoolExecutor(max_workers=config["workers"]) as pool:
        futures = {
            pool.submit(download_file, distribution, output_dir, known, args.force, session): distribution
            for distribution in all_dists if distribution["url"]
        }
        for future in as_completed(futures):
            result = future.result()
            if result:
                downloaded[result["url"]] = result
            else:
                skipped += 1

    # merge newly downloaded files into the existing metadata before saving
    known.update(downloaded)
    metadata["files"] = known
    metadata["last_run"] = datetime.now(timezone.utc).isoformat()

    try:
        metadata_file.write_text(json.dumps(metadata, indent=2))
    except OSError as e:
        log.error(f"Failed to save run metadata: {e}")

    log.info(f"Done — downloaded: {len(downloaded)}, skipped: {skipped}")


if __name__ == "__main__":
    main()
