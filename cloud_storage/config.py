from __future__ import annotations

import os


STORAGE_ACCOUNT_NAME = os.getenv(
    "AZURE_STORAGE_ACCOUNT_NAME",
    "stgrocerydev",
)

ACCOUNT_URL = (
    f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
)

BRONZE_CONTAINER = os.getenv(
    "AZURE_BRONZE_CONTAINER",
    "bronze",
)

SILVER_CONTAINER = os.getenv(
    "AZURE_SILVER_CONTAINER",
    "silver",
)

GOLD_CONTAINER = os.getenv(
    "AZURE_GOLD_CONTAINER",
    "gold",
)

LOGS_CONTAINER = os.getenv(
    "AZURE_LOGS_CONTAINER",
    "logs",
)

SUPPORTED_STORES = frozenset(
    {
        "walmart",
        "heb",
        "whole_foods",
        "sams_club",
        "amazon",
    }
)