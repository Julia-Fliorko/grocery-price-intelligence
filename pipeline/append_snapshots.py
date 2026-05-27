from pathlib import Path
from datetime import datetime
import re
import pandas as pd


PROCESSED_DIR = Path("data/processed")
MASTER_DIR = PROCESSED_DIR / "master"
MASTER_FILE = MASTER_DIR / "price_snapshots_master.csv"

SNAPSHOT_PATTERN = "price_snapshots_*.csv"


def parse_price(value):
    if pd.isna(value):
        return None

    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))

    if not match:
        return None

    return float(match.group())


def parse_unit_price_unit(value):
    if pd.isna(value):
        return None

    text = str(value).lower()
    match = re.search(r"/\s*([a-zA-Z ]+)", text)

    if not match:
        return None

    return match.group(1).strip()


def build_snapshot_id(row):
    return (
        f"{row['store_name']}|"
        f"{row['basket_item_id']}|"
        f"{row['scrape_datetime']}|"
        f"{row['product_url']}"
    )


def load_store_snapshot(file_path):
    df = pd.read_csv(file_path, dtype={"basket_item_id": "string"})

    df["source_file"] = str(file_path)
    df["loaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    df["scrape_datetime"] = pd.to_datetime(df["scrape_datetime"], errors="coerce")
    df["scrape_date"] = df["scrape_datetime"].dt.date.astype("string")

    df["price_numeric"] = df["price_raw"].apply(parse_price)
    df["unit_price_numeric"] = df["unit_price_raw"].apply(parse_price)
    df["unit_price_unit"] = df["unit_price_raw"].apply(parse_unit_price_unit)

    df["snapshot_id"] = df.apply(build_snapshot_id, axis=1)

    return df


def combine_store_snapshots():
    MASTER_DIR.mkdir(parents=True, exist_ok=True)

    snapshot_files = [
        file_path
        for file_path in PROCESSED_DIR.glob(SNAPSHOT_PATTERN)
        if file_path.name != MASTER_FILE.name
    ]

    if not snapshot_files:
        raise FileNotFoundError("No store snapshot files found in data/processed/")

    frames = []

    for file_path in snapshot_files:
        print(f"Loading {file_path}")
        frames.append(load_store_snapshot(file_path))

    master_df = pd.concat(frames, ignore_index=True)

    master_df = master_df.drop_duplicates(subset=["snapshot_id"])

    ordered_columns = [
        "snapshot_id",
        "scrape_session_id",
        "scrape_datetime",
        "scrape_date",
        "store_name",
        "basket_item_id",
        "canonical_name",
        "product_url",
        "product_title_raw",
        "price_raw",
        "price_numeric",
        "unit_price_raw",
        "unit_price_numeric",
        "unit_price_unit",
        "price_block_raw",
        "price_context_raw",
        "availability_raw",
        "status",
        "source_file",
        "loaded_at",
    ]

    master_df = master_df.reindex(columns=ordered_columns)

    master_df.to_csv(MASTER_FILE, index=False)

    print(f"\nMaster snapshot file created:")
    print(MASTER_FILE)
    print(f"Rows saved: {len(master_df)}")


if __name__ == "__main__":
    combine_store_snapshots()