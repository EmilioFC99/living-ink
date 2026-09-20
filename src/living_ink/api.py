"""
reMarkable Cloud API client helpers.
"""

import json as json_module
import logging
from pathlib import Path
from typing import Any, List, Optional

from living_ink.logs import console
from living_ink.models import Document
from living_ink.settings import Settings
from living_ink.transport import (
    DeviceInfo,
    RemarkableTransport,
    TransportUnavailable,
    UnsupportedOperation,
)

logger = logging.getLogger(__name__)

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
        except TypeError:
            # A caller error, not a transport failure. Failing over would print
            # a misleading "the Cloud failed" and then raise the same TypeError
            # from the backup, hiding the real mistake behind a retry.
            raise
        except Exception as e:
            # Broad on purpose: failover exists precisely for the failures
            # nobody enumerated. UnsupportedOperation is re-raised above,
            # because the backup cannot serve what the Protocol does not offer.
            if not self.backup or self.active is self.backup:
                raise
            logger.warning(
                f"{self.primary_name} {method_name} failed ({e}). "
                f"Falling back to {self.backup_name}..."
            )
            console(
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
            # Same reasoning as _with_fallback: any failure is worth a retry
            # on the other transport, and the last one standing re-raises.
            if not self.backup or self.active is self.backup:
                raise
            logger.warning(
                f"{self.primary_name} download failed ({e}). Falling back to {self.backup_name}..."
            )
            console(f"ℹ️ {self.primary_name} download failed. Falling back to {self.backup_name}...")
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

    def get_device_info(self) -> DeviceInfo:
        """Describe the tablet, asking whichever transport can actually see it.

        This is the one operation where an :class:`UnsupportedOperation` from
        the active client is worth retrying on the backup: the capability gap
        is not uniform across transports. Only USB SSH can see the hardware,
        so a Cloud-preferred run must still be able to fall through to SSH.
        Unlike a failover, this does not make the backup the active client —
        the preference was about fetching documents, and still holds.

        Returns:
            The device description from whichever transport could produce one.

        Raises:
            UnsupportedOperation: If neither transport can see the device.
        """
        try:
            return self.active.get_device_info()
        except UnsupportedOperation:
            other = self.backup if self.active is self.primary else self.primary
            if other is None or other is self.active:
                raise
            return other.get_device_info()


def resolve_stored_token(
    settings: Optional[Settings] = None, config_path: Optional[Path] = None
) -> Optional[str]:
    """Find the reMarkable device token, wherever it happens to live.

    The token reaches the tool by three routes, tried in this order:

    1. the resolved settings — the environment, or a ``config.yml`` written by
       an earlier version;
    2. the credentials directory, which is where this build writes it;
    3. ``~/.rmapi``, which is where every build before this one wrote it.

    Anything that needs to know whether the Cloud is usable must consult all
    three, or it will report "disconnected" for a setup that syncs perfectly
    well. A token found by route 1 or 3 is copied into route 2 on the way
    past, so an existing install moves itself to owner-only storage on its next
    run without anyone retyping a pairing code. The original is left where it
    was: this build no longer writes those places, but downgrading to the
    previous one must not mean re-pairing.

    Args:
        settings: Resolved settings for this run. Defaults to resolving them
            from the environment alone.
        config_path: The config file whose credentials directory holds route 2.
            A caller that already resolved a config — ``living-ink info`` with
            a ``--root``, or a second profile under ``LIVING_INK_CONFIG`` — must
            pass it, or the token is read from and migrated into the *default*
            profile's store instead of the one in use.

    Returns:
        The token, or None if no route has one.
    """
    from living_ink.config.credentials import CLOUD_TOKEN, migrate_secret, read_secret

    # Resolved *with* the config path: the credentials store is one of the
    # layers Settings.resolve reads, so resolving without it would answer from
    # the default profile's store and never reach the requested one below.
    resolved = settings or Settings.resolve(config_path=config_path)
    token = resolved.remarkable_token
    if token:
        migrate_secret(CLOUD_TOKEN, token, config_path=config_path)
        return token

    stored = read_secret(CLOUD_TOKEN, config_path=config_path)
    if stored:
        return stored

    rmapi_file = Path.home() / ".rmapi"
    if not rmapi_file.exists():
        return None
    try:
        legacy = rmapi_file.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Could not read %s: %s", rmapi_file, e, exc_info=True)
        return None

    if legacy:
        migrate_secret(CLOUD_TOKEN, legacy, config_path=config_path)
    return legacy


def get_rmapi(settings: Optional[Settings] = None):
    """
    Get or initialize the reMarkable API client with automatic fallback.

    Uses preferred connection (SSH or Cloud) if available, and falls back to the
    secondary method if the primary fails or is disconnected.

    Args:
        settings: Resolved settings for this run. Defaults to resolving them
            from the environment alone, for callers with no config in hand.

    Returns:
        Either RemarkableClient, SSHClient, or FallbackClient.
    """
    resolved = settings or Settings.from_env()

    # 1. Determine preferred connection mode
    pref = resolved.preferred_connection
    if pref in ("ssh", "usb"):
        preferred = "ssh"
    elif pref in ("cloud", "rmapi"):
        preferred = "cloud"
    elif resolved.use_ssh:
        preferred = "ssh"
    else:
        # Through resolve_stored_token, not a bare ~/.rmapi check: the token
        # now lands in the credentials directory, so looking only at the legacy
        # file would guess "ssh" for an install that paired with the Cloud
        # yesterday.
        preferred = "cloud" if resolve_stored_token(resolved) else "ssh"

    # 2. Instantiate potential clients
    ssh_client = None
    cloud_client = None

    try:
        from living_ink.ssh import create_ssh_client

        ssh_client = create_ssh_client(
            host=resolved.ssh_host, user=resolved.ssh_user, port=resolved.ssh_port
        )
    except Exception as e:
        # Broad: an SSH library that will not even construct must not stop the
        # Cloud client below from being built. The result is one transport
        # instead of two, which the selection further down already handles.
        logger.debug("Could not create SSH client: %s", e, exc_info=True)

    token = resolve_stored_token(resolved)

    if token:
        try:
            from living_ink.sync import load_client_from_token

            # Deliberately does not write the token back to ~/.rmapi. Building
            # a client is a read; the only thing that should ever overwrite a
            # stored credential is registration, which is what wrote it.
            cloud_client = load_client_from_token(token)
        except (OSError, ValueError) as e:
            # A token file that cannot be written or parsed. SSH may still be
            # available, so this is reported and the selection continues.
            logger.debug("Could not load Cloud client: %s", e, exc_info=True)

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
            console("ℹ️ USB SSH not connected. Falling back to reMarkable Cloud...")
            return FallbackClient(
                primary_client=cloud_client,
                backup_client=ssh_client,
                primary_name="reMarkable Cloud",
                backup_name="USB SSH",
            )

        raise TransportUnavailable(
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
            console("ℹ️ reMarkable Cloud not configured. Falling back to USB SSH...")
            return ssh_client

        raise TransportUnavailable(
            "No reMarkable token found and USB SSH connection failed.\n"
            "Run 'living-ink setup' to configure your reMarkable connection."
        )


def register_and_get_token(one_time_code: str) -> str:
    """
    Register with reMarkable using a one-time code and return the token.

    Get a code from: https://my.remarkable.com/device/desktop/connect

    The token is stored through :mod:`living_ink.config.credentials`, which
    means atomically and at mode ``0600``. It used to be a bare ``write_text``
    to ``~/.rmapi``: world-readable per umask, and truncated in place, so an
    interrupted registration left an empty file that read back as "no token"
    while the device was in fact paired.
    """
    from living_ink.config.credentials import CLOUD_TOKEN, write_secret
    from living_ink.sync import register_device

    try:
        token_data = register_device(one_time_code)
        token_json = json_module.dumps(token_data)
        write_secret(CLOUD_TOKEN, token_json)
        return token_json
    except Exception as e:
        # Broad because registration reaches the network, the filesystem and a
        # JSON encoder, and the caller is a wizard that wants one sentence.
        # Chained, so the original traceback survives in the log.
        raise RuntimeError(str(e)) from e


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
    except Exception as e:
        # Tags are optional metadata and the two transports read them from
        # different places. Whatever went wrong, the document itself may still
        # carry them, so fall through rather than fail the notebook.
        logger.debug("Could not read tags for %s: %s", doc.id, e, exc_info=True)
    if doc.tags:
        return list(doc.tags)
    return []
