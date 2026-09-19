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
        screen_measured: Whether ``screen`` came from the hardware rather than
            from a stand-in. A clipped or stretched render is much easier to
            diagnose when the geometry admits it was never verified.
    """

    model: str
    firmware: str
    screen: Tuple[int, int]
    color: bool = False
    screen_measured: bool = True

    def describe(self) -> str:
        """Render the device as one line for status output and bug reports.

        Returns:
            A short human-readable description of the device.
        """
        firmware = f" firmware {self.firmware}" if self.firmware else ""
        panel = f"{self.screen[0]}×{self.screen[1]}"
        if not self.screen_measured:
            panel += ", panel unverified"
        return f"{self.model}{firmware} ({panel})"


class UnsupportedOperation(NotImplementedError):
    """Raised when a transport cannot serve an operation the Protocol defines.

    Signals a permanent capability gap, not a transient failure, so callers
    should not retry the same call on the same transport.
    """


class TransportUnavailable(RuntimeError):
    """Raised when no transport can be reached at all.

    The end of the fallback ladder: the preferred route is down and the other
    one is unconfigured, so there is nothing left to try. Distinct from a
    transport *failing mid-run*, which ``FallbackClient`` handles by switching
    routes, and distinct from a bug — the message says what the user can plug
    in or configure, so the front end prints it instead of a traceback.

    A ``RuntimeError`` subclass so that existing broad handlers, including
    ``WatchCommand``'s per-cycle guard, keep treating it as a failed attempt.
    """


def require_document(doc: object, operation: str) -> Document:
    """Reject anything that is not a Document before a transport touches it.

    Every Protocol method that names a document takes the object, never its
    id. Passing the id is an easy mistake from a shell or a test, and without
    this guard it surfaces deep inside a client as ``AttributeError: 'str'
    object has no attribute 'id'``, which names neither the caller's error nor
    its fix.

    Args:
        doc: The value the caller supplied.
        operation: The Protocol method name, for the error message.

    Returns:
        The same object, once it is known to be a Document.

    Raises:
        TypeError: If ``doc`` is not a :class:`~living_ink.models.Document`.
    """
    if isinstance(doc, Document):
        return doc

    hint = ""
    if isinstance(doc, str):
        hint = f" Looks like a document id — call get_doc({doc!r}) first."
    raise TypeError(f"{operation}() takes a Document, not {type(doc).__name__}.{hint}")


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
