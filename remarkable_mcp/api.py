"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
_REMARKABLE_USE_SSH = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"


class FallbackClient:
    """A resilient client wrapping primary and backup reMarkable transports.

    Attempts operations using the preferred client first, and automatically falls back
    to the secondary client if the primary connection fails or is disconnected.
    """

    def __init__(
        self,
        primary_client: Any,
        backup_client: Optional[Any] = None,
        primary_name: str = "USB SSH",
        backup_name: str = "reMarkable Cloud",
    ):
        self.primary = primary_client
        self.backup = backup_client
        self.primary_name = primary_name
        self.backup_name = backup_name
        self.active = primary_client

    def get_meta_items(self, limit: Optional[int] = None) -> List[Any]:
        """Fetch notebook metadata, falling back to backup client if primary fails."""
        try:
            return self.active.get_meta_items(limit=limit)
        except Exception as e:
            if self.backup and self.active is not self.backup:
                logger.warning(
                    f"{self.primary_name} get_meta_items failed ({e}). Falling back to {self.backup_name}..."
                )
                print(f"ℹ️ {self.primary_name} failed ({e}). Falling back to {self.backup_name}...")
                self.active = self.backup
                return self.active.get_meta_items(limit=limit)
            raise

    def download(self, doc: Any) -> bytes:
        """Download document content zip, falling back to backup client if needed."""
        try:
            return self.active.download(doc)
        except Exception as e:
            if self.backup and self.active is not self.backup:
                logger.warning(
                    f"{self.primary_name} download failed ({e}). Falling back to {self.backup_name}..."
                )
                print(
                    f"ℹ️ {self.primary_name} download failed. Falling back to {self.backup_name}..."
                )
                self.active = self.backup
                # Find matching doc in backup if needed
                doc_id = getattr(doc, "id", getattr(doc, "ID", ""))
                backup_doc = None
                if hasattr(self.backup, "get_doc"):
                    backup_doc = self.backup.get_doc(doc_id)
                return self.active.download(backup_doc or doc)
            raise

    def get_doc(self, doc_id: str) -> Optional[Any]:
        """Get document by ID from active client."""
        if hasattr(self.active, "get_doc"):
            return self.active.get_doc(doc_id)
        return None

    def get_file_type(self, doc: Any) -> Optional[str]:
        """Get file type from active client."""
        if hasattr(self.active, "get_file_type"):
            return self.active.get_file_type(doc)
        return None

    def download_raw_file(self, doc: Any, extension: str) -> Optional[bytes]:
        """Download raw file from active client."""
        if hasattr(self.active, "download_raw_file"):
            return self.active.download_raw_file(doc, extension)
        return None


def get_rmapi():
    """
    Get or initialize the reMarkable API client with automatic fallback.

    Uses preferred connection (SSH or Cloud) if available, and falls back to the
    secondary method if the primary fails or is disconnected.
    Returns either RemarkableClient, SSHClient, or FallbackClient.
    """
    # 1. Determine preferred connection mode
    pref_env = os.environ.get("REMARKABLE_PREFERRED_CONNECTION", "").strip().lower()
    use_ssh_env = (
        os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
        or _REMARKABLE_USE_SSH
    )

    if pref_env in ("ssh", "usb"):
        preferred = "ssh"
    elif pref_env in ("cloud", "rmapi"):
        preferred = "cloud"
    elif use_ssh_env:
        preferred = "ssh"
    else:
        token_candidate = (
            os.environ.get("REMARKABLE_TOKEN")
            or REMARKABLE_TOKEN
            or (Path.home() / ".rmapi").exists()
        )
        preferred = "cloud" if token_candidate and not use_ssh_env else "ssh"

    # 2. Instantiate potential clients
    ssh_client = None
    cloud_client = None

    try:
        from remarkable_mcp.ssh import create_ssh_client

        ssh_client = create_ssh_client()
    except Exception as e:
        logger.debug(f"Could not create SSH client: {e}")

    token = os.environ.get("REMARKABLE_TOKEN") or REMARKABLE_TOKEN
    rmapi_file = Path.home() / ".rmapi"
    if not token and rmapi_file.exists():
        try:
            token = rmapi_file.read_text(encoding="utf-8").strip()
        except Exception:
            token = None

    if token:
        try:
            from remarkable_mcp.sync import load_client_from_token

            # Also persist to ~/.rmapi for compatibility
            rmapi_file.write_text(token, encoding="utf-8")
            cloud_client = load_client_from_token(token)
        except Exception as e:
            logger.debug(f"Could not load Cloud client: {e}")

    # 3. Connection selection with fallback
    if preferred == "ssh":
        ssh_available = ssh_client and ssh_client.check_connection()
        if ssh_available:
            if cloud_client:
                return FallbackClient(
                    primary_client=ssh_client,
                    backup_client=cloud_client,
                    primary_name="USB SSH",
                    backup_name="reMarkable Cloud",
                )
            return ssh_client

        # SSH unavailable (e.g. tablet unplugged) — try Cloud backup
        if cloud_client:
            logger.info(
                "USB SSH connection unavailable (tablet not connected). Using reMarkable Cloud..."
            )
            print("ℹ️ USB SSH not connected. Falling back to reMarkable Cloud...")
            return FallbackClient(
                primary_client=cloud_client,
                backup_client=ssh_client,
                primary_name="reMarkable Cloud",
                backup_name="USB SSH",
            )

        raise RuntimeError(
            "Could not connect to reMarkable tablet via USB SSH, and reMarkable Cloud is not configured.\n"
            "Please check that your tablet is plugged in via USB and SSH is enabled,\n"
            "or run 'living-ink setup' to configure reMarkable Cloud."
        )

    else:  # preferred == "cloud"
        if cloud_client:
            if ssh_client:
                return FallbackClient(
                    primary_client=cloud_client,
                    backup_client=ssh_client,
                    primary_name="reMarkable Cloud",
                    backup_name="USB SSH",
                )
            return cloud_client

        # Cloud not configured, try SSH
        if ssh_client and ssh_client.check_connection():
            logger.info("reMarkable Cloud token not configured. Using USB SSH...")
            print("ℹ️ reMarkable Cloud not configured. Falling back to USB SSH...")
            return ssh_client

        raise RuntimeError(
            "No reMarkable token found and USB SSH connection failed.\n"
            "Run 'living-ink setup' to configure your reMarkable connection."
        )


def ensure_config_dir():
    """Ensure configuration directory exists."""
    REMARKABLE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/desktop/connect
    """
    from remarkable_mcp.sync import register_device

    try:
        token_data = register_device(one_time_code)

        # Save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        token_json = json_module.dumps(token_data)
        rmapi_file.write_text(token_json)

        return token_json
    except Exception as e:
        raise RuntimeError(str(e))


def get_items_by_id(collection) -> Dict[str, Any]:
    """Build a lookup dict of items by ID."""
    return {item.ID: item for item in collection}


def get_items_by_parent(collection) -> Dict[str, List]:
    """Build a lookup dict of items grouped by parent ID."""
    items_by_parent: Dict[str, List] = {}
    for item in collection:
        parent = item.Parent if hasattr(item, "Parent") else ""
        if parent not in items_by_parent:
            items_by_parent[parent] = []
        items_by_parent[parent].append(item)
    return items_by_parent


def get_item_path(item, items_by_id: Dict[str, Any]) -> str:
    """Get the full path of an item."""
    path_parts = [item.VissibleName]
    parent_id = item.Parent if hasattr(item, "Parent") else ""
    while parent_id and parent_id in items_by_id:
        parent = items_by_id[parent_id]
        path_parts.insert(0, parent.VissibleName)
        parent_id = parent.Parent if hasattr(parent, "Parent") else ""
    return "/" + "/".join(path_parts)


def download_raw_file(client, doc, extension: str):
    """
    Download a raw file (PDF or EPUB) for a document.

    Args:
        client: The reMarkable API client (SSH or Cloud)
        doc: The document to download
        extension: File extension without dot (e.g., 'pdf', 'epub')

    Returns:
        Raw file bytes, or None if file doesn't exist or not supported
    """
    # SSH client has direct download_raw_file method
    if hasattr(client, "download_raw_file"):
        return client.download_raw_file(doc, extension)

    # Cloud client - raw files are not available via API
    # The cloud API only returns the notebook annotations, not source PDFs/EPUBs
    return None


def get_file_type(client, doc) -> str:
    """
    Get the file type (pdf, epub, notebook) for a document.

    Args:
        client: The reMarkable API client (SSH or Cloud)
        doc: The document to check

    Returns:
        File type string: 'pdf', 'epub', or 'notebook'
    """
    # SSH client has direct get_file_type method
    if hasattr(client, "get_file_type"):
        file_type = client.get_file_type(doc)
        if file_type:
            return file_type

    # Infer from document name
    name = doc.VissibleName.lower()
    if name.endswith(".pdf"):
        return "pdf"
    elif name.endswith(".epub"):
        return "epub"

    return "notebook"
