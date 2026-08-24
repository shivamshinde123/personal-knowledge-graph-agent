"""A hand-built minimal single-page PDF, for extractor tests.

pypdf can read this without needing a PDF-writing library as a test-only
dependency: it's a valid but minimal file with one page of Helvetica text
in a single content stream.
"""

from __future__ import annotations


def make_pdf_bytes(text: str) -> bytes:
    r"""Build a minimal one-page PDF containing ``text``.

    Args:
        text: Text to place on the page. Must not contain ``(``, ``)``, or
            ``\\`` — the content stream doesn't escape PDF string literals.

    Returns:
        The raw bytes of a valid PDF file.
    """
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 200 200] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode()
    objects.append(
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        b"" + stream + b"\nendstream"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n" f"{xref_offset}\n%%EOF"
    ).encode()
    return bytes(out)
