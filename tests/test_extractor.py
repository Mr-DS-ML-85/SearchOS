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
    result = extract_text("just some plain text".encode())
    assert result.content == "just some plain text"


def test_extract_pdf_invalid_bytes_raises():
    from pypdf.errors import PdfReadError

    with pytest.raises(PdfReadError):
        extract_pdf(b"not a real pdf")
