from __future__ import annotations

from pathlib import Path
from typing import Mapping

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobClient, BlobServiceClient

from cloud_storage.auth import get_azure_credential
from cloud_storage.config import ACCOUNT_URL, BRONZE_CONTAINER
from cloud_storage.paths import build_bronze_blob_name


def get_blob_service_client() -> BlobServiceClient:
    """
    Create an authenticated Azure Blob Storage service client.
    """
    return BlobServiceClient(
        account_url=ACCOUNT_URL,
        credential=get_azure_credential(),
    )


def get_blob_client(
    *,
    container_name: str,
    blob_name: str,
) -> BlobClient:
    """
    Create a client for one blob.
    """
    service_client = get_blob_service_client()

    return service_client.get_blob_client(
        container=container_name,
        blob=blob_name,
    )


def upload_file(
    *,
    local_file: Path,
    container_name: str,
    blob_name: str,
    metadata: Mapping[str, str] | None = None,
    overwrite: bool = False,
) -> str:
    """
    Upload one local file to Azure Blob Storage.

    Existing blobs are not overwritten unless overwrite=True is
    explicitly provided.
    """
    local_file = local_file.resolve()

    if not local_file.exists():
        raise FileNotFoundError(
            f"Local file was not found: {local_file}"
        )

    if not local_file.is_file():
        raise ValueError(
            f"Expected a file but received: {local_file}"
        )

    blob_client = get_blob_client(
        container_name=container_name,
        blob_name=blob_name,
    )

    with local_file.open("rb") as file_data:
        blob_client.upload_blob(
            data=file_data,
            overwrite=overwrite,
            metadata=dict(metadata or {}),
        )

    return blob_name


def upload_bronze_file(
    *,
    local_file: Path,
    store: str,
    scrape_session_id: str,
    metadata: Mapping[str, str] | None = None,
) -> str:
    """
    Upload one immutable source file into the Bronze layer.
    """
    blob_name = build_bronze_blob_name(
        store=store,
        local_file=local_file,
        scrape_session_id=scrape_session_id,
    )

    bronze_metadata = {
        "source_store": store,
        "pipeline_layer": "bronze",
        "scrape_session_id": scrape_session_id,
        "source_format": local_file.suffix.lower().lstrip("."),
        "project": "grocery_price_intelligence",
        "schema_version": "1",
    }

    if metadata:
        bronze_metadata.update(metadata)

    return upload_file(
        local_file=local_file,
        container_name=BRONZE_CONTAINER,
        blob_name=blob_name,
        metadata=bronze_metadata,
        overwrite=False,
    )


def main() -> None:
    """
    Run a controlled upload test.
    """
    local_file = Path("azure_connection_test.txt")

    try:
        blob_name = upload_bronze_file(
            local_file=local_file,
            store="walmart",
            scrape_session_id="connection_test",
            metadata={
                "upload_purpose": "connection_test",
            },
        )
    except ResourceExistsError:
        print("Upload stopped: the blob already exists.")
        print("No Azure data was overwritten.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"Upload failed: {exc}")
        raise SystemExit(1)

    print("Upload succeeded.")
    print(f"Container: {BRONZE_CONTAINER}")
    print(f"Blob: {blob_name}")


if __name__ == "__main__":
    main()

    