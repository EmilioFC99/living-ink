"""A transport that serves a xochitl-shaped directory.

:class:`CorpusTransport` implements :class:`~living_ink.transport.RemarkableTransport`
against a directory laid out the way the tablet lays out
``/home/root/.local/share/remarkable/xochitl``. Point it at the committed
fixture corpus and it is a device that works on any machine; point it at a
capture of the real tablet and it is that tablet, offline and repeatable.

This is deliberately not backed by a dict. A dict can only return what the test
author believed the device returns, so a wrong belief about the format gets
encoded into the fixture instead of caught by it. Parsing the same JSON the real
client parses means a format mistake fails a test.

For exercising ``living_ink.ssh`` itself — its argv construction, its BusyBox
output parsing — see :class:`FakeSSHRunner`, which fakes one layer lower.
"""

from __future__ import annotations

import io
import json
import shlex
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from living_ink.models import Document
from living_ink.transport import DeviceInfo, UnsupportedOperation, require_document

#: Where the real device keeps its documents. The fake reproduces the path so
#: that anything asserting on ``Document.local_path`` sees what it would see
#: against hardware.
XOCHITL_PATH = "/home/root/.local/share/remarkable/xochitl"

#: A Paper Pure, which is the device this project is developed against.
DEFAULT_DEVICE = DeviceInfo(
    model="reMarkable Paper Pro Move",
    firmware="3.20.0.92",
    screen=(1404, 1872),
    color=True,
)


class CorpusTransport:
    """Serve a xochitl-shaped directory as a reMarkable transport.

    Attributes:
        root: The corpus directory being served.
        calls: Every Protocol method call, in order, as
            ``(method_name, argument)``. Lets a test assert that a second sync
            downloaded nothing without reaching into private state.
    """

    def __init__(
        self,
        root: Path,
        *,
        connected: bool = True,
        include_deleted: bool = False,
        device: DeviceInfo = DEFAULT_DEVICE,
    ) -> None:
        """Wrap a corpus directory.

        Args:
            root: Directory holding ``*.metadata``, ``*.content`` and the
                per-document page directories.
            connected: What :meth:`check_connection` reports. False models an
                unplugged tablet without needing one.
            include_deleted: Whether trashed documents appear in listings. The
                shipped SSH client filters them out; orphan-handling tests need
                to see them.
            device: What :meth:`get_device_info` reports.
        """
        self.root = Path(root)
        self.calls: List[tuple] = []
        self._connected = connected
        self._include_deleted = include_deleted
        self._device = device
        self._documents: Optional[List[Document]] = None
        self._by_id: Dict[str, Document] = {}

    # -- Protocol ---------------------------------------------------------

    def check_connection(self) -> bool:
        """Report whether the corpus is readable.

        Returns:
            True when the transport is marked connected and the directory
            exists.
        """
        self.calls.append(("check_connection", None))
        return self._connected and self.root.is_dir()

    def get_meta_items(self, limit: Optional[int] = None) -> List[Document]:
        """List every document and folder in the corpus.

        Args:
            limit: Stop after this many items, as the real transports do.

        Returns:
            The documents, sorted by id so the order is stable across
            filesystems.

        Raises:
            ConnectionError: If the transport is marked disconnected.
        """
        self.calls.append(("get_meta_items", limit))
        self._require_connection()

        documents = []
        for path in sorted(self.root.glob("*.metadata")):
            doc = self._read_metadata(path)
            if doc is None:
                continue
            if doc.deleted and not self._include_deleted:
                continue
            documents.append(doc)
            if limit is not None and len(documents) >= limit:
                break

        self._documents = documents
        self._by_id = {d.id: d for d in documents}
        return list(documents)

    def get_doc(self, doc_id: str) -> Optional[Document]:
        """Look up one document by id.

        Args:
            doc_id: The document UUID.

        Returns:
            The document, or None if the corpus has no such id.
        """
        self.calls.append(("get_doc", doc_id))
        if self._documents is None:
            self.get_meta_items()
        return self._by_id.get(doc_id)

    def download(self, doc: Document) -> bytes:
        """Zip a document's files the way the transports do.

        The archive is flat: page files keep their basename, and the
        ``.content`` and any source PDF or EPUB sit beside them. That is the
        layout both shipped clients produce and the one ``extract`` expects.

        Args:
            doc: The document to download.

        Returns:
            The zip archive bytes.

        Raises:
            ConnectionError: If the transport is marked disconnected.
            TypeError: If given a document id rather than a Document.
        """
        doc = require_document(doc, "download")
        self.calls.append(("download", doc.id))
        self._require_connection()

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            page_dir = self.root / doc.id
            if page_dir.is_dir():
                for page in sorted(page_dir.iterdir()):
                    if page.is_file():
                        archive.writestr(page.name, page.read_bytes())

            for suffix in (".content", ".pagedata", ".pdf", ".epub"):
                sidecar = self.root / f"{doc.id}{suffix}"
                if sidecar.is_file():
                    archive.writestr(sidecar.name, sidecar.read_bytes())

        return buffer.getvalue()

    def get_file_type(self, doc: Document) -> Optional[str]:
        """Return the document's source file type.

        Args:
            doc: The document to inspect.

        Returns:
            ``"pdf"``, ``"epub"``, ``"notebook"``, or None when there is no
            readable ``.content``.

        Raises:
            TypeError: If given a document id rather than a Document.
        """
        doc = require_document(doc, "get_file_type")
        self.calls.append(("get_file_type", doc.id))
        content = self._read_content(doc.id)
        return content.get("fileType") if content else None

    def download_raw_file(self, doc: Document, extension: str) -> Optional[bytes]:
        """Return the source PDF or EPUB behind a document.

        Args:
            doc: The document to read.
            extension: File extension without the dot.

        Returns:
            The file bytes, or None if the document has no such file.

        Raises:
            TypeError: If given a document id rather than a Document.
        """
        doc = require_document(doc, "download_raw_file")
        self.calls.append(("download_raw_file", (doc.id, extension)))
        path = self.root / f"{doc.id}.{extension}"
        return path.read_bytes() if path.is_file() else None

    def get_tags(self, doc: Document) -> List[str]:
        """Return the document's tags, document-level and page-level.

        Args:
            doc: The document to read.

        Returns:
            Normalised, de-duplicated tags in declaration order.

        Raises:
            TypeError: If given a document id rather than a Document.
        """
        doc = require_document(doc, "get_tags")
        self.calls.append(("get_tags", doc.id))
        if doc.tags:
            return list(doc.tags)

        content = self._read_content(doc.id)
        if not content:
            return []

        from living_ink.extract import extract_tags_from_dict

        tags = extract_tags_from_dict(content)
        doc.tags = tags
        return list(tags)

    def get_device_info(self) -> DeviceInfo:
        """Describe the tablet this corpus came from.

        Returns:
            The configured device description.
        """
        self.calls.append(("get_device_info", None))
        return self._device

    # -- Corpus reading ---------------------------------------------------

    def page_order(self, doc_id: str) -> List[str]:
        """Return page ids in display order, skipping deleted pages.

        Page order is the order of ``cPages.pages``, not the filename sort
        order, and a removed page stays in that array carrying a marker. A
        reader that sorts filenames or ignores the marker publishes pages the
        tablet does not show.

        Args:
            doc_id: The document UUID.

        Returns:
            Page UUIDs in the order the tablet displays them.
        """
        content = self._read_content(doc_id)
        if not content:
            return []
        pages = content.get("cPages", {}).get("pages", [])
        return [p["id"] for p in pages if not p.get("deleted")]

    def _read_content(self, doc_id: str) -> Optional[dict]:
        """Parse a document's ``.content`` file.

        Args:
            doc_id: The document UUID.

        Returns:
            The parsed dict, or None if the file is absent or unreadable.
        """
        path = self.root / f"{doc_id}.content"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _read_metadata(self, path: Path) -> Optional[Document]:
        """Turn one ``.metadata`` file into a Document.

        Mirrors ``ssh.RemarkableSSHClient._parse_and_add_document``, including
        the epoch-milliseconds-in-a-string timestamp and the use of
        ``lastModified`` as the change hash.

        Args:
            path: The ``.metadata`` file.

        Returns:
            The document, or None if the file will not parse.
        """
        doc_id = path.name[: -len(".metadata")]
        try:
            metadata = json.loads(path.read_text())
        except (OSError, ValueError):
            return None

        last_modified = None
        raw = metadata.get("lastModified")
        if raw is not None:
            try:
                last_modified = datetime.fromtimestamp(int(raw) / 1000)
            except (TypeError, ValueError):
                pass

        return Document(
            id=doc_id,
            hash=str(raw) if raw else doc_id,
            name=metadata.get("visibleName", doc_id),
            doc_type=metadata.get("type", "DocumentType"),
            parent=metadata.get("parent", ""),
            deleted=bool(metadata.get("deleted", False)),
            pinned=bool(metadata.get("pinned", False)),
            synced=bool(metadata.get("synced", True)),
            last_modified=last_modified,
            size=0,
            local_path=f"{XOCHITL_PATH}/{doc_id}",
        )

    def _require_connection(self) -> None:
        """Raise if the transport is standing in for an unplugged tablet.

        Raises:
            ConnectionError: When marked disconnected.
        """
        if not self._connected:
            raise ConnectionError("Corpus transport is marked disconnected")


class UnsupportedCorpusTransport(CorpusTransport):
    """A corpus transport that cannot describe its device.

    The Cloud serves documents, not hardware, so it answers
    ``get_device_info`` with :class:`~living_ink.transport.UnsupportedOperation`.
    Callers must not use ``hasattr`` to find that out, and this is what proves
    they do not.
    """

    def get_device_info(self) -> DeviceInfo:
        """Refuse to describe the device.

        Raises:
            UnsupportedOperation: Always.
        """
        self.calls.append(("get_device_info", None))
        raise UnsupportedOperation("This transport cannot see the device")


class FakeSSHRunner:
    """Answer ``subprocess.run`` for ``ssh`` from a corpus directory.

    :class:`CorpusTransport` replaces ``living_ink.ssh`` wholesale, so it never
    executes the code that builds an argv, quotes a path into a remote shell
    command, or parses BusyBox output — and the argv is where the subprocess
    invariant (S5) lives. This fake sits one layer lower: patch it over
    ``subprocess.run`` and the real :class:`~living_ink.ssh.SSHClient` runs end
    to end against the corpus.

    It answers ``ssh`` and nothing else, because that is all the client runs.
    ``_scp_download`` is named for a tool it does not use: it shells out to
    ``ssh … cat`` and reads the bytes off stdout. A fake that also accepted
    ``scp`` would let a test assert on a transfer that never happens, which is
    the wrong belief this fixture exists to prevent.

    Because the client mixes both modes — ``text=True`` for shell output,
    raw bytes for ``cat`` — every handler produces bytes and :meth:`__call__`
    decodes them only when the caller passed ``text=True``, exactly as
    ``subprocess.run`` does. Returning ``str`` unconditionally is what let an
    earlier version of this fake build an archive of three empty pages.

    The tablet runs **BusyBox**, whose ``head`` rejects ``-5`` and wants
    ``-n 5``. Accepting GNU-only flags here would let the suite pass on argv the
    device refuses, so they are rejected.

    Attributes:
        root: The corpus directory being served.
        commands: Every remote command string received, in order.
        argvs: Every argv list received, in order.
    """

    #: Flags BusyBox's applets do not accept, checked against the remote command.
    GNU_ONLY = ("--color", "--time-style", "-printf")

    def __init__(self, root: Path) -> None:
        """Wrap a corpus directory.

        Args:
            root: Directory laid out the way xochitl lays out its documents.
        """
        self.root = Path(root)
        self.commands: List[str] = []
        self.argvs: List[List[str]] = []

    def __call__(self, argv, **kwargs):
        """Stand in for ``subprocess.run``.

        Args:
            argv: The command, as a list.
            **kwargs: Honoured for ``text``; the rest are accepted and ignored
                so the signature matches ``subprocess.run``.

        Returns:
            An object with ``returncode``, ``stdout`` and ``stderr``, whose
            streams are ``str`` when ``text=True`` and ``bytes`` otherwise.

        Raises:
            AssertionError: If the argv is not an ``ssh`` invocation, or uses a
                flag BusyBox does not accept.
        """
        argv = list(argv)
        self.argvs.append(argv)

        if argv[0] != "ssh":
            raise AssertionError(
                f"The SSH client only ever runs ssh; got {argv[0]!r}. If that "
                "changed in ssh.py, teach this fake the new tool rather than "
                "loosening the check."
            )
        return self._ssh(argv).decoded(bool(kwargs.get("text")))

    def _ssh(self, argv: List[str]) -> "_Completed":
        """Serve one remote shell command.

        Args:
            argv: The full ``ssh`` argv.

        Returns:
            A completed-process stand-in carrying bytes.
        """
        command = argv[-1]
        self.commands.append(command)
        for flag in self.GNU_ONLY:
            if flag in command:
                raise AssertionError(
                    f"{flag!r} is GNU-only; the tablet runs BusyBox and will reject it"
                )

        if command.startswith("cat "):
            target = shlex.split(command)[1]
            source = self._local(target)
            if not source.is_file():
                return _Completed(1, b"", b"cat: can't open: No such file or directory")
            return _Completed(0, source.read_bytes())

        if command.startswith("test -f"):
            target = shlex.split(command)[2]
            exists = self._local(target).is_file()
            return _Completed(0 if exists else 1, b"exists\n" if exists else b"")

        if command.startswith("find "):
            target = shlex.split(command)[1]
            local = self._local(target)
            if not local.is_dir():
                return _Completed(0, b"")
            names = sorted(p.name for p in local.iterdir() if p.is_file())
            listing = "".join(f"{target}/{n}\n" for n in names)
            return _Completed(0, listing.encode())

        if "===FILE===" in command:
            chunks = []
            for path in sorted(self.root.glob("*.metadata")):
                chunks.append(f"===FILE==={path.name[: -len('.metadata')]}\n")
                chunks.append(path.read_text())
                chunks.append("\n")
            return _Completed(0, "".join(chunks).encode())

        return _Completed(0, b"")

    def _local(self, remote_path: str) -> Path:
        """Map a device path onto the corpus directory.

        Args:
            remote_path: An absolute path on the tablet.

        Returns:
            The corresponding path inside the corpus.
        """
        relative = remote_path.replace(XOCHITL_PATH, "").lstrip("/")
        return self.root / relative


class _Completed:
    """A stand-in for ``subprocess.CompletedProcess``.

    Streams are held as bytes, which is what a process actually produces;
    :meth:`decoded` applies the caller's ``text=`` the way ``subprocess.run``
    would.

    Attributes:
        returncode: Process exit status.
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    def __init__(
        self,
        returncode: int,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
    ) -> None:
        """Record one process outcome.

        Args:
            returncode: Exit status.
            stdout: Standard output, as bytes before :meth:`decoded`.
            stderr: Standard error, as bytes before :meth:`decoded`.
        """
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def decoded(self, text: bool) -> "_Completed":
        """Return this outcome with its streams shaped for the caller.

        Args:
            text: Whether the caller passed ``text=True``.

        Returns:
            Self when the caller wanted bytes, otherwise a copy whose streams
            are decoded. Undecodable bytes are replaced rather than raising,
            because that is what a ``text=True`` read of binary output does
            under the default error handler on a real run.
        """
        if not text:
            return self
        return _Completed(
            self.returncode,
            self.stdout.decode(errors="replace"),
            self.stderr.decode(errors="replace"),
        )
