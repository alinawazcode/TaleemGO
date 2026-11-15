"""
doc_text.py - Document text extraction for TaleemGo Study Helper.
Supports .txt, .pdf, .pptx, and .docx formats with size caps and hard timeouts.
Lazy imports for pypdf, python-pptx, and python-docx.
"""
import os
import threading
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExtractionResult:
    text: str = ""
    pages_used: int = 0
    total_pages: int = 0
    was_truncated: bool = False
    error_code: Optional[str] = None  # None, "too_large", "unsupported_type", "scanned", "encrypted", "timeout", "empty", "corrupt"


def _extract_txt(file_path: str) -> tuple[str, int, int]:
    """Read a .txt file trying utf-8, utf-8-sig, then cp1252."""
    with open(file_path, "rb") as f:
        raw = f.read()

    decoded = None
    for enc in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            decoded = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        decoded = raw.decode("utf-8", errors="replace")

    return decoded, 1, 1


def _extract_pdf(file_path: str, max_pages: int) -> tuple[Optional[str], int, int, Optional[str]]:
    """Extract text from a PDF file using pypdf. Detects scanned or encrypted PDFs."""
    try:
        import pypdf
    except ImportError:
        return None, 0, 0, "unsupported_type"

    try:
        reader = pypdf.PdfReader(file_path)
        if reader.is_encrypted:
            try:
                # Attempt empty password decryption
                if not reader.decrypt(""):
                    return None, 0, 0, "encrypted"
            except Exception:
                return None, 0, 0, "encrypted"

        total_pages = len(reader.pages)
        if total_pages == 0:
            return None, 0, 0, "empty"

        extracted_pages = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            try:
                t = page.extract_text() or ""
                if t.strip():
                    extracted_pages.append(t.strip())
            except Exception:
                continue

        if not extracted_pages:
            # Document has pages but zero extractable text (e.g. scanned image PDF)
            return None, 0, total_pages, "scanned"

        pages_used = min(total_pages, max_pages)
        full_text = "\n\n".join(extracted_pages)
        return full_text, pages_used, total_pages, None

    except Exception as e:
        err_msg = str(e).lower()
        if "encrypt" in err_msg or "password" in err_msg:
            return None, 0, 0, "encrypted"
        return None, 0, 0, "corrupt"


def _extract_pptx(file_path: str, max_pages: int) -> tuple[Optional[str], int, int, Optional[str]]:
    """Extract text from slides keeping slide order and titles using python-pptx."""
    try:
        import pptx
    except ImportError:
        return None, 0, 0, "unsupported_type"

    try:
        prs = pptx.Presentation(file_path)
        total_slides = len(prs.slides)
        if total_slides == 0:
            return None, 0, 0, "empty"

        slides_text = []
        for i, slide in enumerate(prs.slides):
            if i >= max_pages:
                break
            lines = []
            title_text = ""
            try:
                if slide.shapes.title and slide.shapes.title.text.strip():
                    title_text = slide.shapes.title.text.strip()
                    lines.append(f"Slide {i + 1}: {title_text}")
                else:
                    lines.append(f"Slide {i + 1}:")
            except Exception:
                lines.append(f"Slide {i + 1}:")

            for shape in slide.shapes:
                try:
                    if shape == slide.shapes.title:
                        continue
                except Exception:
                    pass
                if getattr(shape, "has_text_frame", False):
                    try:
                        t = shape.text_frame.text.strip()
                        if t:
                            lines.append(t)
                    except Exception:
                        pass

            content = [l for l in lines if not l.startswith(f"Slide {i + 1}:")]
            if content or title_text:
                slides_text.append("\n".join(lines))

        if not slides_text:
            return None, 0, total_slides, "empty"

        pages_used = min(total_slides, max_pages)
        full_text = "\n\n".join(slides_text)
        return full_text, pages_used, total_slides, None

    except Exception:
        return None, 0, 0, "corrupt"


def _extract_docx(file_path: str, max_pages: int) -> tuple[Optional[str], int, int, Optional[str]]:
    """Extract paragraphs and tables from a .docx file using python-docx."""
    try:
        import docx
    except ImportError:
        return None, 0, 0, "unsupported_type"

    try:
        doc = docx.Document(file_path)
        lines = []
        for p in doc.paragraphs:
            t = p.text.strip()
            if t:
                lines.append(t)
        for tbl in doc.tables:
            for row in tbl.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    lines.append(" | ".join(cells))

        if not lines:
            return None, 0, 0, "empty"

        # Approximate 25 lines per page for docx
        total_pages = max(1, (len(lines) + 24) // 25)
        pages_used = min(total_pages, max_pages)
        full_text = "\n".join(lines)
        return full_text, pages_used, total_pages, None

    except Exception:
        return None, 0, 0, "corrupt"


def extract_document_text(
    file_path: str,
    max_pages: Optional[int] = None,
    max_chars: Optional[int] = None,
    max_file_bytes: Optional[int] = None,
    timeout_sec: float = 15.0,
) -> ExtractionResult:
    """
    Extract text from .txt, .pdf, .pptx, or .docx file.
    Runs extraction inside a bounded worker thread to prevent pathological files from hanging.
    """
    if max_pages is None:
        max_pages = int(os.environ.get("STUDY_MAX_PAGES", "20"))
    if max_chars is None:
        max_chars = int(os.environ.get("STUDY_MAX_CHARS", "24000"))
    if max_file_bytes is None:
        max_file_bytes = int(os.environ.get("STUDY_MAX_FILE_BYTES", "20000000"))

    if not os.path.exists(file_path):
        return ExtractionResult(error_code="corrupt")

    try:
        size = os.path.getsize(file_path)
    except OSError:
        return ExtractionResult(error_code="corrupt")

    if size > max_file_bytes:
        return ExtractionResult(error_code="too_large")

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in (".txt", ".pdf", ".pptx", ".docx"):
        return ExtractionResult(error_code="unsupported_type")

    result_box = {}

    def _worker():
        try:
            if ext == ".txt":
                text, pages_used, total_pages = _extract_txt(file_path)
                result_box["result"] = (text, pages_used, total_pages, None)
            elif ext == ".pdf":
                result_box["result"] = _extract_pdf(file_path, max_pages)
            elif ext == ".pptx":
                result_box["result"] = _extract_pptx(file_path, max_pages)
            elif ext == ".docx":
                result_box["result"] = _extract_docx(file_path, max_pages)
        except Exception as e:
            result_box["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        return ExtractionResult(error_code="timeout")

    if "error" in result_box or "result" not in result_box:
        return ExtractionResult(error_code="corrupt")

    text, pages_used, total_pages, err = result_box["result"]
    if err:
        return ExtractionResult(pages_used=pages_used, total_pages=total_pages, error_code=err)

    if not text or not text.strip():
        return ExtractionResult(pages_used=pages_used, total_pages=total_pages, error_code="empty")

    was_truncated = False
    if total_pages > max_pages:
        was_truncated = True

    if len(text) > max_chars:
        text = text[:max_chars]
        was_truncated = True

    return ExtractionResult(
        text=text.strip(),
        pages_used=pages_used,
        total_pages=total_pages,
        was_truncated=was_truncated,
        error_code=None,
    )
