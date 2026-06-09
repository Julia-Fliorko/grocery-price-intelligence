# scraper/grocery_scraper.py

from pathlib import Path
from datetime import datetime
import re
import random
import time
import pandas as pd
from pandas.errors import EmptyDataError
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


MAX_RETRIES = 2
PAGE_TIMEOUT_MS = 60000
TEXT_TIMEOUT_MS = 10000
PAGE_SETTLE_MS = 0
RETRY_WAIT_MS = 30000

MIN_PRODUCT_INTERVAL_SECONDS = 5
MAX_PRODUCT_INTERVAL_SECONDS = 10
MIN_PAGE_CLOSE_DELAY_SECONDS = 0
MAX_PAGE_CLOSE_DELAY_SECONDS = 10

SCRAPED_COLUMN = "scraped"
LAST_SCRAPE_STATUS_COLUMN = "last_scrape_status"
LAST_SCRAPE_DATETIME_COLUMN = "last_scrape_datetime"
FAILED_SCRAPE_COUNT_COLUMN = "failed_scrape_count"
MAX_FAILED_SCRAPE_COUNT = 3

BLOCKED_STATUS = "blocked"
FAILED_STATUS = "failed"
SUCCESS_STATUS = "success"

SKIP_ROUND_IF_FAILED = False

WALMART_SCRAPE = True
HEB_SCRAPE = True
WHOLE_FOODS_SCRAPE = True
SAMS_CLUB_SCRAPE = True

STORE_SCRAPE_FLAGS = {
    "walmart": WALMART_SCRAPE,
    "heb": HEB_SCRAPE,
    "whole_foods": WHOLE_FOODS_SCRAPE,
    "sams_club": SAMS_CLUB_SCRAPE,
}

BLOCK_INDICATORS = [
    "Robot or human",
    "Press & Hold",
    "press and hold",
    "verify you are human",
    "Are you a robot",
    "access denied",
    "blocked",
    "Pardon Our Interruption",
    "This page could not load",
    "Please verify you are a human",
]

RAW_COLUMNS = [
    "snapshot_id",
    "scrape_session_id",
    "scrape_datetime",
    "store_name",
    "basket_item_id",
    "canonical_name",
    "product_url",
    "product_title_raw",
    "price_raw",
    "old_price_raw",
    "unit_price_raw",
    "coupon_text_raw",
    "coupon_value_raw",
    "price_block_raw",
    "price_context_raw",
    "availability_raw",
    "status",
    "error_message",
    "attempt_count",
]

PROCESSED_COLUMNS = [
    "snapshot_id",
    "scrape_session_id",
    "scrape_datetime",
    "store_name",
    "basket_item_id",
    "canonical_name",
    "product_url",
    "product_title_raw",
    "price_raw",
    "old_price_raw",
    "unit_price_raw",
    "coupon_text_raw",
    "coupon_value_raw",
    "price_block_raw",
    "price_context_raw",
    "availability_raw",
    "status",
]
# New function for extracting HEB coupon info

def extract_heb_coupon_info(body_text):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    coupon_heading_indexes = [
        index for index, line in enumerate(lines)
        if line.lower() == "coupon"
    ]

    for coupon_heading_index in coupon_heading_indexes:
        coupon_lines = lines[coupon_heading_index + 1:coupon_heading_index + 12]

        coupon_value = None
        coupon_text = None

        for index, line in enumerate(coupon_lines):
            lower_line = line.lower()

            value_match = re.search(
                r"(?:\d+\s*for\s*\$\s*\d+(?:\.\d{2})?|\d+%\s*off|\$\s*\d+(?:\.\d{2})?\s*off)",
                line,
                flags=re.IGNORECASE,
            )

            if value_match and coupon_value is None:
                coupon_value = normalize_text(value_match.group(0))

                if (
                    "expires" not in lower_line
                    and "unlimited use" not in lower_line
                    and lower_line != coupon_value.lower()
                ):
                    coupon_text = line

                for next_line in coupon_lines[index + 1:index + 4]:
                    next_lower_line = next_line.lower()

                    if "expires" in next_lower_line:
                        continue

                    if "unlimited use" in next_lower_line:
                        continue

                    if next_lower_line == "clip":
                        continue

                    if "$" in next_line or "%" in next_line or " off" in next_lower_line:
                        coupon_text = next_line
                        break

                if coupon_text is None:
                    coupon_text = line

                return coupon_text, coupon_value

    joined_text = normalize_text(" ".join(lines))

    if not joined_text or "coupon" not in joined_text.lower():
        return None, None

    coupon_match = re.search(
        r"coupon\s+(?:.*?\s+)?((?:\d+\s*for\s*\$\s*\d+(?:\.\d{2})?|\d+%\s*off|\$\s*\d+(?:\.\d{2})?\s*off)(?:[^.]{0,120})?)",
        joined_text,
        flags=re.IGNORECASE,
    )

    if not coupon_match:
        return None, None

    coupon_text = normalize_text(coupon_match.group(1))
    value_match = re.search(
        r"(?:\d+\s*for\s*\$\s*\d+(?:\.\d{2})?|\d+%\s*off|\$\s*\d+(?:\.\d{2})?\s*off)",
        coupon_text,
        flags=re.IGNORECASE,
    )

    coupon_value = normalize_text(value_match.group(0)) if value_match else None

    return coupon_text, coupon_value

STORES = {
    "walmart": {
        "store_name": "Walmart",
        "input_file": Path("config/product_urls/walmart_urls.csv"),
        "raw_dir": Path("data/raw/walmart"),
        "processed_file": Path("data/processed/price_snapshots_walmart.csv"),
        "browser_profile_dir": Path("browser_profiles/walmart"),
    },
    "heb": {
        "store_name": "HEB",
        "input_file": Path("config/product_urls/heb_urls.csv"),
        "raw_dir": Path("data/raw/heb"),
        "processed_file": Path("data/processed/price_snapshots_heb.csv"),
        "browser_profile_dir": Path("browser_profiles/heb"),
    },
    "whole_foods": {
        "store_name": "Whole Foods",
        "input_file": Path("config/product_urls/wholefoods_urls.csv"),
        "raw_dir": Path("data/raw/whole_foods"),
        "processed_file": Path("data/processed/price_snapshots_whole_foods.csv"),
        "browser_profile_dir": Path("browser_profiles/whole_foods"),
    },
    "sams_club": {
        "store_name": "Sam's Club",
        "input_file": Path("config/product_urls/sams_club_urls.csv"),
        "raw_dir": Path("data/raw/sams_club"),
        "processed_file": Path("data/processed/price_snapshots_sams_club.csv"),
        "browser_profile_dir": Path("browser_profiles/sams_club"),
    },
}

USE_REMOTE_DEBUGGING_CHROME = True
REMOTE_DEBUGGING_URL = "http://127.0.0.1:9222"

USE_PERSONAL_CHROME_PROFILE = False
PERSONAL_CHROME_USER_DATA_DIR = Path.home() / "Library/Application Support/Google/Chrome"
PERSONAL_CHROME_PROFILE_DIRECTORY = "Profile 12"


def make_scrape_session_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def make_snapshot_id(store_name, basket_item_id, scrape_datetime):
    store_key = str(store_name).lower().replace(" ", "_")

    if isinstance(scrape_datetime, datetime):
        timestamp = scrape_datetime.strftime("%Y%m%d_%H%M%S")
    else:
        timestamp = pd.to_datetime(scrape_datetime).strftime("%Y%m%d_%H%M%S")

    return f"{store_key}|{basket_item_id}|{timestamp}"


def normalize_text(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def get_canonical_name(row):
    if "canonical_name" in row and pd.notna(row.get("canonical_name")):
        return row.get("canonical_name")

    if "canonical_name_suggestion" in row and pd.notna(row.get("canonical_name_suggestion")):
        return row.get("canonical_name_suggestion")

    return None


def safe_inner_text(page, selector, timeout=TEXT_TIMEOUT_MS):
    try:
        return page.locator(selector).first.inner_text(timeout=timeout).strip()
    except Exception:
        return None


def get_body_text(page):
    return safe_inner_text(page, "body", timeout=TEXT_TIMEOUT_MS) or ""


def is_blocked_page(body_text):
    body_text_lower = body_text.lower()
    return any(indicator.lower() in body_text_lower for indicator in BLOCK_INDICATORS)


def normalize_scraped_flags(urls_df):
    if SCRAPED_COLUMN not in urls_df.columns:
        urls_df[SCRAPED_COLUMN] = False

    if LAST_SCRAPE_STATUS_COLUMN not in urls_df.columns:
        urls_df[LAST_SCRAPE_STATUS_COLUMN] = None

    if LAST_SCRAPE_DATETIME_COLUMN not in urls_df.columns:
        urls_df[LAST_SCRAPE_DATETIME_COLUMN] = None

    if FAILED_SCRAPE_COUNT_COLUMN not in urls_df.columns:
        urls_df[FAILED_SCRAPE_COUNT_COLUMN] = 0

    urls_df[LAST_SCRAPE_STATUS_COLUMN] = urls_df[LAST_SCRAPE_STATUS_COLUMN].astype("string")
    urls_df[LAST_SCRAPE_DATETIME_COLUMN] = urls_df[LAST_SCRAPE_DATETIME_COLUMN].astype("string")
    urls_df[FAILED_SCRAPE_COUNT_COLUMN] = (
        pd.to_numeric(urls_df[FAILED_SCRAPE_COUNT_COLUMN], errors="coerce")
        .fillna(0)
        .astype(int)
    )

    urls_df[SCRAPED_COLUMN] = (
        urls_df[SCRAPED_COLUMN]
        .fillna(False)
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes", "y"])
    )

    return urls_df


def get_unscraped_rows(urls_df, skipped_row_indexes=None):
    unscraped_rows = urls_df[urls_df[SCRAPED_COLUMN] == False]

    if skipped_row_indexes:
        valid_skipped_indexes = [
            index for index in skipped_row_indexes
            if index in unscraped_rows.index
        ]

        if valid_skipped_indexes:
            unscraped_rows = unscraped_rows.drop(index=valid_skipped_indexes)

    return unscraped_rows


def save_url_progress(urls_df, input_file):
    urls_df.to_csv(input_file, index=False)


def mark_row_progress(urls_df, row_index, result, input_file):
    urls_df.loc[row_index, LAST_SCRAPE_STATUS_COLUMN] = result["status"]
    urls_df.loc[row_index, LAST_SCRAPE_DATETIME_COLUMN] = result["scrape_datetime"]

    if result["status"] == SUCCESS_STATUS:
        urls_df.loc[row_index, SCRAPED_COLUMN] = True
        urls_df.loc[row_index, FAILED_SCRAPE_COUNT_COLUMN] = 0

    elif result["status"] == FAILED_STATUS:
        current_failed_count = urls_df.loc[row_index, FAILED_SCRAPE_COUNT_COLUMN]

        if pd.isna(current_failed_count):
            current_failed_count = 0

        failed_count = int(current_failed_count) + 1
        urls_df.loc[row_index, FAILED_SCRAPE_COUNT_COLUMN] = failed_count

        if failed_count >= MAX_FAILED_SCRAPE_COUNT:
            urls_df.loc[row_index, SCRAPED_COLUMN] = True
            urls_df.loc[row_index, LAST_SCRAPE_STATUS_COLUMN] = FAILED_STATUS
            print(
                f"Marked row as scraped after {failed_count} failed scrape attempt(s): "
                f"{result.get('canonical_name')}"
            )
        else:
            urls_df.loc[row_index, SCRAPED_COLUMN] = False
            print(
                f"Failed scrape count for {result.get('canonical_name')}: "
                f"{failed_count}/{MAX_FAILED_SCRAPE_COUNT}"
            )

    elif result["status"] == BLOCKED_STATUS:
        urls_df.loc[row_index, SCRAPED_COLUMN] = False

    save_url_progress(urls_df, input_file)


def get_session_file_paths(store_key, raw_dir, processed_file, scrape_session_id):
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_file.parent.mkdir(parents=True, exist_ok=True)

    return {
        "raw": raw_dir / f"{store_key}_raw_{scrape_session_id}.csv",
        "failed": raw_dir / f"{store_key}_failed_{scrape_session_id}.csv",
    }


def close_page_after_random_delay(page):
    if page is None or page.is_closed():
        return

    delay_seconds = random.randint(
        MIN_PAGE_CLOSE_DELAY_SECONDS,
        MAX_PAGE_CLOSE_DELAY_SECONDS,
    )

    if delay_seconds > 0:
        print(f"Keeping page open for {delay_seconds} second(s) before closing.")
        time.sleep(delay_seconds)

    if not page.is_closed():
        page.close()




def append_row_to_csv(row, file_path, columns):
    row_df = pd.DataFrame([row]).reindex(columns=columns)

    if file_path.exists() and file_path.stat().st_size > 0:
        with open(file_path, "rb+") as file:
            file.seek(-1, 2)
            last_character = file.read(1)

            if last_character != b"\n":
                file.write(b"\n")

    file_exists = file_path.exists() and file_path.stat().st_size > 0

    row_df.to_csv(
        file_path,
        mode="a",
        header=not file_exists,
        index=False,
    )


def append_row_to_csv_with_schema_check(row, file_path, columns):
    if file_path.exists() and file_path.stat().st_size > 0:
        try:
            existing_columns = list(pd.read_csv(file_path, nrows=0).columns)
        except EmptyDataError:
            existing_columns = []

        if existing_columns and existing_columns != columns:
            backup_file = file_path.with_name(
                f"{file_path.stem}_schema_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}{file_path.suffix}"
            )
            file_path.rename(backup_file)
            print(f"Schema mismatch. Backed up old file to: {backup_file}")

    append_row_to_csv(row, file_path, columns)


def append_result_files(result, file_paths, processed_file):
    append_row_to_csv(result, file_paths["raw"], RAW_COLUMNS)

    if result["status"] == FAILED_STATUS:
        append_row_to_csv(result, file_paths["failed"], RAW_COLUMNS)

    if result["status"] == SUCCESS_STATUS:
        processed_row = {column: result.get(column) for column in PROCESSED_COLUMNS}
        append_row_to_csv_with_schema_check(processed_row, processed_file, PROCESSED_COLUMNS)



_BAD_PRICE_TOKENS_BASE = ["$0.00", "cart", "subtotal", "checkout", "estimated total"]

_BAD_PRICE_TOKENS_BY_STORE = {
    "sams_club": _BAD_PRICE_TOKENS_BASE + ["savings", "reorder"],
    "walmart": _BAD_PRICE_TOKENS_BASE + [
        "protection plan", "allstate", "onepay", "/mo", "per month", "as low as",
    ],
    "whole_foods": _BAD_PRICE_TOKENS_BASE,
}


def is_bad_price_candidate(text, store_key):
    text = normalize_text(text)
    if not text:
        return True
    lower_text = text.lower()
    return any(token in lower_text for token in _BAD_PRICE_TOKENS_BY_STORE.get(store_key, _BAD_PRICE_TOKENS_BASE))


# Convenience aliases kept for call-site clarity
def is_bad_sams_price_candidate(text):
    return is_bad_price_candidate(text, "sams_club")


def is_bad_walmart_price_candidate(text):
    return is_bad_price_candidate(text, "walmart")


def extract_main_price_from_text(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    dollar_matches = re.findall(r"\$\s*(?!0\.00)\d+(?:\.\d{2})?", price_text)

    if not dollar_matches:
        return None

    return dollar_matches[0].replace(" ", "")

# New function: extract_old_price_from_text
def extract_old_price_from_text(price_text, store_key=None):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    lower_text = price_text.lower()
    non_unit_prices = []

    for match in re.finditer(r"\$\s*(?!0\.00)\d+(?:\.\d{2})?", price_text):
        candidate = match.group(0).replace(" ", "")
        text_after_candidate = price_text[match.end():match.end() + 20]

        # Only skip the candidate if this exact price is directly followed by a unit marker.
        # Example to skip: "$0.97/ea".
        # Example not to skip: "$21.48 $0.97/ea".
        if re.match(
            r"\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)\b",
            text_after_candidate,
            flags=re.IGNORECASE,
        ):
            continue

        non_unit_prices.append(candidate)

    if len(non_unit_prices) < 2:
        return None

    # Sale/coupon/deal blocks usually contain current and old prices together.
    # Sam's Club example: "Now $13.86 $14.77 $0.77/ea" -> old price is $14.77.
    # Walmart example: "Now $378.00 $424.00" -> old price is $424.00.
    # HEB example: "Sale $1.25 $0.97 each" -> old price is $1.25.
    if store_key == "sams_club" and "now" in lower_text:
        return non_unit_prices[1]

    if store_key == "walmart" and "now" in lower_text:
        return non_unit_prices[1]

    if store_key == "heb" and (
        "sale" in lower_text
        or "coupon" in lower_text
        or "deal" in lower_text
        or "price cut" in lower_text
    ):
        return non_unit_prices[0]

    if store_key == "whole_foods":
        # Discounted weighted items:
        # "$1.66/lb $1.99/lb | Total est. price: $1.33"
        whole_foods_unit_prices = re.findall(
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
            price_text,
            flags=re.IGNORECASE,
        )

        if len(whole_foods_unit_prices) >= 2:
            return (
                normalize_text(whole_foods_unit_prices[1])
                .replace(" ", "")
                .replace("ounce", "oz")
                .replace("each", "ea")
            )

        # Discounted packaged items:
        # "$7.43 $9.49 ($0.31 / ounce)"
        if len(non_unit_prices) >= 2:
            return non_unit_prices[1]

        return None

    if "was" in lower_text or "save" in lower_text or "off" in lower_text:
        return non_unit_prices[1]

    if "now" in lower_text:
        return non_unit_prices[1]

    return None


def extract_unit_price_from_text(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    unit_patterns = [
        r"\(?\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)\s*\)?",
        r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*(?:per|/)\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
        r"\d+(?:\.\d+)?\s*¢\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
        r"\d+(?:\.\d+)?\s*¢\s*(?:per|/)\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
    ]

    for pattern in unit_patterns:
        match = re.search(pattern, price_text, flags=re.IGNORECASE)

        if match:
            return (
                match.group(0)
                .replace(" ", "")
                .replace("(", "")
                .replace(")", "")
                .replace("ounce", "oz")
                .replace("each", "ea")
            )

    return None


def classify_price_context(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    lower_text = price_text.lower()
    context_flags = []

    if "avg price" in lower_text:
        context_flags.append("avg_price")

    if "final cost by weight" in lower_text:
        context_flags.append("final_cost_by_weight")

    if "price when purchased online" in lower_text:
        context_flags.append("online_price")

    same_line_unit_sale_prices = re.findall(
        r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
        price_text,
        flags=re.IGNORECASE,
    )

    if len(same_line_unit_sale_prices) >= 2:
        context_flags.append("discounted")

    if "now" in lower_text:
        context_flags.append("rollback_or_sale_price")

    if "was" in lower_text or "save" in lower_text:
        context_flags.append("discounted")

    if "sale" in lower_text:
        context_flags.append("sale_price")

    if "price cut" in lower_text:
        context_flags.append("price_cut")

    if "coupon" in lower_text or "deal" in lower_text:
        context_flags.append("discounted")

    if not context_flags:
        context_flags.append("standard_price")

    return "|".join(context_flags)



def _combined_lower(body_text, price_block_text):
    return f"{(body_text or '').lower()} {(price_block_text or '').lower()}"


def extract_walmart_availability(body_text, price=None, price_block_text=None):
    combined = _combined_lower(body_text, price_block_text)

    # Walmart can contain generic hidden/background "unavailable" text even when the main product is purchasable.
    # Strong positive buying signals must win first.
    if "add to cart" in combined:
        return "in_stock"
    if "one-time purchase" in combined:
        return "in_stock"
    if "subscribe" in combined and price:
        return "in_stock"
    if "out of stock" in combined:
        return "out_of_stock"
    if "sold out" in combined:
        return "sold_out"
    if "currently unavailable" in combined:
        return "unavailable"
    if "not available" in combined:
        return "unavailable"
    if "pickup" in combined or "delivery" in combined or "shipping" in combined:
        return "likely_in_stock"
    return "unknown"


def extract_heb_availability(body_text, price=None, price_block_text=None):
    combined = _combined_lower(body_text, price_block_text)

    if "out of stock" in combined:
        return "out_of_stock"
    if "sold out" in combined:
        return "sold_out"
    if "currently unavailable" in combined:
        return "unavailable"
    if "unavailable" in combined or "not available" in combined:
        return "unavailable"
    if "add to cart" in combined or "add to basket" in combined:
        return "in_stock"
    if "pickup" in combined or "delivery" in combined or "curbside" in combined:
        return "likely_in_stock"
    return "unknown"


def extract_wholefoods_availability(body_text, price=None, price_block_text=None):
    combined = _combined_lower(body_text, price_block_text)

    # Whole Foods can show disabled Add to Cart states, so explicit unavailable signals win first.
    if re.search(r"\b0\.00\b", combined):
        return "out_of_stock"
    if "currently unavailable" in combined:
        return "out_of_stock"
    if "currently not sold" in combined:
        return "out_of_stock"
    if "unavailable for delivery" in combined:
        return "out_of_stock"
    if "out of stock" in combined:
        return "out_of_stock"
    if "sold out" in combined:
        return "sold_out"
    if "add to cart" in combined or "add to basket" in combined:
        return "in_stock" if price else "likely_in_stock"
    if "snap ebt eligible" in combined:
        return "likely_in_stock"
    if "pickup" in combined or "delivery" in combined:
        return "likely_in_stock"
    return "unknown"


# Sam's Club availability extraction
def extract_sams_club_availability(body_text, price=None, price_block_text=None):
    combined = _combined_lower(body_text, price_block_text)

    # Sam's Club can show "Shipping out of stock" while Pickup/Delivery are still available.
    # Strong purchasability signals must win over shipping-only unavailable text.
    if price and "add to cart" in combined:
        return "in_stock"
    if price and "pickup" in combined:
        return "in_stock"
    if price and "delivery" in combined:
        return "in_stock"
    if "sold out" in combined:
        return "sold_out"
    if "out of stock" in combined and not price:
        return "out_of_stock"
    if "not available" in combined and not price:
        return "unavailable"
    if "shipping not available" in combined and price:
        return "likely_in_stock"
    if price:
        return "likely_in_stock"
    return "unknown"


_AVAILABILITY_EXTRACTORS = {
    "walmart": extract_walmart_availability,
    "heb": extract_heb_availability,
    "whole_foods": extract_wholefoods_availability,
    "sams_club": extract_sams_club_availability,
}


def extract_availability_from_text(body_text, store_key, price=None, price_block_text=None):
    extractor = _AVAILABILITY_EXTRACTORS.get(store_key)
    if extractor:
        return extractor(body_text, price=price, price_block_text=price_block_text)
    return "unknown"



def extract_walmart_price_block_from_lines(lines):
    lines = [normalize_text(line) for line in lines if normalize_text(line)]

    # Walmart rollback block example:
    # "Now $378.00" followed by "$424.00" or same-line "Now $378.00 $424.00".
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_walmart_price_candidate(clean_line):
            continue

        lower_line = clean_line.lower()

        if "you save" in lower_line:
            continue

        now_match = re.search(
            r"now\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?",
            clean_line,
            flags=re.IGNORECASE,
        )

        if now_match:
            price_lines = [normalize_text(now_match.group(0))]
            same_line_prices = re.findall(
                r"\$\s*(?!0\.00)\d+(?:\.\d{2})?",
                clean_line,
                flags=re.IGNORECASE,
            )

            if len(same_line_prices) >= 2:
                price_lines.append(same_line_prices[1])
                return normalize_text(" ".join(price_lines))

            for next_index in range(index + 1, min(index + 8, len(lines))):
                next_line = normalize_text(lines[next_index])

                if not next_line or "$" not in next_line:
                    continue

                if is_bad_walmart_price_candidate(next_line):
                    continue

                next_lower_line = next_line.lower()

                if "you save" in next_lower_line:
                    continue

                old_price_match = re.search(
                    r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?$",
                    next_line,
                    flags=re.IGNORECASE,
                )

                if old_price_match:
                    price_lines.append(normalize_text(old_price_match.group(0)))
                    break

            return normalize_text(" ".join(price_lines))

    # Standard Walmart product price block.
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_walmart_price_candidate(clean_line):
            continue

        lower_line = clean_line.lower()

        if "you save" in lower_line:
            continue

        if re.search(r"/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)\b", lower_line):
            continue

        standard_price_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?$",
            clean_line,
            flags=re.IGNORECASE,
        )

        if standard_price_match:
            price_lines = [normalize_text(standard_price_match.group(0))]

            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = normalize_text(lines[next_index])

                if next_line and re.search(
                    r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|ounce|each|ea|ct|count|gal)",
                    next_line,
                    flags=re.IGNORECASE,
                ):
                    price_lines.append(next_line)
                    break

            return normalize_text(" ".join(price_lines))

    return None


def extract_walmart_price_block_from_body(body_text, product_title=None):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    # Anchor around the visible product title so we don't grab ad, financing, cart, or protection-plan prices.
    if product_title:
        normalized_title = normalize_text(product_title).lower()
        title_index = None

        for index, line in enumerate(lines):
            if normalize_text(line).lower() == normalized_title:
                title_index = index
                break

        if title_index is not None:
            title_scoped_lines = lines[title_index + 1:title_index + 55]
            title_scoped_price_block = extract_walmart_price_block_from_lines(title_scoped_lines)

            if title_scoped_price_block:
                return title_scoped_price_block

    return extract_walmart_price_block_from_lines(lines)


def extract_walmart_price_block(page, product_title=None):
    body_text = get_body_text(page)
    body_price_block = extract_walmart_price_block_from_body(
        body_text,
        product_title=product_title,
    )

    if body_price_block:
        return body_price_block

    selectors = [
        '[data-testid="price-wrap"]',
        '[itemprop="price"]',
        '[data-testid="product-price"]',
        '[data-testid="price"]',
        '.price',
        'text=/Now\\s*\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/',
        'text=/\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/',
    ]

    for selector in selectors:
        value = safe_inner_text(page, selector)

        if value and "$" in value:
            extracted_price = extract_walmart_price_block_from_body(
                value,
                product_title=product_title,
            )

            if extracted_price:
                return extracted_price

            normalized_value = normalize_text(value)

            if normalized_value and not is_bad_walmart_price_candidate(normalized_value):
                return normalized_value

    return None


def extract_heb_price_block_from_body(body_text):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    # HEB sale pages can show prices across separate nearby lines:
    # Sale / $1.25 / $0.97 / each
    # We preserve both prices so price_raw can store the current price
    # and old_price_raw can store the crossed-out price.
    for index, line in enumerate(lines):
        lower_line = line.lower()

        if (
            lower_line not in {"sale", "coupon", "deal", "price cut"}
            and "sale" not in lower_line
            and "price cut" not in lower_line
        ):
            continue

        price_lines = [line]
        nearby_price_lines = []

        for next_index in range(index + 1, min(index + 10, len(lines))):
            next_line = normalize_text(lines[next_index])

            if not next_line:
                continue

            # Stop if we hit clear non-price product sections after collecting sale prices.
            if nearby_price_lines and next_line.lower() in {
                "add to cart",
                "add to list",
                "find in-stock nearby",
                "prices may vary between in-store, curbside, and delivery.",
            }:
                break

            if "$" not in next_line:
                continue

            if re.search(
                r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*(?:each|ea|lb|oz|ct|count)?$",
                next_line,
                flags=re.IGNORECASE,
            ):
                nearby_price_lines.append(next_line)

            if len(nearby_price_lines) >= 2:
                break

        if nearby_price_lines:
            return normalize_text(" ".join(price_lines + nearby_price_lines))

    # HEB sale fallback for body text where the word Sale and prices are compressed together.
    joined_text = normalize_text(" ".join(lines))

    sale_block_match = re.search(
        r"(?:sale|price\s+cut)\s+(\$\s*(?!0\.00)\d+(?:\.\d{2})?)\s+(\$\s*(?!0\.00)\d+(?:\.\d{2})?)(?:\s*(?:each|ea|lb|oz|ct|count))?",
        joined_text,
        flags=re.IGNORECASE,
    )

    if sale_block_match:
        return normalize_text(sale_block_match.group(0))

    # Standard HEB pages.
    for index, line in enumerate(lines):
        if re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*(?:each|ea)?$",
            line,
            flags=re.IGNORECASE,
        ):
            price_lines = [line]

            for next_index in range(index + 1, min(index + 6, len(lines))):
                next_line = lines[next_index]

                if re.search(
                    r"\(?\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|each|ea|ct|count|gal)\s*\)?",
                    next_line,
                    flags=re.IGNORECASE,
                ):
                    price_lines.append(next_line)
                    break

            return normalize_text(" ".join(price_lines))

    return None


def extract_heb_price_block(page):
    body_text = get_body_text(page)
    heb_price_block = extract_heb_price_block_from_body(body_text)

    if heb_price_block:
        return heb_price_block

    selectors = [
        '[data-qe-id="pdp-product-price"]',
        '[data-testid="product-price"]',
        '[data-testid="price"]',
        '[itemprop="price"]',
        ".price",
    ]

    for selector in selectors:
        value = safe_inner_text(page, selector)

        if value and "$" in value:
            return normalize_text(value)

    return None


# Helper for HEB price extraction — delegates to the unified extract_current_price_from_text.
# Kept as a named alias so existing call-sites remain readable.
def extract_heb_main_price(price_block_text):
    return extract_current_price_from_text(price_block_text, store_key="heb")



# Helper for Whole Foods price extraction
def is_bad_wholefoods_price_candidate(text):
    return is_bad_price_candidate(text, "whole_foods")


def extract_wholefoods_price_block_from_body(body_text):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    # First priority: main Whole Foods price line followed by unit price, such as:
    # "$3.49 ($0.22 / ounce) SNAP EBT eligible"
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_wholefoods_price_candidate(clean_line):
            continue

        lower_line = clean_line.lower()

        if re.search(r"^\(?\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)?$", lower_line):
            continue

        sale_same_line_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?\s+\$\s*(?!0\.00)\d+(?:\.\d{2})?(?:\s*\(\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\))?",
            clean_line,
            flags=re.IGNORECASE,
        )

        if sale_same_line_match:
            return clean_line

        same_line_price_with_unit_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*\(\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)",
            clean_line,
            flags=re.IGNORECASE,
        )

        if same_line_price_with_unit_match:
            return clean_line

        total_est_match = re.search(
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea).*total\s+est\.\s+price:\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?",
            clean_line,
            flags=re.IGNORECASE,
        )

        if total_est_match:
            return clean_line

        standard_price_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?(?:\s+snap\s+ebt\s+eligible)?$",
            clean_line,
            flags=re.IGNORECASE,
        )

        if standard_price_match:
            price_lines = [clean_line]

            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = normalize_text(lines[next_index])

                if next_line and re.search(
                    r"\(?\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)?",
                    next_line,
                    flags=re.IGNORECASE,
                ):
                    price_lines.append(next_line)
                    break

            return normalize_text(" ".join(price_lines))

    joined_text = normalize_text(" ".join(lines))

    if joined_text:
        sale_block_match = re.search(
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s+\$\s*(?!0\.00)\d+(?:\.\d{2})?(?:\s*\(\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\))?",
            joined_text,
            flags=re.IGNORECASE,
        )

        if sale_block_match:
            return normalize_text(sale_block_match.group(0))

        same_line_price_with_unit_match = re.search(
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*\(\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)",
            joined_text,
            flags=re.IGNORECASE,
        )

        if same_line_price_with_unit_match:
            return normalize_text(same_line_price_with_unit_match.group(0))

        product_price_before_unit_match = re.search(
            r"(\$\s*(?!0\.00)\d+(?:\.\d{2})?)\s+\(\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)",
            joined_text,
            flags=re.IGNORECASE,
        )

        if product_price_before_unit_match:
            return normalize_text(product_price_before_unit_match.group(0))

        total_est_match = re.search(
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea).*total\s+est\.\s+price:\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?",
            joined_text,
            flags=re.IGNORECASE,
        )

        if total_est_match:
            return normalize_text(total_est_match.group(0))

    return None


def extract_wholefoods_price_block(page):
    body_text = get_body_text(page)
    body_price_block = extract_wholefoods_price_block_from_body(body_text)

    if body_price_block:
        return body_price_block

    selectors = [
        '[data-testid="product-price"]',
        '[data-testid="price"]',
        '[itemprop="price"]',
        '.price',
        'text=/\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/',
    ]

    for selector in selectors:
        value = safe_inner_text(page, selector)

        if value and "$" in value:
            extracted_price = extract_wholefoods_price_block_from_body(value)

            if extracted_price:
                return extracted_price

            normalized_value = normalize_text(value)

            if normalized_value and not is_bad_wholefoods_price_candidate(normalized_value) and not re.search(
                r"^\(?\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)?$",
                normalized_value.lower(),
            ):
                return normalized_value

    try:
        dollar_elements = page.locator("text=/\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/")
        count = min(dollar_elements.count(), 20)

        for index in range(count):
            try:
                text = normalize_text(dollar_elements.nth(index).inner_text(timeout=2000))

                if not text or "$" not in text:
                    continue

                extracted_price = extract_wholefoods_price_block_from_body(text)

                if extracted_price:
                    return extracted_price

            except Exception:
                continue
    except Exception:
        pass

    return None


# Sam's Club price extraction
def extract_sams_club_price_block_from_body(body_text, product_title=None):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    # Sam's Club pages include hidden/related product prices in the body text.
    # Anchor extraction around the visible product title so we do not grab a stale/related price.
    if product_title:
        normalized_title = normalize_text(product_title).lower()
        title_index = None

        for index, line in enumerate(lines):
            if normalize_text(line).lower() == normalized_title:
                title_index = index
                break

        if title_index is not None:
            title_scoped_lines = lines[title_index + 1:title_index + 35]
            title_scoped_price_block = extract_sams_club_price_block_from_lines(title_scoped_lines)

            if title_scoped_price_block:
                return title_scoped_price_block

    return extract_sams_club_price_block_from_lines(lines)


def extract_sams_club_price_block_from_lines(lines):
    lines = [normalize_text(line) for line in lines if normalize_text(line)]

    # Sale price block, for example:
    # "Now $13.86" followed by "$14.77", "$0.77/ea", "$0.91 off".
    # Keep current price + old price + unit price together so old_price_raw does not accidentally become unit_price_raw.
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_sams_price_candidate(clean_line):
            continue

        now_match = re.search(
            r"now\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?",
            clean_line,
            flags=re.IGNORECASE,
        )

        if now_match:
            price_lines = [normalize_text(now_match.group(0))]
            same_line_prices = [match.replace(" ", "") for match in re.findall(
                r"\$\s*(?!0\.00)\d+(?:\.\d{2})?",
                clean_line,
                flags=re.IGNORECASE,
            )]

            if len(same_line_prices) >= 2:
                price_lines.append(same_line_prices[1])

            for next_index in range(index + 1, min(index + 8, len(lines))):
                next_line = normalize_text(lines[next_index])

                if not next_line or "$" not in next_line:
                    continue

                if is_bad_sams_price_candidate(next_line):
                    continue

                next_lower_line = next_line.lower()

                if "off" in next_lower_line or "save" in next_lower_line:
                    continue

                unit_price_match = re.search(
                    r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)",
                    next_line,
                    flags=re.IGNORECASE,
                )

                standalone_old_price_match = re.search(
                    r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?$",
                    next_line,
                    flags=re.IGNORECASE,
                )

                if standalone_old_price_match and len(price_lines) == 1:
                    price_lines.append(normalize_text(standalone_old_price_match.group(0)))
                    continue

                if unit_price_match:
                    price_lines.append(normalize_text(unit_price_match.group(0)))
                    break

            return normalize_text(" ".join(price_lines))

    # Average-weight product price, for example: "$28.14 avg. price".
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_sams_price_candidate(clean_line):
            continue

        lower_line = clean_line.lower()

        if re.search(r"/\s*(?:lb|oz|ounce|count|ct|each|ea)\b", lower_line):
            continue

        avg_price_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?\s+avg\.\s*price$",
            clean_line,
            flags=re.IGNORECASE,
        )

        if avg_price_match:
            price_lines = [clean_line]

            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = normalize_text(lines[next_index])

                if next_line and re.search(
                    r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)",
                    next_line,
                    flags=re.IGNORECASE,
                ):
                    price_lines.append(next_line)
                    break

            return normalize_text(" ".join(price_lines))

    # Standard product price, for example: "$22.68".
    for index, line in enumerate(lines):
        clean_line = normalize_text(line)

        if not clean_line or "$" not in clean_line:
            continue

        if is_bad_sams_price_candidate(clean_line):
            continue

        lower_line = clean_line.lower()

        if re.search(r"/\s*(?:lb|oz|ounce|count|ct|each|ea)\b", lower_line):
            continue

        if "prices may vary" in lower_line:
            continue

        if "shipping" in lower_line or "pickup" in lower_line or "delivery" in lower_line:
            continue

        if "off" in lower_line or "save" in lower_line:
            continue

        standard_price_match = re.search(
            r"^\$\s*(?!0\.00)\d+(?:\.\d{2})?$",
            clean_line,
            flags=re.IGNORECASE,
        )

        if standard_price_match:
            price_lines = [clean_line]

            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = normalize_text(lines[next_index])

                if next_line and re.search(
                    r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)",
                    next_line,
                    flags=re.IGNORECASE,
                ):
                    price_lines.append(next_line)
                    break

            return normalize_text(" ".join(price_lines))

    joined_text = normalize_text(" ".join(lines))

    if joined_text:
        sale_price_with_old_and_unit_match = re.search(
            r"now\s*\$\s*(?!0\.00)\d+(?:\.\d{2})?\s+"
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s+"
            r"\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)",
            joined_text,
            flags=re.IGNORECASE,
        )

        if sale_price_with_old_and_unit_match:
            return normalize_text(sale_price_with_old_and_unit_match.group(0))

        product_price_before_unit_match = re.search(
            r"(\$\s*(?!0\.00)\d+(?:\.\d{2})?)(?:\s+avg\.\s*price)?\s+\$\s*(?!0\.00)\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)",
            joined_text,
            flags=re.IGNORECASE,
        )

        if product_price_before_unit_match:
            return normalize_text(product_price_before_unit_match.group(0))

    return None


def extract_sams_club_price_block(page, product_title=None):
    body_text = get_body_text(page)
    body_price_block = extract_sams_club_price_block_from_body(
        body_text,
        product_title=product_title,
    )

    if body_price_block:
        return body_price_block

    selectors = [
        '[data-testid="product-price"]',
        '[data-testid="price"]',
        '[itemprop="price"]',
        '.price',
        'text=/Now\\s*\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/',
        'text=/\\$\\s*(?!0\\.00)\\d+(?:\\.\\d{2})?/',
    ]

    for selector in selectors:
        value = safe_inner_text(page, selector)

        if value and "$" in value:
            extracted_price = extract_sams_club_price_block_from_body(
                value,
                product_title=product_title,
            )

            if extracted_price:
                return extracted_price

            normalized_value = normalize_text(value)

            if normalized_value and not is_bad_sams_price_candidate(normalized_value) and not re.search(
                r"/\s*(?:lb|oz|ounce|count|ct|each|ea)\b",
                normalized_value.lower(),
            ):
                return normalized_value

    return None


def extract_wholefoods_main_price(price_block_text):
    price_block_text = normalize_text(price_block_text)

    if not price_block_text:
        return None

    total_est_match = re.search(
        r"total\s+est\.\s+price:\s*\$?\s*(\d+(?:\.\d{2})?)",
        price_block_text,
        flags=re.IGNORECASE,
    )

    if total_est_match:
        return f"${total_est_match.group(1)}"

    return extract_main_price_from_text(price_block_text)

# New function: extract_current_price_from_text
def extract_current_price_from_text(price_block_text, store_key=None):
    price_block_text = normalize_text(price_block_text)

    if not price_block_text:
        return None

    lower_text = price_block_text.lower()
    dollar_matches = re.findall(r"\$\s*(?!0\.00)\d+(?:\.\d{2})?", price_block_text)

    if not dollar_matches:
        return None

    cleaned_matches = [match.replace(" ", "") for match in dollar_matches]

    # HEB sale block example: "Sale $1.25 $0.97 each".
    # The last price is the active sale price.
    if store_key == "heb" and (
        "sale" in lower_text
        or "coupon" in lower_text
        or "deal" in lower_text
        or "price cut" in lower_text
    ) and len(cleaned_matches) >= 2:
        return cleaned_matches[-1]

    # Sam's Club sale block example: "Now $12.72 $13.68".
    # The first price is the active sale price.
    if store_key == "sams_club" and "now" in lower_text:
        return cleaned_matches[0]

    if store_key == "walmart" and "now" in lower_text:
        return cleaned_matches[0]

    # Whole Foods weighted sale example:
    # "$1.66/lb $1.99/lb | Total est. price: $1.33"
    # For weighted items, keep the sale unit price as price_raw because that is the comparable shelf price.
    if store_key == "whole_foods":
        return cleaned_matches[0]

    return cleaned_matches[0]


_PRICE_BLOCK_EXTRACTORS = {
    "walmart": lambda page, title: extract_walmart_price_block(page, product_title=title),
    "heb": lambda page, title: extract_heb_price_block(page),
    "whole_foods": lambda page, title: extract_wholefoods_price_block(page),
    "sams_club": lambda page, title: extract_sams_club_price_block(page, product_title=title),
}


def extract_product_fields(page, store_key):
    title = safe_inner_text(page, "h1")
    body_text = get_body_text(page)

    extractor = _PRICE_BLOCK_EXTRACTORS.get(store_key)
    price_block_text = extractor(page, title) if extractor else None

    price = extract_current_price_from_text(price_block_text, store_key=store_key)
    old_price = extract_old_price_from_text(price_block_text, store_key=store_key)
    unit_price = extract_unit_price_from_text(price_block_text) or extract_unit_price_from_text(body_text)

    if store_key == "heb":
        coupon_text, coupon_value = extract_heb_coupon_info(body_text)
    else:
        coupon_text, coupon_value = None, None

    price_context = classify_price_context(price_block_text)
    availability = extract_availability_from_text(
        body_text,
        store_key,
        price=price,
        price_block_text=price_block_text,
    )

    return (
        title,
        price,
        old_price,
        unit_price,
        coupon_text,
        coupon_value,
        price_block_text,
        price_context,
        availability,
    )


def build_base_result(row, store_name, scrape_session_id, attempt_count):
    scrape_datetime = datetime.now()
    return {
        "snapshot_id": make_snapshot_id(
            store_name,
            row.get("basket_item_id"),
            scrape_datetime,
        ),
        "scrape_session_id": scrape_session_id,
        "scrape_datetime": scrape_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        "store_name": store_name,
        "basket_item_id": row.get("basket_item_id"),
        "canonical_name": get_canonical_name(row),
        "product_url": row.get("product_url"),
        "product_title_raw": None,
        "price_raw": None,
        "old_price_raw": None,
        "unit_price_raw": None,
        "coupon_text_raw": None,
        "coupon_value_raw": None,
        "price_block_raw": None,
        "price_context_raw": None,
        "availability_raw": None,
        "status": FAILED_STATUS,
        "error_message": None,
        "attempt_count": attempt_count,
    }


def scrape_product_once(page, row, store_key, store_name, scrape_session_id, attempt_count):
    result = build_base_result(row, store_name, scrape_session_id, attempt_count)

    try:
        page.goto(row["product_url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        if PAGE_SETTLE_MS > 0:
            page.wait_for_timeout(PAGE_SETTLE_MS)

        body_text = get_body_text(page)

        if is_blocked_page(body_text):
            result["status"] = BLOCKED_STATUS
            result["availability_raw"] = None
            result["error_message"] = "Bot protection page detected"
            return result

        (
            title,
            price,
            old_price,
            unit_price,
            coupon_text,
            coupon_value,
            price_block_text,
            price_context,
            availability,
        ) = extract_product_fields(page, store_key)

        result["product_title_raw"] = title
        result["price_raw"] = price
        result["old_price_raw"] = old_price
        result["unit_price_raw"] = unit_price
        result["coupon_text_raw"] = coupon_text
        result["coupon_value_raw"] = coupon_value
        result["price_block_raw"] = price_block_text
        result["price_context_raw"] = price_context
        result["availability_raw"] = availability

        print(f"{store_name} title raw: {title}")
        print(f"{store_name} price block raw: {price_block_text}")
        print(f"{store_name} parsed price: {price}")
        print(f"{store_name} parsed old price: {old_price}")
        print(f"{store_name} parsed unit price: {unit_price}")
        print(f"{store_name} parsed coupon text: {coupon_text}")
        print(f"{store_name} parsed coupon value: {coupon_value}")
        print(f"{store_name} price context: {price_context}")
        print(f"{store_name} availability: {availability}")

        if title and price:
            result["status"] = SUCCESS_STATUS
        else:
            result["status"] = FAILED_STATUS
            result["error_message"] = "Missing title or price"

    except PlaywrightTimeoutError:
        result["status"] = FAILED_STATUS
        result["error_message"] = "Timeout while loading page"

    except Exception as error:
        result["status"] = FAILED_STATUS
        result["error_message"] = str(error)

    return result


def scrape_with_retries(page, row, store_key, store_name, scrape_session_id):
    last_result = None

    for attempt_number in range(1, MAX_RETRIES + 1):
        print(f"{store_name} scrape attempt {attempt_number}/{MAX_RETRIES}.")

        result = scrape_product_once(
            page,
            row,
            store_key,
            store_name,
            scrape_session_id,
            attempt_number,
        )

        last_result = result

        if result["status"] == SUCCESS_STATUS:
            return result

        if attempt_number < MAX_RETRIES:
            if result["status"] == BLOCKED_STATUS:
                print(
                    f"Attempt {attempt_number} was blocked for {store_name}. "
                    f"Retrying same product in {RETRY_WAIT_MS / 1000:.0f} second(s)."
                )
            else:
                print(
                    f"Attempt {attempt_number} failed for {store_name}. "
                    f"Retrying same product in {RETRY_WAIT_MS / 1000:.0f} second(s)."
                )

            page.wait_for_timeout(RETRY_WAIT_MS)

    if last_result is None:
        last_result = build_base_result(
            row,
            store_name,
            scrape_session_id,
            attempt_count=MAX_RETRIES,
        )
        last_result["status"] = FAILED_STATUS
        last_result["error_message"] = "Scrape failed before an attempt result was created"

    last_result["attempt_count"] = MAX_RETRIES

    if last_result["status"] == BLOCKED_STATUS:
        print(
            f"All {MAX_RETRIES} attempts were blocked for {store_name}. "
            "Moving on without counting this as a failed product scrape."
        )
    else:
        print(
            f"All {MAX_RETRIES} attempts failed for {store_name}. "
            "Recording as one failed scrape event."
        )
        last_result["status"] = FAILED_STATUS

    return last_result


def validate_input_file(urls_df, store_name):
    required_columns = {"basket_item_id", "product_url"}
    missing_columns = required_columns - set(urls_df.columns)

    if missing_columns:
        raise ValueError(f"{store_name}: missing required columns: {missing_columns}")

    if "canonical_name" not in urls_df.columns and "canonical_name_suggestion" not in urls_df.columns:
        raise ValueError(f"{store_name}: missing either canonical_name or canonical_name_suggestion")


def load_store_urls(config):
    input_file = config["input_file"]

    if not input_file.exists():
        print(f"Skipping {config['store_name']}: missing file {input_file}")
        return None

    try:
        urls_df = pd.read_csv(input_file, dtype={"basket_item_id": "string"})
    except EmptyDataError:
        print(f"Skipping {config['store_name']}: empty file {input_file}")
        return None

    validate_input_file(urls_df, config["store_name"])

    original_row_count = len(urls_df)

    urls_df["product_url"] = urls_df["product_url"].astype("string").str.strip()

    urls_df = urls_df[
        urls_df["product_url"].notna()
        & (urls_df["product_url"] != "")
    ].copy()

    filtered_row_count = len(urls_df)

    if filtered_row_count < original_row_count:
        skipped_count = original_row_count - filtered_row_count
        print(
            f"{config['store_name']}: skipped {skipped_count} row(s) with missing product_url."
        )

    if urls_df.empty:
        print(
            f"Skipping {config['store_name']}: no valid product_url rows found in {input_file}"
        )
        return None

    urls_df = normalize_scraped_flags(urls_df)
    save_url_progress(urls_df, input_file)

    return urls_df


def scrape_one_product_for_store(
    playwright,
    store_key,
    config,
    scrape_session_id,
    skipped_row_indexes_by_store,
):
    store_name = config["store_name"]
    urls_df = load_store_urls(config)

    if urls_df is None:
        return "no_file"

    skipped_row_indexes = skipped_row_indexes_by_store.get(store_key, set())
    unscraped_rows = get_unscraped_rows(urls_df, skipped_row_indexes)

    if unscraped_rows.empty:
        print(f"\nNo unscraped {store_name} items left.")
        return "done"

    file_paths = get_session_file_paths(
        store_key,
        config["raw_dir"],
        config["processed_file"],
        scrape_session_id,
    )

    row_index, row = next(iter(unscraped_rows.iterrows()))
    canonical_name = get_canonical_name(row)

    print(f"\nOpening {store_name}: {canonical_name}")

    using_remote_debugging_chrome = False

    if USE_REMOTE_DEBUGGING_CHROME:
        browser = playwright.chromium.connect_over_cdp(REMOTE_DEBUGGING_URL)
        print("Connected to existing personal Chrome session through remote debugging.")
        context = browser.contexts[0]
        page = context.new_page()
        using_remote_debugging_chrome = True

    elif USE_PERSONAL_CHROME_PROFILE:
        if not PERSONAL_CHROME_USER_DATA_DIR.exists():
            raise FileNotFoundError(
                f"Personal Chrome profile directory not found: {PERSONAL_CHROME_USER_DATA_DIR}"
            )

        browser = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PERSONAL_CHROME_USER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=[f"--profile-directory={PERSONAL_CHROME_PROFILE_DIRECTORY}"],
        )
        page = browser.new_page()

    else:
        browser_profile_dir = config["browser_profile_dir"]
        browser_profile_dir.mkdir(parents=True, exist_ok=True)

        browser = playwright.chromium.launch_persistent_context(
            user_data_dir=str(browser_profile_dir),
            headless=False,
        )
        page = browser.new_page()

    try:
        result = scrape_with_retries(
            page,
            row,
            store_key,
            store_name,
            scrape_session_id,
        )

        append_result_files(result, file_paths, config["processed_file"])
        mark_row_progress(urls_df, row_index, result, config["input_file"])

        if result["status"] == SUCCESS_STATUS:
            print(f"🟢 SCRAPED: {store_name} | {canonical_name}")
            return "success"

        # Both BLOCKED and FAILED share the same skip-cycle and row-skip logic.
        is_blocked = result["status"] == BLOCKED_STATUS
        outcome_label = "BLOCKED" if is_blocked else "FAILED"
        outcome_key = "blocked" if is_blocked else "failed"
        extra = "" if is_blocked else f" | {result['error_message']}"

        print(f"🔴 {outcome_label}: {store_name} | {canonical_name}{extra}")

        if SKIP_ROUND_IF_FAILED:
            print(f"{store_name} will be skipped for one full cycle.")
        else:
            print(f"{store_name} will not be skipped because SKIP_ROUND_IF_FAILED is False.")

        if not bool(urls_df.loc[row_index, SCRAPED_COLUMN]):
            skipped_row_indexes_by_store.setdefault(store_key, set()).add(row_index)
            print(
                f"{canonical_name} already received {MAX_RETRIES} attempt(s) in this run. "
                "It will be skipped for the rest of this run, but it can be retried next time you run the scraper."
            )

        return outcome_key

    finally:
        try:
            if page is not None and not page.is_closed():
                close_page_after_random_delay(page)
        except Exception as error:
            print(f"Could not close scraped page cleanly: {error}")

        if not using_remote_debugging_chrome:
            browser.close()


def main():
    scrape_session_id = make_scrape_session_id()
    skip_cycles_remaining = {store_key: 0 for store_key in STORES}
    skipped_row_indexes_by_store = {store_key: set() for store_key in STORES}

    with sync_playwright() as playwright:
        while True:
            active_store_count = 0
            attempted_store_count = 0

            for store_key, config in STORES.items():
                if not STORE_SCRAPE_FLAGS.get(store_key, True):
                    print(f"Skipping {config['store_name']} because its scrape flag is False.")
                    continue

                if skip_cycles_remaining.get(store_key, 0) > 0:
                    print(f"Skipping {config['store_name']} for this cycle after a previous failure/block.")
                    skip_cycles_remaining[store_key] -= 1
                    continue

                result_status = scrape_one_product_for_store(
                    playwright,
                    store_key,
                    config,
                    scrape_session_id,
                    skipped_row_indexes_by_store,
                )

                if result_status in {"success", "failed", "blocked"}:
                    attempted_store_count += 1

                if result_status == "success":
                    active_store_count += 1
                elif result_status in {"failed", "blocked"} and SKIP_ROUND_IF_FAILED:
                    skip_cycles_remaining[store_key] = 1

                if result_status in {"success", "failed", "blocked"}:
                    interval_seconds = random.randint(
                        MIN_PRODUCT_INTERVAL_SECONDS,
                        MAX_PRODUCT_INTERVAL_SECONDS,
                    )
                    print(
                        f"Waiting {interval_seconds / 60:.1f} minute(s) "
                        "before opening the next store."
                    )
                    time.sleep(interval_seconds)

            if active_store_count == 0 and attempted_store_count == 0:
                print("\nNo active store items left to scrape.")
                break

    print("\nCombined scrape run complete.")
    print(f"Session ID: {scrape_session_id}")


if __name__ == "__main__":
    main()