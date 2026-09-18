"""
reMarkable Cloud Sync Client

A replacement for rmapy that uses the current reMarkable sync API (v3/v4).
rmapy is abandoned and uses deprecated endpoints that return 500 errors.

Based on the protocol used by ddvk/rmapi.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from living_ink.models import Document
from living_ink.transport import DeviceInfo, UnsupportedOperation

logger = logging.getLogger(__name__)

#: Everything that can go wrong fetching and parsing one blob: the request
#: itself, a token that could not be renewed (:meth:`renew_token` reports that
#: as a RuntimeError), a body that is not the JSON or index format it claims to
#: be, and an index entry missing the fields the format requires. A document
#: made of many blobs stays usable when one of them is any of these, so they
#: are caught where a single blob is read — and nowhere wider.
_BLOB_ERRORS = (requests.RequestException, RuntimeError, ValueError, KeyError, IndexError)

# API endpoints
# Note: my.remarkable.com endpoints redirect to doesnotexist.remarkable.com
# So we use webapp-prod.cloud.remarkable.engineering for auth
AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
DEVICE_TOKEN_URL = f"{AUTH_HOST}/token/json/2/device/new"
USER_TOKEN_URL = f"{AUTH_HOST}/token/json/2/user/new"

SYNC_HOST = "https://internal.cloud.remarkable.com"
ROOT_URL = f"{SYNC_HOST}/sync/v4/root"
FILES_URL = f"{SYNC_HOST}/sync/v3/files"


class RemarkableClient:
    """Client for reMarkable Cloud sync API."""

    def __init__(self, device_token: str = "", user_token: str = ""):
        self.device_token = device_token
        self.user_token = user_token
        self._documents: List[Document] = []
        self._documents_by_id: Dict[str, Document] = {}

    def renew_token(self) -> str:
        """Exchange device token for a fresh user token."""
        if not self.device_token:
            raise RuntimeError("No device token available")

        headers = {"Authorization": f"Bearer {self.device_token}"}

        try:
            response = requests.post(USER_TOKEN_URL, headers=headers, timeout=30)
            if response.status_code == 200 and response.text:
                self.user_token = response.text.strip()
                return self.user_token
        except requests.RequestException as e:
            raise RuntimeError(f"Network error during token renewal: {e}")

        raise RuntimeError(
            f"Failed to renew user token (HTTP {response.status_code}).\n"
            "Your device may need to be re-registered.\n"
            "Get a new code from: https://my.remarkable.com/device/desktop/connect"
        )

    def _request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        """Make an authenticated request."""
        if not self.user_token:
            self.renew_token()

        req_headers = {"Authorization": f"Bearer {self.user_token}"}
        if headers:
            req_headers.update(headers)

        response = requests.request(method, url, headers=req_headers, timeout=60)

        if response.status_code == 401:
            # Token expired, try to renew
            self.renew_token()
            req_headers["Authorization"] = f"Bearer {self.user_token}"
            response = requests.request(method, url, headers=req_headers, timeout=60)

        return response

    def _get_file(self, file_hash: str, file_name: str = "") -> bytes:
        """Download a file by its hash."""
        headers = {"rm-filename": file_name} if file_name else None
        response = self._request(f"{FILES_URL}/{file_hash}", headers=headers)
        response.raise_for_status()
        return response.content

    def _parse_index(self, content: bytes) -> List[Dict[str, Any]]:
        """Parse an index file into entries."""
        lines = content.decode("utf-8").strip().split("\n")
        entries = []

        # First line is schema version
        for line in lines[1:]:
            parts = line.split(":")
            if len(parts) >= 5:
                entries.append(
                    {
                        "hash": parts[0],
                        "type": parts[1],
                        "id": parts[2],
                        "subfiles": int(parts[3]),
                        "size": int(parts[4]),
                    }
                )

        return entries

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """
        Fetch documents and folders from the cloud.

        Args:
            limit: Maximum number of documents to fetch. If None, fetches all.

        Returns a list of Document objects (compatible with rmapy Collection).
        """
        # Get root hash
        response = self._request(ROOT_URL)
        response.raise_for_status()

        # Handle empty or invalid JSON response
        if not response.text or not response.text.strip():
            raise RuntimeError(
                "Empty response from reMarkable API. Your token may have expired.\n"
                "Try re-registering with: living-ink setup"
            )

        try:
            root_data = response.json()
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Invalid JSON from reMarkable API: {e}\nResponse was: {response.text[:200]}"
            )

        if "hash" not in root_data:
            raise RuntimeError(
                f"Unexpected API response format: {root_data}\nThe reMarkable API may have changed."
            )

        root_hash = root_data["hash"]

        # Get root index
        root_index = self._get_file(root_hash, "root.docSchema")
        entries = self._parse_index(root_index)

        documents = []

        for entry in entries:
            doc_id = entry["id"]
            doc_hash = entry["hash"]

            # Fetch the document's blob index
            try:
                blob_content = self._get_file(doc_hash, f"{doc_id}.docSchema")
                blob_entries = self._parse_index(blob_content)
            except _BLOB_ERRORS as e:
                logger.debug("Skipping document %s: %s", doc_id, e, exc_info=True)
                continue

            # Find and fetch the metadata file
            metadata = {}
            files = []

            for blob_entry in blob_entries:
                files.append(blob_entry)
                if blob_entry["id"].endswith(".metadata"):
                    try:
                        meta_content = self._get_file(blob_entry["hash"], blob_entry["id"])
                        metadata = json.loads(meta_content.decode("utf-8"))
                    except _BLOB_ERRORS as e:
                        logger.debug("Could not read metadata for %s: %s", doc_id, e, exc_info=True)

            # Skip deleted documents
            if metadata.get("deleted", False):
                continue

            # Parse last modified timestamp
            last_modified = None
            if "lastModified" in metadata:
                try:
                    ts = int(metadata["lastModified"]) / 1000  # Convert ms to seconds
                    last_modified = datetime.fromtimestamp(ts)
                except (ValueError, TypeError):
                    pass

            doc = Document(
                id=doc_id,
                hash=doc_hash,
                name=metadata.get("visibleName", doc_id),
                doc_type=metadata.get("type", "DocumentType"),
                parent=metadata.get("parent", ""),
                deleted=metadata.get("deleted", False),
                pinned=metadata.get("pinned", False),
                last_modified=last_modified,
                size=entry["size"],
                files=files,
            )

            documents.append(doc)

            # Stop early if we have enough
            if limit is not None and len(documents) >= limit:
                break

        self._documents = documents
        self._documents_by_id = {d.id: d for d in documents}

        return documents

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Get a document by ID."""
        if not self._documents_by_id:
            self.get_meta_items()
        return self._documents_by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """Download a document's content as a zip file."""
        # The document blob contains all the files
        # We need to fetch each file and create a zip
        import io
        import zipfile

        blob_content = self._get_file(doc.hash, f"{doc.id}.docSchema")
        blob_entries = self._parse_index(blob_content)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in blob_entries:
                file_id = entry["id"]
                file_hash = entry["hash"]

                # Download the file
                try:
                    file_content = self._get_file(file_hash, file_id)
                    zf.writestr(file_id, file_content)
                except _BLOB_ERRORS as e:
                    logger.debug("Skipping blob %s: %s", file_id, e, exc_info=True)
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()

    def check_connection(self) -> bool:
        """Report whether the cloud is reachable with the current credentials.

        Returns:
            True if the sync root can be fetched, False otherwise.
        """
        try:
            response = self._request(ROOT_URL)
            return response.status_code == 200
        except (requests.RequestException, RuntimeError) as e:
            logger.debug(f"Cloud connection check failed: {e}")
            return False

    def _blob_entries(self, doc: Document) -> List[Dict[str, Any]]:
        """Return the blob index for a document, reusing the copy from get_meta_items.

        Args:
            doc: Document whose member files are wanted.

        Returns:
            List of index entries, each with at least ``id`` and ``hash``.
        """
        if doc.files:
            return doc.files
        blob_content = self._get_file(doc.hash, f"{doc.id}.docSchema")
        return self._parse_index(blob_content)

    def _content_dict(self, doc: Document) -> Dict[str, Any]:
        """Fetch and parse a document's ``.content`` blob.

        Args:
            doc: Document whose content descriptor is wanted.

        Returns:
            The parsed descriptor, or an empty dict if it is missing or invalid.
        """
        for entry in self._blob_entries(doc):
            if entry["id"].endswith(".content"):
                try:
                    raw = self._get_file(entry["hash"], entry["id"])
                    return json.loads(raw.decode("utf-8"))
                except _BLOB_ERRORS as e:
                    logger.debug(f"Could not read .content for {doc.id}: {e}")
                    return {}
        return {}

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Get the file type ('pdf', 'epub', …) for a document.

        Args:
            doc: The document to inspect.

        Returns:
            The extension without a dot, or None for a plain notebook.
        """
        return self._content_dict(doc).get("fileType") or None

    def get_tags(self, doc: Document) -> List[str]:
        """Get tags for a document from its ``.content`` blob.

        Args:
            doc: The document to inspect.

        Returns:
            List of tag strings, empty if the document has none.
        """
        if doc.tags:
            return list(doc.tags)

        from living_ink.extract import extract_tags_from_dict

        tags = extract_tags_from_dict(self._content_dict(doc))
        doc.tags = tags
        return list(tags)

    def get_device_info(self) -> DeviceInfo:
        """Report that the Cloud cannot describe the tablet.

        The sync service serves documents, not hardware: a notebook in the
        Cloud has no model or firmware attached to it, and the device that
        wrote it may not even be online.

        Raises:
            UnsupportedOperation: Always.
        """
        raise UnsupportedOperation(
            "The reMarkable Cloud serves documents, not device details. "
            "Connect over USB to identify the tablet."
        )

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Download the source PDF or EPUB backing an annotated document.

        Args:
            doc: The document to download from.
            extension: File extension without a dot, e.g. 'pdf' or 'epub'.

        Returns:
            Raw file bytes, or None if the document has no such member.
        """
        suffix = f".{extension}"
        for entry in self._blob_entries(doc):
            if entry["id"].endswith(suffix):
                try:
                    return self._get_file(entry["hash"], entry["id"])
                except _BLOB_ERRORS as e:
                    logger.debug(f"Could not download {entry['id']}: {e}")
                    return None
        return None


def register_device(one_time_code: str) -> Dict[str, str]:
    """
    Register a new device with reMarkable cloud.

    Args:
        one_time_code: Code from https://my.remarkable.com/device/desktop/connect

    Returns:
        Dict with devicetoken and usertoken keys
    """
    from uuid import uuid4

    body = {
        "code": one_time_code,
        "deviceDesc": "desktop-linux",
        "deviceID": str(uuid4()),
    }

    try:
        response = requests.post(DEVICE_TOKEN_URL, json=body, timeout=30)
        if response.status_code == 200 and response.text:
            device_token = response.text.strip()
            return {"devicetoken": device_token, "usertoken": ""}
    except requests.RequestException as e:
        raise RuntimeError(f"Network error during registration: {e}")

    raise RuntimeError(
        f"Registration failed (HTTP {response.status_code}). This usually means:\n"
        "  1. The code has expired (codes are single-use)\n"
        "  2. The code was already used\n"
        "  3. The code was typed incorrectly\n\n"
        "Get a new code from: https://my.remarkable.com/device/desktop/connect"
    )


def load_client_from_token(token_data: str) -> RemarkableClient:
    """
    Create a client from a token string.

    Args:
        token_data: Either:
            - JSON string with devicetoken and optional usertoken
            - Raw JWT device token (legacy format from rmapy)

    Returns:
        Configured RemarkableClient
    """
    token_data = token_data.strip()

    # Try to parse as JSON first
    if token_data.startswith("{"):
        try:
            data = json.loads(token_data)
            return RemarkableClient(
                device_token=data.get("devicetoken", ""),
                user_token=data.get("usertoken", ""),
            )
        except json.JSONDecodeError:
            pass

    # Treat as raw device token (legacy rmapy format - just the JWT)
    # JWT tokens start with "eyJ" (base64 encoded '{"')
    if token_data.startswith("eyJ"):
        return RemarkableClient(device_token=token_data, user_token="")

    raise ValueError(
        f"Invalid token format. Expected JSON or JWT token.\n"
        f"Token starts with: {token_data[:20]}..."
    )
