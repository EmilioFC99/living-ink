"""Tests for the two file-writing stages: page preparation and the transcript.

Both were private methods that needed a ``DocumentJob`` and a ``SyncPipeline``
to exercise. They take what they work on now, so these read as what they are:
an image goes in and a bigger image comes out, pages go in and a file comes out.
"""

import json

import pytest
from PIL import Image

from living_ink.core.stages import prepare_pages, preprocess_image, write_transcript
from tests.builders import make_page


def write_png(path, size=(40, 60), mode="RGB"):
    """Write a small image to disk and return its path."""
    Image.new(mode, size, "white").save(path)
    return path


class TestPreparingAPageForOcr:
    """The model reads a flattened, sharpened, enlarged copy, never the render."""

    def test_the_prepared_page_is_upscaled(self, tmp_path):
        source = write_png(tmp_path / "page-1.png", size=(40, 60))
        out = tmp_path / "pre" / "page-1.png"

        preprocess_image(source, out)

        assert Image.open(out).size == (60, 90)

    def test_large_page_is_capped_at_max_dimension(self, tmp_path):
        """Pages larger than MAX_DIMENSION are capped to prevent context overflow."""
        source = write_png(tmp_path / "page-1.png", size=(1404, 1872))
        out = tmp_path / "pre" / "page-1.png"

        preprocess_image(source, out)

        w, h = Image.open(out).size
        assert max(w, h) == 1800
        assert w == 1350

    def test_transparency_is_flattened_onto_white(self, tmp_path):
        """A transparent render OCRs as a black page without this."""
        source = tmp_path / "page-1.png"
        Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(source)
        out = tmp_path / "pre" / "page-1.png"

        preprocess_image(source, out)

        prepared = Image.open(out)
        assert prepared.mode == "RGB"
        assert prepared.getpixel((0, 0)) == (255, 255, 255)

    def test_the_output_directory_is_created(self, tmp_path):
        source = write_png(tmp_path / "page-1.png")

        preprocess_image(source, tmp_path / "nested" / "deeper" / "page-1.png")

        assert (tmp_path / "nested" / "deeper" / "page-1.png").exists()

    def test_pages_come_back_in_the_order_they_went_in(self, tmp_path):
        """The transcriber reports results positionally; order is the contract."""
        images = [write_png(tmp_path / f"page-{n}.png") for n in (1, 2, 3)]

        prepared = prepare_pages(images, tmp_path / "pre")

        assert [p.name for p in prepared] == ["page-1.png", "page-2.png", "page-3.png"]
        assert all(p.parent == tmp_path / "pre" for p in prepared)

    def test_the_original_render_is_left_alone(self, tmp_path):
        """``--keep-temp`` is for comparing the two; overwriting loses that."""
        source = write_png(tmp_path / "page-1.png", size=(40, 60))

        prepare_pages([source], tmp_path / "pre")

        assert Image.open(source).size == (40, 60)

    def test_no_pages_is_not_an_error(self, tmp_path):
        assert prepare_pages([], tmp_path / "pre") == []


class TestTheTranscriptArtifact:
    """Written for a person to read; nothing in the pipeline reads it back."""

    @pytest.fixture
    def path(self, tmp_path):
        return tmp_path / "transcript.txt"

    def test_the_first_line_is_the_metadata(self, path):
        write_transcript(path, {"notebook": "Notes"}, [])

        assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == {"notebook": "Notes"}

    def test_a_page_keeps_the_number_the_document_calls_it(self, path):
        """An annotated PDF's page 377 is page 377 here, not page 2."""
        write_transcript(path, {}, [make_page(number=377, text="ink")])

        assert "377" in path.read_text(encoding="utf-8")

    def test_a_failed_page_says_why_where_its_text_would_be(self, path):
        write_transcript(path, {}, [make_page(number=1, text="", error="429 rate limited")])

        assert "429 rate limited" in path.read_text(encoding="utf-8")

    def test_a_blank_page_keeps_its_header_and_gets_no_body(self, path):
        """Dropping it would shift every later page out of line with the notebook."""
        write_transcript(path, {}, [make_page(number=1, text=""), make_page(number=2, text="two")])

        written = path.read_text(encoding="utf-8")
        assert "Page 1" in written and "Page 2" in written

    def test_a_text_layer_stands_in_when_there_are_no_pages(self, path):
        """An unannotated PDF renders nothing and still has everything to say."""
        write_transcript(path, {}, [], extracted_text="the whole book")

        assert "the whole book" in path.read_text(encoding="utf-8")

    def test_a_text_layer_is_appended_when_no_page_read(self, path):
        write_transcript(path, {}, [make_page(number=1, text="")], extracted_text="the whole book")

        assert "the whole book" in path.read_text(encoding="utf-8")

    def test_a_text_layer_is_left_out_when_the_pages_read(self, path):
        """Otherwise every annotated page is followed by the whole document again."""
        write_transcript(path, {}, [make_page(number=1, text="ink")], extracted_text="the book")

        assert "the book" not in path.read_text(encoding="utf-8")
