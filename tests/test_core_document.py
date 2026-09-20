"""The domain model: what each type promises, and what it refuses.

These are the invariants every destination is written against, so they are
tested here rather than inside whichever destination happens to rely on one.
"""

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from living_ink.core.document import Document, Page, PublishContext, PublishResult


def page(**kwargs) -> Page:
    """Build a page, defaulting the two fields that have no default."""
    return Page(**{"index": 0, "number": 1, "label": "Page 1", **kwargs})


class TestPageCarriesItsOwnPresentation:
    """Number, label and breadcrumbs are fields, not things to re-derive."""

    def test_the_number_is_not_the_index(self):
        """An annotated PDF's pages are sparse: page 3 of 3 can be p. 377."""
        p = page(index=2, number=377, label="Page 377")
        assert p.index == 2
        assert p.number == 377

    def test_breadcrumbs_default_to_empty_for_a_notebook(self):
        assert page().breadcrumbs == ()

    def test_a_page_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            page().text = "rewritten"

    def test_a_page_is_replaced_not_mutated(self):
        """How the OCR stage puts text on a page rendered three stages earlier."""
        rendered = page(image_path=Path("/tmp/page-1.png"))
        transcribed = dataclasses.replace(rendered, text="Hello")

        assert transcribed.text == "Hello"
        assert transcribed.image_path == rendered.image_path
        assert rendered.text == ""


class TestAFailedPageIsNotABlankOne:
    """The distinction the whole ``error`` field exists for."""

    def test_a_blank_page_has_no_error(self):
        assert page(text="").error is None

    def test_a_failed_page_has_no_text_either(self):
        """Both are ``text=""``; only ``error`` tells them apart."""
        failed = page(text="", error="The provider returned 429.")
        blank = page(text="")

        assert failed.text == blank.text
        assert failed.error != blank.error

    def test_the_document_counts_them(self):
        doc = Document(
            doc_id="d1",
            title="Notes",
            pages=(page(index=0, number=1, label="Page 1", text="a"), page(error="boom")),
        )
        assert doc.failed_pages() == 1

    def test_every_page_failing_is_countable_as_a_whole_document_failure(self):
        doc = Document(doc_id="d1", title="Notes", pages=(page(error="boom"), page(error="boom")))
        assert doc.failed_pages() == len(doc.pages)


class TestDocumentHasText:
    """What "this document transcribed to nothing" means, in one place."""

    def test_a_document_with_a_transcribed_page_has_text(self):
        assert Document(doc_id="d1", title="N", pages=(page(text="Hello"),)).has_text() is True

    def test_a_document_with_only_whitespace_does_not(self):
        assert Document(doc_id="d1", title="N", pages=(page(text="   \n"),)).has_text() is False

    def test_an_extracted_text_layer_counts(self):
        """An unannotated PDF has no pages and no transcription, only text."""
        doc = Document(doc_id="d1", title="N", body_text="Chapter One")
        assert doc.has_text() is True

    def test_a_document_with_no_pages_at_all_has_nothing(self):
        assert Document(doc_id="d1", title="N").has_text() is False


class TestDocumentIdentity:
    """The document id is the identity; the title is a label on it."""

    def test_two_documents_can_share_a_title(self):
        a = Document(doc_id="d1", title="Notes")
        b = Document(doc_id="d2", title="Notes")
        assert a != b

    def test_the_folder_path_is_a_tuple_of_names_not_a_string(self):
        """A separator inside a folder name is a bug waiting in a split()."""
        doc = Document(doc_id="d1", title="N", folder_path=("Work", "Q1 / Q2"))
        assert doc.folder_path[1] == "Q1 / Q2"

    def test_modified_is_a_datetime_not_whatever_the_transport_said(self):
        doc = Document(doc_id="d1", title="N", modified=datetime(2026, 3, 4, 10, 30))
        assert doc.modified.year == 2026


class TestPublishContext:
    """Everything about this publish that is not the document."""

    def test_only_the_document_id_is_required(self):
        """``unpublish`` is driven from a state-store row with no Document."""
        ctx = PublishContext(doc_id="d1")
        assert ctx.dry_run is False
        assert ctx.existing_target is None

    def test_it_carries_the_previous_publication_back_in(self):
        ctx = PublishContext(
            doc_id="d1", existing_external_id="x-1", existing_target="Inbox/Notes.md"
        )
        assert ctx.existing_external_id == "x-1"
        assert ctx.existing_target == "Inbox/Notes.md"

    def test_it_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            PublishContext(doc_id="d1").dry_run = True


class TestPublishResultIsUnchanged:
    """The one type that already shipped keeps its defaults."""

    def test_a_bare_failure_needs_only_ok(self):
        assert PublishResult(ok=False).warnings == ()
