"""Read the text out of an uploaded resume (PDF, Word or plain text)."""

from __future__ import annotations

import io

MAX_BYTES = 10 * 1024 * 1024


class ResumeError(Exception):
    """The file could not be used; the message is shown to the user."""


def extract_text(filename: str, data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ResumeError("That file is larger than 10 MB.")
    name = filename.lower()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif name.endswith(".docx"):
            from docx import Document

            document = Document(io.BytesIO(data))
            parts = [paragraph.text for paragraph in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append("  ".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
        elif name.endswith((".txt", ".md")):
            text = data.decode("utf-8", errors="replace")
        else:
            raise ResumeError("Please upload a PDF, a Word .docx file or a .txt file.")
    except ResumeError:
        raise
    except Exception as error:
        raise ResumeError(f"That file could not be read ({error}).") from error
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if len(text) < 200:
        raise ResumeError(
            "Almost no text could be read from that file. If it is a scanned PDF, "
            "upload a version exported from Word or Google Docs instead."
        )
    return text
