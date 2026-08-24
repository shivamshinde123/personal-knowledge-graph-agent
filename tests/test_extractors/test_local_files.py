"""Tests for the local file extractor.

Runs against real files in a temp directory — text extraction from actual
PDF/DOCX/txt/md files, not mocked readers.
"""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import docx
import pytest

from extractors.local_files import extract_new_items
from tests.test_extractors.pdf_fixture import make_pdf_bytes


@pytest.fixture
def watch_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "extractors.local_files.get_settings",
        lambda: SimpleNamespace(env=SimpleNamespace(watch_dirs=[tmp_path])),
    )
    return tmp_path


def write_txt(directory, name, text):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def write_docx(directory, name, *paragraphs):
    path = directory / name
    doc = docx.Document()
    for paragraph in paragraphs:
        doc.add_paragraph(paragraph)
    doc.save(path)
    return path


def write_pdf(directory, name, text):
    path = directory / name
    path.write_bytes(make_pdf_bytes(text))
    return path


class TestSupportedFileTypes:
    def test_extracts_plain_text_files(self, watch_dir):
        write_txt(watch_dir, "notes.txt", "Extractors never import storage.")

        items = extract_new_items()

        assert len(items) == 1
        assert items[0].raw_text == "Extractors never import storage."
        assert items[0].title == "notes.txt"

    def test_extracts_markdown_files(self, watch_dir):
        write_txt(watch_dir, "readme.md", "# Heading\n\nBody text.")

        items = extract_new_items()

        assert items[0].raw_text == "# Heading\n\nBody text."

    def test_extracts_docx_paragraphs(self, watch_dir):
        write_docx(watch_dir, "doc.docx", "First paragraph.", "Second paragraph.")

        items = extract_new_items()

        assert items[0].raw_text == "First paragraph.\nSecond paragraph."

    def test_extracts_pdf_text(self, watch_dir):
        write_pdf(watch_dir, "file.pdf", "hello pdf")

        items = extract_new_items()

        assert items[0].raw_text == "hello pdf"

    def test_ignores_unsupported_extensions(self, watch_dir):
        write_txt(watch_dir, "image.png", "not really an image")

        assert extract_new_items() == []

    def test_ignores_empty_files(self, watch_dir):
        write_txt(watch_dir, "empty.txt", "   \n  ")

        assert extract_new_items() == []


class TestItemFields:
    def test_populates_the_common_extraction_contract(self, watch_dir):
        write_txt(watch_dir, "notes.txt", "content")

        item = extract_new_items()[0]

        assert item.source_type == "local_file"
        assert item.source_ref_id == str(watch_dir / "notes.txt")
        assert item.url_or_path == str(watch_dir / "notes.txt")
        assert item.author_or_sender is None
        assert isinstance(item.last_edited_at, datetime)
        assert item.last_edited_at.tzinfo is not None

    def test_recurses_into_subdirectories(self, watch_dir):
        sub = watch_dir / "sub" / "nested"
        sub.mkdir(parents=True)
        write_txt(sub, "deep.txt", "buried content")

        items = extract_new_items()

        assert len(items) == 1
        assert items[0].source_ref_id == str(sub / "deep.txt")


class TestSinceFiltering:
    def test_no_since_returns_everything(self, watch_dir):
        write_txt(watch_dir, "a.txt", "a")
        write_txt(watch_dir, "b.txt", "b")

        assert len(extract_new_items(since=None)) == 2

    def test_since_excludes_files_not_modified_after_it(self, watch_dir):
        write_txt(watch_dir, "old.txt", "old content")
        cutoff = datetime.now(UTC)
        time.sleep(0.05)
        write_txt(watch_dir, "new.txt", "new content")

        items = extract_new_items(since=cutoff)

        assert [i.title for i in items] == ["new.txt"]


class TestMissingOrUnreadableInputs:
    def test_missing_watch_directory_is_skipped_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "extractors.local_files.get_settings",
            lambda: SimpleNamespace(
                env=SimpleNamespace(watch_dirs=[tmp_path / "does-not-exist"])
            ),
        )

        assert extract_new_items() == []

    def test_corrupted_pdf_is_skipped_not_raised(self, watch_dir):
        (watch_dir / "broken.pdf").write_bytes(b"not actually a pdf")
        write_txt(watch_dir, "good.txt", "still works")

        items = extract_new_items()

        assert [i.title for i in items] == ["good.txt"]
