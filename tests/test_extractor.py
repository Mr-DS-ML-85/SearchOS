from __future__ import annotations

import pytest
from app.extract.extractor import extract_html, extract_pdf, extract_text

SIMPLE_HTML = b"""
<!DOCTYPE html>
<html>
<head><title>Test Page Title</title></head>
<body>
  <nav>Navigation junk</nav>
  <article>
    <h1>Main Heading</h1>
    <p>This is the first meaningful paragraph about search engines.</p>
    <p>And a second paragraph with <b>bold</b> text.</p>
  </article>
  <footer>Footer junk that should be ignored</footer>
</body>
</html>
"""


def test_extract_html_gets_content_and_title():
    result = extract_html(SIMPLE_HTML)
    assert result.title == "Test Page Title"
    assert "first meaningful paragraph" in result.content
    assert "Navigation junk" not in result.content


def test_extract_text_passthrough():
    result = extract_text(b"just some plain text")
    assert result.content == "just some plain text"


def test_extract_pdf_invalid_bytes_raises():
    from pypdf.errors import PdfReadError

    with pytest.raises(PdfReadError):
        extract_pdf(b"not a real pdf")


class TestExtractPageDispatch:
    """extract_page is the real entry point; the old tests bypassed it."""

    def _page(self, content: bytes, content_type: str):
        from app.extract.fetcher import FetchedPage

        return FetchedPage(
            url="https://x.com/a",
            final_url="https://x.com/a",
            status_code=200,
            content_type=content_type,
            content=content,
            headers={},
        )

    def test_html_goes_through_html_extractor(self):
        """Regression: text/* was tested first, so text/html returned raw markup."""
        from app.extract.extractor import extract_page

        html = (
            b"<html><head><title>Real Title</title></head><body><article><p>Body text here.</p></article></body></html>"
        )
        out = extract_page(self._page(html, "text/html; charset=UTF-8"))
        assert "Body text here." in out.content
        assert "<html>" not in out.content
        assert out.title == "Real Title"

    def test_xhtml_goes_through_html_extractor(self):
        from app.extract.extractor import extract_page

        html = b"<html><head><title>X</title></head><body><article><p>Extracted.</p></article></body></html>"
        out = extract_page(self._page(html, "application/xhtml+xml"))
        assert "Extracted." in out.content

    def test_plain_text_passes_through(self):
        from app.extract.extractor import extract_page

        out = extract_page(self._page(b"just words", "text/plain; charset=utf-8"))
        assert out.content == "just words"

    def test_json_passes_through(self):
        from app.extract.extractor import extract_page

        out = extract_page(self._page(b'{"a":1}', "application/json"))
        assert out.content == '{"a":1}'

    def test_vendor_json_passes_through(self):
        from app.extract.extractor import extract_page

        out = extract_page(self._page(b"{}", "application/vnd.api+json"))
        assert out.content == "{}"

    def test_pdf_dispatched_to_pdf_extractor(self):
        import pypdf
        from app.extract.extractor import extract_page

        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = __import__("io").BytesIO()
        writer.write(buf)
        out = extract_page(self._page(buf.getvalue(), "application/pdf"))
        assert out.content is not None

    def test_binary_is_rejected_not_html_fallback(self):
        """Previously image/png was fed to trafilatura and failed obscurely."""
        import pytest as _pytest
        from app.errors import ValidationError
        from app.extract.extractor import extract_page

        with _pytest.raises(ValidationError, match="image/png"):
            extract_page(self._page(b"\x89PNG\r\n\x1a\n", "image/png"))

    def test_zip_is_rejected(self):
        import pytest as _pytest
        from app.errors import ValidationError
        from app.extract.extractor import extract_page

        with _pytest.raises(ValidationError, match="application/zip"):
            extract_page(self._page(b"PK\x03\x04", "application/zip"))

    def test_missing_content_type_tries_html(self):
        from app.extract.extractor import extract_page

        html = b"<html><head><title>T</title></head><body><article><p>Body.</p></article></body></html>"
        out = extract_page(self._page(html, None))
        assert "Body." in out.content
