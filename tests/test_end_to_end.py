"""One sync, start to finish, with almost nothing faked.

The rest of the suite tests stages. This tests the seam between them, which is
where the bugs that reach a user actually live: a stage can be individually
correct and still hand the next one a value it does not expect.

Two things are faked and nothing else:

============================  ==========  =====================================
Thing                         Real?       Why
============================  ==========  =====================================
Transport                     **faked**   A test must not need a tablet.
AI provider                   **faked**   A test must not make a network call.
Download and unzip            real        ``CorpusTransport`` builds a genuine
                                          archive; ``extract`` opens it.
``.rm`` parsing and render    real        rmc and rmscene, on real bytes.
Page ordering                 real        Read from ``cPages``, not a list.
Tags                          real        ``extract.extract_tags_from_dict``.
Timestamps                    real        ``pipeline.to_datetime`` on the
                                          device's epoch-millisecond strings.
Render and transcript caches  real        On disk, and re-read on the 2nd run.
Obsidian note writing         real        Frontmatter, WikiLinks, attachments.
State store                   real        A genuine ``state.db``.
============================  ==========  =====================================

The provider fake is the honest place to cut: everything before it is this
project's code and runs for real, and what a language model returns for a given
image is not something a test can assert on anyway.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from living_ink.destinations import ObsidianDestination
from living_ink.pipeline import SyncOptions, SyncPipeline
from living_ink.providers import TextRepairProvider
from tests.fixtures import build_corpus as corpus_ids
from tests.fixtures.transport import CorpusTransport


class RecordingProvider(TextRepairProvider):
    """A vision provider that transcribes deterministically and remembers.

    Returns text derived from a digest of the image bytes, so two renders of
    the same page produce the same transcription and two different pages never
    collide. That makes the cache observable: a second run that still reaches
    this provider has failed to reuse a banked transcription, and the recorded
    call list is how the test sees it.

    Attributes:
        images: Every image path handed to :meth:`ocr_image`, in order.
        sizes: The byte size of each of those images, so a test can assert a
            real PNG arrived rather than an empty file.
    """

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.images: list[str] = []
        self.sizes: list[int] = []

    @property
    def name(self) -> str:
        """Identify this provider in logs.

        Returns:
            A fixed name.
        """
        return "recording-test-provider"

    @property
    def supports_vision(self) -> bool:
        """Claim vision support so the OCR path is the multimodal one.

        Returns:
            True, always.
        """
        return True

    def ocr_image(self, image_path: str, instructions: str) -> str:
        """Transcribe an image by digesting it.

        Args:
            image_path: The rendered PNG.
            instructions: The real prompt, loaded from the shipped file.

        Returns:
            A deterministic line of text naming the image's digest.
        """
        data = Path(image_path).read_bytes()
        self.images.append(image_path)
        self.sizes.append(len(data))
        digest = hashlib.sha256(data).hexdigest()[:12]
        return f"transcribed page {digest}"

    def repair_text(self, raw_text: str, instructions: str) -> str:
        """Return the text unchanged.

        Args:
            raw_text: Text to clean.
            instructions: The real cleanup prompt.

        Returns:
            ``raw_text``, untouched.
        """
        return raw_text


@pytest.fixture
def e2e(tmp_path, corpus_root, monkeypatch):
    """Wire a pipeline that syncs the committed corpus into a fresh vault.

    Everything on disk lands under ``tmp_path``: the vault, the state store,
    both caches and the temp render directory. Nothing here can touch the
    developer's own vault, and each test gets caches that start cold.

    Args:
        tmp_path: Pytest's per-test directory.
        corpus_root: The committed fixture corpus.
        monkeypatch: Pytest's patcher.

    Returns:
        A ``_Harness`` exposing ``vault``, ``provider``, ``transport`` and a
        ``sync()`` that runs the pipeline.
    """
    from living_ink import clean
    from living_ink import pipeline as pipeline_module

    data_dir = tmp_path / "data"
    vault = tmp_path / "vault"
    vault.mkdir()

    # Every runtime path is a module constant resolved at import, so each one
    # is redirected by name and then created. ``ensure_runtime_dirs`` caches a
    # "done" flag after its first call anywhere in the session, so clearing it
    # is what stops these directories from silently never being made — which is
    # exactly the failure this fixture hit first time round.
    for name, leaf in (
        ("DATA_DIR", "."),
        ("WHITE_DIR", "remarkable_pngs_white"),
        ("VISION_DIR", "remarkable_pngs_for_vision"),
        ("OCR_DIR", "output"),
        ("PDF_DIR", "remarkable_pdfs"),
        ("DOCS_DIR", "remarkable_documents"),
        ("LOGS_DIR", "logs"),
        ("TRANSCRIPT_CACHE_DIR", "transcripts"),
        ("RENDER_CACHE_DIR", "renders"),
    ):
        monkeypatch.setattr(pipeline_module, name, (data_dir / leaf).resolve())
    monkeypatch.setattr(pipeline_module, "_runtime_dirs_ready", False)
    pipeline_module.ensure_runtime_dirs()

    provider = RecordingProvider()
    monkeypatch.setattr(clean, "_provider", provider)
    monkeypatch.setattr(clean, "ENABLE_REPAIR", True)

    transport = CorpusTransport(corpus_root)

    class _Harness:
        """The pieces a test needs to run and inspect one sync."""

        def __init__(self) -> None:
            """Hold the fixture's wiring."""
            self.vault = vault
            self.data_dir = data_dir
            self.provider = provider
            self.transport = transport

        def sync(self, **options) -> bool:
            """Run one full sync into the vault.

            Args:
                **options: Fields for :class:`~living_ink.pipeline.SyncOptions`.

            Returns:
                Whether the run reported success.
            """
            destination = ObsidianDestination(vault_path=str(vault))
            pipe = SyncPipeline(
                SyncOptions(**options),
                data_dir=data_dir,
                destinations=[destination],
            )
            pipe.connect = lambda: transport
            return pipe.run()

    return _Harness()


class TestOneRealSync:
    """A handwritten notebook goes from ``.rm`` bytes to a note on disk."""

    def test_a_notebook_becomes_a_note(self, e2e):
        """The whole chain runs and writes a Markdown file.

        This is the test that fails when any two stages stop agreeing, which
        no stage-level test can see.
        """
        e2e.sync(notebook=corpus_ids.DOC_HANDWRITTEN)

        notes = list(e2e.vault.rglob("*.md"))
        assert notes, "the sync wrote no note"

        body = notes[0].read_text()
        assert "living_ink_id:" in body, "frontmatter is missing the identity key"
        assert corpus_ids.DOC_HANDWRITTEN in body
        assert "transcribed page" in body, "the transcription did not reach the note"

    def test_the_provider_received_a_real_rendered_page(self, e2e):
        """What reached the AI was a PNG with pixels in it.

        A renderer that silently produced nothing would still let the note be
        written, just empty — which is the failure this asserts against.
        """
        e2e.sync(notebook=corpus_ids.DOC_HANDWRITTEN)

        assert e2e.provider.images, "no page was sent for transcription"
        assert all(size > 1000 for size in e2e.provider.sizes), e2e.provider.sizes
        for image in e2e.provider.images:
            assert Path(image).suffix == ".png"

    def test_a_second_sync_transcribes_nothing(self, e2e):
        """The caches make an unchanged notebook free the second time.

        Both caches are real and on disk here, so this exercises the
        fingerprinting rather than a stub that always reports a hit.
        """
        e2e.sync(notebook=corpus_ids.DOC_HANDWRITTEN)
        first = len(e2e.provider.images)
        assert first

        e2e.provider.images.clear()
        e2e.sync(notebook=corpus_ids.DOC_HANDWRITTEN)
        assert not e2e.provider.images, "the second run re-transcribed a cached page"

    def test_dry_run_writes_nothing(self, e2e):
        """``--dry-run`` transcribes but publishes nothing."""
        e2e.sync(notebook=corpus_ids.DOC_HANDWRITTEN, dry_run=True)
        assert not list(e2e.vault.rglob("*.md"))
