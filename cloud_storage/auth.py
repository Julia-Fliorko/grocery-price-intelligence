from __future__ import annotations
from azure.identity import DefaultAzureCredential
from azure.core.credentials import TokenCredential

def get_azure_credential() -> TokenCredential:
    """
    Return the credential used for Azure SDK requests.

    During local development, DefaultAzureCredential can use the
    identity authenticated through `az login`.

    When deployed to Azure later, the same code can use a managed
    identity without storing credentials in the repository.
    """
    return DefaultAzureCredential()