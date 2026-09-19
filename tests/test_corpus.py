"""Tests that run real reMarkable bytes through the real code.

Everything here reads a ``.rm``, a ``.content`` or a ``.metadata`` file and puts
it through the shipped parser. No dict stands in for a file format. That is the
point: a format bug cannot fail a test that never reads the format, and until
this module existed none of them did.

The committed corpus (tier 1) drives every test here. The ones marked
``corpus`` additionally run against a capture of the real tablet when one is
present, which is what proves the synthetic fixtures model the device correctly
rather than modelling our belief about it.
"""

import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from living_ink import extract
from living_ink.models import Document
from living_ink.transport import RemarkableTransport, UnsupportedOperation
from tests.fixtures import build_corpus as corpus_ids
from tests.fixtures.transport import (
    CorpusTransport,
    FakeSSHRunner,
    UnsupportedCorpusTransport,
)


def _holds_typed_text(rm_file: Path) -> bool:
    """Report whether a page carries typed text rather than only ink.

    Reading the block stream is milliseconds; *exporting* a text page is
    minutes, because of the quadratic described on
    :meth:`TestAgainstTheRealDevice.test_real_rm_pages_render`. This is how the
    fast test knows which pages to leave to the slow one.

    Args:
        rm_file: Path to a ``.rm`` page.

    Returns:
        True if the page has a root text block, False if it does not or if the
        page cannot be read at all.
    """
    from rmscene import read_blocks
    from rmscene.scene_stream import RootTextBlock

    try:
        with open(rm_file, "rb") as handle:
            return any(isinstance(b, RootTextBlock) for b in read_blocks(handle))
    except Exception:  # noqa: BLE001 - an unreadable page is the render test's problem
        return False


class TestCorpusIsWellFormed:
    """The committed corpus is present and shaped like a device."""

    def test_every_document_has_metadata_and_content(self, corpus_root):
        """Each document directory has both sidecar files beside it."""
        for page_dir in corpus_root.iterdir():
            if not page_dir.is_dir():
                continue
            assert (corpus_root / f"{page_dir.name}.metadata").is_file()
            assert (corpus_root / f"{page_dir.name}.content").is_file()

    def test_last_modified_is_a_string_of_epoch_milliseconds(self, corpus_root):
        """The device writes the timestamp as a string, not a number.

        ``pipeline.to_datetime()`` exists to absorb this. A fixture that used a
        number or an ISO string would let a regression there pass.
        """
        path = corpus_root / f"{corpus_ids.DOC_HANDWRITTEN}.metadata"
        raw = json.loads(path.read_text())["lastModified"]
        assert isinstance(raw, str)
        assert int(raw) > 1_000_000_000_000

    def test_page_modification_key_is_misspelled(self, corpus_root):
        """``cPages.pages[].modifed`` is the device's spelling, typo and all."""
        content = json.loads((corpus_root / f"{corpus_ids.DOC_HANDWRITTEN}.content").read_text())
        page = content["cPages"]["pages"][0]
        assert "modifed" in page
        assert "modified" not in page

    def test_all_rm_files_declare_version_six(self, corpus_root):
        """Every page except the deliberate outlier is format version 6."""
        outlier = corpus_root / corpus_ids.DOC_FUTURE_VERSION
        for rm in corpus_root.rglob("*.rm"):
            if rm.parent == outlier:
                continue
            if rm.parent.name == corpus_ids.DOC_TRUNCATED:
                continue
            assert extract.read_rm_version(rm) == 6, rm


class TestRenderingRealBytes:
    """The shipped renderer turns corpus pages into images."""

    def test_handwritten_page_renders_with_ink(self, corpus_root):
        """A page with strokes produces a PNG and reports its strokes."""
        rm = next((corpus_root / corpus_ids.DOC_HANDWRITTEN).glob("*.rm"))
        stats = extract.inspect_rm_page(rm)
        assert stats.strokes == 3
        assert stats.has_content

        png = extract.render_rm_file_to_png(rm, background_color="#FFFFFF")
        assert png is not None
        assert png.startswith(b"\x89PNG")

    def test_blank_page_has_no_strokes_but_still_renders(self, corpus_root):
        """A page the user left blank is legal, and not an error.

        This is the distinction ``sync.skip_empty`` rests on: no ink is not the
        same as a failed transcription.
        """
        pages = sorted((corpus_root / corpus_ids.DOC_BLANK_MIDDLE).glob("*.rm"))
        blank = pages[1]
        stats = extract.inspect_rm_page(blank)
        assert stats.strokes == 0
        assert not stats.has_content
        assert extract.render_rm_file_to_png(blank, background_color="#FFFFFF")

    def test_layers_and_highlighter_render(self, corpus_root):
        """Strokes across three layers all reach the image."""
        rm = next((corpus_root / corpus_ids.DOC_LAYERS).glob("*.rm"))
        stats = extract.inspect_rm_page(rm)
        assert stats.strokes == 5
        assert extract.render_rm_file_to_png(rm, background_color="#FFFFFF")

    def test_typed_text_page_renders(self, corpus_root):
        """A ``RootTextBlock`` page draws even though it has no strokes."""
        rm = next((corpus_root / corpus_ids.DOC_TYPED).glob("*.rm"))
        assert extract.inspect_rm_page(rm).strokes == 0
        assert extract.render_rm_file_to_png(rm, background_color="#FFFFFF")

    def test_future_format_version_is_refused_by_name(self, corpus_root):
        """A version this build cannot read raises rather than rendering blank."""
        rm = next((corpus_root / corpus_ids.DOC_FUTURE_VERSION).glob("*.rm"))
        assert extract.read_rm_version(rm) == 7
        with pytest.raises(extract.UnsupportedRmFormat) as caught:
            extract.render_rm_file_to_png(rm, background_color="#FFFFFF")
        assert "version 7" in str(caught.value)

    def test_truncated_page_does_not_crash_the_run(self, corpus_root):
        """An unreadable page returns nothing; it must not raise past the caller."""
        rm = next((corpus_root / corpus_ids.DOC_TRUNCATED).glob("*.rm"))
        result = extract.render_rm_file_to_png(rm, background_color="#FFFFFF")
        assert result is None

    def test_render_is_deterministic(self, corpus_root):
        """The same page renders to the same bytes twice.

        The render cache keys on the source plus a fingerprint and serves the
        stored PNG, so a non-deterministic renderer would make the cache return
        something the current code would not produce.
        """
        rm = next((corpus_root / corpus_ids.DOC_HANDWRITTEN).glob("*.rm"))
        first = extract.render_rm_file_to_png(rm, background_color="#FFFFFF")
        second = extract.render_rm_file_to_png(rm, background_color="#FFFFFF")
        assert first == second


class TestCorpusTransport:
    """The fake transport behaves like the shipped clients."""

    def test_satisfies_the_protocol(self, corpus_transport):
        """Nothing in the contract is missing."""
        assert isinstance(corpus_transport, RemarkableTransport)

    def test_implements_every_protocol_method(self, corpus_transport):
        """A transport that drops a method breaks callers that do not use hasattr."""
        required = [n for n in vars(RemarkableTransport) if not n.startswith("_")]
        missing = [n for n in required if not callable(getattr(corpus_transport, n, None))]
        assert missing == []

    def test_lists_documents_and_folders(self, corpus_transport):
        """A listing carries both types, with folders flagged."""
        items = corpus_transport.get_meta_items()
        folders = [i for i in items if i.is_folder]
        documents = [i for i in items if not i.is_folder]
        assert len(folders) == 3
        assert documents

    def test_trashed_documents_are_hidden_by_default(self, corpus_root):
        """The shipped SSH client filters deleted documents; so does this."""
        assert not any(
            d.id == corpus_ids.DOC_TRASHED for d in CorpusTransport(corpus_root).get_meta_items()
        )
        visible = CorpusTransport(corpus_root, include_deleted=True).get_meta_items()
        assert any(d.id == corpus_ids.DOC_TRASHED for d in visible)

    def test_limit_stops_the_listing(self, corpus_transport):
        """``limit`` is honoured, as both real transports honour it."""
        assert len(corpus_transport.get_meta_items(limit=4)) == 4

    def test_timestamp_survives_the_round_trip(self, corpus_transport):
        """Epoch milliseconds in a string become a datetime."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_HANDWRITTEN)
        assert doc.last_modified is not None
        assert doc.last_modified.year > 2020

    def test_download_produces_a_readable_archive(self, corpus_transport):
        """The zip is flat and holds the pages plus the content descriptor."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_BLANK_MIDDLE)
        archive = zipfile.ZipFile(BytesIO(corpus_transport.download(doc)))
        names = archive.namelist()
        assert sum(n.endswith(".rm") for n in names) == 3
        assert f"{corpus_ids.DOC_BLANK_MIDDLE}.content" in names

    def test_page_order_follows_cpages_not_filenames(self, corpus_transport):
        """Display order comes from ``cPages.pages``, and skips deleted pages."""
        order = corpus_transport.page_order(corpus_ids.DOC_DELETED_PAGE)
        assert len(order) == 2
        content = json.loads(
            (corpus_transport.root / f"{corpus_ids.DOC_DELETED_PAGE}.content").read_text()
        )
        assert order == [p["id"] for p in content["cPages"]["pages"] if not p.get("deleted")]

    def test_tags_come_back_document_and_page_level(self, corpus_transport):
        """Both tag kinds are read, normalised and de-duplicated."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_TAGGED)
        tags = corpus_transport.get_tags(doc)
        assert "project" in tags
        assert "follow-up" in tags

    def test_file_type_distinguishes_pdf_epub_and_notebook(self, corpus_transport):
        """``fileType`` drives renderer dispatch, so it has to be right."""
        get = corpus_transport.get_doc
        assert corpus_transport.get_file_type(get(corpus_ids.DOC_PDF)) == "pdf"
        assert corpus_transport.get_file_type(get(corpus_ids.DOC_EPUB)) == "epub"
        assert corpus_transport.get_file_type(get(corpus_ids.DOC_HANDWRITTEN)) == "notebook"

    def test_raw_file_round_trips(self, corpus_transport):
        """The source PDF behind an annotated document comes back intact."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_PDF)
        payload = corpus_transport.download_raw_file(doc, "pdf")
        assert payload.startswith(b"%PDF")
        assert corpus_transport.download_raw_file(doc, "epub") is None

    def test_an_id_instead_of_a_document_is_refused(self, corpus_transport):
        """Passing the id is a common slip and must name itself."""
        with pytest.raises(TypeError, match="Looks like a document id"):
            corpus_transport.download(corpus_ids.DOC_HANDWRITTEN)

    def test_disconnected_transport_raises(self, corpus_root):
        """An unplugged tablet is modelled without needing one."""
        transport = CorpusTransport(corpus_root, connected=False)
        assert not transport.check_connection()
        with pytest.raises(ConnectionError):
            transport.get_meta_items()

    def test_missing_capability_raises_rather_than_vanishing(self, corpus_root):
        """A transport that cannot serve a call still defines the method."""
        transport = UnsupportedCorpusTransport(corpus_root)
        assert hasattr(transport, "get_device_info")
        with pytest.raises(UnsupportedOperation):
            transport.get_device_info()


class TestExtractAgainstDownloadedArchives:
    """The extract helpers read a real archive, not a hand-built one."""

    def test_page_count_matches_the_corpus(self, corpus_transport, tmp_path):
        """``get_document_page_count`` counts what the transport shipped."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_BLANK_MIDDLE)
        archive = tmp_path / "doc.zip"
        archive.write_bytes(corpus_transport.download(doc))
        assert extract.get_document_page_count(archive) == 3

    def test_tags_read_from_a_real_zip(self, corpus_transport, tmp_path):
        """``extract_tags_from_zip`` parses the archive the transport built."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_TAGGED)
        archive = tmp_path / "tagged.zip"
        archive.write_bytes(corpus_transport.download(doc))
        assert "project" in extract.extract_tags_from_zip(archive)

    def test_pdf_text_extraction(self, corpus_transport, tmp_path):
        """A generated PDF still carries selectable text."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_PDF)
        pdf = tmp_path / "source.pdf"
        pdf.write_bytes(corpus_transport.download_raw_file(doc, "pdf"))
        assert "Fixture PDF page 1" in extract.extract_text_from_pdf(pdf)

    def test_epub_text_extraction(self, corpus_transport, tmp_path):
        """The hand-assembled EPUB is valid enough for the real reader."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_EPUB)
        book = tmp_path / "source.epub"
        book.write_bytes(corpus_transport.download_raw_file(doc, "epub"))
        assert "quick brown fox" in extract.extract_text_from_epub(book)

    def test_page_source_hashes_are_stable(self, corpus_transport, tmp_path):
        """The render cache keys on these, so they must not move between runs."""
        doc = corpus_transport.get_doc(corpus_ids.DOC_BLANK_MIDDLE)
        archive = tmp_path / "doc.zip"
        archive.write_bytes(corpus_transport.download(doc))
        assert extract.get_page_source_hashes(archive) == extract.get_page_source_hashes(archive)

    def test_render_page_from_document_zip(self, corpus_transport, tmp_path):
        """The archive-to-image path works on a real archive.

        Page numbering here is 1-based, unlike everything around it.
        """
        doc = corpus_transport.get_doc(corpus_ids.DOC_HANDWRITTEN)
        archive = tmp_path / "doc.zip"
        archive.write_bytes(corpus_transport.download(doc))
        png = extract.render_page_from_document_zip(archive, 1, background_color="#FFFFFF")
        assert png and png.startswith(b"\x89PNG")
        assert extract.render_page_from_document_zip(archive, 0) is None


class TestSSHAgainstTheCorpus:
    """The real SSH client, driven through a faked ``subprocess.run``.

    ``CorpusTransport`` replaces ``living_ink.ssh`` entirely, so it never runs
    the argv construction or the output parsing. These do.
    """

    def _client(self, corpus_root, monkeypatch):
        """Build a real SSH client whose subprocess calls hit the corpus.

        Args:
            corpus_root: The corpus directory to serve.
            monkeypatch: Pytest's patcher.

        Returns:
            A ``(client, runner)`` pair.
        """
        from living_ink import ssh

        runner = FakeSSHRunner(corpus_root)
        monkeypatch.setattr(ssh.subprocess, "run", runner)
        return ssh.SSHClient(), runner

    def test_listing_parses_the_batched_metadata_read(self, corpus_root, monkeypatch):
        """The ``===FILE===`` protocol round-trips through the real parser."""
        client, _ = self._client(corpus_root, monkeypatch)
        documents = client.get_meta_items()
        assert any(d.name == "handwritten" for d in documents)
        assert not any(d.id == corpus_ids.DOC_TRASHED for d in documents)

    def test_download_transfers_pages_with_ssh_cat_not_scp(self, corpus_root, monkeypatch):
        """The real zip-assembly path runs, one ``ssh … cat`` per file.

        ``SSHClient._scp_download`` is named for a tool it does not use: it
        pipes the file through ``cat`` over ``ssh`` because ``scp`` to stdout
        is unreliable across platforms. Asserting on the name rather than the
        behaviour is how an earlier version of this test passed while the fake
        silently produced three empty pages, so this checks the bytes too.
        """
        client, runner = self._client(corpus_root, monkeypatch)
        doc = client.get_doc(corpus_ids.DOC_BLANK_MIDDLE)
        archive = zipfile.ZipFile(BytesIO(client.download(doc)))

        pages = [n for n in archive.namelist() if n.endswith(".rm")]
        assert len(pages) == 3
        for name in pages:
            body = archive.read(name)
            assert body.startswith(b"reMarkable"), name

        assert not any(argv[0] == "scp" for argv in runner.argvs)
        cats = [c for c in runner.commands if c.startswith("cat ")]
        assert len(cats) >= len(pages)

    def test_a_hostile_title_never_reaches_the_remote_shell(self, corpus_root, monkeypatch):
        """Document titles are not interpolated into remote commands.

        The remote command is built from the document **id**, which the device
        generates. A title carrying quotes or a semicolon must not appear in an
        argv at all — this asserts the boundary rather than trusting it.
        """
        client, runner = self._client(corpus_root, monkeypatch)
        doc = client.get_doc(corpus_ids.DOC_HOSTILE_TITLE)
        assert '"draft"' in doc.name
        client.download(doc)
        for command in runner.commands:
            assert "draft" not in command
            assert "<v2>" not in command

    def test_busybox_only_flags(self, corpus_root, monkeypatch):
        """The fake rejects GNU-only flags, because the tablet does."""
        client, runner = self._client(corpus_root, monkeypatch)
        with pytest.raises(AssertionError, match="BusyBox"):
            client._ssh_command("ls --color /tmp")
        assert runner.commands


@pytest.mark.corpus
class TestAgainstTheRealDevice:
    """The same assertions, against bytes the tablet actually wrote.

    Skipped unless a capture exists. This is what proves the synthetic corpus
    models the device rather than modelling our belief about it.
    """

    def test_capture_is_a_xochitl_directory(self, device_corpus):
        """The capture has the layout the fake expects."""
        assert list(device_corpus.glob("*.metadata"))

    def test_real_metadata_parses_into_documents(self, device_corpus):
        """Every captured document survives the real parser."""
        transport = CorpusTransport(device_corpus)
        documents = transport.get_meta_items()
        assert documents
        assert all(isinstance(d, Document) for d in documents)
        assert all(d.name for d in documents)

    def test_real_timestamps_are_epoch_milliseconds_in_a_string(self, device_corpus):
        """The format assumption holds on real data, not just on fixtures."""
        for path in device_corpus.glob("*.metadata"):
            raw = json.loads(path.read_text()).get("lastModified")
            if raw is None:
                continue
            assert isinstance(raw, str), path.name
            assert int(raw) > 1_000_000_000_000, path.name

    def test_real_content_uses_the_misspelled_page_key(self, device_corpus):
        """``modifed`` is the device's spelling, confirmed against the device."""
        seen = False
        for path in device_corpus.glob("*.content"):
            pages = json.loads(path.read_text()).get("cPages", {}).get("pages", [])
            for page in pages:
                if "modifed" in page:
                    seen = True
                assert "modified" not in page, path.name
        assert seen, "no captured document carried a cPages entry"

    def test_real_rm_pages_render(self, device_corpus):
        """Every captured page of handwriting goes through the shipped renderer.

        Pages carrying **typed** text are left to
        :meth:`test_every_real_page_renders` because exporting one is
        quadratic, and the cause is not this project's code: ``rmscene``'s
        ``CrdtSequence.__iter__`` re-runs ``toposort_items`` on every
        iteration, and ``rmc`` iterates the text sequence once per character.
        A profile of one 32 KB typed page recorded 46,792 topological sorts,
        1.09 billion calls to ``hash``, and 210 seconds. A 58 KB page of pure
        ink in the same capture renders in 0.02 s, so the trigger is the text,
        not the size.

        Detecting it is cheap and exact — reading the block stream to look for
        a ``RootTextBlock`` takes 3 ms — so this skips on the real cause rather
        than on a size threshold that would happen to work today and stop
        working when someone writes a long note by hand.
        """
        pages = sorted(device_corpus.rglob("*.rm"))
        if not pages:
            pytest.skip("capture holds no handwritten pages")

        rendered = 0
        for rm in pages:
            if extract.read_rm_version(rm) != 6 or _holds_typed_text(rm):
                continue
            assert extract.render_rm_file_to_png(rm, background_color="#FFFFFF"), rm.name
            rendered += 1
        assert rendered, "no captured page rendered"

    @pytest.mark.slow
    def test_every_real_page_renders(self, device_corpus):
        """Every captured page renders, however long that takes.

        Deselected by default — see :meth:`test_real_rm_pages_render` for why
        one page costs minutes. Run it with ``pytest -m slow``. This is the
        test that would catch a renderer change breaking a page the fast
        sample never reaches.
        """
        pages = sorted(device_corpus.rglob("*.rm"))
        if not pages:
            pytest.skip("capture holds no handwritten pages")

        failed = []
        for rm in pages:
            if extract.read_rm_version(rm) != 6:
                continue
            if not extract.render_rm_file_to_png(rm, background_color="#FFFFFF"):
                failed.append(rm.name)
        assert not failed, f"pages rendered to nothing: {failed}"

    def test_real_documents_download_through_the_fake(self, device_corpus, tmp_path):
        """A captured document assembles into an archive extract can read."""
        transport = CorpusTransport(device_corpus)
        for doc in transport.get_meta_items():
            if doc.is_folder or not (device_corpus / doc.id).is_dir():
                continue
            archive = tmp_path / f"{doc.id}.zip"
            archive.write_bytes(transport.download(doc))
            assert extract.get_document_page_count(archive) >= 0
            return
        pytest.skip("capture holds no page directories")


class TestDefectsTheCorpusFound:
    """Two bugs that only a real ``.content`` file exposes.

    Both are recorded here as the behaviour that ships today, with the correct
    behaviour named. Each is fixed by a later slice, and the assertion flips
    when it is.
    """

    def test_a_deleted_page_still_reaches_the_pipeline(self, corpus_transport, tmp_path):
        """``_get_ordered_rm_files`` ignores the per-page deletion marker.

        A page the user removed stays in ``cPages.pages`` carrying
        ``deleted``. ``extract._get_ordered_rm_files`` (``extract.py:826``)
        builds its order from every entry in that array without checking the
        marker, so a removed page is rendered, transcribed and published.

        The fixture has three pages with the middle one removed: the tablet
        shows two.
        """
        doc = corpus_transport.get_doc(corpus_ids.DOC_DELETED_PAGE)
        archive = tmp_path / "doc.zip"
        archive.write_bytes(corpus_transport.download(doc))

        assert len(corpus_transport.page_order(corpus_ids.DOC_DELETED_PAGE)) == 2
        # Correct behaviour is 2. This pins the defect until it is fixed.
        assert extract.get_document_page_count(archive) == 3

    def test_trash_is_a_parent_not_a_deleted_flag(self, corpus_root):
        """A trashed document carries ``parent: "trash"`` and no ``deleted`` key.

        ``ssh.RemarkableSSHClient._parse_and_add_document`` (``ssh.py:205``)
        drops a document when ``metadata["deleted"]`` is true. The tablet does
        not set that key when a document is thrown away — it reparents it to
        ``"trash"`` — so the filter never fires and deleted notebooks are
        listed as live ones. Nothing downstream re-checks: ``discover_documents``
        filters on type and file type only, and ``get_notebook_path``
        (``pipeline.py:732``) merely labels the path ``[TRASH]``.

        Confirmed against the device on 2026-09-19: a listing returned all 24
        items, the 7 trashed ones included, every one with ``deleted=False``.
        """
        metadata = json.loads((corpus_root / f"{corpus_ids.DOC_TRASHED}.metadata").read_text())
        assert metadata["parent"] == "trash"

        transport = CorpusTransport(corpus_root, include_deleted=True)
        trashed = transport.get_doc(corpus_ids.DOC_TRASHED)
        assert trashed.parent == "trash"


class TestFixtureBuildersAreDeterministic:
    """A committed fixture has to be reproducible to be reviewable."""

    def test_the_same_page_builds_the_same_bytes(self, tmp_path):
        """No clock, no ``uuid4``, no randomness."""
        from tests.fixtures.builders import Layer, stroke, write_rm

        layers = [Layer("Layer 1", [stroke([(0, 0), (100, 100)])])]
        first = write_rm(tmp_path / "a.rm", layers).read_bytes()
        second = write_rm(tmp_path / "b.rm", layers).read_bytes()
        assert first == second

    def test_rebuilding_the_corpus_reproduces_the_committed_bytes(self, tmp_path):
        """The committed corpus matches what its builder produces today.

        A mismatch is not automatically a bug — an ``rmscene`` upgrade can move
        the bytes, and the corpus is deliberately frozen against that. It does
        mean the change needs a human decision, which is why this asserts.
        """
        rebuilt = corpus_ids.build(tmp_path / "corpus")
        committed = Path(__file__).parent / "fixtures" / "corpus"
        for produced in sorted(rebuilt.rglob("*")):
            if not produced.is_file():
                continue
            mirror = committed / produced.relative_to(rebuilt)
            assert mirror.is_file(), f"{mirror} is missing from the committed corpus"
            assert produced.read_bytes() == mirror.read_bytes(), mirror
