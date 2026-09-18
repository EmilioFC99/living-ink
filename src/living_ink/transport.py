"""The transport seam: what every reMarkable client must be able to do.

A transport is anything that can list documents on a reMarkable and hand back
their bytes — USB SSH (:mod:`living_ink.ssh`), the reMarkable Cloud
(:mod:`living_ink.sync`), or a future Wi-Fi, folder-watcher or fake transport.

:class:`RemarkableTransport` is the whole contract. Implementations must provide
every method; one that genuinely cannot serve a call raises
:class:`UnsupportedOperation` rather than omitting the method, so that callers
never have to ask ``hasattr`` at runtime.
"""

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from living_ink.models import Document


@dataclass(frozen=True)
class DeviceInfo:
    """What a transport can say about the tablet on the other end.

    Attributes:
        model: The model name, e.g. ``"reMarkable 2"``. ``"unknown"`` when the
            device answered but said nothing recognisable.
        firmware: The xochitl release version, empty if it could not be read.
        screen: Panel size in pixels, as ``(width, height)``.
        color: Whether the panel can display colour.
    """

    model: str
    firmware: str
    screen: Tuple[int, int]
    color: bool = False

    def describe(self) -> str:
        """Render the device as one line for status output and bug reports.

        Returns:
            A short human-readable description of the device.
        """
        firmware = f" firmware {self.firmware}" if self.firmware else ""
        return f"{self.model}{firmware} ({self.screen[0]}×{self.screen[1]})"


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

    def get_device_info(self) -> DeviceInfo:
        """Describe the tablet this transport talks to.

        Raises:
            UnsupportedOperation: If the transport cannot see the device itself
                — the Cloud serves documents, not hardware.
        """
        ...
