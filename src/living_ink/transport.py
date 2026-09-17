"""The transport seam: what every reMarkable client must be able to do.

A transport is anything that can list documents on a reMarkable and hand back
their bytes — USB SSH (:mod:`living_ink.ssh`), the reMarkable Cloud
(:mod:`living_ink.sync`), or a future Wi-Fi, folder-watcher or fake transport.

:class:`RemarkableTransport` is the whole contract. Implementations must provide
every method; one that genuinely cannot serve a call raises
:class:`UnsupportedOperation` rather than omitting the method, so that callers
never have to ask ``hasattr`` at runtime.
"""

from typing import List, Optional, Protocol, runtime_checkable

from living_ink.models import Document


class UnsupportedOperation(NotImplementedError):
    """Raised when a transport cannot serve an operation the Protocol defines.

    Signals a permanent capability gap, not a transient failure, so callers
    should not retry the same call on the same transport.
    """


@runtime_checkable
class RemarkableTransport(Protocol):
    """The operations every reMarkable client supports."""

    def check_connection(self) -> bool:
        """Report whether the device or service is currently reachable."""
        ...

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """List documents and folders, optionally stopping after ``limit``."""
        ...

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Look up a single document by id, or None if it is not present."""
        ...

    def download(self, doc: Document) -> bytes:
        """Download a document's contents as a zip archive."""
        ...

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Return 'pdf', 'epub', … for a file-backed document, else None."""
        ...

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Download the source PDF or EPUB behind a document, if it has one."""
        ...

    def get_tags(self, doc: Document) -> List[str]:
        """Return the document's tags, empty if it has none."""
        ...
