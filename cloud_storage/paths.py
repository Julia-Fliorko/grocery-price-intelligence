from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

from cloud_storage.config import SUPPORTED_STORES


_SAFE_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


def validate_store(store: str) -> str:
    """
    Validate and normalize a supported store identifier.
    """
    normalized_store = store.strip().lower()

    if normalized_store not in SUPPORTED_STORES:
        supported = ", ".join(sorted(SUPPORTED_STORES))
        raise ValueError(
            f"Unsupported store '{store}'. "
            f"Expected one of: {supported}"
        )

    return normalized_store


def sanitize_identifier(value: str) -> str:
    """
    Convert an identifier into a safe blob-name component.
    """
    cleaned = _SAFE_ID_PATTERN.sub("_", value.strip())
    cleaned = cleaned.strip("_")

    if not cleaned:
        raise ValueError("Identifier cannot be empty.")

    return cleaned


def build_bronze_blob_name(
    *,
    store: str,
    local_file: Path,
    scrape_session_id: str,
    timestamp: datetime | None = None,
) -> str:
    """
    Build the canonical Bronze blob path.

    Example:
    grocery_prices/walmart/year=2026/month=07/day=29/
    walmart_20260729T151000Z_session123.csv
    """
    normalized_store = validate_store(store)
    safe_session_id = sanitize_identifier(scrape_session_id)

    event_time = timestamp or datetime.now(timezone.utc)

    if event_time.tzinfo is None:
        raise ValueError("Timestamp must include timezone information.")

    event_time = event_time.astimezone(timezone.utc)

    suffix = local_file.suffix.lower()

    if not suffix:
        raise ValueError(
            f"Local file must have an extension: {local_file}"
        )

    filename = (
        f"{normalized_store}_"
        f"{event_time:%Y%m%dT%H%M%SZ}_"
        f"{safe_session_id}"
        f"{suffix}"
    )

    return (
        f"grocery_prices/{normalized_store}/"
        f"year={event_time:%Y}/"
        f"month={event_time:%m}/"
        f"day={event_time:%d}/"
        f"{filename}"
    )