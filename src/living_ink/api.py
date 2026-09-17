"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import logging
import os
from pathlib import Path
from typing import Any, List, Optional

from living_ink.models import Document
from living_ink.transport import RemarkableTransport, UnsupportedOperation

logger = logging.getLogger(__name__)

# Configuration - check env var first, then fall back to file
REMARKABLE_TOKEN = os.environ.get("REMARKABLE_TOKEN")
_REMARKABLE_USE_SSH = os.environ.get("REMARKABLE_USE_SSH", "").lower() in ("1", "true", "yes")
REMARKABLE_CONFIG_DIR = Path.home() / ".remarkable"
REMARKABLE_TOKEN_FILE = REMARKABLE_CONFIG_DIR / "token"
CACHE_DIR = REMARKABLE_CONFIG_DIR / "cache"


class FallbackClient:
    """A resilient transport wrapping a primary and a backup reMarkable client.

    Every :class:`~living_ink.transport.RemarkableTransport` operation is tried
    on the preferred client first and retried once on the backup if it fails,
    so the fallback promise holds for the whole surface rather than method by
    method. The first successful failover makes the backup the active client.
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

    def _with_fallback(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        """Call a transport method, retrying once on the backup client.

        Args:
            method_name: Name of the transport method to invoke.
            *args: Positional arguments forwarded to the method.
            **kwargs: Keyword arguments forwarded to the method.

        Returns:
            Whatever the active (or, after a failover, the backup) client returns.

        Raises:
            Exception: The original error, if there is no usable backup.
        """
        try:
            return getattr(self.active, method_name)(*args, **kwargs)
        except UnsupportedOperation:
            raise
        except Exception as e:
            if not self.backup or self.active is self.backup:
                raise
            logger.warning(
                f"{self.primary_name} {method_name} failed ({e}). "
                f"Falling back to {self.backup_name}..."
            )
            print(
                f"ℹ️ {self.primary_name} {method_name} failed. Falling back to {self.backup_name}..."
            )
            self.active = self.backup
            return getattr(self.active, method_name)(*args, **kwargs)

    def check_connection(self) -> bool:
        """Report whether either transport is reachable."""
        return self._with_fallback("check_connection")

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """Fetch document metadata, falling back to the backup client if needed."""
        return self._with_fallback("get_meta_items", limit=limit)

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by id, falling back to the backup client if needed."""
        return self._with_fallback("get_doc", doc_id)

    def download(self, doc: Document) -> bytes:
        """Download a document zip, falling back to the backup client if needed.

        On failover the document is re-resolved against the backup, because the
        two transports identify the same notebook by different hashes.
        """
        try:
            return self.active.download(doc)
        except Exception as e:
            if not self.backup or self.active is self.backup:
                raise
            logger.warning(
                f"{self.primary_name} download failed ({e}). Falling back to {self.backup_name}..."
            )
            print(f"ℹ️ {self.primary_name} download failed. Falling back to {self.backup_name}...")
            self.active = self.backup
            backup_doc = self.active.get_doc(getattr(doc, "id", ""))
            return self.active.download(backup_doc or doc)

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Get a document's file type, falling back to the backup client if needed."""
        return self._with_fallback("get_file_type", doc)

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Download a document's source file, falling back to the backup if needed."""
        return self._with_fallback("download_raw_file", doc, extension)

    def get_tags(self, doc: Document) -> List[str]:
        """Get a document's tags, falling back to the backup client if needed."""
        return self._with_fallback("get_tags", doc)


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
        from living_ink.ssh import create_ssh_client

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
            from living_ink.sync import load_client_from_token

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


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/desktop/connect
    """
    from living_ink.sync import register_device

    try:
        token_data = register_device(one_time_code)

        # Save to ~/.rmapi for compatibility
        rmapi_file = Path.home() / ".rmapi"
        token_json = json_module.dumps(token_data)
        rmapi_file.write_text(token_json)

        return token_json
    except Exception as e:
        raise RuntimeError(str(e))


def download_raw_file(client: RemarkableTransport, doc: Document, extension: str):
    """
    Download a raw file (PDF or EPUB) for a document.

    Args:
        client: The reMarkable transport (SSH, Cloud or Fallback)
        doc: The document to download
        extension: File extension without dot (e.g., 'pdf', 'epub')

    Returns:
        Raw file bytes, or None if the file doesn't exist or isn't supported
    """
    try:
        return client.download_raw_file(doc, extension)
    except UnsupportedOperation:
        return None


def get_file_type(client: RemarkableTransport, doc: Document) -> str:
    """
    Get the file type (pdf, epub, notebook) for a document.

    Falls back to the document name when the transport cannot tell, which
    covers documents whose descriptor is missing or unreadable.

    Args:
        client: The reMarkable transport (SSH, Cloud or Fallback)
        doc: The document to check

    Returns:
        File type string: 'pdf', 'epub', or 'notebook'
    """
    try:
        file_type = client.get_file_type(doc)
        if file_type:
            return file_type
    except UnsupportedOperation:
        pass

    name = doc.VissibleName.lower()
    if name.endswith(".pdf"):
        return "pdf"
    elif name.endswith(".epub"):
        return "epub"

    return "notebook"


def get_document_tags(client: RemarkableTransport, doc: Document) -> List[str]:
    """Get tags for a document from the transport, or from the document itself.

    Args:
        client: The reMarkable transport.
        doc: The document to check.

    Returns:
        List of tag strings.
    """
    try:
        tags = client.get_tags(doc)
        if tags:
            return list(tags)
    except Exception:
        pass
    if doc.tags:
        return list(doc.tags)
    return []
