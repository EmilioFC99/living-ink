"""
reMarkable SSH Client

Direct access to reMarkable tablet via SSH when connected over USB.
Default connection: root@10.11.99.1 (USB connection)

The tablet stores documents at:
/home/root/.local/share/remarkable/xochitl/

Each document is a folder with:
- {uuid}.metadata - JSON with visibleName, type, parent, etc.
- {uuid}.content - JSON with file info
- {uuid}/ - folder with .rm files (pages), .pdf, etc.
"""

import io
import json
import logging
import os
import subprocess
import zipfile
from datetime import datetime
from typing import Dict, List, Optional

from living_ink.devices import DEFAULT_PROFILE, identify
from living_ink.models import Document
from living_ink.transport import DeviceInfo

logger = logging.getLogger(__name__)

#: How this client reports a failed round trip to the tablet.
#: :meth:`RemarkableSSHClient._ssh_command` and ``_scp_download`` turn every
#: subprocess outcome — non-zero exit, timeout, no ``ssh`` on PATH — into a
#: RuntimeError, and OSError covers the process failing to start at all.
_SSH_ERRORS = (RuntimeError, OSError)

#: What a metadata or content file that is not the JSON it claims to be looks
#: like. ``json.JSONDecodeError`` and ``UnicodeDecodeError`` are both ValueError.
_PARSE_ERRORS = (ValueError, TypeError, KeyError)

# Default SSH settings for USB connection
DEFAULT_SSH_HOST = "10.11.99.1"
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PORT = 22

# Document storage path on the tablet
XOCHITL_PATH = "/home/root/.local/share/remarkable/xochitl"


class SSHClient:
    """Client for accessing reMarkable tablet via SSH (passwordless key-based auth)."""

    def __init__(
        self,
        host: str = DEFAULT_SSH_HOST,
        user: str = DEFAULT_SSH_USER,
        port: int = DEFAULT_SSH_PORT,
    ):
        self.host = host
        self.user = user
        self.port = port
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}
        # Maps document id -> fileType ("pdf", "epub", or None for notebooks).
        # Populated lazily; see get_file_type().
        self._file_type_cache: Dict[str, Optional[str]] = {}
        self._file_types_loaded = False
        # Cached because the device does not change mid-run and each SSH call
        # is a fresh connection.
        self._device_info: Optional[DeviceInfo] = None

    def _ssh_command(self, command: str, timeout: int = 30) -> str:
        """Execute a command on the tablet via SSH."""
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            command,
        ]

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"SSH command failed: {result.stderr}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH command timed out after {timeout}s")
        except FileNotFoundError:
            raise RuntimeError("SSH client not found. Install openssh-client.")

    def _scp_download(self, remote_path: str, timeout: int = 60) -> bytes:
        """Download a file from the tablet via SSH cat (more reliable than SCP)."""
        # Use SSH + cat instead of SCP for binary file transfer
        # This avoids issues with /dev/stdout on various platforms
        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(self.port),
            f"{self.user}@{self.host}",
            f"cat '{remote_path}'",
        ]

        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"SSH cat failed: {result.stderr.decode()}")
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"SSH cat timed out after {timeout}s")

    def check_connection(self) -> bool:
        """Check if SSH connection to tablet is available."""
        try:
            self._ssh_command("echo ok", timeout=5)
            return True
        except _SSH_ERRORS as e:
            logger.debug(f"SSH connection check failed: {e}")
            return False

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the tablet via SSH.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects.
        """
        # Return cached documents if available and no limit specified
        if self._documents and limit is None:
            return self._documents

        # If we have cached docs and limit is within cache, return slice
        if self._documents and limit is not None and len(self._documents) >= limit:
            return self._documents[:limit]

        # Read all metadata files in a single SSH command for efficiency
        # Output format: filename<TAB>content (JSON)
        try:
            # Use a single command to read all metadata files at once
            # This is MUCH faster than individual cat commands
            output = self._ssh_command(
                f"for f in {XOCHITL_PATH}/*.metadata; do "
                f'echo "===FILE===$(basename $f .metadata)"; cat "$f" 2>/dev/null; echo; '
                f"done",
                timeout=60,
            )
        except _SSH_ERRORS as e:
            raise RuntimeError(f"Failed to read metadata: {e}") from e

        documents = []

        # Parse the output by ===FILE=== delimiter
        for part in output.split("===FILE==="):
            part = part.strip()
            if not part:
                continue
            lines = part.split("\n", 1)
            doc_id = lines[0].strip()
            content = lines[1] if len(lines) > 1 else ""
            self._parse_and_add_document(doc_id, content, documents, limit)
            if limit is not None and len(documents) >= limit:
                break

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        logger.info(f"Loaded {len(documents)} documents via SSH")
        return documents

    def _parse_and_add_document(
        self,
        doc_id: str,
        content: str,
        documents: List[Document],
        limit: Optional[int],
    ) -> None:
        """Parse metadata JSON and add document to list."""
        if limit is not None and len(documents) >= limit:
            return

        try:
            metadata = json.loads(content.strip())

            # Skip deleted documents
            if metadata.get("deleted", False):
                return

            # Parse last modified timestamp
            last_modified = None
            if "lastModified" in metadata:
                try:
                    ts = int(metadata["lastModified"]) / 1000
                    last_modified = datetime.fromtimestamp(ts)
                except (ValueError, TypeError):
                    pass

            # Use lastModified if present, otherwise doc_id, so modifications trigger sync
            last_mod_raw = metadata.get("lastModified")
            doc_hash = str(last_mod_raw) if last_mod_raw else doc_id

            doc = Document(
                id=doc_id,
                hash=doc_hash,
                name=metadata.get("visibleName", doc_id),
                doc_type=metadata.get("type", "DocumentType"),
                parent=metadata.get("parent", ""),
                deleted=metadata.get("deleted", False),
                pinned=metadata.get("pinned", False),
                synced=metadata.get("synced", True),
                last_modified=last_modified,
                size=0,
                local_path=f"{XOCHITL_PATH}/{doc_id}",
            )

            documents.append(doc)

        except _PARSE_ERRORS as e:
            # One unreadable metadata file costs one document, not the library.
            logger.debug("Failed to parse metadata for %s: %s", doc_id, e, exc_info=True)

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """
        Download a document's content as a zip file.

        Creates a zip archive with the same structure as the cloud API.
        """
        doc_path = f"{XOCHITL_PATH}/{doc.id}"

        # List files in the document folder
        try:
            output = self._ssh_command(f"find '{doc_path}' -type f 2>/dev/null || true")
        except _SSH_ERRORS as e:
            logger.debug("Could not list %s: %s", doc_path, e, exc_info=True)
            output = ""

        file_list = [f.strip() for f in output.strip().split("\n") if f.strip()]

        # Also include the .content file if it exists
        content_file = f"{XOCHITL_PATH}/{doc.id}.content"
        try:
            self._ssh_command(f"test -f '{content_file}' && echo exists")
            file_list.append(content_file)
        except _SSH_ERRORS:
            # `test -f` exits non-zero when the file is absent, which is the
            # ordinary case for a notebook with no content descriptor.
            pass

        # Also include raw PDF or EPUB files if they exist
        for ext in ("pdf", "epub"):
            raw_file = f"{XOCHITL_PATH}/{doc.id}.{ext}"
            try:
                self._ssh_command(f"test -f '{raw_file}' && echo exists")
                file_list.append(raw_file)
            except _SSH_ERRORS:
                # Absent is the normal answer: most documents are not a PDF.
                pass

        # Create zip archive
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for remote_path in file_list:
                try:
                    content = self._scp_download(remote_path)
                    # Use relative path in zip
                    rel_path = os.path.basename(remote_path)
                    if "/" in remote_path.replace(f"{XOCHITL_PATH}/{doc.id}", ""):
                        # Preserve subdirectory structure
                        rel_path = remote_path.replace(f"{XOCHITL_PATH}/{doc.id}/", "")
                    zf.writestr(rel_path, content)
                except _SSH_ERRORS as e:
                    logger.debug("Failed to download %s: %s", remote_path, e, exc_info=True)
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """
        Download a raw file (PDF or EPUB) for a document.

        Args:
            doc: The document to download
            extension: File extension without dot (e.g., 'pdf', 'epub')

        Returns:
            Raw file bytes, or None if file doesn't exist
        """
        file_path = f"{XOCHITL_PATH}/{doc.id}.{extension}"

        try:
            # Check if file exists first
            self._ssh_command(f"test -f '{file_path}'", timeout=5)
            # Download the file
            return self._scp_download(file_path, timeout=120)
        except _SSH_ERRORS as e:
            logger.debug("Raw file not found: %s: %s", file_path, e, exc_info=True)
            return None

    def get_file_type(self, doc: Document) -> Optional[str]:
        """
        Get the file type (pdf, epub, etc.) for a document.

        The result is memoised. On the first miss the whole library is
        batch-loaded via get_all_file_types(), because discovery asks for the
        type of every document and one SSH round-trip per document is slow
        over USB.

        Returns the extension without dot, or None if not a file-based document.
        """
        if doc.id in self._file_type_cache:
            return self._file_type_cache[doc.id]

        if not self._file_types_loaded:
            self.get_all_file_types()
            if doc.id in self._file_type_cache:
                return self._file_type_cache[doc.id]

        # Fall back to a single-document probe: the batch read may have been
        # partial, or this document may have appeared since it ran.
        content_file = f"{XOCHITL_PATH}/{doc.id}.content"
        try:
            content = self._scp_download(content_file, timeout=10)
            data = json.loads(content.decode("utf-8"))
            file_type = data.get("fileType")
        except (*_SSH_ERRORS, *_PARSE_ERRORS) as e:
            logger.debug("Could not read the file type for %s: %s", doc.id, e, exc_info=True)
            file_type = None

        self._file_type_cache[doc.id] = file_type
        return file_type

    def get_tags(self, doc: Document) -> List[str]:
        """Get tags for a document from its .content file.

        Args:
            doc: Document instance.

        Returns:
            List of tag strings.
        """
        if getattr(doc, "tags", None):
            return list(doc.tags)

        content_file = f"{XOCHITL_PATH}/{doc.id}.content"
        try:
            content = self._scp_download(content_file, timeout=10)
            data = json.loads(content.decode("utf-8"))
            from living_ink.extract import extract_tags_from_dict

            tags = extract_tags_from_dict(data)
            doc.tags = tags
            return list(tags)
        except (*_SSH_ERRORS, *_PARSE_ERRORS) as e:
            logger.debug("Could not read tags for %s: %s", doc.id, e, exc_info=True)
            return []

    def get_device_info(self) -> DeviceInfo:
        """Ask the tablet what it is, over a single SSH round trip.

        The model comes from the kernel's machine string and the firmware from
        the release file xochitl ships. Either may be missing on a device this
        project has not seen, so each is read with a fallback and the result is
        still returned rather than raising: knowing half of it is useful, and a
        bug report that says "unknown" is more actionable than one that errored.

        Returns:
            The device's model, firmware and panel geometry.

        Raises:
            RuntimeError: If the tablet could not be reached at all.
        """
        if self._device_info is not None:
            return self._device_info

        # One command, because each SSH invocation is a fresh connection and
        # this runs on the interactive path. '|| true' keeps a missing file
        # from failing the whole read.
        # One command, because each SSH invocation is a fresh connection and
        # this runs on the interactive path. '|| true' keeps a missing file
        # from failing the whole read. Three firmware sources in preference
        # order: the reMarkable release file, the human-readable IMG_VERSION
        # the Paper Pro's Codex Linux carries, and the raw build timestamp in
        # /etc/version, which is a real answer but not one anyone recognises.
        command = (
            "cat /proc/device-tree/model 2>/dev/null"
            " || cat /sys/devices/soc0/machine 2>/dev/null || true; echo '===';"
            " grep -h REMARKABLE_RELEASE_VERSION /usr/share/remarkable/update.conf 2>/dev/null"
            " || grep -h IMG_VERSION /etc/os-release 2>/dev/null"
            " || cat /etc/version 2>/dev/null || true"
        )
        try:
            output = self._ssh_command(command, timeout=15)
        except _SSH_ERRORS as e:
            raise RuntimeError(f"Could not read device info: {e}") from e

        machine, _, firmware_raw = output.partition("===")
        # \x00 because /proc/device-tree entries are NUL-terminated strings.
        machine = machine.strip().strip("\x00").strip()
        firmware = firmware_raw.strip().rpartition("=")[2].strip().strip('"')

        profile = identify(machine)
        if profile is None:
            # Reporting the raw string rather than DEFAULT_PROFILE.name: the
            # geometry has to fall back to something, but calling an unknown
            # tablet a "reMarkable 2" turns a visible guess into a false
            # measurement, which is the one thing this path must not do.
            logger.info("Unrecognised reMarkable machine string %r.", machine)
            profile = DEFAULT_PROFILE
            model = machine or "unknown"
        else:
            model = profile.name

        self._device_info = DeviceInfo(
            model=model,
            firmware=firmware,
            screen=profile.screen,
            color=profile.color,
        )
        return self._device_info

    def get_all_file_types(self) -> dict[str, Optional[str]]:
        """
        Get file types for all documents in a single SSH command.

        Returns a dict mapping document ID to file type (pdf, epub, or None).
        Much more efficient than calling get_file_type() for each document.
        """
        if self._file_types_loaded:
            return self._file_type_cache

        # Set before the read so a failure is not retried on every lookup;
        # get_file_type() still falls back to a per-document probe.
        self._file_types_loaded = True

        try:
            # Read all .content files in a single command
            output = self._ssh_command(
                f"for f in {XOCHITL_PATH}/*.content; do "
                f'echo "===FILE===$(basename $f .content)"; cat "$f" 2>/dev/null; '
                f"done",
                timeout=60,
            )

            current_id = None
            current_content = []

            for line in output.split("\n"):
                if line.startswith("===FILE==="):
                    # Parse previous content
                    if current_id and current_content:
                        try:
                            data = json.loads("\n".join(current_content))
                            self._file_type_cache[current_id] = data.get("fileType")
                        except json.JSONDecodeError:
                            self._file_type_cache[current_id] = None

                    current_id = line.replace("===FILE===", "").strip()
                    current_content = []
                else:
                    current_content.append(line)

            # Don't forget the last one
            if current_id and current_content:
                try:
                    data = json.loads("\n".join(current_content))
                    self._file_type_cache[current_id] = data.get("fileType")
                except json.JSONDecodeError:
                    self._file_type_cache[current_id] = None

        except (*_SSH_ERRORS, *_PARSE_ERRORS) as e:
            # The caller falls back to a per-document probe, so a failed batch
            # is slow rather than fatal.
            logger.warning("Failed to batch-load file types: %s", e, exc_info=True)

        return self._file_type_cache


def create_ssh_client(
    host: Optional[str] = None,
    user: Optional[str] = None,
    port: Optional[int] = None,
) -> SSHClient:
    """
    Create an SSH client for the tablet.

    Uses passwordless SSH key authentication (BatchMode=yes).
    Copy your key to tablet first: ssh-copy-id root@10.11.99.1

    Args:
        host: Tablet address. Defaults to the USB address, 10.11.99.1.
        user: SSH user. Defaults to root.
        port: SSH port. Defaults to 22.

    Returns:
        An SSHClient pointed at the given endpoint. Connection settings are
        resolved by :class:`living_ink.settings.Settings` and passed in by the
        caller rather than read from the environment here.
    """
    return SSHClient(
        host=host or DEFAULT_SSH_HOST,
        user=user or DEFAULT_SSH_USER,
        port=port or DEFAULT_SSH_PORT,
    )
