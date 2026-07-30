from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Supports both:
#   python pipeline/bootstrap_master_to_bronze.py
#   python -m pipeline.bootstrap_master_to_bronze
if __package__ in (None, ""):
    repository_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository_root))

from cloud_storage.ingestion.batch_builder import (
    build_master_batches,
    new_migration_id,
)
from cloud_storage.ingestion.config import (
    DEFAULT_BRONZE_CONTAINER,
    DEFAULT_MASTER_PATH,
    DEFAULT_RECEIPT_ROOT,
    DEFAULT_STAGING_ROOT,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct historical immutable daily batches from the master "
            "snapshot CSV and upload them into Azure Bronze by store/date."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_MASTER_PATH,
        help=f"Master snapshot CSV. Default: {DEFAULT_MASTER_PATH}",
    )
    parser.add_argument(
        "--storage-account",
        default=os.getenv("AZURE_STORAGE_ACCOUNT_NAME"),
        help=(
            "Azure Storage account name. Defaults to "
            "AZURE_STORAGE_ACCOUNT_NAME."
        ),
    )
    parser.add_argument(
        "--container",
        default=os.getenv(
            "AZURE_BRONZE_CONTAINER",
            DEFAULT_BRONZE_CONTAINER,
        ),
        help=f"Azure Blob container. Default: {DEFAULT_BRONZE_CONTAINER}",
    )
    parser.add_argument(
        "--staging-root",
        type=Path,
        default=DEFAULT_STAGING_ROOT,
        help=f"Temporary batch root. Default: {DEFAULT_STAGING_ROOT}",
    )
    parser.add_argument(
        "--receipt-root",
        type=Path,
        default=DEFAULT_RECEIPT_ROOT,
        help=f"Upload receipt root. Default: {DEFAULT_RECEIPT_ROOT}",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Validate and split the master file without connecting to Azure.",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help=(
            "Validate grouping and duplicate handling without writing the "
            "historical daily CSV files or connecting to Azure."
        ),
    )
    return parser.parse_args()


def print_build_summary(summary) -> None:
    print("\nHistorical daily Bronze bootstrap")
    print("---------------------------------")
    print(f"Source: {summary.source_path}")
    print(f"Source rows: {summary.source_row_count:,}")
    print(f"Prepared rows: {summary.prepared_row_count:,}")
    print(f"Duplicates removed: {summary.duplicate_rows_removed:,}")
    print(f"Historical daily batch files: {summary.batch_count:,}")
    print(f"Staging: {summary.staging_directory}")
    print(f"Receipt: {summary.receipt_path}")
    print(f"Duplicate audit: {summary.duplicate_audit_path}")

    for store, counts in sorted(summary.counts_by_store().items()):
        print(
            f"  {store:<12} "
            f"{counts['files']:>4} files  "
            f"{counts['source_rows']:>7,} source rows  "
            f"{counts['prepared_rows']:>7,} prepared rows"
        )


def main() -> int:
    args = parse_args()
    migration_id = new_migration_id()

    try:
        summary = build_master_batches(
            source_path=args.source,
            staging_root=args.staging_root,
            receipt_root=args.receipt_root,
            migration_id=migration_id,
            write_files=not args.audit_only,
        )
        print_build_summary(summary)

        if args.audit_only:
            print("\nAudit succeeded. No daily CSV files were generated.")
            print("No Azure upload performed.")
            return 0

        if args.prepare_only:
            print("\nPreparation succeeded. No Azure upload performed.")
            return 0

        if not args.storage_account:
            raise ValueError(
                "Storage account name is required. Pass --storage-account "
                "or set AZURE_STORAGE_ACCOUNT_NAME."
            )

        # Lazy imports let --prepare-only run before Azure packages are installed.
        from cloud_storage.azure.blob_client import AzureBlobStorageClient
        from cloud_storage.ingestion.uploader import (
            upload_batches,
            write_success_receipt,
        )

        client = AzureBlobStorageClient(args.storage_account)

        results = upload_batches(
            client=client,
            container_name=args.container,
            migration_id=migration_id,
            summary=summary,
        )

        receipt = write_success_receipt(
            receipt_root=args.receipt_root,
            migration_id=migration_id,
            container_name=args.container,
            storage_account_name=args.storage_account,
            summary=summary,
            results=results,
        )

        uploaded = sum(result.status == "uploaded" for result in results)
        already_present = sum(
            result.status == "already_present" for result in results
        )

        print("\nHistorical Bronze bootstrap completed successfully.")
        print(f"New blobs uploaded: {uploaded:,}")
        print(f"Matching blobs already present: {already_present:,}")
        print(f"Verified rows: {summary.prepared_row_count:,}")
        print(f"Receipt: {receipt}")
        return 0

    except Exception as error:
        print(f"\nBootstrap failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
