from pathlib import Path
from datetime import datetime
import sys
import termios
import tty
import re
import random
import time
import pandas as pd
from pandas.errors import EmptyDataError
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


INPUT_FILE = Path("config/product_urls/wholefoods_urls.csv")
RAW_DIR = Path("data/raw/whole_foods")
PROCESSED_FILE = Path("data/processed/price_snapshots_whole_foods.csv")
BROWSER_PROFILE_DIR = Path("browser_profiles/whole_foods")

STORE_NAME = "Whole Foods"
USE_REMOTE_DEBUGGING_CHROME = True
REMOTE_DEBUGGING_URL = "http://127.0.0.1:9222"

MAX_RETRIES = 2
PAGE_TIMEOUT_MS = 60000
TEXT_TIMEOUT_MS = 10000
PAGE_SETTLE_MS = 0
RETRY_WAIT_MS = 3000

MIN_PRODUCT_INTERVAL_SECONDS = 60
MAX_PRODUCT_INTERVAL_SECONDS = 300
BLOCKED_NEXT_INTERVAL_SECONDS = 900

MIN_PAGE_CLOSE_DELAY_SECONDS = 0
MAX_PAGE_CLOSE_DELAY_SECONDS = 60

SCRAPED_COLUMN = "scraped"
LAST_SCRAPE_STATUS_COLUMN = "last_scrape_status"
LAST_SCRAPE_DATETIME_COLUMN = "last_scrape_datetime"

BLOCKED_STATUS = "blocked"
FAILED_STATUS = "failed"
SUCCESS_STATUS = "success"

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
]


RAW_COLUMNS = [
    "scrape_session_id",
    "scrape_datetime",
    "store_name",
    "basket_item_id",
    "canonical_name",
    "product_url",
    "product_title_raw",
    "price_raw",
    "unit_price_raw",
    "price_block_raw",
    "price_context_raw",
    "availability_raw",
    "status",
    "error_message",
    "attempt_count",
]


PROCESSED_COLUMNS = [
    "scrape_session_id",
    "scrape_datetime",
    "store_name",
    "basket_item_id",
    "canonical_name",
    "product_url",
    "product_title_raw",
    "price_raw",
    "unit_price_raw",
    "price_block_raw",
    "price_context_raw",
    "availability_raw",
    "status",
]


def make_scrape_session_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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

    urls_df[LAST_SCRAPE_STATUS_COLUMN] = urls_df[LAST_SCRAPE_STATUS_COLUMN].astype("string")
    urls_df[LAST_SCRAPE_DATETIME_COLUMN] = urls_df[LAST_SCRAPE_DATETIME_COLUMN].astype("string")

    urls_df[SCRAPED_COLUMN] = (
        urls_df[SCRAPED_COLUMN]
        .fillna(False)
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes", "y"])
    )

    return urls_df


def get_unscraped_rows(urls_df):
    return urls_df[urls_df[SCRAPED_COLUMN] == False]


def save_url_progress(urls_df):
    urls_df.to_csv(INPUT_FILE, index=False)


def mark_row_progress(urls_df, row_index, result):
    urls_df.loc[row_index, LAST_SCRAPE_STATUS_COLUMN] = result["status"]
    urls_df.loc[row_index, LAST_SCRAPE_DATETIME_COLUMN] = result["scrape_datetime"]

    if result["status"] == SUCCESS_STATUS:
        urls_df.loc[row_index, SCRAPED_COLUMN] = True

    save_url_progress(urls_df)


def reset_scraped_flags(urls_df):
    urls_df[SCRAPED_COLUMN] = False
    urls_df[LAST_SCRAPE_STATUS_COLUMN] = None
    urls_df[LAST_SCRAPE_DATETIME_COLUMN] = None
    save_url_progress(urls_df)


def read_single_key():
    if not sys.stdin.isatty():
        value = input("Type 'reset' to remove scraping flags, or press Enter to continue: ").strip().lower()
        return "backspace" if value == "reset" else "enter"

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)
        key = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if key in ("\x7f", "\b"):
        return "backspace"

    if key in ("\r", "\n"):
        return "enter"

    return key


def handle_bottom_of_list(urls_df):
    while True:
        unscraped_count = len(get_unscraped_rows(urls_df))

        if unscraped_count == 0:
            print("\nNo unscraped Whole Foods items left.")
            print("Press Backspace to remove all scraping flags, or Enter to exit.")
        else:
            print(f"\nReached bottom of the list. Unscraped items left: {unscraped_count}")
            print("Press Backspace to remove all scraping flags, or Enter to check for unscraped items again.")

        action = read_single_key()

        if action == "backspace":
            reset_scraped_flags(urls_df)
            print("\nAll scraping flags were removed from the Whole Foods URL file.")
            return "reset"

        if action == "enter":
            if len(get_unscraped_rows(urls_df)) == 0:
                return "done"
            return "continue"

        print("\nUnrecognized key. Use Backspace or Enter.")


def get_next_interval_seconds(previous_status):
    if previous_status == BLOCKED_STATUS:
        return BLOCKED_NEXT_INTERVAL_SECONDS

    return random.randint(MIN_PRODUCT_INTERVAL_SECONDS, MAX_PRODUCT_INTERVAL_SECONDS)


def close_page_after_random_delay(page):
    if page is None or page.is_closed():
        return

    delay_seconds = random.randint(MIN_PAGE_CLOSE_DELAY_SECONDS, MAX_PAGE_CLOSE_DELAY_SECONDS)

    if delay_seconds > 0:
        print(f"Keeping page open for {delay_seconds} second(s) before closing.")
        time.sleep(delay_seconds)

    if not page.is_closed():
        page.close()


def get_session_file_paths(scrape_session_id):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)

    return {
        "raw": RAW_DIR / f"wholefoods_raw_{scrape_session_id}.csv",
        "failed": RAW_DIR / f"wholefoods_failed_{scrape_session_id}.csv",
    }


def append_row_to_csv(row, file_path, columns):
    row_df = pd.DataFrame([row]).reindex(columns=columns)

    if file_path.exists() and file_path.stat().st_size > 0:
        with open(file_path, "rb+") as file:
            file.seek(-1, 2)
            last_character = file.read(1)

            if last_character != b"\n":
                file.write(b"\n")

    file_exists = file_path.exists() and file_path.stat().st_size > 0
    row_df.to_csv(file_path, mode="a", header=not file_exists, index=False)


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
            print(f"Existing file schema did not match current scraper schema. Backed it up to: {backup_file}")

    append_row_to_csv(row, file_path, columns)


def append_result_files(result, file_paths):
    append_row_to_csv(result, file_paths["raw"], RAW_COLUMNS)

    if result["status"] == FAILED_STATUS:
        append_row_to_csv(result, file_paths["failed"], RAW_COLUMNS)

    if result["status"] == SUCCESS_STATUS:
        processed_row = {column: result.get(column) for column in PROCESSED_COLUMNS}
        append_row_to_csv_with_schema_check(processed_row, PROCESSED_FILE, PROCESSED_COLUMNS)


def normalize_text(value):
    if value is None:
        return None

    return re.sub(r"\s+", " ", str(value)).strip()


def extract_main_price_from_text(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    dollar_matches = re.findall(r"\$\s*\d+(?:\.\d{2})?", price_text)

    if not dollar_matches:
        return None

    return dollar_matches[0].replace(" ", "")


def extract_unit_price_from_text(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    unit_patterns = [
        r"\(?\s*\$\s*\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|fl oz|ounce|each|ct|count|gal)\s*\)?",
        r"\$\s*\d+(?:\.\d{2})?\s*(?:per|/)\s*(?:lb|oz|fl oz|ounce|each|ct|count|gal)",
        r"\d+(?:\.\d+)?\s*¢\s*/\s*(?:lb|oz|fl oz|ounce|each|ct|count|gal)",
        r"\d+(?:\.\d+)?\s*¢\s*(?:per|/)\s*(?:lb|oz|fl oz|ounce|each|ct|count|gal)",
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
            )

    return None


def classify_price_context(price_text):
    price_text = normalize_text(price_text)

    if not price_text:
        return None

    lower_text = price_text.lower()
    context_flags = []

    if "total est. price" in lower_text:
        context_flags.append("estimated_total_price")

    if "final cost by actual weight" in lower_text:
        context_flags.append("final_cost_by_weight")

    if "was" in lower_text or "save" in lower_text or "off" in lower_text:
        context_flags.append("discounted")

    if "sale" in lower_text:
        context_flags.append("sale_price")

    if "prime" in lower_text:
        context_flags.append("prime_offer")

    if not context_flags:
        context_flags.append("standard_price")

    return "|".join(context_flags)


def extract_wholefoods_price_block_from_body(body_text):
    body_text = body_text or ""
    lines = [normalize_text(line) for line in body_text.splitlines()]
    lines = [line for line in lines if line]

    for line in lines:
        # Examples:
        # "$5.49 ($0.40 / ounce) SNAP EBT eligible"
        # "$9.49/lb $9.99/lb | Total est. price: $14.24* SNAP EBT eligible"
        # "$1.79/lb | Total est. price: $0.97* SNAP EBT eligible"
        if "$" in line and (
            "snap ebt" in line.lower()
            or "total est. price" in line.lower()
            or re.search(r"\$\s*\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)", line, flags=re.IGNORECASE)
        ):
            return normalize_text(line)

    joined_text = normalize_text(" ".join(lines))

    if joined_text:
        whole_foods_price_match = re.search(
            r"\$\s*\d+(?:\.\d{2})?\s*\(\s*\$\s*\d+(?:\.\d{2})?\s*/\s*(?:lb|oz|ounce|count|ct|each|ea)\s*\)",
            joined_text,
            flags=re.IGNORECASE,
        )

        if whole_foods_price_match:
            return normalize_text(whole_foods_price_match.group(0))

        simple_price_match = re.search(
            r"\$\s*\d+(?:\.\d{2})?",
            joined_text,
            flags=re.IGNORECASE,
        )

        if simple_price_match and "add to cart" in joined_text.lower():
            return normalize_text(simple_price_match.group(0))

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
        'text=/\\$\\s*\\d+(?:\\.\\d{2})?/',
    ]

    for selector in selectors:
        value = safe_inner_text(page, selector)

        if value and "$" in value:
            return normalize_text(value)

    try:
        dollar_elements = page.locator("text=/\\$\\s*\\d+(?:\\.\\d{2})?/")
        count = min(dollar_elements.count(), 10)

        for index in range(count):
            try:
                text = normalize_text(dollar_elements.nth(index).inner_text(timeout=2000))

                if text and re.search(r"\$\s*\d+(?:\.\d{2})?", text):
                    return text
            except Exception:
                continue
    except Exception:
        pass

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


def build_base_result(row, scrape_session_id, attempt_count=1):
    return {
        "scrape_session_id": scrape_session_id,
        "scrape_datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store_name": STORE_NAME,
        "basket_item_id": row.get("basket_item_id"),
        "canonical_name": get_canonical_name(row),
        "product_url": row.get("product_url"),
        "product_title_raw": None,
        "price_raw": None,
        "unit_price_raw": None,
        "price_block_raw": None,
        "price_context_raw": None,
        "availability_raw": None,
        "status": FAILED_STATUS,
        "error_message": None,
        "attempt_count": attempt_count,
    }


def extract_availability_from_text(body_text, price=None, price_block_text=None):
    body_text = body_text or ""
    price_block_text = price_block_text or ""
    combined_text_lower = f"{body_text.lower()} {price_block_text.lower()}"

    # Whole Foods can show disabled Add to Cart states, so explicit unavailable signals win first.
    if re.search(r"\b0\.00\b", combined_text_lower):
        return "out_of_stock"

    if "currently unavailable" in combined_text_lower:
        return "out_of_stock"

    if "unavailable for delivery" in combined_text_lower:
        return "out_of_stock"

    if "currently not sold" in combined_text_lower:
        return "out_of_stock"

    if "out of stock" in combined_text_lower:
        return "out_of_stock"

    if "sold out" in combined_text_lower:
        return "sold_out"

    if "add to cart" in combined_text_lower or "add to basket" in combined_text_lower:
        if price:
            return "in_stock"
        return "likely_in_stock"

    if "snap ebt eligible" in combined_text_lower:
        return "likely_in_stock"

    if "pickup" in combined_text_lower or "delivery" in combined_text_lower:
        return "likely_in_stock"

    return "unknown"


def extract_product_fields(page):
    title = safe_inner_text(page, "h1")
    body_text = get_body_text(page)
    price_block_text = extract_wholefoods_price_block(page)

    price = extract_wholefoods_main_price(price_block_text)
    unit_price = (
        extract_unit_price_from_text(price_block_text)
        or extract_unit_price_from_text(body_text)
    )
    price_context = classify_price_context(price_block_text)
    availability = extract_availability_from_text(
        body_text,
        price=price,
        price_block_text=price_block_text,
    )

    return title, price, unit_price, price_block_text, price_context, availability, body_text


def scrape_product_once(page, row, scrape_session_id, attempt_count):
    result = build_base_result(row, scrape_session_id, attempt_count=attempt_count)

    try:
        page.goto(row["product_url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

        if PAGE_SETTLE_MS > 0:
            print(f"Waiting {PAGE_SETTLE_MS // 1000} seconds for the page to settle.")
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
            unit_price,
            price_block_text,
            price_context,
            availability,
            body_text,
        ) = extract_product_fields(page)

        result["product_title_raw"] = title
        result["price_raw"] = price
        result["unit_price_raw"] = unit_price
        result["price_block_raw"] = price_block_text
        result["price_context_raw"] = price_context
        result["availability_raw"] = availability

        print(f"Whole Foods title raw: {title}")
        print(f"Whole Foods price block raw: {price_block_text}")
        print(f"Whole Foods parsed price: {price}")
        print(f"Whole Foods parsed unit price: {unit_price}")
        print(f"Whole Foods price context: {price_context}")
        print(f"Whole Foods availability: {availability}")

        if title and price:
            result["status"] = SUCCESS_STATUS
            result["error_message"] = None
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


def scrape_with_retries(page, row, scrape_session_id):
    last_result = None

    for attempt_count in range(1, MAX_RETRIES + 1):
        result = scrape_product_once(page, row, scrape_session_id, attempt_count)
        last_result = result

        if result["status"] == SUCCESS_STATUS:
            return result

        if result["status"] == BLOCKED_STATUS:
            print("Bot protection detected. This row will be saved as blocked; the script will not retry this page.")
            return result

        page.wait_for_timeout(RETRY_WAIT_MS)

    return last_result


def validate_input_file(urls_df):
    required_columns = {"basket_item_id", "product_url"}
    missing_columns = required_columns - set(urls_df.columns)

    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    if "canonical_name" not in urls_df.columns and "canonical_name_suggestion" not in urls_df.columns:
        raise ValueError("Missing required column: either 'canonical_name' or 'canonical_name_suggestion'")


def load_urls():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    try:
        urls_df = pd.read_csv(INPUT_FILE, dtype={"basket_item_id": "string"})
    except EmptyDataError:
        raise ValueError(
            f"{INPUT_FILE} is empty. Add a header row and at least one Whole Foods product URL. "
            "Required minimum columns: basket_item_id, canonical_name, product_url"
        )

    validate_input_file(urls_df)

    original_row_count = len(urls_df)
    urls_df["product_url"] = urls_df["product_url"].astype("string").str.strip()
    urls_df = urls_df[
        urls_df["product_url"].notna()
        & (urls_df["product_url"] != "")
    ].copy()

    filtered_row_count = len(urls_df)

    if filtered_row_count < original_row_count:
        skipped_count = original_row_count - filtered_row_count
        print(f"Whole Foods: skipped {skipped_count} row(s) with missing product_url.")

    if urls_df.empty:
        raise ValueError(f"No valid product_url rows found in {INPUT_FILE}")

    urls_df = normalize_scraped_flags(urls_df)
    save_url_progress(urls_df)

    return urls_df


def main():
    urls_df = load_urls()

    unscraped_rows = get_unscraped_rows(urls_df)
    print(f"Loaded {len(urls_df)} Whole Foods product URL(s).")
    print(f"Unscraped Whole Foods product URL(s): {len(unscraped_rows)}")

    scrape_session_id = make_scrape_session_id()
    results = []
    file_paths = get_session_file_paths(scrape_session_id)
    previous_status = None

    using_remote_debugging_chrome = False

    with sync_playwright() as playwright:
        if USE_REMOTE_DEBUGGING_CHROME:
            browser = playwright.chromium.connect_over_cdp(REMOTE_DEBUGGING_URL)
            print("Connected to existing personal Chrome session through remote debugging.")
            context = browser.contexts[0]
            using_remote_debugging_chrome = True
        else:
            BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=False,
            )
            context = browser

        page = None

        while True:
            unscraped_rows = get_unscraped_rows(urls_df)

            if unscraped_rows.empty:
                bottom_action = handle_bottom_of_list(urls_df)

                if bottom_action == "reset":
                    break

                if bottom_action == "done":
                    break

                continue

            for row_index, row in unscraped_rows.iterrows():
                if previous_status is not None:
                    interval_seconds = get_next_interval_seconds(previous_status)
                    interval_minutes = interval_seconds / 60
                    print(f"Waiting {interval_minutes:.1f} minute(s) before opening the next Whole Foods page.")
                    time.sleep(interval_seconds)

                if page is not None and not page.is_closed():
                    page.close()

                page = context.new_page()

                canonical_name = get_canonical_name(row)

                result = scrape_with_retries(page, row, scrape_session_id)
                results.append(result)
                append_result_files(result, file_paths)
                mark_row_progress(urls_df, row_index, result)

                if result["status"] == SUCCESS_STATUS:
                    print("Saved this result to the raw file and processed snapshot file.")
                else:
                    print("Saved this result to the raw file only. It was not added to the processed snapshot file.")

                if result["status"] == SUCCESS_STATUS:
                    print(f"🟢 SCRAPED: {canonical_name}")
                elif result["status"] == BLOCKED_STATUS:
                    print(f"🔴 BLOCKED: {canonical_name}")
                else:
                    print(f"FAILED: {canonical_name} | {result['error_message']}")

                previous_status = result["status"]
                close_page_after_random_delay(page)
                page = None

            bottom_action = handle_bottom_of_list(urls_df)

            if bottom_action in {"reset", "done"}:
                break

        if using_remote_debugging_chrome:
            print("Remote Chrome mode detected. Leaving personal Chrome session fully open.")
        else:
            if page is not None and not page.is_closed():
                page.close()

            browser.close()

    df = pd.DataFrame(results)

    if df.empty:
        success_count = 0
        failed_count = 0
        blocked_count = 0
    else:
        success_count = len(df[df["status"] == SUCCESS_STATUS])
        failed_count = len(df[df["status"] == FAILED_STATUS])
        blocked_count = len(df[df["status"] == BLOCKED_STATUS])

    remaining_unscraped_count = len(get_unscraped_rows(urls_df))

    print("\nScrape summary")
    print(f"Session ID: {scrape_session_id}")
    print(f"Total products processed in this run: {len(results)}")
    print(f"Scraped successfully: {success_count}")
    print(f"Blocked: {blocked_count}")
    print(f"Failed: {failed_count}")
    print(f"Still left unscraped: {remaining_unscraped_count}")
    print(f"Raw file: {file_paths['raw']}")
    print(f"Processed snapshot file: {PROCESSED_FILE}")

    if file_paths["failed"].exists():
        print(f"Failed file: {file_paths['failed']}")


if __name__ == "__main__":
    main()
