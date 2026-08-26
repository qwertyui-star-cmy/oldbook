from __future__ import annotations

import csv
import concurrent.futures
import atexit
import bisect
import ctypes
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import zipfile
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from string import punctuation
from typing import Callable
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import reportlab.pdfbase.ttfonts as reportlab_ttfonts


def make_unicode_cmap(fontname: str, subset: list[int]) -> str:
    """ReportLab 5 writes non-BMP code points as odd-length PDF hex strings."""
    mappings = []
    for index, value in enumerate(subset):
        target = chr(value).encode("utf-16-be", errors="surrogatepass").hex().upper()
        mappings.append(f"<{index:02X}> <{target}>")
    cmap = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo",
        f"<< /Registry ({fontname})",
        f"/Ordering ({fontname})",
        "/Supplement 0",
        ">> def",
        f"/CMapName /{fontname} def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        f"<00> <{len(subset) - 1:02X}>",
        "endcodespacerange",
        f"{len(subset)} beginbfchar",
        *mappings,
        "endbfchar",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return "\n".join(cmap)


reportlab_ttfonts.makeToUnicodeCMap = make_unicode_cmap


ROOT = Path(__file__).resolve().parent
VENDOR_PYTHON = ROOT / ".vendor" / "python"
if VENDOR_PYTHON.exists() and str(VENDOR_PYTHON) not in sys.path:
    sys.path.insert(0, str(VENDOR_PYTHON))
try:
    import pypdfium2 as pdfium
except ImportError:
    pdfium = None
try:
    from pypinyin import lazy_pinyin
except ImportError:
    lazy_pinyin = None
JOBS_DIR = ROOT / ".cache" / "text-layer-jobs"
POPPLER = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
CN_PUNCT = "，。！？；：、“”‘’（）《》〈〉【】〔〕［］—…·　「」『』﹁﹂﹃﹄"
SKIP_CHARS = set(punctuation + CN_PUNCT)
TEXT_FONT = "HanText"
TEXT_FONT_REGISTERED = False
EXTB_TEXT_FONT = "HanTextExtB"
EXTB_TEXT_FONT_REGISTERED = False
LAYOUT_ENGINE_VERSION = "next-page-start-v18-mixed-output"
ANCHOR_CACHE_VERSION = 11
TEXT_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\STSONG.TTF"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
    Path(r"C:\Windows\Fonts\msyh.ttc"),
]
EXTB_TEXT_FONT_CANDIDATES = [
    Path(r"C:\Windows\Fonts\simsunb.ttf"),
    Path(r"C:\Windows\Fonts\SimsunExtG.ttf"),
]
OCR_ENGINE = None
OPENCC_CONVERTER = None
STATUS_LOCKS: dict[str, threading.RLock] = {}
STATUS_LOCKS_GUARD = threading.Lock()
OCR_IDLE_TIMEOUT_SECONDS = max(60, int(os.environ.get("TEXT_LAYER_OCR_IDLE_TIMEOUT", "600") or "600"))
FULL_OCR_FALLBACK_ENABLED = str(os.environ.get("TEXT_LAYER_FULL_OCR_FALLBACK") or "").strip() in {"1", "true", "yes", "on"}
STRICT_MANIFEST_ENABLED = str(os.environ.get("TEXT_LAYER_STRICT_MANIFEST") or "1").strip().lower() in {"1", "true", "yes", "on"}
FAST_MANIFEST_FUZZY_ENABLED = str(os.environ.get("TEXT_LAYER_FAST_FUZZY") or "").strip() in {"1", "true", "yes", "on"}
ALLOW_UNRESOLVED_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_UNRESOLVED_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_ESTIMATED_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_ESTIMATED_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_OCR_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_OCR_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
STRICT_BOUNDARY_BLOCK_STATUSES = {"页界未唯一锁定", "页边文字未精确对应"}
SOURCE_UNITS_CACHE: dict[str, tuple[float, int, list["SourceUnit"]]] = {}
SOURCE_UNITS_CACHE_LOCK = threading.Lock()
NORMALIZED_SOURCE_CACHE: dict[int, tuple[str, str, list[int]]] = {}
NORMALIZED_SOURCE_CACHE_LOCK = threading.Lock()
SOURCE_SEARCH_CORPUS_CACHE: dict[str, str] = {}
PDFIUM_DOCUMENTS: dict[tuple[str, int, int], object] = {}
PIPELINE_STAGES = (
    ("input", "来源与任务检查"),
    ("ocr", "页边 OCR 缓存"),
    ("align", "权威正文逐页锁定"),
    ("classify", "非权威页整页 OCR"),
    ("layer", "文字层逐页写入"),
    ("assemble", "整本 PDF 合并"),
    ("text-check", "连续搜索与文字校验"),
    ("visual-check", "扫描画面校验"),
)


class TaskPaused(Exception):
    pass
COMMON_GLYPH_VARIANTS = {
    "徳": "德",
    "髙": "高",
    "荅": "答",
    "賔": "賓",
    "槩": "概",
    "塟": "葬",
    "逺": "遠",
    "歛": "斂",
    "寖": "浸",
    "甿": "氓",
    "輙": "輒",
    "𭣣": "收",
    "䃅": "氐",
    "磾": "氐",
    "㻅": "会",
}


class PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self.parts.append(text)


class LinkHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []
        self.labeled_links: list[tuple[str, str]] = []
        self._active_href = ""
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            absolute = urljoin(self.base_url, href)
            self.links.append(absolute)
            self._active_href = absolute
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href and data.strip():
            self._active_text.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active_href:
            label = re.sub(r"\s+", " ", " ".join(self._active_text)).strip()
            self.labeled_links.append((self._active_href, label))
            self._active_href = ""
            self._active_text = []


@dataclass
class JobPaths:
    root: Path
    pdf: Path
    source: Path
    meta: Path


@dataclass
class SourceUnit:
    title: str
    url: str
    text: str
    order: int = 0
    kind: str = "web"

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "text": self.text,
            "order": self.order,
            "kind": self.kind,
        }


@dataclass
class SourceWindow:
    title: str
    url: str
    text: str
    order: int
    kind: str
    start_order: int
    end_order: int
    start_url: str
    end_url: str
    start_title: str
    end_title: str
    global_norm_start: int
    global_raw_start: int
    boundary: int | None = None


def source_alignment_windows(units: list[SourceUnit]) -> list[SourceWindow]:
    """Expose source units in one monotonic coordinate space, including adjacent-unit pages."""
    windows = []
    norm_cursor = 0
    raw_cursor = 0
    positions = []
    for index, unit in enumerate(units):
        unit_norm, _ = normalize_source_cached(unit.text)
        key = unit.url or unit.title
        positions.append((norm_cursor, raw_cursor, len(unit_norm)))
        windows.append(SourceWindow(
            unit.title, key, unit.text, index, unit.kind,
            index, index, key, key, unit.title, unit.title,
            norm_cursor, raw_cursor,
        ))
        norm_cursor += len(unit_norm)
        raw_cursor += len(unit.text)
    for index, (left, right) in enumerate(zip(units, units[1:])):
        norm_start, raw_start, left_norm_length = positions[index]
        left_key = left.url or left.title
        right_key = right.url or right.title
        windows.append(SourceWindow(
            f"{left.title} / {right.title}",
            f"cross-unit:{index}:{left_key}|{right_key}",
            left.text + right.text,
            index,
            left.kind if left.kind == right.kind else "mixed-authority",
            index, index + 1, left_key, right_key, left.title, right.title,
            norm_start, raw_start, left_norm_length,
        ))
    return windows


def safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._")
    return cleaned or fallback


def make_job_id(seed: str) -> str:
    raw = f"{seed}|{time.time_ns()}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def job_id_from_root(root: Path) -> str:
    meta_path = root / "job.json"
    if meta_path.exists():
        try:
            value = str(json.loads(meta_path.read_text(encoding="utf-8")).get("id") or "").lower()
            if re.fullmatch(r"[0-9a-f]{16}", value):
                return value
        except (OSError, json.JSONDecodeError):
            pass
    match = re.search(r"(?:^|-)([0-9a-f]{16})$", root.name.lower())
    return match.group(1) if match else ""


def book_package_slug(filename: str) -> str:
    stem = Path(str(filename or "book")).stem
    stem = re.split(r"[_\-—（(【\[]", stem, maxsplit=1)[0].strip() or stem
    if lazy_pinyin:
        value = "".join(lazy_pinyin(stem, errors=lambda chars: list(chars)))
    else:
        value = stem
    value = unicodedata.normalize("NFKD", value).encode("ascii", errors="ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value or "book")[:48].rstrip("-")


def new_job_paths(job_id: str, pdf_original: str) -> JobPaths:
    root = JOBS_DIR / f"{book_package_slug(pdf_original)}-{job_id}"
    return JobPaths(root=root, pdf=root / "source.pdf", source=root / "source.txt", meta=root / "job.json")


def migrate_legacy_job_packages() -> int:
    if not JOBS_DIR.exists():
        return 0
    migrated = 0
    for root in JOBS_DIR.iterdir():
        if not root.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", root.name.lower()):
            continue
        meta_path = root / "job.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        job_id = str(meta.get("id") or root.name).lower()
        target = new_job_paths(job_id, str(meta.get("pdfOriginal") or "book.pdf")).root
        if target.exists():
            continue
        old_root = str(root.resolve())
        root.rename(target)
        for key in ("pdf", "sourceText", "sourceArchive"):
            value = str(meta.get(key) or "")
            if value.startswith(old_root):
                meta[key] = str(target.resolve()) + value[len(old_root):]
        atomic_write_json(target / "job.json", meta)
        status_path = target / "full-status.json"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                for output in status.get("outputs") or []:
                    value = str(output.get("path") or "")
                    if value.startswith(old_root):
                        output["path"] = str(target.resolve()) + value[len(old_root):]
                atomic_write_json(status_path, status)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        migrated += 1
    return migrated


def job_paths(job_id: str) -> JobPaths:
    job_id = str(job_id).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise ValueError("任务编号无效。")
    root = JOBS_DIR / job_id
    if not root.exists() and JOBS_DIR.exists():
        matches = [candidate for candidate in JOBS_DIR.glob(f"*-{job_id}") if candidate.is_dir()]
        if len(matches) == 1:
            root = matches[0]
    return JobPaths(root=root, pdf=root / "source.pdf", source=root / "source.txt", meta=root / "job.json")


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, value.encode(encoding))


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def status_lock(job_id: str) -> threading.RLock:
    with STATUS_LOCKS_GUARD:
        return STATUS_LOCKS.setdefault(job_id, threading.RLock())


def write_pdf_atomic(writer: PdfWriter, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("wb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        PdfReader(str(temporary), strict=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def assemble_page_pdfs(page_files: list[Path], path: Path) -> str:
    """Assemble pages atomically and linearize with QPDF when available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    backend = "pypdf"
    try:
        try:
            import pikepdf

            with pikepdf.Pdf.new() as destination:
                for page_file in page_files:
                    with pikepdf.Pdf.open(page_file) as source:
                        destination.pages.extend(source.pages)
                destination.save(temporary, linearize=True)
            with pikepdf.Pdf.open(temporary) as checked:
                if len(checked.pages) != len(page_files):
                    raise ValueError("QPDF 合并后的页数异常。")
            backend = "qpdf-linearized"
        except ImportError:
            writer = PdfWriter()
            for page_file in page_files:
                page_reader = PdfReader(str(page_file), strict=False)
                writer.add_page(page_reader.pages[0])
            with temporary.open("wb") as handle:
                writer.write(handle)
                handle.flush()
                os.fsync(handle.fileno())
        PdfReader(str(temporary), strict=False)
        os.replace(temporary, path)
        return backend
    finally:
        temporary.unlink(missing_ok=True)


def source_units_path(job_id: str) -> Path:
    return job_paths(job_id).root / "source-units.json"


def load_source_units(job: dict) -> list[SourceUnit]:
    job_id = str(job.get("id") or "").strip()
    path = source_units_path(job_id) if job_id else None
    if path and path.exists():
        try:
            stat = path.stat()
            cache_key = str(path.resolve())
            with SOURCE_UNITS_CACHE_LOCK:
                cached = SOURCE_UNITS_CACHE.get(cache_key)
                if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                    return cached[2]
            payload = json.loads(path.read_text(encoding="utf-8"))
            units = []
            for item in payload.get("units", payload if isinstance(payload, list) else []):
                text = clean_reference_text(str(item.get("text", "")))
                if text:
                    units.append(SourceUnit(
                        title=str(item.get("title") or "参考正文"),
                        url=str(item.get("url") or ""),
                        text=text,
                        order=int(item.get("order") or len(units)),
                        kind=str(item.get("kind") or "web"),
                    ))
            if units:
                with SOURCE_UNITS_CACHE_LOCK:
                    SOURCE_UNITS_CACHE[cache_key] = (stat.st_mtime, stat.st_size, units)
                return units
        except Exception:
            pass
    source_value = str(job.get("sourceText", "")).strip()
    source_path = Path(source_value) if source_value else None
    if source_path and source_path.exists():
        text = clean_reference_text(source_path.read_text(encoding="utf-8", errors="ignore"))
        if text:
            return [SourceUnit(
                title=source_title_from_url(str(job.get("sourceOriginal") or "")) or "参考正文",
                url=str(job.get("sourceOriginal") or ""),
                text=text,
            )]
    return []


def save_source_units(job: dict, units: list[SourceUnit]) -> None:
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return
    unique = []
    seen = set()
    for unit in units:
        text = clean_reference_text(unit.text)
        normalized, _ = normalize_for_match(text)
        fingerprint = hashlib.sha1(normalized[:2000].encode("utf-8", errors="ignore")).hexdigest()
        if not normalized or fingerprint in seen:
            continue
        seen.add(fingerprint)
        unit.text = text
        unit.order = len(unique)
        unique.append(unit)
    path = source_units_path(job_id)
    atomic_write_json(path, {"units": [unit.as_dict() for unit in unique]})
    combined = "\n\n".join(unit.text for unit in unique if unit.text.strip())
    atomic_write_text(job_paths(job_id).source, combined)
    job["sourceText"] = str(job_paths(job_id).source)
    job["sourceUnitCount"] = len(unique)
    atomic_write_json(job_paths(job_id).meta, job)


def write_upload(field, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    field.file.seek(0)
    with path.open("wb") as handle:
        shutil.copyfileobj(field.file, handle)


def upload_sha256(field) -> str:
    digest = hashlib.sha256()
    field.file.seek(0)
    while chunk := field.file.read(1024 * 1024):
        digest.update(chunk)
    field.file.seek(0)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def job_input_fingerprint(pdf_digest: str, source_identity: str, layout: str) -> str:
    payload = f"pdf:{pdf_digest}\nsource:{source_identity}\nlayout:{layout.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def reusable_job_id(
    fingerprint: str,
    pdf_original: str,
    source_original: str,
    pdf_digest: str,
    source_identity: str,
    requested_layout: str,
) -> str:
    if not JOBS_DIR.exists():
        return ""
    for meta_path in sorted(JOBS_DIR.glob("*/job.json"), key=lambda path: path.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(meta.get("inputFingerprint") or "") == fingerprint:
            return str(meta.get("id") or job_id_from_root(meta_path.parent))
        if meta.get("pdfOriginal") != pdf_original or str(meta.get("sourceOriginal") or "") != source_original:
            continue
        candidate_layout = str(meta.get("requestedLayout") or meta.get("layout") or "auto")
        if requested_layout != "auto" and candidate_layout != requested_layout:
            continue
        candidate_job_id = str(meta.get("id") or job_id_from_root(meta_path.parent))
        if not candidate_job_id:
            continue
        paths = job_paths(candidate_job_id)
        candidate_pdf_digest = str(meta.get("pdfSha256") or "")
        if not candidate_pdf_digest and paths.pdf.exists():
            candidate_pdf_digest = file_sha256(paths.pdf)
        candidate_source_identity = str(meta.get("sourceIdentity") or "")
        if not candidate_source_identity:
            if source_original.startswith(("http://", "https://")):
                candidate_source_identity = f"url:{source_original.strip()}"
            else:
                archive_value = str(meta.get("sourceArchive") or "")
                archive = Path(archive_value) if archive_value else None
                if archive and archive.exists():
                    candidate_source_identity = f"file:{file_sha256(archive)}"
                elif not source_original:
                    candidate_source_identity = "none"
        if candidate_pdf_digest == pdf_digest and candidate_source_identity == source_identity:
            meta.update({
                "inputFingerprint": fingerprint,
                "pdfSha256": pdf_digest,
                "sourceIdentity": source_identity,
                "requestedLayout": requested_layout,
            })
            atomic_write_json(meta_path, meta)
            return candidate_job_id
    return ""


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", value)
    parser = PlainTextHTMLParser()
    parser.feed(value)
    return clean_reference_text("\n".join(parser.parts))


def convert_cjk_codepoints(text: str) -> str:
    def replace(match: re.Match) -> str:
        value = int(match.group(1), 16)
        if 0x3400 <= value <= 0x9FFF or 0x20000 <= value <= 0x3FFFF:
            try:
                return chr(value)
            except ValueError:
                return match.group(0)
        return match.group(0)
    return re.sub(r"\bU\+([0-9A-Fa-f]{4,6})\b", replace, text)


def clean_reference_text(text: str) -> str:
    text = re.sub(r"(?is)@font-face\s*\{.*?\}", " ", text)
    text = re.sub(r"(?is)unicode-range\s*:[^;]+;", " ", text)
    text = re.sub(r"(?im)^\s*(font-family|font-style|font-weight|src)\s*:.*$", " ", text)
    text = convert_cjk_codepoints(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_ocr_text(text: str) -> str:
    text = clean_reference_text(text)
    text = re.sub(r"因\s*巽\s*錄", "因話錄", text)
    text = re.sub(r"因\s*巽\s*录", "因话录", text)
    replacements = {
        "因巽錄": "因話錄",
        "因巽录": "因话录",
    }
    for wrong, correct in replacements.items():
        text = text.replace(wrong, correct)
    return text


def source_text_looks_bad(text: str) -> bool:
    head = text[:3000]
    markers = ("localStorage", "addEventListener", "querySelector", "function(", "=>", "unicode-range", "@font-face")
    return sum(1 for marker in markers if marker in head) >= 2


def read_job_source_text(job: dict) -> str:
    source_value = str(job.get("sourceText", "")).strip()
    if not source_value:
        return ""
    source_path = Path(source_value)
    text = ""
    if source_path.exists():
        text = clean_reference_text(source_path.read_text(encoding="utf-8", errors="ignore"))
    source_original = str(job.get("sourceOriginal", "")).strip()
    if source_original.startswith(("http://", "https://")) and (not text or source_text_looks_bad(text)):
        try:
            text = fetch_source_url(source_original)
            source_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(source_path, text)
        except Exception:
            pass
    return text


def natural_archive_key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name))


def epub_spine_units(path: Path) -> list[SourceUnit]:
    """Read EPUB content in OPF spine order, with a natural-order fallback."""
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        ordered: list[tuple[str, str]] = []
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            opf_name = str(rootfile.attrib.get("full-path") or "") if rootfile is not None else ""
            package = ET.fromstring(archive.read(opf_name))
            opf_dir = posixpath.dirname(opf_name)
            manifest_items = {
                str(item.attrib.get("id") or ""): item
                for item in package.findall(".//{*}manifest/{*}item")
            }
            manifest = {
                item_id: str(item.attrib.get("href") or "")
                for item_id, item in manifest_items.items()
            }
            guide_references = package.findall(".//{*}guide/{*}reference")
            guide_titles = {
                posixpath.normpath(posixpath.join(opf_dir, str(item.attrib.get("href") or ""))):
                str(item.attrib.get("title") or "")
                for item in guide_references
            }
            for itemref in package.findall(".//{*}spine/{*}itemref"):
                href = manifest.get(str(itemref.attrib.get("idref") or ""), "")
                archive_name = posixpath.normpath(posixpath.join(opf_dir, href))
                if archive_name in names and archive_name.lower().endswith((".html", ".xhtml", ".htm")):
                    ordered.append((archive_name, guide_titles.get(archive_name, "")))
            ordered_names = {name for name, _ in ordered}
            navigation = []
            for item in manifest_items.values():
                properties = str(item.attrib.get("properties") or "").split()
                if "nav" not in properties:
                    continue
                archive_name = posixpath.normpath(posixpath.join(opf_dir, str(item.attrib.get("href") or "")))
                if archive_name in names and archive_name not in ordered_names and archive_name.lower().endswith((".html", ".xhtml", ".htm")):
                    navigation.append((archive_name, guide_titles.get(archive_name, "目录")))
                    ordered_names.add(archive_name)
            for reference in guide_references:
                if str(reference.attrib.get("type") or "").lower() != "toc":
                    continue
                archive_name = posixpath.normpath(posixpath.join(opf_dir, str(reference.attrib.get("href") or "")))
                if archive_name in names and archive_name not in ordered_names and archive_name.lower().endswith((".html", ".xhtml", ".htm")):
                    navigation.append((archive_name, str(reference.attrib.get("title") or "目录")))
                    ordered_names.add(archive_name)
            if navigation:
                ordered = navigation + ordered
        except Exception:
            ordered = []

        if not ordered:
            ordered = [
                (name, "")
                for name in sorted(names, key=natural_archive_key)
                if name.lower().endswith((".html", ".xhtml", ".htm"))
            ]

        units = []
        for archive_name, guide_title in ordered:
            body = archive.read(archive_name).decode("utf-8", errors="ignore")
            text = html_to_text(body)
            if not text:
                continue
            title = guide_title or html_title(body) or Path(archive_name).stem
            units.append(SourceUnit(
                title=title,
                url=f"{path.name}#{archive_name}",
                text=text,
                order=len(units),
                kind="epub",
            ))
        return units


def epub_to_text(path: Path) -> str:
    return "\n\n".join(unit.text for unit in epub_spine_units(path) if unit.text.strip())


def source_file_to_units(path: Path) -> list[SourceUnit]:
    if path.name.lower().endswith(".epub"):
        return epub_spine_units(path)
    text = source_file_to_text(path)
    if not text:
        return []
    return [SourceUnit(Path(path.name).stem or "本地参考文本", path.name, text, kind="file")]


def source_file_to_text(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".epub"):
        return epub_to_text(path)
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="ignore")
    if lower.endswith((".html", ".xhtml", ".htm")):
        return html_to_text(text)
    return clean_reference_text(text)


def get_ocr_engine():
    global OCR_ENGINE
    if OCR_ENGINE is not None:
        return OCR_ENGINE
    try:
        from rapidocr import RapidOCR
    except Exception:
        return None
    OCR_ENGINE = RapidOCR()
    return OCR_ENGINE


def normalize_cjk_variant(char: str) -> str:
    global OPENCC_CONVERTER
    char = COMMON_GLYPH_VARIANTS.get(char, char)
    if not ("\u3400" <= char <= "\u9fff" or "\U00020000" <= char <= "\U0003ffff"):
        return char.lower() if char.isascii() else char
    if OPENCC_CONVERTER is None:
        try:
            from opencc import OpenCC
            OPENCC_CONVERTER = OpenCC("t2s")
        except Exception:
            OPENCC_CONVERTER = False
    if OPENCC_CONVERTER:
        converted = OPENCC_CONVERTER.convert(char)
        if len(converted) == 1:
            return converted
    return char


def request_bytes(url: str, limit: int = 8 * 1024 * 1024) -> tuple[bytes, str, str]:
    request = Request(url, headers={"User-Agent": "TextLocator/0.2 (+local text alignment)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                content_type = response.headers.get_content_type() or ""
                return response.read(limit), charset, content_type
        except HTTPError as exc:
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
            delay = min(8, max(2, int(exc.headers.get("Retry-After") or 2)))
            time.sleep(delay)
        except (URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("网页来源暂时无法读取。")


def source_title_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    name = Path(parsed.path.rstrip("/")).name
    return name if name and not name.isdigit() else parsed.netloc


def html_title(html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if not match:
        return ""
    return html_to_text(match.group(1)).split(" - ", 1)[0].strip()


def fetch_wuguo_units(base_url: str, html: str) -> list[SourceUnit]:
    parsed = urlparse(base_url)
    match = re.search(r"/(?:read|novel)/(\d+)(?:/(?:chapter/)?(\d+))?", parsed.path)
    if not match:
        return []
    book_id = match.group(1)
    chapter_id = match.group(2)
    if not chapter_id:
        link_parser = LinkHTMLParser(base_url)
        link_parser.feed(html)
        for link in link_parser.links:
            found = re.search(rf"/(?:read/{book_id}|novel/{book_id}/chapter)/(\d+)", urlparse(link).path)
            if found:
                chapter_id = found.group(1)
                break
    if not chapter_id:
        return []

    stream_url = urljoin(base_url, f"/read/{book_id}/{chapter_id}/stream/chapter?renderer=modern")
    units = []
    seen_chapters = set()
    for _ in range(2000):
        try:
            raw, _, _ = request_bytes(stream_url)
            payload = json.loads(raw.decode("utf-8", errors="ignore"))
            data = payload.get("data") or {}
            current_id = str(data.get("chapter_id") or "")
            if not current_id or current_id in seen_chapters:
                break
            seen_chapters.add(current_id)
            chapter_title = str(data.get("chapter_title") or f"第 {len(units) + 1} 章")
            chapter_url = urljoin(base_url, str(data.get("chapter_url") or f"/read/{book_id}/{current_id}"))
            text = html_to_text(str(data.get("content_html") or ""))
            if text:
                units.append(SourceUnit(chapter_title, chapter_url, text, len(units), "wuguo"))
            next_chapter = data.get("next_chapter") or {}
            next_stream = str(next_chapter.get("stream_url") or "")
            if data.get("is_last_chapter") or not next_stream:
                break
            stream_url = urljoin(base_url, next_stream)
        except Exception:
            break
    return units


def fetch_source_bundle(url: str) -> list[SourceUnit]:
    raw, charset, content_type = request_bytes(url)
    if "json" in content_type:
        payload = json.loads(raw.decode(charset, errors="ignore"))
        text = clean_reference_text(json.dumps(payload, ensure_ascii=False))
        return [SourceUnit(source_title_from_url(url) or "网页正文", url, text)] if text else []
    body = raw.decode(charset, errors="ignore")
    if "<html" not in body[:2000].lower():
        text = clean_reference_text(body)
        return [SourceUnit(source_title_from_url(url) or "网页正文", url, text)] if text else []
    if urlparse(url).netloc.endswith("wuguo.net"):
        units = fetch_wuguo_units(url, body)
        if units:
            return units
    units = fetch_chapter_units(url, body)
    if units:
        return units
    text = html_to_text(body)
    return [SourceUnit(html_title(body) or source_title_from_url(url) or "网页正文", url, text)] if text else []


def fetch_source_url(url: str) -> str:
    return "\n\n".join(unit.text for unit in fetch_source_bundle(url) if unit.text.strip())


def fetch_chapter_units(base_url: str, html: str) -> list[SourceUnit]:
    parser = LinkHTMLParser(base_url)
    parser.feed(html)
    base_host = urlparse(base_url).netloc
    chapter_links = []
    generic_links = []
    seen = set()
    labels = {link: label for link, label in parser.labeled_links}
    for link in parser.links:
        parsed = urlparse(link)
        if parsed.netloc != base_host:
            continue
        legacy_pattern = bool(re.search(r"/(?:novel/\d+/chapter|read/\d+)/\d+", parsed.path))
        label = labels.get(link, "")
        chapter_label = bool(re.search(r"(?:第[0-9一二三四五六七八九十百千]+[卷章回篇]|卷[上下中]|目录|目錄|下一[卷章回篇])", label))
        chapter_path = bool(
            re.search(r"(?:chapter|chap|juan|volume|read|text)[/_-]?\d+", parsed.path, re.I)
            or re.search(r"(?:chapter|chap|juan|volume)(?:_?id)?=\d+", parsed.query, re.I)
        )
        if not legacy_pattern and not (chapter_label or chapter_path):
            continue
        clean_link = parsed._replace(query="mode=text" if "/read/" in parsed.path else "", fragment="").geturl()
        if clean_link not in seen:
            seen.add(clean_link)
            (chapter_links if legacy_pattern else generic_links).append(clean_link)
    if len(generic_links) >= 2:
        chapter_links.extend(generic_links)
    if not chapter_links:
        return []

    def sort_key(link: str) -> tuple[int, int, str]:
        numbers = [int(value) for value in re.findall(r"\d+", urlparse(link).path)]
        if len(numbers) >= 2:
            return numbers[-2], numbers[-1], link
        return 0, numbers[-1] if numbers else 0, link

    units = []
    for link in sorted(chapter_links, key=sort_key)[:2000]:
        try:
            data, charset, _ = request_bytes(link)
            chapter_html = data.decode(charset, errors="ignore")
            text = html_to_text(chapter_html)
            if text:
                units.append(SourceUnit(html_title(chapter_html) or f"第 {len(units) + 1} 章", link, text, len(units), "chapter"))
        except Exception:
            continue
    return units


def fetch_chapter_texts(base_url: str, html: str) -> str:
    return "\n\n".join(unit.text for unit in fetch_chapter_units(base_url, html) if unit.text.strip())


def assess_source_units(units: list[SourceUnit]) -> dict:
    texts = [unit.text.strip() for unit in units if unit.text.strip()]
    total_chars = sum(len(text) for text in texts)
    fingerprints = [hashlib.sha1(normalize_for_match(text)[0].encode("utf-8")).hexdigest() for text in texts]
    duplicate_units = max(0, len(fingerprints) - len(set(fingerprints)))
    suspicious_units = sum(1 for text in texts if source_text_looks_bad(text))
    warnings = []
    if not texts:
        warnings.append("没有提取到可用正文")
    elif total_chars < 1000:
        warnings.append("提取文字过短，可能只是目录或说明页")
    if len(units) == 1 and units[0].kind == "web":
        warnings.append("只读取到当前网页，未发现可确认的章节链接")
    if duplicate_units:
        warnings.append(f"发现 {duplicate_units} 个重复内容单元")
    if suspicious_units:
        warnings.append(f"发现 {suspicious_units} 个疑似脚本或样式污染单元")
    return {
        "unitCount": len(units),
        "totalChars": total_chars,
        "duplicateUnits": duplicate_units,
        "suspiciousUnits": suspicious_units,
        "warnings": warnings,
        "usable": bool(texts) and total_chars >= 200 and not suspicious_units,
    }


WIKISOURCE_API = "https://zh.wikisource.org/w/api.php"


def pdf_title_candidates(job: dict) -> list[str]:
    name = Path(str(job.get("pdfOriginal") or "")).stem
    name = re.sub(r"[（(\[].*?[）)\]]", " ", name)
    parts = re.split(r"[_\-—·\s]+", name)
    candidates = []
    for part in parts:
        for value in re.findall(r"[\u3400-\u9fff]{2,14}", part):
            value = re.sub(r"(?:全本|影印|校注|译注|譯注|整理|中华书局|中華書局)$", "", value)
            if len(value) < 2 or re.search(r"(?:撰|著|译|譯|注|校)$", value):
                continue
            if value not in candidates:
                candidates.append(value)
    return candidates[:8]


def mediawiki_json(params: dict) -> dict:
    query = {"format": "json", "formatversion": "2", "origin": "*", **params}
    raw, _, _ = request_bytes(f"{WIKISOURCE_API}?{urlencode(query)}", limit=12 * 1024 * 1024)
    return json.loads(raw.decode("utf-8", errors="ignore"))


def search_wikisource_titles(query: str) -> list[str]:
    try:
        payload = mediawiki_json({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": "8",
        })
    except Exception:
        return []
    return [str(item.get("title") or "") for item in payload.get("query", {}).get("search", []) if item.get("title")]


def fetch_wikisource_unit(title: str) -> SourceUnit | None:
    try:
        payload = mediawiki_json({"action": "parse", "page": title, "prop": "text|displaytitle"})
        parsed = payload.get("parse") or {}
        text = html_to_text(str(parsed.get("text") or ""))
        if "/" in title:
            text = re.sub(r"^.*?(?:下一卷▶|全書終|全书终)\s*", "", text, count=1, flags=re.S)
            text = re.split(r"(?:此作品在全世界|Public domain)", text, maxsplit=1)[0].strip()
        if len(normalize_for_match(text)[0]) < 200:
            return None
        actual_title = html_to_text(str(parsed.get("displaytitle") or parsed.get("title") or title)) or title
        page_url = f"https://zh.wikisource.org/wiki/{quote(str(parsed.get('title') or title).replace(' ', '_'))}"
        return SourceUnit(actual_title, page_url, text, kind="wikisource")
    except Exception:
        return None


def natural_volume_key(title: str) -> tuple[int, str]:
    chinese_digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    tail = title.rsplit("/", 1)[-1]
    number = re.search(r"\d+", tail)
    if number:
        return int(number.group()), title
    chinese = re.search(r"卷([一二三四五六七八九十]+)", tail)
    if chinese:
        value = chinese.group(1)
        if value == "十":
            return 10, title
        if value.startswith("十"):
            return 10 + chinese_digits.get(value[-1], 0), title
        if value.endswith("十"):
            return chinese_digits.get(value[0], 0) * 10, title
        return chinese_digits.get(value, 999), title
    return 999, title


def fetch_wikisource_book_units(base_title: str) -> list[SourceUnit]:
    """Fetch a work and all of its structured subpages, not just the landing page."""
    titles = []
    try:
        continuation = ""
        for _ in range(20):
            params = {
                "action": "query",
                "list": "allpages",
                "apprefix": f"{base_title}/",
                "apnamespace": "0",
                "aplimit": "max",
            }
            if continuation:
                params["apcontinue"] = continuation
            payload = mediawiki_json(params)
            titles.extend(str(item.get("title") or "") for item in payload.get("query", {}).get("allpages", []))
            continuation = str((payload.get("continue") or {}).get("apcontinue") or "")
            if not continuation:
                break
    except Exception:
        titles = []
    try:
        payload = mediawiki_json({"action": "parse", "page": base_title, "prop": "links"})
        links = (payload.get("parse") or {}).get("links") or []
        titles.extend(
            str(item.get("title") or "")
            for item in links
            if str(item.get("title") or "").startswith(f"{base_title}/")
        )
    except Exception:
        pass
    titles = sorted({title for title in titles if title}, key=natural_volume_key)
    if not titles:
        titles = [base_title]
    units = []
    for order, title in enumerate(titles):
        unit = fetch_wikisource_unit(title)
        if unit:
            unit.order = order
            units.append(unit)
    return units


def source_authority_rank(kind: str) -> int:
    return {
        "wikisource": 50,
        "epub": 45,
        "file": 45,
        "text": 45,
        "local": 45,
        "chapter": 25,
        "wuguo": 20,
        "web": 10,
    }.get(kind, 15)


def source_preference_rank(job: dict, unit: SourceUnit) -> int:
    rank = source_authority_rank(unit.kind)
    title_norm, _ = normalize_for_match(unit.title.split("/", 1)[0])
    pdf_norm, _ = normalize_for_match(Path(str(job.get("pdfOriginal") or "")).stem)
    if title_norm and title_norm in pdf_norm:
        rank += 3
    if re.search(r"/全[覽览]$", unit.title):
        rank -= 6
    qualifier = re.search(r"[（(]([^）)]+)[）)]", unit.title)
    if qualifier:
        qualifier_norm, _ = normalize_for_match(qualifier.group(1))
        if qualifier_norm and qualifier_norm not in pdf_norm:
            rank -= 5
    return rank


def exact_anchor_evidence(source_norm: str, anchor_text: str, side: str = "best") -> tuple[int | None, int, str]:
    anchor_norm, _ = normalize_for_match(anchor_text)
    if len(anchor_norm) < 6:
        return None, 0, ""
    best = (None, 0, "")
    unique_candidates = []
    for size in (16, 14, 12, 10, 8, 6):
        if len(anchor_norm) < size:
            continue
        for offset in range(0, len(anchor_norm) - size + 1):
            needle = anchor_norm[offset:offset + size]
            first = source_norm.find(needle)
            if first < 0:
                continue
            second = source_norm.find(needle, first + 1)
            uniqueness = 14 if second < 0 else 0
            score = size * 5 + uniqueness
            if second < 0 and size >= 6:
                unique_candidates.append((first, score, needle))
            if score > best[1]:
                best = (first, score, needle)
        if side == "best" and best[0] is not None and best[1] >= size * 5 + 14:
            break
    if unique_candidates and side == "start":
        return min(unique_candidates, key=lambda item: (item[0], -len(item[2])))
    if unique_candidates and side == "end":
        return max(unique_candidates, key=lambda item: (item[0] + len(item[2]), len(item[2])))
    return best


def anchor_present_in_source_corpus(source_corpus: str, anchor_text: str, side: str) -> bool:
    if not source_corpus:
        return False
    lines = [normalize_for_match(line)[0] for line in re.split(r"[\r\n]+", anchor_text)]
    lines = [line for line in lines if len(line) >= 6]
    if not lines:
        normalized, _ = normalize_for_match(anchor_text)
        lines = [normalized] if len(normalized) >= 6 else []
    for line in lines:
        for size in (16, 12, 10, 8, 6):
            if len(line) < size:
                continue
            needle = line[-size:] if side == "end" else line[:size]
            if needle in source_corpus:
                return True
    return False


def line_anchor_evidence(source_norm: str, anchor_text: str, side: str) -> tuple[int | None, int, str]:
    candidates = []
    for line in re.split(r"[\r\n]+", anchor_text):
        line_norm, _ = normalize_for_match(line)
        if len(line_norm) < 6:
            continue
        pos, score, needle = exact_anchor_evidence(source_norm, line, side)
        if pos is not None and score >= 44:
            candidates.append((pos, score, needle, len(line_norm)))
    if not candidates:
        return exact_anchor_evidence(source_norm, anchor_text, side)
    long_lines = [item for item in candidates if item[3] >= 10]
    usable = (long_lines or candidates) if side == "start" else candidates
    if side == "start":
        pos, score, needle, _ = min(usable, key=lambda item: (item[0], -item[1]))
    else:
        pos, score, needle, _ = max(usable, key=lambda item: (item[0] + len(item[2]), item[1]))
    return pos, score, needle


def strict_pair_in_text(source_text: str, start_text: str, end_text: str) -> dict | None:
    source_norm, mapping = normalize_source_cached(source_text)
    if not source_norm or not mapping:
        return None
    start_pos, start_score, start_needle = line_anchor_evidence(source_norm, start_text, "start")
    end_pos, end_score, end_needle = line_anchor_evidence(source_norm, end_text, "end")
    if start_pos is None:
        start_needle = anchor_from_text(start_text, "start", size=24)
        start_pos, start_score = find_anchor_fuzzy(source_norm, start_needle, 0, len(source_norm), "start")
    if end_pos is None:
        end_needle = anchor_from_text(end_text, "end", size=24)
        end_boundary, end_score = find_anchor_fuzzy(source_norm, end_needle, 0, len(source_norm), "end")
        end_pos = (end_boundary - len(end_needle)) if end_boundary is not None else None
    if start_pos is None or end_pos is None:
        return None
    if start_score < 44 or end_score < 44:
        return None
    end_boundary = end_pos + len(end_needle)
    if end_boundary <= start_pos or end_boundary - start_pos > 1200:
        return None
    refined_start = refine_boundary_from_recognized_edge(
        source_text, mapping, start_pos, start_text, "start"
    )
    refined_end = refine_boundary_from_recognized_edge(
        source_text, mapping, end_boundary, end_text, "end"
    )
    if refined_start is None or refined_end is None:
        return None
    start_pos, raw_start = refined_start
    end_boundary, raw_end = refined_end
    if end_boundary <= start_pos or end_boundary - start_pos > 1200 or raw_end <= raw_start:
        return None
    confidence = min(99, 75 + min(len(start_needle), len(end_needle)) * 2)
    return {
        "start": start_pos,
        "end": end_boundary,
        "rawStart": raw_start,
        "rawEnd": raw_end,
        "text": source_text[raw_start:raw_end].strip(),
        "confidence": confidence,
        "startNeedle": start_needle,
        "endNeedle": end_needle,
    }


def discover_source_for_page(job: dict, start_text: str, end_text: str) -> SourceUnit | None:
    attempt_count = int(job.get("discoveryAttemptCount") or 0)
    if attempt_count >= 60:
        return None
    job["discoveryAttemptCount"] = attempt_count + 1
    tried = set(str(value) for value in job.get("discoveryTried", []))
    queries = pdf_title_candidates(job)
    for anchor_text in (start_text, end_text):
        normalized, _ = normalize_for_match(anchor_text)
        if len(normalized) >= 10:
            queries.append(normalized[:12])
    candidate_titles = []
    for query in queries:
        if not query:
            continue
        tried.add(query)
        for title in search_wikisource_titles(query):
            base_title = title.split("/", 1)[0]
            if base_title not in candidate_titles:
                candidate_titles.append(base_title)
    existing_units = load_source_units(job)
    existing_urls = {unit.url for unit in existing_units}
    for title in candidate_titles[:20]:
        book_units = fetch_wikisource_book_units(title)
        if not assess_source_units(book_units)["usable"]:
            continue
        matching = next((unit for unit in book_units if strict_pair_in_text(unit.text, start_text, end_text)), None)
        if matching:
            units = existing_units[:]
            for unit in book_units:
                if unit.url not in existing_urls:
                    units.append(unit)
                    existing_urls.add(unit.url)
            job["discoveryTried"] = sorted(tried)
            save_source_units(job, units)
            return matching
    job["discoveryTried"] = sorted(tried)
    if job.get("id"):
        atomic_write_json(job_paths(str(job["id"])).meta, job)
    return None


def sample_pages(page_count: int) -> list[int]:
    if page_count <= 10:
        return list(range(1, page_count + 1))
    picks = {1, 2, 3, page_count}
    for ratio in (0.1, 0.25, 0.5, 0.75, 0.9):
        picks.add(max(1, min(page_count, round(page_count * ratio))))
    return sorted(picks)


def inspect_pdf(pdf_path: Path, requested_layout: str = "auto") -> dict:
    reader = PdfReader(str(pdf_path), strict=False)
    page_count = len(reader.pages)
    samples = sample_pages(page_count)
    text_lengths = []
    sizes = []
    for page_no in samples:
        page = reader.pages[page_no - 1]
        sizes.append((round(float(page.mediabox.width)), round(float(page.mediabox.height))))
        text = page.extract_text() or ""
        text_lengths.append(len(text.strip()))

    textful = sum(1 for length in text_lengths if length > 80)
    ocr_score = round(100 * textful / max(1, len(text_lengths)))
    position_score = 0
    layout = requested_layout if requested_layout != "auto" else guess_layout(sizes)
    messages = []
    if ocr_score < 30:
        messages.append("扫描页已就绪；可以直接生成整本，也可以先任选一页查看定位预览。")
    else:
        messages.append("PDF 已就绪；原有文字层不会直接沿用，将在生成时重新核对。")

    return {
        "pageCount": page_count,
        "textLayerLabel": "有可读文字" if ocr_score else "需要配文本",
        "layout": layout,
        "layoutLabel": layout_label(layout),
        "ocrAnchorScore": ocr_score,
        "positionReuseScore": position_score,
        "messages": messages,
    }


def guess_layout(sizes: list[tuple[int, int]]) -> str:
    if not sizes:
        return "auto"
    wide = sum(1 for w, h in sizes if h > w * 1.25)
    return "vertical-double" if wide >= len(sizes) / 2 else "horizontal"


def layout_label(layout: str) -> str:
    return {
        "vertical-double": "竖排上下双页",
        "vertical-single": "竖排单页",
        "horizontal": "横排",
        "auto": "自动判断",
    }.get(layout, layout)


def ensure_text_font() -> str:
    global TEXT_FONT_REGISTERED
    if TEXT_FONT_REGISTERED:
        return TEXT_FONT
    for font_path in TEXT_FONT_CANDIDATES:
        if not font_path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(TEXT_FONT, str(font_path)))
            TEXT_FONT_REGISTERED = True
            return TEXT_FONT
        except Exception:
            continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    TEXT_FONT_REGISTERED = True
    return "STSong-Light"


def ensure_char_font(char: str) -> str:
    global EXTB_TEXT_FONT_REGISTERED
    if ord(char) <= 0xFFFF:
        return ensure_text_font()
    if EXTB_TEXT_FONT_REGISTERED:
        return EXTB_TEXT_FONT
    for font_path in EXTB_TEXT_FONT_CANDIDATES:
        if not font_path.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(EXTB_TEXT_FONT, str(font_path)))
            EXTB_TEXT_FONT_REGISTERED = True
            return EXTB_TEXT_FONT
        except Exception:
            continue
    return ensure_text_font()


def runs(mask: list[bool], min_len: int = 5) -> list[tuple[int, int]]:
    found = []
    start = None
    for index, value in enumerate(mask):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_len:
                found.append((start, index - 1))
            start = None
    if start is not None and len(mask) - start >= min_len:
        found.append((start, len(mask) - 1))
    return found


def detect_vertical_blocks(image: Image.Image, layout: str) -> list[dict]:
    gray = image.convert("L")
    pix = gray.load()
    width, height = gray.size
    threshold = 170

    row_counts = [
        sum(1 for x in range(width) if pix[x, y] < threshold)
        for y in range(height)
    ]
    col_counts = [
        sum(1 for y in range(height) if pix[x, y] < threshold)
        for x in range(width)
    ]
    frame_rows = runs([count > width * 0.42 for count in row_counts], 3)
    frame_cols = runs([count > height * 0.40 for count in col_counts], 3)

    y_edges = [round((a + b) / 2) for a, b in frame_rows if a > 5]
    if layout == "vertical-single":
        if len(y_edges) >= 2 and max(y_edges) - min(y_edges) > height * .45:
            y_pairs = [(max(0, min(y_edges)), min(height - 1, max(y_edges)))]
        else:
            y_pairs = [(round(height * .12), round(height * .88))]
    else:
        y_pairs = [
            (a, b) for a, b in zip(y_edges, y_edges[1:])
            if height * .22 < b - a < height * .50
        ]
    if not y_pairs:
        y_pairs = [(round(height * .12), round(height * .48)), (round(height * .52), round(height * .88))]

    if len(frame_cols) >= 2:
        x0 = round((frame_cols[0][0] + frame_cols[0][1]) / 2)
        x1 = round((frame_cols[-1][0] + frame_cols[-1][1]) / 2)
    else:
        x0, x1 = round(width * .12), round(width * .88)

    blocks = []
    for y0, y1 in y_pairs[:2 if layout != "vertical-single" else 1]:
        ix0, ix1 = x0 + round(width * .02), x1 - round(width * .02)
        iy0, iy1 = y0 + round(height * .014), y1 - round(height * .014)
        column_runs = text_column_runs(image, (ix0, iy0, ix1, iy1))
        cols = [round((start + end) / 2) for start, end in reversed(column_runs)]
        if len(cols) < 3:
            column_count = max(8, min(24, round((ix1 - ix0) / max(24, width * .036))))
            step = (ix1 - ix0) / max(1, column_count - 1)
            cols = [ix1 - step * index for index in range(column_count)]
        blocks.append({"box": (x0, y0, x1, y1), "inner": (ix0, iy0, ix1, iy1), "cols": cols})
    return blocks


def detect_horizontal_block(image: Image.Image) -> list[dict]:
    width, height = image.size
    return [{"box": (0, 0, width, height), "inner": (round(width * .1), round(height * .1), round(width * .9), round(height * .9)), "cols": []}]


def stable_vertical_blocks(page_w: float, page_h: float, layout: str) -> tuple[list[dict], tuple[int, int]]:
    """Use a stable book block for placement instead of noisy full-page OCR columns."""
    image_w = 1000
    image_h = max(1000, round(image_w * page_h / max(1.0, page_w)))
    x0, x1 = round(image_w * .12), round(image_w * .88)
    column_count = max(10, min(24, round((x1 - x0) / 38)))
    step = (x1 - x0) / max(1, column_count - 1)
    cols = [x1 - step * index for index in range(column_count)]
    if layout == "vertical-single":
        y_ranges = [(round(image_h * .12), round(image_h * .88))]
    else:
        y_ranges = [
            (round(image_h * .12), round(image_h * .48)),
            (round(image_h * .52), round(image_h * .88)),
        ]
    blocks = []
    for y0, y1 in y_ranges:
        blocks.append({
            "box": (x0, y0, x1, y1),
            "inner": (x0, y0, x1, y1),
            "cols": list(cols),
        })
    return blocks, (image_w, image_h)


def close_pdfium_documents() -> None:
    for document in PDFIUM_DOCUMENTS.values():
        try:
            document.close()
        except Exception:
            pass
    PDFIUM_DOCUMENTS.clear()


atexit.register(close_pdfium_documents)


def render_page_images_persistent(pdf_path: Path, page_numbers: list[int], dpi: int = 140) -> list[Image.Image]:
    """Render with one persistent PDFium document per OCR worker process."""
    if pdfium is None:
        return render_page_images(pdf_path, page_numbers, dpi=dpi)
    resolved = pdf_path.resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    document = PDFIUM_DOCUMENTS.get(key)
    if document is None:
        close_pdfium_documents()
        document = pdfium.PdfDocument(str(resolved))
        PDFIUM_DOCUMENTS[key] = document
    images = []
    for page_no in page_numbers:
        page = document[page_no - 1]
        bitmap = None
        try:
            bitmap = page.render(scale=dpi / 72.0)
            images.append(bitmap.to_pil().convert("RGB").copy())
        finally:
            if bitmap is not None:
                bitmap.close()
            page.close()
    return images


def render_page_images(pdf_path: Path, page_numbers: list[int], dpi: int = 140) -> list[Image.Image]:
    if not page_numbers:
        return []
    if page_numbers != list(range(page_numbers[0], page_numbers[-1] + 1)):
        raise ValueError("批量渲染只接受连续页。")
    if not POPPLER.exists():
        raise FileNotFoundError(f"Poppler renderer not found: {POPPLER}")
    identity = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:8]
    first_page, last_page = page_numbers[0], page_numbers[-1]
    out_prefix = pdf_path.parent / f".render-pages-{first_page:04d}-{last_page:04d}-{identity}-{os.getpid()}-{threading.get_ident()}"
    candidates = []
    try:
        result = subprocess.run(
            [str(POPPLER), "-f", str(first_page), "-l", str(last_page), "-png", "-r", str(dpi), str(pdf_path), str(out_prefix)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="ignore").strip() or "页面渲染器没有返回可用信息。"
            raise RuntimeError(f"第 {first_page}-{last_page} 页没有渲染成功：{detail}")
        candidates = sorted(out_prefix.parent.glob(f"{out_prefix.name}-*.png"))
        if len(candidates) != len(page_numbers):
            raise FileNotFoundError(f"第 {first_page}-{last_page} 页渲染数量不完整。")
        images = []
        for rendered in candidates:
            with Image.open(rendered) as opened:
                images.append(opened.convert("RGB"))
        return images
    finally:
        for candidate in candidates or out_prefix.parent.glob(f"{out_prefix.name}-*.png"):
            candidate.unlink(missing_ok=True)


def render_page_image(pdf_path: Path, page_no: int, dpi: int = 140) -> Image.Image:
    return render_page_images(pdf_path, [page_no], dpi=dpi)[0]


def draw_guides(image: Image.Image, blocks: list[dict], out_path: Path) -> None:
    debug = image.copy()
    draw = ImageDraw.Draw(debug)
    for block in blocks:
        draw.rectangle(block["box"], outline=(0, 126, 190), width=3)
        draw.rectangle(block["inner"], outline=(35, 160, 92), width=2)
        ix0, iy0, ix1, iy1 = block["inner"]
        for x in block.get("cols", []):
            draw.line([(x, iy0), (x, iy1)], fill=(216, 54, 45), width=1)
    debug.save(out_path)


def remove_text(page, pdf_context) -> None:
    contents = page.get_contents()
    if contents is None:
        return
    stream = ContentStream(contents, pdf_context)
    text_showing_operators = {b"Tj", b"TJ", b"'", b'"'}
    stream.operations = [
        (operands, operator)
        for operands, operator in stream.operations
        if operator not in text_showing_operators
    ]
    page.replace_contents(stream)


def draw_vertical_text(pdf_canvas, text: str, blocks: list[dict], page_w: float, page_h: float, image_w: int, image_h: int) -> None:
    draw_word_style_authoritative_text(pdf_canvas, text, page_w, page_h, "vertical-single")


def draw_horizontal_text(pdf_canvas, text: str, page_w: float, page_h: float) -> None:
    draw_word_style_authoritative_text(pdf_canvas, text, page_w, page_h, "horizontal")


def canonical_output_text(text: str) -> str:
    return "".join(char for char in text if char != "\x00" and not char.isspace())


def word_frame_font_size(char_count: int, usable_w: float, usable_h: float, initial: float = 12.0) -> float:
    if char_count <= 0:
        return initial

    def capacity(font_size: float) -> int:
        return max(1, int(usable_w / font_size)) * max(1, int(usable_h / font_size))

    if capacity(initial) >= char_count:
        return initial
    low = min(0.1, max(0.001, (usable_w * usable_h / char_count) ** 0.5 * 0.1))
    high = initial
    for _ in range(48):
        middle = (low + high) / 2
        if capacity(middle) >= char_count:
            low = middle
        else:
            high = middle
    return max(0.001, low * 0.999)


def pdf_actual_text_hex(text: str) -> str:
    return (b"\xfe\xff" + text.encode("utf-16-be", errors="surrogatepass")).hex().upper()


def draw_word_style_authoritative_text(
    pdf_canvas,
    text: str,
    page_w: float,
    page_h: float,
    layout: str,
) -> dict:
    """Place exact source text in an independent 3 cm Word-style page frame."""
    chars = canonical_output_text(text)
    if not chars:
        return {"fontSize": 12.0, "columns": 0, "rows": 0}
    requested_margin = 3.0 * 72.0 / 2.54
    margin_x = min(requested_margin, max(0.0, page_w / 2 - 0.001))
    margin_y = min(requested_margin, max(0.0, page_h / 2 - 0.001))
    usable_w = max(0.001, page_w - margin_x * 2)
    usable_h = max(0.001, page_h - margin_y * 2)
    font_size = word_frame_font_size(len(chars), usable_w, usable_h)
    cells_across = max(1, int(usable_w / font_size))
    cells_down = max(1, int(usable_h / font_size))
    vertical = layout != "horizontal"

    pdf_canvas._code.append(f"/Span << /ActualText <{pdf_actual_text_hex(chars)}> >> BDC")
    text_obj = pdf_canvas.beginText()
    text_obj.setTextRenderMode(3)
    current_font = ""
    for index, char in enumerate(chars):
        font = ensure_char_font(char)
        if font != current_font:
            text_obj.setFont(font, font_size)
            current_font = font
        if vertical:
            column = index // cells_down
            row = index % cells_down
            x = page_w - margin_x - font_size - column * font_size
            y = page_h - margin_y - font_size - row * font_size
        else:
            row = index // cells_across
            column = index % cells_across
            x = margin_x + column * font_size
            y = page_h - margin_y - font_size - row * font_size
        text_obj.setTextOrigin(x, y)
        text_obj.textOut(char)
    pdf_canvas.drawText(text_obj)
    pdf_canvas._code.append("EMC")
    columns = (len(chars) + cells_down - 1) // cells_down if vertical else cells_across
    rows = cells_down if vertical else (len(chars) + cells_across - 1) // cells_across
    return {"fontSize": round(font_size, 3), "columns": columns, "rows": rows}


def expected_text_layer_norm(text: str) -> str:
    return canonical_output_text(text)


def page_text_from_sources(job: dict, page_no: int, layout: str | None = None, require_anchor_for_scan: bool = True) -> str:
    pdf_path = Path(job["pdf"])
    reader = PdfReader(str(pdf_path), strict=False)
    selected_layout = layout or job.get("layout", "auto")
    resolved = resolve_trial_page_by_next_start(job, reader, page_no, selected_layout)
    if require_anchor_for_scan and resolved.get("kind") == "unresolved":
        return ""
    return str(resolved.get("text") or "")


def source_segments(job: dict, page_count: int) -> list[str] | None:
    source_value = str(job.get("sourceText", "")).strip()
    if not source_value:
        return None
    source_path = Path(source_value)
    if not source_path.exists():
        return None
    text = read_job_source_text(job)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return None
    chunk = max(600, round(len(text) / max(1, page_count)))
    segments = []
    for page_no in range(page_count):
        start = max(0, page_no * chunk)
        end = min(len(text), start + chunk)
        segments.append(text[start:end])
    return segments


def normalize_for_match(text: str) -> tuple[str, list[int]]:
    chars = []
    mapping = []
    for index, char in enumerate(text):
        if char.isspace() or char in SKIP_CHARS:
            continue
        chars.append(normalize_cjk_variant(char))
        mapping.append(index)
    return "".join(chars), mapping


def normalize_source_cached(text: str) -> tuple[str, list[int]]:
    key = id(text)
    with NORMALIZED_SOURCE_CACHE_LOCK:
        cached = NORMALIZED_SOURCE_CACHE.get(key)
        if cached and cached[0] is text:
            return cached[1], cached[2]
    normalized, mapping = normalize_for_match(text)
    with NORMALIZED_SOURCE_CACHE_LOCK:
        if len(NORMALIZED_SOURCE_CACHE) >= 256:
            NORMALIZED_SOURCE_CACHE.clear()
        NORMALIZED_SOURCE_CACHE[key] = (text, normalized, mapping)
    return normalized, mapping


def normalize_edge_literal(text: str) -> tuple[str, list[int]]:
    chars = []
    mapping = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char if char in SKIP_CHARS else normalize_cjk_variant(char))
        mapping.append(index)
    return "".join(chars), mapping


def recognized_edge_literal(anchor_text: str, side: str, size: int = 12) -> str:
    lines = [line for line in re.split(r"[\r\n]+", anchor_text) if line.strip()]
    if not lines:
        return ""
    literal, _ = normalize_edge_literal("".join(lines))
    if side == "end":
        return literal[-size:]
    return literal[:size]


def refine_boundary_from_recognized_edge(
    source_text: str,
    mapping: list[int],
    strong_boundary: int,
    anchor_text: str,
    side: str,
) -> tuple[int, int] | None:
    """Return normalized/raw boundary only when the literal page edge is unique nearby."""
    literal = recognized_edge_literal(anchor_text, side)
    if not literal or not mapping:
        return None
    source_edge, edge_mapping = normalize_edge_literal(source_text)
    if not source_edge or not edge_mapping:
        return None
    strong_index = max(0, min(strong_boundary, len(mapping) - 1))
    strong_raw = mapping[strong_index]
    edge_cursor = bisect.bisect_left(edge_mapping, strong_raw)
    if side == "start":
        low = max(0, edge_cursor - 180)
        high = min(len(source_edge), edge_cursor + 20)
    else:
        low = max(0, edge_cursor - 20)
        high = min(len(source_edge), edge_cursor + 180)
    positions = []
    cursor = low
    while len(positions) < 2:
        found = source_edge.find(literal, cursor, high)
        if found < 0:
            break
        positions.append(found)
        cursor = found + 1
    if len(positions) == 1:
        found = positions[0]
        if side == "start":
            raw_boundary = edge_mapping[found]
            normalized_boundary = bisect.bisect_left(mapping, raw_boundary)
        else:
            raw_boundary = edge_mapping[found + len(literal) - 1] + 1
            normalized_boundary = bisect.bisect_left(mapping, raw_boundary)
        return normalized_boundary, raw_boundary

    # OCR often omits punctuation inside an otherwise unique edge phrase. Keep
    # an explicitly recognized outer punctuation literal strict, but allow the
    # already unique normalized anchor to define the same character boundary.
    source_norm, _ = normalize_for_match(source_text)
    anchor_pos, anchor_score, anchor_needle = line_anchor_evidence(source_norm, anchor_text, side)
    if anchor_pos is None or anchor_score < 44 or not anchor_needle:
        return None
    normalized_boundary = anchor_pos if side == "start" else anchor_pos + len(anchor_needle)
    if normalized_boundary != strong_boundary:
        return None
    if side == "start":
        raw_boundary = mapping[normalized_boundary]
    else:
        raw_boundary = mapping[normalized_boundary - 1] + 1
    outer_char = literal[0] if side == "start" else literal[-1]
    if outer_char in SKIP_CHARS or unicodedata.category(outer_char)[:1] in {"P", "S"}:
        if side == "start":
            if raw_boundary > 0 and source_text[raw_boundary - 1] == outer_char:
                raw_boundary -= 1
            elif source_text[raw_boundary:raw_boundary + 1] != outer_char:
                return None
        else:
            if source_text[raw_boundary:raw_boundary + 1] == outer_char:
                raw_boundary += 1
            elif source_text[raw_boundary - 1:raw_boundary] != outer_char:
                return None
    return normalized_boundary, raw_boundary


def strict_page_slice_from_edges(
    source_text: str,
    mapping: list[int],
    start: int,
    end: int,
    start_anchor: str,
    end_anchor: str,
) -> dict | None:
    refined_start = refine_boundary_from_recognized_edge(
        source_text, mapping, start, start_anchor, "start"
    )
    refined_end = refine_boundary_from_recognized_edge(
        source_text, mapping, end, end_anchor, "end"
    )
    if refined_start is None or refined_end is None:
        return None
    normalized_start, raw_start = refined_start
    normalized_end, raw_end = refined_end
    if normalized_start != start or normalized_end != end or raw_end <= raw_start:
        return None
    return {
        "start": normalized_start,
        "end": normalized_end,
        "rawStart": raw_start,
        "rawEnd": raw_end,
        "text": source_text[raw_start:raw_end].strip(),
    }


def strict_page_text_from_edges(
    source_text: str,
    mapping: list[int],
    start: int,
    end: int,
    start_anchor: str,
    end_anchor: str,
) -> str | None:
    page_slice = strict_page_slice_from_edges(
        source_text, mapping, start, end, start_anchor, end_anchor
    )
    return str(page_slice["text"]) if page_slice else None


def anchor_from_text(text: str, side: str, size: int = 18) -> str:
    normalized, _ = normalize_for_match(text)
    if len(normalized) < 8:
        return ""
    if side == "end":
        return normalized[-size:]
    return normalized[:size]


def find_anchor_near(source_norm: str, anchor: str, cursor: int, window: int) -> tuple[int | None, int]:
    if not anchor:
        return None, 0
    start = max(0, cursor - window // 5)
    end = min(len(source_norm), cursor + window)
    for size in (len(anchor), 16, 14, 12, 10, 8):
        if len(anchor) < size:
            continue
        needle = anchor[:size]
        found = source_norm.find(needle, start, end)
        if found >= 0:
            return found, round(100 * size / max(1, len(anchor)))
    found = source_norm.find(anchor[:8], cursor)
    if found >= 0:
        return found, 45
    return None, 0


def find_end_anchor_near(source_norm: str, anchor: str, cursor: int, window: int) -> tuple[int | None, int]:
    if not anchor:
        return None, 0
    start = max(0, cursor - window // 5)
    end = min(len(source_norm), cursor + window)
    for size in (len(anchor), 16, 14, 12, 10, 8):
        if len(anchor) < size:
            continue
        needle = anchor[-size:]
        found = source_norm.find(needle, start, end)
        if found >= 0:
            return found + size, round(100 * size / max(1, len(anchor)))
    found = source_norm.find(anchor[-8:], cursor)
    if found >= 0:
        return found + 8, 45
    return None, 0


def find_anchor_unique(source_norm: str, anchor: str, side: str) -> tuple[int | None, int]:
    if not anchor:
        return None, 0
    for size in (min(len(anchor), 18), 16, 14, 12, 10, 8):
        if len(anchor) < size:
            continue
        needle = anchor[-size:] if side == "end" else anchor[:size]
        positions = []
        start = 0
        while len(positions) < 2:
            found = source_norm.find(needle, start)
            if found < 0:
                break
            positions.append(found)
            start = found + 1
        if len(positions) == 1:
            pos = positions[0] + size if side == "end" else positions[0]
            return pos, round(100 * size / max(1, len(anchor)))
    return None, 0


def find_anchor_fuzzy(source_norm: str, anchor: str, cursor: int, window: int, side: str) -> tuple[int | None, int]:
    if len(anchor) < 10:
        return None, 0
    start = max(0, cursor - window // 4)
    end = min(len(source_norm), cursor + window)
    region = source_norm[start:end]
    try:
        from rapidfuzz import fuzz

        needle = anchor[-min(len(anchor), 24):] if side == "end" else anchor[:min(len(anchor), 24)]
        hit = fuzz.partial_ratio_alignment(needle, region, score_cutoff=78)
        if hit is None:
            return None, 0
        span = int(hit.dest_end) - int(hit.dest_start)
        if span < max(7, round(len(needle) * 0.68)):
            return None, 0

        # A second similarly good location means a short/common phrase is not a safe lock.
        masked = region[:max(0, int(hit.dest_start) - 2)] + ("\x00" * (span + 4)) + region[min(len(region), int(hit.dest_end) + 2):]
        second = fuzz.partial_ratio_alignment(needle, masked, score_cutoff=78)
        second_score = float(second.score) if second is not None else 0.0
        if float(hit.score) < 84 or (second_score and float(hit.score) - second_score < 7):
            return None, 0
        position = start + int(hit.dest_start)
        return (position + span if side == "end" else position), round(float(hit.score))
    except ImportError:
        pass

    best_pos = None
    best_score = 0.0
    second_score = 0.0
    for size in (min(len(anchor), 18), 16, 14, 12):
        if len(anchor) < size:
            continue
        needle = anchor[-size:] if side == "end" else anchor[:size]
        for pos in range(start, max(start, end - size + 1)):
            candidate = source_norm[pos:pos + size]
            if not candidate:
                continue
            shared = len(set(needle) & set(candidate))
            if shared < max(5, round(size * 0.45)):
                continue
            score = SequenceMatcher(None, needle, candidate).ratio()
            if score > best_score:
                second_score = best_score
                best_score = score
                best_pos = pos + size if side == "end" else pos
            elif score > second_score:
                second_score = score
        if best_score >= 0.84 and best_score - second_score >= 0.06:
            return best_pos, round(best_score * 100)
    if best_score >= 0.88:
        return best_pos, round(best_score * 100)
    return None, 0


def original_slice(source_text: str, mapping: list[int], start_norm: int, end_norm: int) -> str:
    if not mapping:
        return ""
    start_norm = max(0, min(start_norm, len(mapping) - 1))
    end_norm = max(start_norm, min(end_norm, len(mapping) - 1))
    start = mapping[start_norm]
    end = mapping[end_norm] + 1
    return source_text[start:end].strip()


def sort_ocr_items(boxes, txts, scores, layout: str) -> list[str]:
    items = []
    boxes = [] if boxes is None else boxes
    txts = [] if txts is None else txts
    scores = [] if scores is None else scores
    for box, text, score in zip(boxes, txts, scores):
        text = str(text).strip()
        if not text or float(score or 0) < 0.35:
            continue
        points = [[float(value) for value in point] for point in box]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        items.append({
            "text": text,
            "cx": sum(xs) / max(1, len(xs)),
            "cy": sum(ys) / max(1, len(ys)),
        })
    if layout.startswith("vertical"):
        items.sort(key=lambda item: (-item["cx"], item["cy"]))
    else:
        items.sort(key=lambda item: (round(item["cy"] / 18), item["cx"]))
    return [item["text"] for item in items]


def ocr_image_text(image: Image.Image, layout: str) -> str:
    engine = get_ocr_engine()
    if engine is None:
        return ""
    result = engine(image)
    return clean_ocr_text("\n".join(sort_ocr_items(result.boxes, result.txts, result.scores, layout)))


def attach_ocr_column_geometry(job: dict, page_no: int, image: Image.Image, blocks: list[dict], page_text: str) -> list[dict]:
    if not blocks or not page_text.strip():
        return blocks
    job_id = str(job.get("id") or "")
    cache_root = job_paths(job_id).root if job_id else Path(job["pdf"]).parent
    cache_path = cache_root / f"page-{page_no:04d}-ocr-layout-v1.json"
    payload = None
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            payload = None
    if payload is None:
        engine = get_ocr_engine()
        if engine is None:
            return blocks
        result = engine(image)
        items = []
        boxes = [] if result.boxes is None else result.boxes
        txts = [] if result.txts is None else result.txts
        scores = [] if result.scores is None else result.scores
        for box, recognized, score in zip(boxes, txts, scores):
            points = [[float(value) for value in point] for point in box]
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            items.append({
                "x": sum(xs) / len(xs),
                "y0": min(ys),
                "y1": max(ys),
                "width": max(xs) - min(xs),
                "recognized": str(recognized or "").strip(),
                "score": float(score or 0),
            })
        payload = {"items": items, "imageSize": list(image.size)}
        atomic_write_json(cache_path, payload)

    cached_size = payload.get("imageSize") or list(image.size)
    scale_x = image.width / max(1, float(cached_size[0]))
    scale_y = image.height / max(1, float(cached_size[1]))
    items = []
    for cached in payload.get("items", []):
        item = dict(cached)
        item["x"] = float(item["x"]) * scale_x
        item["y0"] = float(item["y0"]) * scale_y
        item["y1"] = float(item["y1"]) * scale_y
        item["width"] = float(item.get("width") or 0) * scale_x
        items.append(item)

    page_norm, _ = normalize_for_match(page_text)
    for block in blocks:
        ix0, iy0, ix1, iy1 = block["inner"]
        candidates = []
        for item in items:
            height = item["y1"] - item["y0"]
            if not (ix0 <= item["x"] <= ix1 and item["y1"] >= iy0 and item["y0"] <= iy1):
                continue
            if height < max(28, item["width"] * 1.35):
                continue
            normalized, _ = normalize_for_match(item["recognized"])
            if len(normalized) < 2:
                continue
            matched = exact_anchor_evidence(page_norm, item["recognized"], "best")[0] is not None
            candidates.append({**item, "matched": matched})
        matched_x = [item["x"] for item in candidates if item["matched"]]
        if len(matched_x) >= 2:
            left, right = min(matched_x), max(matched_x)
            candidates = [item for item in candidates if item["matched"] or left <= item["x"] <= right]
        elif matched_x:
            center = matched_x[0]
            candidates = [item for item in candidates if item["matched"] or abs(item["x"] - center) <= image.width * .12]
        else:
            candidates = []

        merged = []
        for item in sorted(candidates, key=lambda value: (-value["x"], value["y0"])):
            existing = next((column for column in merged if abs(column["x"] - item["x"]) <= image.width * .014), None)
            if existing:
                existing["y0"] = min(existing["y0"], item["y0"])
                existing["y1"] = max(existing["y1"], item["y1"])
                existing["recognized"] = f"{existing['recognized']}{item['recognized']}"
                existing["matched"] = existing["matched"] or item["matched"]
            else:
                merged.append({
                    "x": item["x"],
                    "y0": max(iy0, item["y0"]),
                    "y1": min(iy1, item["y1"]),
                    "recognized": item["recognized"],
                    "matched": item["matched"],
                })
        if len(matched_x) >= 2:
            left, right = min(matched_x), max(matched_x)
            gray = image.convert("L")
            pixels = gray.load()
            for run_start, run_end in text_column_runs(image, block["inner"]):
                center = (run_start + run_end) / 2
                if not (left <= center <= right):
                    continue
                if any(abs(column["x"] - center) <= image.width * .025 for column in merged):
                    continue
                ink_rows = [
                    y for y in range(iy0, iy1)
                    if any(pixels[x, y] < 180 for x in range(max(ix0, run_start - 2), min(ix1, run_end + 3)))
                ]
                if len(ink_rows) < 8:
                    continue
                merged.append({
                    "x": center,
                    "y0": min(ink_rows),
                    "y1": max(ink_rows),
                    "recognized": "",
                    "matched": False,
                })
            merged.sort(key=lambda column: -column["x"])
        if len(merged) >= 3:
            block["ocrColumns"] = merged
            block["cols"] = [column["x"] for column in merged]
    return blocks


def ocr_grouped_images(crops: list[Image.Image], layout: str) -> list[str]:
    if not crops:
        return []
    engine = get_ocr_engine()
    if engine is None:
        return ["" for _ in crops]
    gap = 36
    column_count = min(2, len(crops))
    row_count = math.ceil(len(crops) / column_count)
    column_widths = [
        max(crops[index].width for index in range(column, len(crops), column_count))
        for column in range(column_count)
    ]
    row_heights = [
        max(crops[index].height for index in range(row * column_count, min(len(crops), (row + 1) * column_count)))
        for row in range(row_count)
    ]
    x_offsets = [sum(column_widths[:column]) + gap * column for column in range(column_count)]
    y_offsets = [sum(row_heights[:row]) + gap * row for row in range(row_count)]
    combined = Image.new(
        "RGB",
        (sum(column_widths) + gap * (column_count - 1), sum(row_heights) + gap * (row_count - 1)),
        "white",
    )
    crop_rects = []
    for index, crop in enumerate(crops):
        column = index % column_count
        row = index // column_count
        x0, y0 = x_offsets[column], y_offsets[row]
        combined.paste(crop, (x0, y0))
        crop_rects.append((x0, y0, x0 + crop.width, y0 + crop.height))
    result = engine(combined)
    groups = [[] for _ in crops]
    boxes = [] if result.boxes is None else result.boxes
    txts = [] if result.txts is None else result.txts
    scores = [] if result.scores is None else result.scores
    for box, text, score in zip(boxes, txts, scores):
        text = str(text).strip()
        if not text or float(score or 0) < 0.35:
            continue
        points = [[float(value) for value in point] for point in box]
        cx = sum(point[0] for point in points) / max(1, len(points))
        cy = sum(point[1] for point in points) / max(1, len(points))
        group_index = None
        for candidate, (x0, y0, x1, y1) in enumerate(crop_rects):
            if x0 - gap / 2 <= cx <= x1 + gap / 2 and y0 - gap / 2 <= cy <= y1 + gap / 2:
                group_index = candidate
                break
        if group_index is None:
            continue
        x0, y0, _, _ = crop_rects[group_index]
        groups[group_index].append({"text": text, "cx": cx - x0, "cy": cy - y0})
    texts = []
    for items in groups:
        if layout.startswith("vertical"):
            items.sort(key=lambda item: (-item["cx"], item["cy"]))
        else:
            items.sort(key=lambda item: (round(item["cy"] / 18), item["cx"]))
        texts.append(clean_ocr_text("\n".join(item["text"] for item in items)))
    return texts


def text_column_runs(image: Image.Image, inner: tuple[int, int, int, int]) -> list[tuple[int, int]]:
    ix0, iy0, ix1, iy1 = inner
    gray = image.convert("L")
    pix = gray.load()
    inner_h = max(1, iy1 - iy0)
    counts = []
    for x in range(ix0, ix1):
        count = sum(1 for y in range(iy0, iy1) if pix[x, y] < 170)
        counts.append(count)
    raw_runs = runs([count > inner_h * 0.03 for count in counts], 2)
    found = []
    for start, end in raw_runs:
        absolute = (ix0 + start, ix0 + end)
        peak = max(counts[start:end + 1] or [0])
        if peak < inner_h * 0.08:
            continue
        found.append(absolute)
    return found


def ocr_anchor_crops(
    image: Image.Image,
    layout: str,
    units: int = 1,
    blocks: list[dict] | None = None,
    edge_runs: tuple[list[tuple[int, int]], list[tuple[int, int]]] | None = None,
) -> list[Image.Image]:
    def valid_box(x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
        left, right = sorted((max(0, min(image.width - 1, x0)), max(1, min(image.width, x1))))
        top, bottom = sorted((max(0, min(image.height - 1, y0)), max(1, min(image.height, y1))))
        if right <= left:
            right = min(image.width, left + 1)
        if bottom <= top:
            bottom = min(image.height, top + 1)
        return left, top, right, bottom

    if layout == "horizontal":
        block = detect_horizontal_block(image)[0]
        ix0, iy0, ix1, iy1 = block["inner"]
        line_height = max(56, round((iy1 - iy0) * 0.08 * units))
        return [
            image.crop((ix0, iy0, ix1, min(iy1, iy0 + line_height))),
            image.crop((ix0, max(iy0, iy1 - line_height), ix1, iy1)),
        ]

    blocks = blocks if blocks is not None else detect_vertical_blocks(image, layout)
    if not blocks:
        return [image]
    start_block = blocks[0]
    end_block = blocks[-1]
    six0, siy0, six1, siy1 = start_block["inner"]
    eix0, eiy0, eix1, eiy1 = end_block["inner"]
    if edge_runs is None:
        start_runs = text_column_runs(image, start_block["inner"])
        end_runs = text_column_runs(image, end_block["inner"])
    else:
        start_runs, end_runs = edge_runs
    start_columns = start_runs[-max(1, units):] if start_runs else [(six1 - 70 * units, six1)]
    end_columns = end_runs[:max(1, units)] if end_runs else [(eix0, eix0 + 70 * units)]
    start_left_edge = min(column[0] for column in start_columns)
    start_right_edge = max(column[1] for column in start_columns)
    end_left_edge = min(column[0] for column in end_columns)
    end_right_edge = max(column[1] for column in end_columns)
    start_right = min(six1, max(six0 + 1, start_right_edge + 8))
    start_left = max(six0, min(start_right - 1, start_left_edge - 18))
    edge_pad = max(40, round(image.width * 0.10))
    end_left = max(0, min(eix1 - 1, end_left_edge - edge_pad))
    end_right = min(eix1, max(end_left + 1, end_right_edge + 18))
    return [
        image.crop(valid_box(start_left, siy0 - 70, start_right, siy1 + 35)),
        image.crop(valid_box(end_left, eiy0 - 70, end_right, eiy1 + 35)),
    ]


def ocr_page_anchor_pair(
    job: dict,
    page_no: int,
    layout: str,
    rendered_image: Image.Image | None = None,
    render_dpi: int = 140,
) -> tuple[str, str]:
    job_id = str(job.get("id") or "").strip()
    paths = job_paths(job_id) if job_id else None
    cache_name = f"page-{page_no:04d}-ocr-anchors-v{ANCHOR_CACHE_VERSION}.json"
    cache_path = (paths.root / cache_name) if paths else Path(job["pdf"]).parent / cache_name
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            start_text = str(payload.get("start") or "")
            end_text = str(payload.get("end") or "")
            if (
                payload.get("layout") == layout
                and payload.get("inputFingerprint", "") == str(job.get("inputFingerprint") or "")
                and (payload.get("complete") or start_text or end_text)
            ):
                return start_text, end_text
        except Exception:
            pass
    legacy_start = ""
    if ANCHOR_CACHE_VERSION in {9, 10} and str(job.get("layout") or "") == layout:
        legacy_path = cache_path.with_name(f"page-{page_no:04d}-ocr-anchors-v8.json")
        if legacy_path.exists():
            try:
                legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
                legacy_start = str(legacy_payload.get("start") or "")
            except (OSError, json.JSONDecodeError):
                pass
    image = rendered_image if rendered_image is not None else render_page_image(Path(job["pdf"]), page_no, dpi=140)
    detected_blocks = detect_horizontal_block(image) if layout == "horizontal" else detect_vertical_blocks(image, layout)
    edge_runs = None
    if layout != "horizontal" and detected_blocks:
        edge_runs = (
            text_column_runs(image, detected_blocks[0]["inner"]),
            text_column_runs(image, detected_blocks[-1]["inner"]),
        )
    primary_crops = ocr_anchor_crops(image, layout, units=1, blocks=detected_blocks, edge_runs=edge_runs)
    expanded_crops = ocr_anchor_crops(image, layout, units=2, blocks=detected_blocks, edge_runs=edge_runs)
    if legacy_start and len(primary_crops) >= 2:
        end_variants = ocr_grouped_images([primary_crops[1], expanded_crops[1]], layout)
        chunks = [legacy_start, end_variants[0] if end_variants else ""]
        expanded_texts = {2: end_variants[1] if len(end_variants) > 1 else ""}
        indexes = (2,)
    else:
        batched = ocr_grouped_images([*primary_crops[:2], *expanded_crops[:2]], layout)
        chunks = batched[:2]
        expanded_texts = {
            1: batched[2] if len(batched) > 2 else "",
            2: batched[3] if len(batched) > 3 else "",
        }
        indexes = tuple(range(1, min(2, len(chunks)) + 1))
    source_corpus = source_search_corpus(job)
    for index in indexes:
        text = chunks[index - 1]
        normalized, _ = normalize_for_match(text)
        side = "start" if index == 1 else "end"
        source_match = anchor_present_in_source_corpus(source_corpus, text, side)
        if len(normalized) <= 4 or not source_match:
            chunks[index - 1] = expanded_texts.get(index) or text
    while len(chunks) < 2:
        chunks.append("")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cache_path, {
        "complete": True,
        "start": chunks[0],
        "end": chunks[1],
        "imageSize": list(image.size),
        "blocks": detected_blocks,
        "reusedV8Start": bool(legacy_start),
        "layout": layout,
        "inputFingerprint": str(job.get("inputFingerprint") or ""),
        "renderer": "pdfium-persistent" if rendered_image is not None and pdfium is not None else "poppler",
        "renderDpi": render_dpi,
    })
    return chunks[0], chunks[1]


def source_search_corpus(job: dict) -> str:
    corpus_key = str(job.get("inputFingerprint") or job.get("id") or job.get("sourceText") or "")
    source_corpus = SOURCE_SEARCH_CORPUS_CACHE.get(corpus_key)
    if source_corpus is None:
        source_corpus = "\n".join(normalize_source_cached(unit.text)[0] for unit in load_source_units(job))
        if len(SOURCE_SEARCH_CORPUS_CACHE) >= 4:
            SOURCE_SEARCH_CORPUS_CACHE.clear()
        SOURCE_SEARCH_CORPUS_CACHE[corpus_key] = source_corpus
    return source_corpus


def anchor_pair_strong_for_source(job: dict, start_text: str, end_text: str) -> bool:
    corpus = source_search_corpus(job)
    evidence = []
    for side, text in (("start", start_text), ("end", end_text)):
        normalized, _ = normalize_for_match(text)
        evidence.append(len(normalized) >= 8 and anchor_present_in_source_corpus(corpus, text, side))
    return all(evidence)


def precompute_anchor_worker(payload: tuple[str, tuple[int, ...], str]) -> list[tuple[int, bool, str]]:
    job_id, page_numbers, layout = payload
    try:
        paths = job_paths(job_id)
        job = json.loads(paths.meta.read_text(encoding="utf-8"))
        images = render_page_images_persistent(Path(job["pdf"]), list(page_numbers), dpi=120)
    except Exception as error:
        return [(page_no, False, str(error)) for page_no in page_numbers]
    results = []
    for page_no, image in zip(page_numbers, images):
        try:
            start_text, end_text = ocr_page_anchor_pair(
                job, page_no, layout, rendered_image=image, render_dpi=120
            )
            if not anchor_pair_strong_for_source(job, start_text, end_text):
                cache_path = job_paths(job_id).root / f"page-{page_no:04d}-ocr-anchors-v{ANCHOR_CACHE_VERSION}.json"
                cache_path.unlink(missing_ok=True)
                high_image = render_page_images_persistent(Path(job["pdf"]), [page_no], dpi=140)[0]
                start_text, end_text = ocr_page_anchor_pair(
                    job, page_no, layout, rendered_image=high_image, render_dpi=140
                )
            results.append((page_no, bool(start_text or end_text), ""))
        except Exception as error:
            results.append((page_no, False, str(error)))
    return results


def anchor_cache_ready(job_id: str, page_no: int, layout: str, input_fingerprint: str = "") -> bool:
    cache_path = job_paths(job_id).root / f"page-{page_no:04d}-ocr-anchors-v{ANCHOR_CACHE_VERSION}.json"
    if not cache_path.exists():
        return False
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return bool(
            payload.get("layout") == layout
            and payload.get("inputFingerprint", "") == input_fingerprint
            and (payload.get("complete") or payload.get("start") or payload.get("end"))
        )
    except Exception:
        return False


def adaptive_ocr_workers(requested: int | None = None) -> int:
    logical_cpus = os.cpu_count() or 4
    # OCR engines are both CPU- and memory-heavy. Always leave at least two
    # logical CPUs for Windows, the browser, and the local web service.
    cpu_limit = max(1, min(4, logical_cpus - 2, max(1, logical_cpus // 3)))
    automatic = cpu_limit
    available_mb = available_memory_mb()
    if available_mb:
        # Reserve 2 GiB for the operating system and budget about 1.2 GiB for
        # each OCR process. This deliberately prefers a slower healthy run to
        # paging or terminating a large-book job.
        memory_limit = max(1, min(4, int(max(0, available_mb - 2048) // 1200)))
        automatic = min(automatic, memory_limit)
    override = str(os.environ.get("TEXT_LAYER_OCR_WORKERS") or "").strip()
    if override.isdigit():
        automatic = min(automatic, max(1, int(override)))
    return max(1, min(automatic, requested)) if requested else max(1, automatic)


def available_memory_mb() -> int:
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys // (1024 * 1024))
        if Path("/proc/meminfo").exists():
            match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", Path("/proc/meminfo").read_text(), re.MULTILINE)
            if match:
                return int(match.group(1)) // 1024
    except (OSError, ValueError, AttributeError):
        pass
    return 0


def throughput_metrics(started_at: float, completed: int, total: int, **extra) -> dict:
    elapsed = max(0.001, time.time() - started_at)
    rate = completed * 60 / elapsed if completed else 0.0
    eta = round(max(0, total - completed) * 60 / rate) if rate else 0
    return {
        **extra,
        "pagesPerMinute": round(rate, 1),
        "etaSeconds": eta,
        "freeMemoryMB": available_memory_mb(),
    }


def precompute_anchor_cache(
    job_id: str,
    page_count: int,
    layout: str,
    first_page: int = 1,
    workers: int | None = None,
    ready_callback: Callable[[int], None] | None = None,
) -> bool:
    job = json.loads(job_paths(job_id).meta.read_text(encoding="utf-8"))
    input_fingerprint = str(job.get("inputFingerprint") or "")
    all_pages = list(range(max(1, first_page), page_count + 1))
    pages = [page_no for page_no in all_pages if not anchor_cache_ready(job_id, page_no, layout, input_fingerprint)]
    cached = len(all_pages) - len(pages)
    if not pages:
        if ready_callback:
            ready_callback(page_count)
        update_pipeline_stage(
            job_id,
            "ocr",
            "done",
            state="planning",
            processed=len(all_pages),
            total=page_count,
            detail=f"已从缓存恢复 {len(all_pages)} 页",
            message=f"逐页双锁边 OCR 已全部从缓存恢复，共 {len(all_pages)} 页。",
        )
        return True
    completed = cached
    newly_ocr = 0
    work_started = time.time()
    ready_pages = {
        page_no for page_no in all_pages
        if anchor_cache_ready(job_id, page_no, layout, input_fingerprint)
    }
    contiguous_ready = first_page - 1
    while contiguous_ready + 1 in ready_pages:
        contiguous_ready += 1
    if ready_callback and contiguous_ready >= first_page:
        ready_callback(contiguous_ready)
    worker_count = adaptive_ocr_workers(workers)
    reusable_starts = sum(
        (job_paths(job_id).root / f"page-{page_no:04d}-ocr-anchors-v8.json").exists()
        for page_no in pages
    ) if ANCHOR_CACHE_VERSION in {9, 10} and str(job.get("layout") or "") == layout else 0
    update_pipeline_stage(
        job_id,
        "ocr",
        "running",
        state="planning",
        processed=completed,
        total=page_count,
        detail=f"{worker_count} 路并行，{'PDFium 常驻打开' if pdfium is not None else 'Poppler 分块渲染'}，复用旧页首 {reusable_starts} 页",
        metrics=throughput_metrics(
            work_started, newly_ocr, len(pages), workers=worker_count,
            cachedPages=cached, newlyOcrPages=newly_ocr, renderDpi="120→140 按需复核",
        ),
        message=(
            f"正在复用旧页首、仅更新页尾 OCR {completed} / {len(all_pages)}（{worker_count} 路并行）。"
            if reusable_starts else
            f"正在逐页进行 OCR 双锁边 {completed} / {len(all_pages)}（{worker_count} 路并行）。"
        ),
    )
    batch_size = 1 if pdfium is not None else 4
    page_batches = []
    for page_no in pages:
        if page_batches and len(page_batches[-1]) < batch_size and page_no == page_batches[-1][-1] + 1:
            page_batches[-1].append(page_no)
        else:
            page_batches.append([page_no])
    with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
        page_queue = iter(page_batches)
        future_pages = {}
        pending = set()

        def fill_worker_queue() -> None:
            # Re-evaluate memory before dispatching more pages. Existing work is
            # allowed to finish, while new work is throttled when RAM becomes
            # scarce during a long book.
            current_limit = adaptive_ocr_workers(worker_count)
            while len(pending) < current_limit:
                try:
                    batch = next(page_queue)
                except StopIteration:
                    break
                future = pool.submit(precompute_anchor_worker, (job_id, tuple(batch), layout))
                future_pages[future] = tuple(batch)
                pending.add(future)

        fill_worker_queue()
        last_progress = time.time()
        last_heartbeat = last_progress
        paused = False
        while pending:
            done, pending = concurrent.futures.wait(
                pending,
                timeout=5,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            if not done:
                idle_seconds = time.time() - last_progress
                if idle_seconds > OCR_IDLE_TIMEOUT_SECONDS:
                    waiting_pages = sorted(page for future in pending for page in future_pages[future])
                    for future in pending:
                        future.cancel()
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "error",
                        state="error",
                        processed=completed,
                        total=page_count,
                        detail=f"等待页面：{', '.join(map(str, waiting_pages[:8]))}",
                        message=f"OCR 已 {int(idle_seconds)} 秒没有新页面完成，疑似卡在第 {', '.join(map(str, waiting_pages[:8]))} 页。重启后点击生成整本可继续使用现有缓存。",
                        pauseRequested=False,
                        stalledPages=waiting_pages[:20],
                    )
                    raise TimeoutError("OCR worker stalled")
                if time.time() - last_heartbeat >= 30:
                    waiting_pages = sorted(page for future in pending for page in future_pages[future])
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "running",
                        state="planning",
                        processed=completed,
                        total=page_count,
                        detail=f"工作进程仍在计算第 {', '.join(map(str, waiting_pages[:4]))} 页",
                        metrics=throughput_metrics(
                            work_started, newly_ocr, len(pages), workers=worker_count,
                            cachedPages=cached, newlyOcrPages=newly_ocr,
                            idleSeconds=round(idle_seconds, 1), renderDpi="120→140 按需复核",
                        ),
                        message="OCR 工作进程仍在计算，连续页进度将在页面完成后推进。",
                    )
                    last_heartbeat = time.time()
                continue
            last_progress = time.time()
            last_heartbeat = last_progress
            previous_contiguous = contiguous_ready
            for future in done:
                batch_pages = future_pages.pop(future, ())
                try:
                    results = future.result()
                except Exception as error:
                    for pending_future in pending:
                        pending_future.cancel()
                    failed_page = batch_pages[0] if batch_pages else None
                    page_detail = f"第 {failed_page} 页" if failed_page else "当前批次"
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "error",
                        state="error",
                        processed=completed,
                        total=page_count,
                        currentPage=failed_page,
                        detail=f"{page_detail}工作进程异常退出；已有缓存保留",
                        message=f"OCR 工作进程异常退出，任务已停止且没有误报进度：{error}",
                    )
                    raise RuntimeError(f"{page_detail} OCR 工作进程异常退出：{error}") from error
                worker_failure = next(((page_no, error) for page_no, _, error in results if error), None)
                for result_page, _, worker_error in results:
                    if worker_error:
                        continue
                    completed += 1
                    newly_ocr += 1
                    ready_pages.add(result_page)
                    while contiguous_ready + 1 in ready_pages:
                        contiguous_ready += 1
                if worker_failure:
                    result_page, worker_error = worker_failure
                    for pending_future in pending:
                        pending_future.cancel()
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "error",
                        state="error",
                        processed=completed,
                        total=page_count,
                        currentPage=result_page,
                        detail=f"第 {result_page} 页 OCR 失败；已有缓存保留",
                        message=f"第 {result_page} 页 OCR 失败，任务已停止，未把失败页误计为完成：{worker_error}",
                    )
                    raise RuntimeError(f"第 {result_page} 页 OCR 失败：{worker_error}")
                page_no = results[-1][0] if results else None
                if page_no and (completed == 1 or completed % 5 == 0 or completed == len(all_pages)):
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "running",
                        state="planning",
                        processed=completed,
                        total=page_count,
                        currentPage=page_no,
                        detail=f"{worker_count} 路并行，当前完成第 {page_no} 页",
                        metrics=throughput_metrics(
                            work_started, newly_ocr, len(pages), workers=worker_count,
                            cachedPages=cached, newlyOcrPages=newly_ocr,
                            idleSeconds=0, renderDpi="120→140 按需复核",
                        ),
                        message=f"正在逐页进行 OCR 双锁边 {completed} / {len(all_pages)}（{worker_count} 路并行）。",
                    )
                if read_full_status(job_id).get("pauseRequested"):
                    paused = True
                    for pending_future in pending:
                        pending_future.cancel()
                    pending.clear()
                    break
            if not paused:
                fill_worker_queue()
                if ready_callback and contiguous_ready > previous_contiguous:
                    ready_callback(contiguous_ready)
    if paused:
        update_pipeline_stage(
            job_id,
            "ocr",
            "paused",
            state="paused",
            processed=completed,
            total=page_count,
            detail=f"缓存已保存 {completed} 页",
            message=f"已暂停，双锁边 OCR 已保存至第 {completed} / {len(all_pages)} 页。",
            pauseRequested=False,
        )
        return False
    update_pipeline_stage(
        job_id,
        "ocr",
        "done",
        state="planning",
        processed=len(all_pages),
        total=page_count,
        detail=f"双锁边 OCR 已完成；缓存 {len(all_pages)} 页",
        metrics=throughput_metrics(
            work_started, newly_ocr, len(pages), workers=worker_count,
            cachedPages=cached, newlyOcrPages=newly_ocr, renderDpi="120→140 按需复核",
        ),
        message="双锁边 OCR 已完成，对齐器正在收口检查连续页与章节边界。",
    )
    return True


def ocr_page_text(job: dict, page_no: int, layout: str, anchors_only: bool = True) -> str:
    if anchors_only:
        return "\n".join(value for value in ocr_page_anchor_pair(job, page_no, layout) if value.strip())
    cache_path = full_ocr_cache_path(job, page_no, layout)
    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8", errors="ignore")
        if text.strip() or get_ocr_engine() is None:
            cleaned = clean_ocr_text(text)
            if cleaned != text:
                atomic_write_text(cache_path, cleaned)
            return cleaned
    image = render_page_image(Path(job["pdf"]), page_no, dpi=160)
    text = ocr_image_text(image, layout)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache_path, text)
    return text


def full_ocr_cache_path(job: dict, page_no: int, layout: str) -> Path:
    job_id = str(job.get("id") or "").strip()
    cache_name = f"page-{page_no:04d}-ocr-full-v3-{layout}.txt"
    return job_paths(job_id).root / cache_name if job_id else Path(job["pdf"]).parent / cache_name


def full_page_ocr_worker(payload: tuple[str, int, str]) -> tuple[int, str, str]:
    job_id, page_no, layout = payload
    try:
        job = json.loads(job_paths(job_id).meta.read_text(encoding="utf-8"))
        cache_path = full_ocr_cache_path(job, page_no, layout)
        if cache_path.exists():
            return page_no, ocr_page_text(job, page_no, layout, anchors_only=False), ""
        image = render_page_images_persistent(Path(job["pdf"]), [page_no], dpi=160)[0]
        text = ocr_image_text(image, layout)
        atomic_write_text(cache_path, text)
        return page_no, text, ""
    except Exception as error:
        return page_no, "", str(error)


def page_anchor_text(job: dict, reader: PdfReader, page_no: int, layout: str) -> str:
    job_id = str(job.get("id") or "").strip()
    if job_id and anchor_cache_ready(job_id, page_no, layout, str(job.get("inputFingerprint") or "")):
        return ocr_page_text(job, page_no, layout, anchors_only=True)
    return ocr_page_text(job, page_no, layout, anchors_only=True)


def page_anchor_pair(job: dict, reader: PdfReader, page_no: int, layout: str) -> tuple[str, str]:
    job_id = str(job.get("id") or "").strip()
    if job_id and anchor_cache_ready(job_id, page_no, layout, str(job.get("inputFingerprint") or "")):
        return ocr_page_anchor_pair(job, page_no, layout)
    # Existing PDF text may be stale or incorrectly ordered OCR. Page-boundary
    # evidence must always come from the scan image itself.
    return ocr_page_anchor_pair(job, page_no, layout)


PAGE_OCR_MARKERS = (
    "目錄", "目录", "出版說明", "出版说明", "版權", "版权", "印刷", "書號", "书号",
    "前言", "序言", "自序", "原序", "凡例", "校勘說明", "校勘说明", "內容提要", "内容提要",
    "圖書在版編目", "图书在版编目", "ISBN", "責任編輯", "责任编辑",
)


def image_ink_ratio(image: Image.Image) -> float:
    gray = image.convert("L").resize((180, 250))
    values = list(gray.getdata())
    return sum(1 for value in values if value < 210) / max(1, len(values))


def classify_page(job: dict, reader: PdfReader, page_no: int, layout: str, full_text: str | None = None) -> dict:
    text = full_text if full_text is not None else (reader.pages[page_no - 1].extract_text() or "")
    normalized, _ = normalize_for_match(text)
    if len(normalized) < 4:
        image = render_page_image(Path(job["pdf"]), page_no, dpi=72)
        if image_ink_ratio(image) < 0.006:
            return {"kind": "blank", "text": "", "reason": "空白或纯图像页"}
    lines = [normalize_for_match(line)[0] for line in re.split(r"[\r\n]+", text) if normalize_for_match(line)[0]]
    short_ratio = sum(1 for line in lines if len(line) <= 10) / max(1, len(lines))
    marker = next((value for value in PAGE_OCR_MARKERS if value in text), "")
    numeric_lines = sum(1 for line in lines if re.search(r"[一二三四五六七八九十百千0-9]{1,5}$", line))
    likely_list = len(lines) >= 10 and short_ratio >= 0.62 and (page_no <= 20 or numeric_lines >= 4)
    sparse_title = 2 <= len(normalized) <= 30 and len(lines) <= 8
    if marker or likely_list or sparse_title:
        return {
            "kind": "ocr",
            "text": text,
            "reason": f"检测到{marker}" if marker else ("短条目密集页面" if likely_list else "书名或卷首页面"),
        }
    return {"kind": "body", "text": text, "reason": "连续正文候选"}


def prepared_ocr_page_text(text: str, reason: str) -> str:
    if reason != "书名或卷首页面":
        return text
    lines = []
    for line in text.splitlines():
        normalized, _ = normalize_for_match(line)
        if normalized and re.fullmatch(r"[0-9一二三四五六七八九十百千]+", normalized):
            continue
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def match_page_source(
    job: dict,
    start_text: str,
    end_text: str,
    allow_discovery: bool = True,
    candidate_units: list[SourceUnit | SourceWindow] | None = None,
) -> dict | None:
    best = None
    source_candidates = candidate_units
    if source_candidates is None:
        source_candidates = source_alignment_windows(load_source_units(job))
    for unit in source_candidates:
        match = strict_pair_in_text(unit.text, start_text, end_text)
        if not match:
            continue
        if isinstance(unit, SourceWindow) and unit.boundary is not None:
            if not (int(match["start"]) < unit.boundary < int(match["end"])):
                continue
        candidate = {**match, "sourceTitle": unit.title, "sourceUrl": unit.url, "sourceKind": unit.kind}
        if isinstance(unit, SourceWindow):
            candidate.update({
                "sourceStartOrder": unit.start_order,
                "sourceEndOrder": unit.end_order,
                "sourceStartUrl": unit.start_url,
                "sourceEndUrl": unit.end_url,
                "sourceStartTitle": unit.start_title,
                "sourceEndTitle": unit.end_title,
                "globalStart": unit.global_norm_start + int(match["start"]),
                "globalEnd": unit.global_norm_start + int(match["end"]),
                "globalRawStart": unit.global_raw_start + int(match["rawStart"]),
                "globalRawEnd": unit.global_raw_start + int(match["rawEnd"]),
                "crossesSourceUnit": unit.boundary is not None,
            })
        candidate["authorityRank"] = source_preference_rank(job, unit)
        if best is None or (candidate["authorityRank"], candidate["confidence"]) > (best.get("authorityRank", 0), best["confidence"]):
            best = candidate
    if not allow_discovery:
        return best
    if best and best.get("authorityRank", 0) >= 40:
        return best
    discovered = discover_source_for_page(job, start_text, end_text)
    if not discovered:
        return best
    match = strict_pair_in_text(discovered.text, start_text, end_text)
    if not match:
        return best
    candidate = {
        **match,
        "sourceTitle": discovered.title,
        "sourceUrl": discovered.url,
        "sourceKind": discovered.kind,
        "authorityRank": source_preference_rank(job, discovered),
    }
    if best is None or (candidate["authorityRank"], candidate["confidence"]) > (best.get("authorityRank", 0), best["confidence"]):
        return candidate
    return best


def match_full_ocr_bounds(job: dict, full_text: str) -> dict | None:
    """Use whole-page OCR only as a boundary fallback; returned text still comes from the source."""
    best = None
    for unit in source_alignment_windows(load_source_units(job)):
        match = strict_pair_in_text(unit.text, full_text, full_text)
        if not match:
            continue
        if unit.boundary is not None and not (int(match["start"]) < unit.boundary < int(match["end"])):
            continue
        if unit.boundary is None and "/" in unit.title and int(match.get("start") or 0) < 500:
            base_norm, _ = normalize_for_match(unit.title.split("/", 1)[0])
            tail_norm, _ = normalize_for_match(unit.title.rsplit("/", 1)[-1])
            page_norm, _ = normalize_for_match(full_text.replace("第", ""))
            if base_norm and tail_norm and base_norm in page_norm and tail_norm in page_norm:
                source_norm, mapping = normalize_source_cached(unit.text)
                match["start"] = 0
                match["text"] = original_slice(unit.text, mapping, 0, max(0, int(match["end"]) - 1))
        candidate = {
            **match,
            "sourceTitle": unit.title,
            "sourceUrl": unit.url,
            "sourceKind": unit.kind,
            "authorityRank": source_preference_rank(job, unit),
            "sourceStartOrder": unit.start_order,
            "sourceEndOrder": unit.end_order,
            "sourceStartUrl": unit.start_url,
            "sourceEndUrl": unit.end_url,
            "sourceStartTitle": unit.start_title,
            "sourceEndTitle": unit.end_title,
            "globalStart": unit.global_norm_start + int(match["start"]),
            "globalEnd": unit.global_norm_start + int(match["end"]),
            "globalRawStart": unit.global_raw_start + int(match["rawStart"]),
            "globalRawEnd": unit.global_raw_start + int(match["rawEnd"]),
            "crossesSourceUnit": unit.boundary is not None,
        }
        if best is None or (candidate["authorityRank"], candidate["confidence"]) > (best["authorityRank"], best["confidence"]):
            best = candidate
    return best


def apply_previous_page_continuity(job: dict, previous: dict | None, current: dict) -> dict:
    if not previous or previous.get("kind") != "body" or current.get("kind") != "body":
        return current
    previous_key = str(previous.get("sourceUrl") or previous.get("sourceTitle") or "")
    current_key = str(current.get("sourceUrl") or current.get("sourceTitle") or "")
    if not previous_key or previous_key != current_key:
        return current
    previous_end = int(previous.get("end") or 0)
    current_start = int(current.get("start") or 0)
    gap = current_start - previous_end
    if gap == 0:
        return current
    return {
        **current,
        "kind": "unresolved",
        "status": "页界未唯一锁定",
        "text": "",
        "confidence": 0,
        "boundaryGap": gap,
        "reason": "本页识别边界与上一页不连续；系统保留 OCR 页首、页尾，不再自动补字或删字。",
    }


def enforce_adjacent_page_boundaries(manifest: list[dict]) -> int:
    conflicts = 0
    for index, item in enumerate(manifest[:-1]):
        following = manifest[index + 1]
        if item.get("kind") != "body" or following.get("kind") != "body":
            continue
        use_global_boundary = "globalRawEnd" in item and "globalRawStart" in following
        source_key = item_source_key(item)
        if not use_global_boundary and (not source_key or source_key != item_source_key(following)):
            continue
        use_raw_boundary = "rawEnd" in item and "rawStart" in following
        if use_global_boundary:
            end = int(item["globalRawEnd"])
            following_start = int(following["globalRawStart"])
            if end != following_start and source_key != item_source_key(following):
                end = int(item.get("globalEnd", end))
                following_start = int(following.get("globalStart", following_start))
        else:
            end = int(item.get("rawEnd") if use_raw_boundary else item.get("end") or 0)
            following_start = int(following.get("rawStart") if use_raw_boundary else following.get("start") or 0)
        if end == following_start:
            item["nextPageGap"] = 0
            following["previousPageGap"] = 0
            continue
        gap = following_start - end
        conflicts += 1
        item["nextPageGap"] = gap
        following["previousPageGap"] = gap
        message = "相邻两页的权威正文边界不完全相接；系统保留单页锁定结果并阻止整本发布。"
        item["continuityWarning"] = message
        following["continuityWarning"] = message
    return conflicts


def enforce_recognized_page_edges(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    units_by_key: dict[str, SourceUnit | SourceWindow],
) -> int:
    rejected = 0
    for index, item in enumerate(manifest):
        if item.get("kind") != "body":
            continue
        unit = units_by_key.get(item_source_key(item))
        if unit is None:
            continue
        source_norm, mapping = normalize_source_cached(unit.text)
        start = int(item.get("start") or 0)
        end = int(item.get("end") or 0)
        if not source_norm or not mapping or end <= start:
            page_slice = None
            start_anchor = str(item.get("startAnchor") or "")
            end_anchor = str(item.get("endAnchor") or "")
        else:
            start_anchor = str(item.get("startAnchor") or "")
            end_anchor = str(item.get("endAnchor") or "")
            if not start_anchor or not end_anchor:
                start_anchor, end_anchor = page_anchor_pair(job, reader, index + 1, layout)
            page_slice = strict_page_slice_from_edges(
                unit.text, mapping, start, end, start_anchor, end_anchor
            )
        if page_slice is None:
            item.update({
                "kind": "unresolved",
                "status": "页边文字未精确对应",
                "text": "",
                "confidence": 0,
                "startAnchor": start_anchor,
                "endAnchor": end_anchor,
                "reason": "写入范围未能从识别出的首字符（含标点）开始并在识别出的末字符结束。",
            })
            rejected += 1
            continue
        item.update(page_slice)
        if isinstance(unit, SourceWindow):
            item["globalStart"] = unit.global_norm_start + int(page_slice["start"])
            item["globalEnd"] = unit.global_norm_start + int(page_slice["end"])
            item["globalRawStart"] = unit.global_raw_start + int(page_slice["rawStart"])
            item["globalRawEnd"] = unit.global_raw_start + int(page_slice["rawEnd"])
        item["startAnchor"] = start_anchor
        item["endAnchor"] = end_anchor
        item["edgeVerified"] = True
    return rejected


def resolve_page(
    job: dict,
    reader: PdfReader,
    page_no: int,
    layout: str,
    allow_discovery: bool = True,
    candidate_units: list[SourceUnit | SourceWindow] | None = None,
) -> dict:
    early_full_text = None
    early_decision = None
    if FULL_OCR_FALLBACK_ENABLED and page_no <= 12:
        early_full_text = ocr_page_text(job, page_no, layout, anchors_only=False)
        early_decision = classify_page(job, reader, page_no, layout, early_full_text)
        if early_decision["kind"] == "blank":
            return {
                "kind": "blank",
                "status": "空白页",
                "page": page_no,
                "text": "",
                "confidence": 100,
                "sourceTitle": "",
                "reason": early_decision["reason"],
            }
    start_text, end_text = page_anchor_pair(job, reader, page_no, layout)
    anchor_lines = [normalize_for_match(line)[0] for line in re.split(r"[\r\n]+", f"{start_text}\n{end_text}") if normalize_for_match(line)[0]]
    anchor_short_ratio = sum(1 for line in anchor_lines if len(line) <= 9) / max(1, len(anchor_lines))
    looks_like_directory = page_no <= 20 and len(anchor_lines) >= 5 and anchor_short_ratio >= 0.78
    match = match_page_source(job, start_text, end_text, allow_discovery=False, candidate_units=candidate_units)
    weak_match = match if match and int(match.get("authorityRank") or 0) < 40 else None
    if match and not weak_match:
        match.update({
            "kind": "body",
            "status": "跨章节双头锁边" if match.get("crossesSourceUnit") else "双头锁边",
            "page": page_no,
            "startAnchor": start_text,
            "endAnchor": end_text,
        })
        return match
    full_text = ""
    if FULL_OCR_FALLBACK_ENABLED:
        full_text = early_full_text if early_full_text is not None else ocr_page_text(job, page_no, layout, anchors_only=False)
        full_match = match_full_ocr_bounds(job, full_text)
        if full_match:
            full_match.update({
                "kind": "body",
                "status": "全文 OCR 边界校准",
                "page": page_no,
                "startAnchor": start_text,
                "endAnchor": end_text,
                "reason": "页边含题名或夹注，使用整页 OCR 确认正文边界；写入文字来自权威正文。",
            })
            return full_match
    if weak_match:
        weak_match.update({
            "kind": "body",
            "status": "双头锁边",
            "page": page_no,
            "startAnchor": start_text,
            "endAnchor": end_text,
            "reason": "本地权威来源未覆盖该页，采用已提供网页正文的双锁结果。",
        })
        return weak_match
    title_switch = False
    special_unresolved = None
    if FULL_OCR_FALLBACK_ENABLED:
        decision = early_decision or classify_page(job, reader, page_no, layout, full_text)
        if decision["kind"] == "ocr":
            special_unresolved = {
                "kind": "unresolved",
                "status": "来源内容未锁定",
                "page": page_no,
                "text": "",
                "confidence": 0,
                "sourceTitle": "",
                "startAnchor": start_text,
                "endAnchor": end_text,
                "reason": "该页疑似目录、卷题或其他特殊内容，但未在权威来源中严格锁定；若来源未收录，最终仅保留扫描底图。" if looks_like_directory else "该特殊页面未在权威来源中严格锁定，不使用 OCR 文字替代权威文字。",
            }
        if decision["kind"] == "blank":
            return {
                "kind": "blank",
                "status": "空白页",
                "page": page_no,
                "text": "",
                "confidence": 100,
                "sourceTitle": "",
                "reason": decision["reason"],
            }
        title_switch = bool(re.search(r"(?:卷第|卷[一二三四五六七八九十上下中]|[著撰])", full_text))
    has_authority = any(source_authority_rank(unit.kind) >= 40 for unit in load_source_units(job))
    if allow_discovery and (title_switch or not has_authority):
        discovered_match = match_page_source(job, start_text, end_text, allow_discovery=True)
        if discovered_match:
            discovered_match.update({
                "kind": "body",
                "status": "双头锁边",
                "page": page_no,
                "startAnchor": start_text,
                "endAnchor": end_text,
            })
            return discovered_match
    if special_unresolved:
        return special_unresolved
    return {
        "kind": "unresolved",
        "status": "未锁定",
        "page": page_no,
        "text": "",
        "confidence": 0,
        "sourceTitle": "",
        "reason": "正文页的页首、页尾尚未在同一可靠来源中锁定。",
        "startAnchor": start_text,
        "endAnchor": end_text,
    }


def source_text_by_page_anchors(job: dict, reader: PdfReader, page_no: int, layout: str, allow_estimate: bool = True) -> str:
    source_value = str(job.get("sourceText", "")).strip()
    if not source_value:
        return ""
    source_path = Path(source_value)
    if not source_path.exists():
        return ""
    source_text = read_job_source_text(job)
    source_norm, mapping = normalize_for_match(source_text)
    page_count = max(1, len(reader.pages))
    if not source_norm or not mapping:
        return ""

    approx = max(160, len(source_norm) // page_count)
    expected_start = max(0, (page_no - 1) * approx)
    window = max(approx * 10, 16000)
    anchor_text = page_anchor_text(job, reader, page_no, layout)
    page_norm, _ = normalize_for_match(anchor_text)
    start_anchor = anchor_from_text(anchor_text, "start")
    end_anchor = anchor_from_text(anchor_text, "end")
    start_found, _ = find_anchor_near(source_norm, start_anchor, expected_start, window)
    if start_found is None:
        start_found, _ = find_anchor_unique(source_norm, start_anchor, "start")
    if start_found is None:
        start_found, _ = find_anchor_fuzzy(source_norm, start_anchor, expected_start, window, "start")
    end_cursor = (start_found if start_found is not None else expected_start) + approx
    end_found, _ = find_end_anchor_near(source_norm, end_anchor, end_cursor, window)
    if end_found is None:
        end_found, _ = find_anchor_unique(source_norm, end_anchor, "end")
    if end_found is None:
        end_found, _ = find_anchor_fuzzy(source_norm, end_anchor, end_cursor, window, "end")

    if start_found is not None and end_found is not None and end_found > start_found + 20:
        return original_slice(source_text, mapping, start_found, end_found)
    if start_found is not None:
        if page_no < page_count:
            next_text = page_anchor_text(job, reader, page_no + 1, layout)
            next_anchor = anchor_from_text(next_text, "start")
            next_found, _ = find_anchor_near(source_norm, next_anchor, start_found + approx, window)
            if next_found is None:
                next_found, _ = find_anchor_unique(source_norm, next_anchor, "start")
            if next_found is None:
                next_found, _ = find_anchor_fuzzy(source_norm, next_anchor, start_found + approx, window, "start")
            if next_found is not None and next_found > start_found:
                return original_slice(source_text, mapping, start_found, next_found)
        if not allow_estimate:
            return ""
        return original_slice(source_text, mapping, start_found, min(len(source_norm) - 1, start_found + approx))
    if end_found is not None:
        if not allow_estimate:
            return ""
        return original_slice(source_text, mapping, max(0, end_found - approx), end_found)

    if not allow_estimate:
        return ""

    raw_approx = max(160, round(len(source_text) / page_count))
    pad = round(raw_approx * 0.15)
    start = max(0, (page_no - 1) * raw_approx - pad)
    end = min(len(source_text), start + raw_approx + pad * 2)
    return source_text[start:end]


def build_page_manifest(job: dict, reader: PdfReader) -> list[dict] | None:
    source_value = str(job.get("sourceText", "")).strip()
    if not source_value:
        return None
    source_path = Path(source_value)
    if not source_path.exists():
        return None
    source_text = read_job_source_text(job)
    source_norm, mapping = normalize_for_match(source_text)
    page_count = len(reader.pages)
    if not source_norm or not mapping:
        return None

    page_norms = []
    start_locks: list[int | None] = [None] * page_count
    end_locks: list[int | None] = [None] * page_count
    confidence: list[int] = [0] * page_count
    statuses: list[str] = ["连续估算"] * page_count
    approx_page = max(600, len(source_norm) // max(1, page_count))
    cursor = 0
    window = max(approx_page * 8, 16000)
    layout = job.get("layout", "auto")
    for page_index, page in enumerate(reader.pages):
        extracted = page_anchor_text(job, reader, page_index + 1, layout)
        page_norm, _ = normalize_for_match(extracted)
        page_norms.append(page_norm)
        start_anchor = anchor_from_text(extracted, "start")
        end_anchor = anchor_from_text(extracted, "end")
        start_found, start_score = find_anchor_near(source_norm, start_anchor, cursor, window)
        if start_found is None and FAST_MANIFEST_FUZZY_ENABLED:
            start_found, start_score = find_anchor_fuzzy(source_norm, start_anchor, cursor, window, "start")
        if start_found is not None and start_found < cursor - max(200, approx_page):
            start_found = None
            start_score = 0

        expected_end = (start_found if start_found is not None else cursor) + max(40, min(len(page_norm), approx_page))
        end_found, end_score = find_end_anchor_near(source_norm, end_anchor, expected_end, window)
        if end_found is None and FAST_MANIFEST_FUZZY_ENABLED:
            end_found, end_score = find_anchor_fuzzy(source_norm, end_anchor, expected_end, window, "end")
        if end_found is not None:
            lower_bound = (start_found if start_found is not None else cursor) + 20
            if end_found <= lower_bound or end_found < cursor:
                end_found = None
                end_score = 0

        if start_found is not None:
            start_locks[page_index] = start_found
        if end_found is not None:
            end_locks[page_index] = end_found

        if start_found is not None and end_found is not None:
            confidence[page_index] = min(98, round((start_score + end_score) / 2) + 8)
            statuses[page_index] = "双头锁边"
            cursor = end_found
        elif start_found is not None:
            confidence[page_index] = start_score
            statuses[page_index] = "页首锁边"
            cursor = start_found + max(1, min(len(page_norm), approx_page))
        elif end_found is not None:
            confidence[page_index] = end_score
            statuses[page_index] = "页尾锁边"
            cursor = end_found

    if not any(lock is not None for lock in [*start_locks, *end_locks]):
        return [
            {
                "page": index + 1,
                "text": segment,
                "status": "估算",
                "confidence": 35,
                "reason": "没有可用页首锚点，按全书长度平均分配。",
            }
            for index, segment in enumerate(source_segments(job, page_count) or [])
        ]

    boundaries: list[int] = [0] * (page_count + 1)
    known_boundaries = {}
    for index, lock in enumerate(start_locks):
        if lock is not None:
            known_boundaries[index] = lock
    for index, lock in enumerate(end_locks):
        if lock is not None and (index + 1 not in known_boundaries or lock > known_boundaries[index + 1]):
            known_boundaries[index + 1] = lock
    if 0 not in known_boundaries:
        first_index = min(known_boundaries) if known_boundaries else 0
        known_boundaries[0] = max(0, known_boundaries.get(first_index, 0) - approx_page * first_index)
    if page_count not in known_boundaries:
        known_boundaries[page_count] = len(source_norm) - 1
    known = sorted((index, pos) for index, pos in known_boundaries.items())
    for index, pos in known:
        boundaries[index] = max(0, min(len(source_norm) - 1, pos))

    for current, (left_index, left_pos) in enumerate(known):
        right = known[current + 1] if current + 1 < len(known) else None
        if right:
            right_index, right_pos = right
            gap_pages = right_index - left_index
            gap_chars = max(1, right_pos - left_pos)
            for offset in range(1, gap_pages):
                boundaries[left_index + offset] = left_pos + round(gap_chars * offset / gap_pages)

    manifest = []
    for page_index in range(page_count):
        start_norm = boundaries[page_index]
        end_norm = boundaries[page_index + 1] if page_index + 1 <= page_count else min(len(source_norm) - 1, start_norm + approx_page)
        if end_norm <= start_norm:
            end_norm = min(len(source_norm) - 1, start_norm + approx_page)
        status = statuses[page_index]
        reason = "" if status != "连续估算" else "由前后已锁定页面推定。"
        manifest.append({
            "page": page_index + 1,
            "text": original_slice(source_text, mapping, start_norm, end_norm),
            "status": status,
            "confidence": confidence[page_index] if status != "连续估算" else 55,
            "reason": reason,
            "sourceStart": start_norm,
            "sourceEnd": end_norm,
        })
    return manifest


PIPELINE_COMMIT_STATUSES = {"双头锁边", "跨章节双头锁边", "全文 OCR 边界校准"}


def strict_pair_committable(previous: dict, current: dict) -> bool:
    if previous.get("kind") != "body" or current.get("kind") != "body":
        return False
    if previous.get("status") not in PIPELINE_COMMIT_STATUSES or current.get("status") not in PIPELINE_COMMIT_STATUSES:
        return False
    if "globalRawEnd" in previous and "globalRawStart" in current:
        if int(previous["globalRawEnd"]) == int(current["globalRawStart"]):
            return True
        if item_source_key(previous) != item_source_key(current):
            return (
                "globalEnd" in previous
                and "globalStart" in current
                and int(previous["globalEnd"]) == int(current["globalStart"])
            )
        return False
    previous_key = item_source_key(previous)
    if not previous_key or previous_key != item_source_key(current):
        return False
    return "rawEnd" in previous and "rawStart" in current and int(previous["rawEnd"]) == int(current["rawStart"])


def build_legacy_strict_page_manifest(
    job: dict,
    reader: PdfReader,
    layout: str,
    status_job_id: str = "",
    commit_callback: Callable[[int, dict], None] | None = None,
) -> list[dict]:
    page_count = len(reader.pages)
    manifest = []
    source_cursors: dict[str, int] = {}
    units = load_source_units(job)
    alignment_windows = source_alignment_windows(units)
    unit_indexes = {(unit.url or unit.title): index for index, unit in enumerate(units)}
    unit_indexes.update({window.url: window.end_order for window in alignment_windows})
    active_unit = 0
    global_cursor = 0
    alignment_started = time.time()

    def process_page(page_no: int) -> None:
        nonlocal active_unit, global_cursor
        if units:
            left = max(0, active_unit - 1)
            right = min(len(units), active_unit + 4)
            candidates = [
                window for window in alignment_windows
                if window.start_order >= left and window.end_order < right
            ]
        else:
            candidates = None
        resolved = resolve_page(
            job,
            reader,
            page_no,
            layout,
            allow_discovery=not bool(units),
            candidate_units=candidates,
        )
        source_key = str(resolved.get("sourceUrl") or resolved.get("sourceTitle") or "")
        if resolved.get("kind") == "body" and source_key:
            resolved_start_unit = int(resolved.get("sourceStartOrder", unit_indexes.get(source_key, active_unit)))
            resolved_end_unit = int(resolved.get("sourceEndOrder", unit_indexes.get(source_key, active_unit)))
            if resolved_start_unit < active_unit:
                resolved = {
                    **resolved,
                    "kind": "unresolved",
                    "status": "章节顺序冲突",
                    "text": "",
                    "confidence": 0,
                    "reason": "该页会使 EPUB 章节顺序倒退，已拒绝写入。",
                }
            else:
                active_unit = max(active_unit, resolved_end_unit)
                global_start = resolved.get("globalStart")
                global_end = resolved.get("globalEnd")
                if global_start is not None and int(global_start) < global_cursor - 120:
                    resolved = {
                        **resolved,
                        "kind": "unresolved",
                        "status": "顺序冲突",
                        "text": "",
                        "confidence": 0,
                        "reason": "该页会使全书权威正文坐标倒退，已拒绝写入。",
                    }
                elif global_end is not None:
                    global_cursor = max(global_cursor, int(global_end))
                previous = source_cursors.get(source_key)
                start = int(resolved.get("start") or 0)
                end = int(resolved.get("end") or 0)
                if previous is not None and start < previous - 120:
                    resolved = {
                        **resolved,
                        "kind": "unresolved",
                        "status": "顺序冲突",
                        "text": "",
                        "confidence": 0,
                        "reason": "该页会使同一正文来源倒退，已拒绝写入。",
                    }
                else:
                    source_cursors[source_key] = max(previous or 0, end)
        manifest.append(resolved)
        if len(manifest) >= 2 and commit_callback:
            previous_item = manifest[-2]
            if strict_pair_committable(previous_item, resolved):
                commit_callback(page_no - 1, dict(previous_item))
        if status_job_id and (page_no == 1 or page_no % 25 == 0 or page_no == page_count):
            update_pipeline_stage(
                status_job_id,
                "align",
                "running",
                state="planning",
                processed=page_no,
                total=page_count,
                currentPage=page_no,
                detail=f"当前第 {page_no} 页",
                metrics=throughput_metrics(
                    alignment_started, page_no, page_count,
                    operation="权威正文检索与严格页界校验",
                ),
                message=f"OCR、严格对齐与已确认页面写入正在流水运行：已对齐 {page_no} / {page_count} 页。",
            )

    def process_contiguous_ready(contiguous_page: int) -> None:
        target = max(0, min(page_count, contiguous_page - 1))
        while len(manifest) < target:
            process_page(len(manifest) + 1)

    if status_job_id:
        update_pipeline_stage(
            status_job_id,
            "align",
            "running",
            state="planning",
            processed=0,
            total=page_count,
            detail="等待连续 OCR 页，保留一页反向确认窗口",
            message="OCR 与权威正文对齐已进入流水处理。",
        )
        if not precompute_anchor_cache(
            status_job_id,
            page_count,
            layout,
            first_page=1,
            ready_callback=process_contiguous_ready,
        ):
            raise TaskPaused()
    while len(manifest) < page_count:
        process_page(len(manifest) + 1)

    units_by_key = {window.url: window for window in alignment_windows}

    if FULL_OCR_FALLBACK_ENABLED:
        for index, item in enumerate(manifest):
            if item.get("kind") != "unresolved":
                continue
            page_no = index + 1
            full_text = ocr_page_text(job, page_no, layout, anchors_only=False)
            fallback = match_full_ocr_bounds(job, full_text)
            if not fallback:
                continue
            item.update({
                **fallback,
                "kind": "body",
                "status": "全文 OCR 边界校准",
                "page": page_no,
                "reason": "页首页尾 OCR 不足，仅用整页 OCR 确认边界；写入文字仍来自权威正文。",
            })

    boundary_conflicts = enforce_adjacent_page_boundaries(manifest)
    recovered = recover_unresolved_runs(job, reader, manifest, layout, units_by_key)
    chapter_recovered = recover_chapter_transition_runs(job, reader, manifest, layout, units, unit_indexes)
    boundary_conflicts += enforce_adjacent_page_boundaries(manifest)
    edge_rejected = enforce_recognized_page_edges(job, reader, manifest, layout, units_by_key)
    boundary_conflicts += enforce_adjacent_page_boundaries(manifest)

    for index, item in enumerate(manifest[:-1]):
        next_item = manifest[index + 1]
        if item.get("kind") != "body" or next_item.get("kind") != "body":
            continue
        if "globalEnd" in item and "globalStart" in next_item:
            end = int(item["globalEnd"])
            next_start = int(next_item["globalStart"])
        else:
            source_key = str(item.get("sourceUrl") or item.get("sourceTitle") or "")
            next_key = str(next_item.get("sourceUrl") or next_item.get("sourceTitle") or "")
            if not source_key or source_key != next_key:
                continue
            end = int(item.get("end") or 0)
            next_start = int(next_item.get("start") or 0)
        gap = next_start - end
        item["nextPageGap"] = gap
        if abs(gap) > 80:
            item["continuityWarning"] = "本页页尾与下一页页首存在未解释间隔，禁止用下一页边界覆盖本页。"
    mark_source_omitted_pages(job, reader, manifest, layout, units)
    source_omitted_ocr = fill_source_omitted_ocr(job, manifest, layout, status_job_id)
    if status_job_id:
        unresolved = sum(1 for item in manifest if item.get("kind") == "unresolved")
        locked = sum(1 for item in manifest if item.get("kind") == "body")
        omitted = sum(1 for item in manifest if item.get("kind") == "source-omitted")
        update_pipeline_stage(
            status_job_id,
            "align",
            "blocked" if unresolved else "done",
            state="planning",
            processed=page_count,
            total=page_count,
            detail=f"正文锁定 {locked} 页，恢复 {recovered + chapter_recovered} 页，待核对 {unresolved} 页",
            metrics={"locked": locked, "recovered": recovered + chapter_recovered, "chapterRecovered": chapter_recovered, "boundaryConflicts": boundary_conflicts, "edgeRejected": edge_rejected, "unresolved": unresolved},
        )
        update_pipeline_stage(
            status_job_id,
            "classify",
            "done",
            state="planning",
            processed=omitted,
            total=omitted,
            detail=f"来源未收录 {omitted} 页，保留扫描画面",
            metrics={"sourceOmitted": omitted, "sourceOmittedOcr": source_omitted_ocr},
        )
    return manifest


def page_start_needles(anchor_text: str) -> list[str]:
    """Return longest-first normalized prefixes from the physical page start."""
    normalized, _ = normalize_for_match(anchor_text)
    if len(normalized) < 6:
        return []
    sizes = [min(32, len(normalized)), 28, 24, 20, 18, 16, 14, 12, 10, 8, 6]
    return list(dict.fromkeys(normalized[:size] for size in sizes if len(normalized) >= size))


def unique_page_start_lock(
    units: list[SourceUnit],
    anchor_text: str,
    active_unit: int,
    cursor: int,
    unresolved_since_lock: int = 0,
) -> dict | None:
    """Find one monotonic page-start anchor in a bounded forward source region."""
    needles = page_start_needles(anchor_text)
    if not needles or not units:
        return None
    first_unit = max(0, min(active_unit, len(units) - 1))
    last_unit = min(len(units), first_unit + 4 + max(0, unresolved_since_lock))
    forward_window = min(24000, 6000 + max(0, unresolved_since_lock) * 2500)
    for needle in needles:
        hits = []
        for unit_index in range(first_unit, last_unit):
            unit = units[unit_index]
            source_norm, mapping = normalize_source_cached(unit.text)
            low = max(0, cursor + 1) if unit_index == first_unit else 0
            high = min(len(source_norm), low + forward_window)
            if high - low < len(needle):
                continue
            position = source_norm.find(needle, low, high)
            while position >= 0 and len(hits) < 2:
                hits.append((unit_index, position, mapping[position]))
                position = source_norm.find(needle, position + 1, high)
            if len(hits) >= 2:
                break
        if len(hits) == 1:
            unit_index, position, raw_position = hits[0]
            return {
                "unitIndex": unit_index,
                "start": position,
                "rawStart": raw_position,
                "needle": needle,
                "confidence": min(99, 76 + len(needle)),
            }
    return None


def unique_page_start_lock_anywhere(units: list[SourceUnit], anchor_text: str) -> dict | None:
    """Global one-off lookup for the user-selected trial page only."""
    needles = page_start_needles(anchor_text)
    for needle in needles:
        hits = []
        for unit_index, unit in enumerate(units):
            source_norm, mapping = normalize_source_cached(unit.text)
            position = source_norm.find(needle)
            while position >= 0 and len(hits) < 2:
                hits.append((unit_index, position, mapping[position]))
                position = source_norm.find(needle, position + 1)
            if len(hits) >= 2:
                break
        if len(hits) == 1:
            unit_index, position, raw_position = hits[0]
            return {
                "unitIndex": unit_index, "start": position, "rawStart": raw_position,
                "needle": needle, "confidence": min(99, 76 + len(needle)),
            }
    return None


def page_tail_crosscheck(source_slice: str, end_anchor: str) -> dict:
    """Check the current page tail without allowing it to change page bounds."""
    source_norm, _ = normalize_for_match(source_slice)
    anchor_norm, _ = normalize_for_match(end_anchor)
    if not source_norm or len(anchor_norm) < 6:
        return {"passed": False, "reason": "页尾 OCR 证据不足"}
    tail_region_start = max(0, len(source_norm) - 360)
    tail_region = source_norm[tail_region_start:]
    sizes = [min(24, len(anchor_norm)), 20, 18, 16, 14, 12, 10, 8, 6]
    for size in sizes:
        if len(anchor_norm) < size:
            continue
        needle = anchor_norm[-size:]
        positions = []
        cursor = 0
        while len(positions) < 2:
            found = tail_region.find(needle, cursor)
            if found < 0:
                break
            positions.append(found)
            cursor = found + 1
        if len(positions) == 1 and len(tail_region) - (positions[0] + size) <= 80:
            return {"passed": True, "needle": needle, "distanceFromEnd": len(tail_region) - (positions[0] + size)}
    return {"passed": False, "reason": "页尾 OCR 与次页页首划定的范围不一致"}


CHAPTER_HEADING_PATTERN = re.compile(
    r"^(?:第.{1,12}[卷章回篇]|卷[一二三四五六七八九十百千上下中0-9]+|[紀纪志傳传表書书].{0,12}第[一二三四五六七八九十百千0-9]+)"
)


def source_slice_has_internal_chapter_heading(source_slice: str) -> bool:
    """Reject pages that visibly mix an old chapter tail with a new heading/body."""
    lines = [re.sub(r"\s+", "", line) for line in source_slice.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if index == 0 or len(line) > 32:
            continue
        normalized, _ = normalize_for_match(line)
        if normalized and CHAPTER_HEADING_PATTERN.match(normalized):
            return True
    return False


def fill_non_authoritative_ocr(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    status_job_id: str = "",
) -> int:
    """After authoritative alignment, turn every remaining nonblank page into OCR."""
    candidates = [item for item in manifest if item.get("kind") != "body"]
    if not candidates:
        return 0
    by_page = {int(item["page"]): item for item in candidates}
    completed = 0
    started = time.time()
    worker_count = adaptive_ocr_workers(3)

    def apply_text(page_no: int, text: str) -> None:
        nonlocal completed
        item = by_page[page_no]
        decision = classify_page(job, reader, page_no, layout, text)
        if decision.get("kind") == "blank":
            item.update({
                "kind": "blank", "status": "空白页", "text": "", "confidence": 100,
                "textOrigin": "blank", "reason": decision.get("reason") or "空白页",
            })
        else:
            item.update({
                "kind": "ocr", "status": "整页 OCR", "text": text, "confidence": 70 if text.strip() else 0,
                "sourceTitle": "本页 OCR", "sourceUrl": "", "textOrigin": "page-ocr",
                "reason": "本页未进入权威文本层；整页 OCR 仅用于本页搜索，不参与任何权威页边界。",
            })
        completed += 1
        if status_job_id and (completed == 1 or completed % 5 == 0 or completed == len(candidates)):
            update_pipeline_stage(
                status_job_id, "classify", "running", state="planning",
                processed=completed, total=len(candidates), currentPage=page_no,
                detail=f"非权威页整页 OCR {completed} / {len(candidates)}",
                metrics=throughput_metrics(started, completed, len(candidates), workers=worker_count),
                message=f"权威页已经固定，正在处理纯 OCR 页 {completed} / {len(candidates)}。",
            )

    pending_pages = []
    for page_no in sorted(by_page):
        cache_path = full_ocr_cache_path(job, page_no, layout)
        if cache_path.exists():
            apply_text(page_no, ocr_page_text(job, page_no, layout, anchors_only=False))
            if status_job_id and read_full_status(status_job_id).get("pauseRequested"):
                update_pipeline_stage(
                    status_job_id, "classify", "paused", state="paused",
                    processed=completed, total=len(candidates), detail="已完成 OCR 和缓存均已保留",
                    pauseRequested=False,
                )
                raise TaskPaused()
        else:
            pending_pages.append(page_no)

    if len(pending_pages) == 1:
        page_no = pending_pages[0]
        apply_text(page_no, ocr_page_text(job, page_no, layout, anchors_only=False))
    elif pending_pages:
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
            page_queue = iter(pending_pages)
            pending: dict[concurrent.futures.Future, int] = {}

            def fill_queue() -> None:
                current_limit = adaptive_ocr_workers(worker_count)
                while len(pending) < current_limit:
                    try:
                        page_no = next(page_queue)
                    except StopIteration:
                        break
                    pending[pool.submit(full_page_ocr_worker, (str(job.get("id") or ""), page_no, layout))] = page_no

            fill_queue()
            while pending:
                done, _ = concurrent.futures.wait(tuple(pending), return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    expected_page = pending.pop(future)
                    page_no, text, error = future.result()
                    if error:
                        raise RuntimeError(f"第 {expected_page} 页整页 OCR 失败：{error}")
                    apply_text(page_no, text)
                if status_job_id and read_full_status(status_job_id).get("pauseRequested"):
                    for future in pending:
                        future.cancel()
                    update_pipeline_stage(
                        status_job_id, "classify", "paused", state="paused",
                        processed=completed, total=len(candidates), detail="已完成 OCR 和缓存均已保留",
                        pauseRequested=False,
                    )
                    raise TaskPaused()
                fill_queue()
    if status_job_id:
        update_pipeline_stage(
            status_job_id, "classify", "done", state="planning",
            processed=completed, total=len(candidates), detail=f"纯 OCR/空白页处理完成，共 {completed} 页",
        )
    return completed


def build_strict_page_manifest(
    job: dict,
    reader: PdfReader,
    layout: str,
    status_job_id: str = "",
    commit_callback: Callable[[int, dict], None] | None = None,
) -> list[dict]:
    """Build pages from current-page start to next-page start; OCR everything else."""
    page_count = len(reader.pages)
    units = load_source_units(job)
    if not units:
        raise ValueError("没有可用的校准文本，无法建立权威文字页。")
    if status_job_id:
        if not precompute_anchor_cache(status_job_id, page_count, layout, first_page=1):
            raise TaskPaused()

    locks: list[dict | None] = []
    anchors: list[tuple[str, str]] = []
    active_unit = 0
    cursor = -1
    unresolved_since_lock = 0
    align_started = time.time()
    for page_no in range(1, page_count + 1):
        start_anchor, end_anchor = page_anchor_pair(job, reader, page_no, layout)
        anchors.append((start_anchor, end_anchor))
        lock = unique_page_start_lock(units, start_anchor, active_unit, cursor, unresolved_since_lock)
        locks.append(lock)
        if lock:
            active_unit = int(lock["unitIndex"])
            cursor = int(lock["start"])
            unresolved_since_lock = 0
        else:
            unresolved_since_lock += 1
        if status_job_id and (page_no == 1 or page_no % 25 == 0 or page_no == page_count):
            update_pipeline_stage(
                status_job_id, "align", "running", state="planning",
                processed=page_no, total=page_count, currentPage=page_no,
                detail=f"页首单调定位 {page_no} / {page_count}",
                metrics=throughput_metrics(align_started, page_no, page_count),
                message=f"正在按全书顺序锁定页首 {page_no} / {page_count}。",
            )

    manifest = []
    for index in range(page_count):
        page_no = index + 1
        start_anchor, end_anchor = anchors[index]
        current = locks[index]
        following = locks[index + 1] if index + 1 < page_count else None
        base = {
            "page": page_no, "kind": "unresolved", "status": "未锁定", "text": "", "confidence": 0,
            "sourceTitle": "", "sourceUrl": "", "startAnchor": start_anchor, "endAnchor": end_anchor,
        }
        if index == page_count - 1:
            base["reason"] = "最后一页没有次页页首，按规则直接进入整页 OCR。"
            manifest.append(base)
            continue
        if not current or not following:
            base["reason"] = "本页页首或次页页首未能在校准文本的后续范围中唯一锁定。"
            manifest.append(base)
            continue
        current_unit_index = int(current["unitIndex"])
        following_unit_index = int(following["unitIndex"])
        unit = units[current_unit_index]
        start = int(current["start"])
        raw_start = int(current["rawStart"])
        if current_unit_index == following_unit_index:
            end = int(following["start"])
            raw_end = int(following["rawStart"])
        elif following_unit_index == current_unit_index + 1 and int(following["start"]) == 0:
            # A clean chapter break between physical pages: the current page
            # contains only the remainder of the old unit.
            end = len(normalize_source_cached(unit.text)[0])
            raw_end = len(unit.text)
        else:
            base["status"] = "章节混排"
            base["reason"] = "次页从新章节内部开始，说明本页混入章节标题或新章正文，改用本页 OCR。"
            manifest.append(base)
            continue
        if end <= start or raw_end <= raw_start:
            base["status"] = "顺序冲突"
            base["reason"] = "相邻页首页在校准文本中的顺序无效，改用本页 OCR。"
            manifest.append(base)
            continue
        page_text = unit.text[raw_start:raw_end].strip()
        if not page_text or source_slice_has_internal_chapter_heading(page_text):
            base["status"] = "章节混排"
            base["reason"] = "页内出现新章节标题或没有可写权威文字，改用本页 OCR。"
            manifest.append(base)
            continue
        tail = page_tail_crosscheck(page_text, end_anchor)
        row = {
            **base,
            "kind": "body", "status": "页首与次页页首锁边", "text": page_text,
            "confidence": min(int(current["confidence"]), int(following["confidence"])),
            "sourceTitle": unit.title, "sourceUrl": unit.url or unit.title, "sourceKind": unit.kind,
            "start": start, "end": end, "rawStart": raw_start, "rawEnd": raw_end,
            "startNeedle": current["needle"], "nextStartNeedle": following["needle"],
            "tailCrosscheck": tail,
            "reason": "本页起点由本页页首确定，终点由次页页首反推；本页页尾 OCR 仅作非阻断交叉验证。",
            "adjacentVerified": True,
        }
        manifest.append(row)
        if commit_callback:
            commit_callback(page_no, dict(row))

    fill_non_authoritative_ocr(job, reader, manifest, layout, status_job_id)
    if status_job_id:
        body_count = sum(1 for item in manifest if item.get("kind") == "body")
        ocr_count = sum(1 for item in manifest if item.get("kind") == "ocr")
        blank_count = sum(1 for item in manifest if item.get("kind") == "blank")
        update_pipeline_stage(
            status_job_id, "align", "done", state="planning",
            processed=page_count, total=page_count,
            detail=f"权威页 {body_count}，OCR 页 {ocr_count}，空白页 {blank_count}",
            metrics={"authoritative": body_count, "ocr": ocr_count, "blank": blank_count},
        )
    return manifest


def resolve_trial_page_by_next_start(
    job: dict, reader: PdfReader, page_no: int, layout: str
) -> dict:
    """Resolve one preview page with the same current-start/next-start contract."""
    page_count = len(reader.pages)
    if page_no >= page_count:
        text = ocr_page_text(job, page_no, layout, anchors_only=False)
        return {
            "page": page_no, "kind": "ocr", "status": "整页 OCR", "text": text,
            "confidence": 70 if text.strip() else 0, "sourceTitle": "本页 OCR",
            "reason": "最后一页按规则直接使用整页 OCR。",
        }
    units = load_source_units(job)
    start_anchor, end_anchor = page_anchor_pair(job, reader, page_no, layout)
    next_start_anchor, _ = page_anchor_pair(job, reader, page_no + 1, layout)
    current = unique_page_start_lock_anywhere(units, start_anchor)
    following = None
    if current:
        following = unique_page_start_lock(
            units, next_start_anchor, int(current["unitIndex"]), int(current["start"]), 0
        )
    if not current or not following:
        return {
            "page": page_no, "kind": "unresolved", "status": "未锁定", "text": "", "confidence": 0,
            "startAnchor": start_anchor, "endAnchor": end_anchor, "nextStartAnchor": next_start_anchor,
            "reason": "本页页首与次页页首没有在同一校准文本单元中唯一、顺序锁定。",
        }
    current_unit_index = int(current["unitIndex"])
    following_unit_index = int(following["unitIndex"])
    unit = units[current_unit_index]
    raw_start = int(current["rawStart"])
    start = int(current["start"])
    if current_unit_index == following_unit_index:
        raw_end = int(following["rawStart"])
        end = int(following["start"])
    elif following_unit_index == current_unit_index + 1 and int(following["start"]) == 0:
        raw_end = len(unit.text)
        end = len(normalize_source_cached(unit.text)[0])
    else:
        return {
            "page": page_no, "kind": "unresolved", "status": "章节混排", "text": "", "confidence": 0,
            "startAnchor": start_anchor, "endAnchor": end_anchor, "nextStartAnchor": next_start_anchor,
            "reason": "次页从新章节内部开始，本页应使用整页 OCR。",
        }
    text = unit.text[raw_start:raw_end].strip() if raw_end > raw_start else ""
    if not text or source_slice_has_internal_chapter_heading(text):
        return {
            "page": page_no, "kind": "unresolved", "status": "章节混排", "text": "", "confidence": 0,
            "startAnchor": start_anchor, "endAnchor": end_anchor, "nextStartAnchor": next_start_anchor,
            "reason": "本页跨章节或没有可写权威文本，应在整本任务中使用整页 OCR。",
        }
    return {
        "page": page_no, "kind": "body", "status": "页首与次页页首锁边", "text": text,
        "confidence": min(int(current["confidence"]), int(following["confidence"])),
        "sourceTitle": unit.title, "sourceUrl": unit.url or unit.title, "sourceKind": unit.kind,
        "start": start, "end": end, "rawStart": raw_start, "rawEnd": raw_end,
        "startAnchor": start_anchor, "endAnchor": end_anchor, "nextStartAnchor": next_start_anchor,
        "tailCrosscheck": page_tail_crosscheck(text, end_anchor),
        "reason": "本页页首确定起点，次页页首确定终点；页尾 OCR 仅作交叉验证。",
    }


def unresolved_runs(manifest: list[dict]) -> list[tuple[int, int]]:
    runs = []
    run_start = None
    for index, item in enumerate([*manifest, {"kind": "sentinel"}]):
        if item.get("kind") == "unresolved" and run_start is None:
            run_start = index
        elif item.get("kind") != "unresolved" and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    return runs


def recover_unresolved_runs(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    units_by_key: dict[str, SourceUnit],
) -> int:
    """Recover only unresolved runs whose page boundaries are fully anchored."""
    recovered = 0
    for start_index, end_index in unresolved_runs(manifest):
        if any(manifest[index].get("status") in STRICT_BOUNDARY_BLOCK_STATUSES for index in range(start_index, end_index + 1)):
            continue
        if start_index == 0 or end_index + 1 >= len(manifest):
            continue
        previous = manifest[start_index - 1]
        following = manifest[end_index + 1]
        previous_key = str(previous.get("sourceUrl") or previous.get("sourceTitle") or "")
        following_key = str(following.get("sourceUrl") or following.get("sourceTitle") or "")
        if previous.get("kind") != "body" or following.get("kind") != "body" or not previous_key or previous_key != following_key:
            continue
        unit = units_by_key.get(previous_key)
        if not unit:
            continue
        source_norm, mapping = normalize_source_cached(unit.text)
        start = int(previous.get("end") or 0)
        end = int(following.get("start") or 0)
        page_total = end_index - start_index + 1
        if not source_norm or not mapping or end <= start or end - start > max(5000, 1800 * page_total):
            continue

        boundaries: dict[int, int] = {0: start, page_total: end}
        usable = True
        for offset, page_no in enumerate(range(start_index + 1, end_index + 2)):
            hits = bounded_page_anchor_hits(job, reader, page_no, layout, unit, start, end)
            start_hits = [hit for hit in hits if hit[0] == "start"]
            end_hits = [hit for hit in hits if hit[0] == "end"]
            if start_hits and offset > 0:
                boundaries[offset] = min(hit[1] for hit in start_hits)
            if end_hits and offset + 1 < page_total:
                boundaries[offset + 1] = max(hit[2] for hit in end_hits)
            if not start_hits and not end_hits:
                usable = False
                break
        if not usable or any(index not in boundaries for index in range(page_total + 1)):
            continue
        ordered = [boundaries[index] for index in range(page_total + 1)]
        if any(right <= left for left, right in zip(ordered, ordered[1:])):
            continue

        page_slices = []
        for offset, page_no in enumerate(range(start_index + 1, end_index + 2)):
            start_anchor, end_anchor = page_anchor_pair(job, reader, page_no, layout)
            page_text = strict_page_text_from_edges(
                unit.text, mapping, ordered[offset], ordered[offset + 1], start_anchor, end_anchor
            )
            if page_text is None:
                page_slices = []
                break
            page_slices.append(page_text)
        if len(page_slices) != page_total:
            continue

        for offset, page_no in enumerate(range(start_index + 1, end_index + 2)):
            page_start = ordered[offset]
            page_end = ordered[offset + 1]
            manifest[page_no - 1].update({
                "kind": "body",
                "status": "多页缺口双锚恢复" if page_total > 1 else "相邻双锁约束",
                "sourceTitle": unit.title,
                "sourceUrl": unit.url,
                "sourceKind": unit.kind,
                "start": page_start,
                "end": page_end,
                "text": page_slices[offset],
                "confidence": max(75, min(int(previous.get("confidence") or 0), int(following.get("confidence") or 0)) - 5),
                "reason": "连续缺口由前后同一来源正文锚点夹定，且缺口内每页页边证据形成单调边界。",
                "adjacentVerified": True,
            })
            recovered += 1
    return recovered


def item_source_key(item: dict | None) -> str:
    return str((item or {}).get("sourceUrl") or (item or {}).get("sourceTitle") or "")


def chapter_title_score(unit: SourceUnit, anchor_text: str) -> int:
    anchor_norm, _ = normalize_for_match(anchor_text)
    if not anchor_norm:
        return 0
    title_norm, _ = normalize_for_match(unit.title)
    candidates = [title_norm]
    tail = re.split(r"[#/]", unit.title)[-1]
    tail_norm, _ = normalize_for_match(tail)
    candidates.append(tail_norm)
    for candidate in candidates:
        if len(candidate) >= 3 and candidate in anchor_norm:
            return 35
    return 0


def chapter_boundary_evidence(
    job: dict,
    reader: PdfReader,
    page_no: int,
    layout: str,
    unit: SourceUnit,
    start: int,
    end: int,
) -> tuple[int, list[tuple[str, int, int, int]]]:
    start_anchor, end_anchor = page_anchor_pair(job, reader, page_no, layout)
    hits = bounded_page_anchor_hits(job, reader, page_no, layout, unit, start, end)
    hit_score = max((hit[3] for hit in hits), default=0)
    score = 15
    if hits:
        score += 45 if len({hit[0] for hit in hits}) == 1 else 60
    score += chapter_title_score(unit, f"{start_anchor}\n{end_anchor}")
    return min(100, max(score, hit_score)), hits


def recover_chapter_side_pages(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    pages: list[int],
    unit: SourceUnit,
    source_start: int,
    source_end: int,
    status: str,
) -> int:
    page_total = len(pages)
    if not pages or source_end <= source_start or source_end - source_start > max(5000, 1800 * page_total):
        return 0
    source_norm, mapping = normalize_source_cached(unit.text)
    if not source_norm or not mapping:
        return 0
    boundaries: dict[int, int] = {0: source_start, page_total: source_end}
    page_scores = []
    for offset, page_no in enumerate(pages):
        score, hits = chapter_boundary_evidence(job, reader, page_no, layout, unit, source_start, source_end)
        page_scores.append(score)
        start_hits = [hit for hit in hits if hit[0] == "start"]
        end_hits = [hit for hit in hits if hit[0] == "end"]
        if start_hits and offset > 0:
            boundaries[offset] = min(hit[1] for hit in start_hits)
        if end_hits and offset + 1 < page_total:
            boundaries[offset + 1] = max(hit[2] for hit in end_hits)
    if min(page_scores, default=0) < 50:
        return 0
    if any(index not in boundaries for index in range(page_total + 1)):
        return 0
    ordered = [boundaries[index] for index in range(page_total + 1)]
    if any(right <= left for left, right in zip(ordered, ordered[1:])):
        return 0
    page_slices = []
    for offset, page_no in enumerate(pages):
        start_anchor, end_anchor = page_anchor_pair(job, reader, page_no, layout)
        page_text = strict_page_text_from_edges(
            unit.text, mapping, ordered[offset], ordered[offset + 1], start_anchor, end_anchor
        )
        if page_text is None:
            return 0
        page_slices.append(page_text)
    for offset, page_no in enumerate(pages):
        start = ordered[offset]
        end = ordered[offset + 1]
        manifest[page_no - 1].update({
            "kind": "body",
            "status": status,
            "sourceTitle": unit.title,
            "sourceUrl": unit.url,
            "sourceKind": unit.kind,
            "start": start,
            "end": end,
            "text": page_slices[offset],
            "confidence": max(78, min(94, page_scores[offset])),
            "reason": "章节过渡页由 EPUB 章节顺序、章节标题或页边证据共同约束，未发生章节回跳。",
            "adjacentVerified": True,
        })
    return len(pages)


def recover_chapter_transition_runs(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    units: list[SourceUnit],
    unit_indexes: dict[str, int],
) -> int:
    recovered = 0
    for start_index, end_index in unresolved_runs(manifest):
        if any(manifest[index].get("status") in STRICT_BOUNDARY_BLOCK_STATUSES for index in range(start_index, end_index + 1)):
            continue
        if start_index == 0 or end_index + 1 >= len(manifest):
            continue
        previous = manifest[start_index - 1]
        following = manifest[end_index + 1]
        previous_index = unit_indexes.get(item_source_key(previous))
        following_index = unit_indexes.get(item_source_key(following))
        if (
            previous.get("kind") != "body"
            or following.get("kind") != "body"
            or previous_index is None
            or following_index is None
            or following_index != previous_index + 1
        ):
            continue
        previous_unit = units[previous_index]
        following_unit = units[following_index]
        first_page = start_index + 1
        last_page = end_index + 1

        previous_start = int(previous.get("end") or 0)
        previous_end = len(normalize_source_cached(previous_unit.text)[0])
        previous_scores = [
            page_no for page_no in range(first_page, last_page + 1)
            if chapter_boundary_evidence(job, reader, page_no, layout, previous_unit, previous_start, previous_end)[0] >= 50
        ]
        previous_cluster = edge_evidence_cluster(previous_scores, first_page, last_page, "start")

        following_start = 0
        following_end = int(following.get("start") or 0)
        following_scores = [
            page_no for page_no in range(first_page, last_page + 1)
            if chapter_boundary_evidence(job, reader, page_no, layout, following_unit, following_start, following_end)[0] >= 50
        ]
        following_cluster = edge_evidence_cluster(following_scores, first_page, last_page, "end")

        previous_last = max(previous_cluster) if previous_cluster else first_page - 1
        following_first = min(following_cluster) if following_cluster else last_page + 1
        if previous_last >= following_first:
            continue
        if previous_cluster:
            recovered += recover_chapter_side_pages(
                job, reader, manifest, layout, previous_cluster, previous_unit,
                previous_start, previous_end, "章节过渡前章约束",
            )
        if following_cluster:
            recovered += recover_chapter_side_pages(
                job, reader, manifest, layout, following_cluster, following_unit,
                following_start, following_end, "章节过渡后章约束",
            )
    return recovered


def bounded_page_anchor_hits(
    job: dict,
    reader: PdfReader,
    page_no: int,
    layout: str,
    unit: SourceUnit,
    start: int,
    end: int,
) -> list[tuple[str, int, int, int]]:
    """Return exact page-edge evidence inside a small authoritative source range."""
    source_norm, _ = normalize_source_cached(unit.text)
    start = max(0, min(int(start), len(source_norm)))
    end = max(start, min(int(end), len(source_norm)))
    if end <= start:
        return []
    segment = source_norm[start:end]
    start_text, end_text = page_anchor_pair(job, reader, page_no, layout)
    hits = []
    for side, anchor_text in (("start", start_text), ("end", end_text)):
        position, score, needle = line_anchor_evidence(segment, anchor_text, side)
        if position is None or score < 44 or len(needle) < 6:
            continue
        hits.append((side, start + position, start + position + len(needle), score))
    return hits


def edge_evidence_cluster(evidence_pages: list[int], run_start: int, run_end: int, side: str) -> list[int]:
    """Keep only the evidence cluster touching a run edge, allowing one OCR-missed page."""
    pages = sorted(set(evidence_pages))
    if not pages:
        return []
    if side == "start":
        if pages[0] != run_start:
            return []
        edge_pages = [pages[0]]
        for page_no in pages[1:]:
            if page_no - edge_pages[-1] > 2:
                break
            edge_pages.append(page_no)
        return list(range(run_start, edge_pages[-1] + 1))
    if pages[-1] != run_end:
        return []
    edge_pages = [pages[-1]]
    for page_no in reversed(pages[:-1]):
        if edge_pages[-1] - page_no > 2:
            break
        edge_pages.append(page_no)
    return list(range(min(edge_pages), run_end + 1))


def set_boundary_body_page(item: dict, unit: SourceUnit, start: int, end: int) -> None:
    source_norm, mapping = normalize_source_cached(unit.text)
    start = max(0, min(int(start), len(source_norm)))
    end = max(start, min(int(end), len(source_norm)))
    if end <= start or not mapping:
        return
    item.update({
        "kind": "body",
        "status": "章节边界约束",
        "sourceTitle": unit.title,
        "sourceUrl": unit.url,
        "sourceKind": unit.kind,
        "start": start,
        "end": end,
        "text": original_slice(unit.text, mapping, start, end - 1),
        "confidence": 92,
        "reason": "本页位于权威正文的章节边界，页边证据与相邻已锁定页面共同唯一约束正文范围。",
        "adjacentVerified": True,
    })


def mark_source_omitted_pages(
    job: dict,
    reader: PdfReader,
    manifest: list[dict],
    layout: str,
    units: list[SourceUnit] | None = None,
) -> None:
    """Separate source-absent interleaves from unresolved authoritative body pages."""
    units = units if units is not None else load_source_units(job)
    if not units or not manifest:
        return
    unit_indexes = {(unit.url or unit.title): index for index, unit in enumerate(units)}
    runs = []
    run_start = None
    for index, item in enumerate([*manifest, {"kind": "sentinel"}]):
        if item.get("kind") == "unresolved" and run_start is None:
            run_start = index
        elif item.get("kind") != "unresolved" and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None

    for start_index, end_index in runs:
        if any(manifest[index].get("status") in STRICT_BOUNDARY_BLOCK_STATUSES for index in range(start_index, end_index + 1)):
            continue
        previous = manifest[start_index - 1] if start_index else None
        following = manifest[end_index + 1] if end_index + 1 < len(manifest) else None
        previous_key = str((previous or {}).get("sourceUrl") or (previous or {}).get("sourceTitle") or "")
        following_key = str((following or {}).get("sourceUrl") or (following or {}).get("sourceTitle") or "")
        previous_unit_index = unit_indexes.get(previous_key)
        following_unit_index = unit_indexes.get(following_key)

        is_leading = previous is None and following_unit_index is not None
        is_trailing = following is None and previous_unit_index is not None
        is_transition = (
            previous_unit_index is not None
            and following_unit_index is not None
            and following_unit_index == previous_unit_index + 1
        )
        if not (is_leading or is_trailing or is_transition):
            continue

        previous_accounted = is_leading
        following_accounted = is_trailing
        if previous_unit_index is not None:
            previous_norm, _ = normalize_source_cached(units[previous_unit_index].text)
            previous_accounted = int((previous or {}).get("end") or 0) == len(previous_norm)
        if following_unit_index is not None:
            following_accounted = int((following or {}).get("start") or 0) == 0
        if not previous_accounted or not following_accounted:
            continue

        for page_no in range(start_index + 1, end_index + 2):
            manifest[page_no - 1].update({
                "kind": "source-omitted",
                "status": "来源未收录",
                "text": "",
                "confidence": 100,
                "sourceTitle": "",
                "sourceUrl": "",
                "sourceAbsentVerified": True,
                "reason": "该页位于相邻权威正文边界之外，已确认来源 EPUB 未收录。",
            })


def fill_source_omitted_ocr(
    job: dict,
    manifest: list[dict],
    layout: str,
    status_job_id: str = "",
) -> int:
    """Use same-page OCR only after source absence has been proven by strict boundaries."""
    candidates = [item for item in manifest if item.get("kind") == "source-omitted" and item.get("sourceAbsentVerified")]
    if not candidates:
        return 0
    by_page = {int(item.get("page") or 0): item for item in candidates if int(item.get("page") or 0) > 0}
    completed = 0
    finished = 0
    cached = 0
    work_started = time.time()

    def apply_text(page_no: int, text: str) -> None:
        nonlocal completed, finished
        item = by_page[page_no]
        if text.strip():
            item.update({
                "status": "来源未收录·整页 OCR",
                "text": text,
                "confidence": 70,
                "sourceTitle": "本页 OCR",
                "textOrigin": "page-ocr",
                "reason": "权威来源已确认未收录该页；文字层使用本 PDF 页的整页 OCR，不参与权威正文对齐。",
            })
            completed += 1
        finished += 1

    pending_pages = []
    for page_no in sorted(by_page):
        cache_path = full_ocr_cache_path(job, page_no, layout)
        if cache_path.exists():
            apply_text(page_no, ocr_page_text(job, page_no, layout, anchors_only=False))
            cached += 1
        else:
            pending_pages.append(page_no)

    worker_count = adaptive_ocr_workers(3)

    def report(page_no: int, force: bool = False) -> None:
        if status_job_id and (force or finished == 1 or finished % 5 == 0 or finished == len(by_page)):
            update_pipeline_stage(
                status_job_id,
                "classify",
                "running",
                state="planning",
                processed=finished,
                total=len(by_page),
                currentPage=page_no,
                detail=f"来源未收录页 OCR {finished} / {len(by_page)}（{worker_count} 路）",
                metrics=throughput_metrics(
                    work_started, max(0, finished - cached), max(1, len(pending_pages)),
                    workers=worker_count, cachedPages=cached, newlyOcrPages=max(0, finished - cached),
                    renderDpi=160,
                ),
                message=f"正在为已确认来源未收录的页面写入本页 OCR：{finished} / {len(by_page)}。",
            )

    if cached:
        report(max(page for page in by_page if full_ocr_cache_path(job, page, layout).exists()), force=True)
    if len(pending_pages) == 1:
        page_no = pending_pages[0]
        text = ocr_page_text(job, page_no, layout, anchors_only=False)
        apply_text(page_no, text)
        report(page_no, force=True)
    elif pending_pages:
        with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(full_page_ocr_worker, (str(job.get("id") or ""), page_no, layout)): page_no
                for page_no in pending_pages
            }
            for future in concurrent.futures.as_completed(futures):
                page_no, text, error = future.result()
                if error:
                    for pending in futures:
                        pending.cancel()
                    raise RuntimeError(f"第 {page_no} 页整页 OCR 失败：{error}")
                apply_text(page_no, text)
                report(page_no)
                if status_job_id and read_full_status(status_job_id).get("pauseRequested"):
                    for pending in futures:
                        pending.cancel()
                    update_pipeline_stage(
                        status_job_id, "classify", "paused", state="paused",
                        processed=finished, total=len(by_page), detail="已完成结果和 OCR 缓存均已保留",
                        pauseRequested=False,
                    )
                    raise TaskPaused()
    return completed


def page_text_for_full(reader: PdfReader, job: dict, page_no: int, segments: list[str] | None, manifest: list[dict] | None = None) -> str:
    if manifest and page_no - 1 < len(manifest):
        return str(manifest[page_no - 1].get("text", ""))
    if segments:
        index = max(0, min(page_no - 1, len(segments) - 1))
        return segments[index]
    text = reader.pages[page_no - 1].extract_text() or ""
    return text


def overlay_for_page(page, text: str, blocks: list[dict], layout: str, image_size: tuple[int, int] | None) -> object:
    packet = io.BytesIO()
    width, height = float(page.mediabox.width), float(page.mediabox.height)
    overlay_canvas = canvas.Canvas(packet, pagesize=(width, height), pageCompression=1)
    draw_word_style_authoritative_text(overlay_canvas, text, width, height, layout)
    overlay_canvas.showPage()
    overlay_canvas.save()
    packet.seek(0)
    return PdfReader(packet).pages[0]


def read_full_status(job_id: str) -> dict:
    paths = job_paths(job_id)
    status_path = paths.root / "full-status.json"
    if status_path.exists():
        try:
            return json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "jobId": job_id,
        "state": "idle",
        "processed": 0,
        "total": 0,
        "message": "尚未开始整本任务。",
        "outputs": [],
    }


def completed_output_is_current(job_id: str, layout: str) -> bool:
    paths = job_paths(job_id)
    if not paths.meta.exists():
        return False
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    status = read_full_status(job_id)
    return bool(
        status.get("state") == "done"
        and status.get("engineVersion") == LAYOUT_ENGINE_VERSION
        and status.get("outputLayout") == selected_layout
        and (paths.root / "text-positioned-full.pdf").is_file()
    )


def write_full_status(job_id: str, **updates) -> dict:
    with status_lock(job_id):
        paths = job_paths(job_id)
        current = read_full_status(job_id)
        current.update(updates)
        current["updatedAt"] = time.time()
        atomic_write_json(paths.root / "full-status.json", current)
        return current


def new_pipeline() -> list[dict]:
    return [
        {"id": stage_id, "label": label, "state": "pending", "processed": 0, "total": 0, "detail": ""}
        for stage_id, label in PIPELINE_STAGES
    ]


def ensure_pipeline(status: dict) -> dict:
    if status.get("pipeline"):
        return status
    pipeline = new_pipeline()
    total = int(status.get("total") or 0)
    alignment = status.get("alignment") or {}
    if total:
        pipeline[0].update(state="done", processed=1, total=1, detail="PDF 与权威来源已载入")
        pipeline[1].update(state="done", processed=total, total=total, detail="页边 OCR 缓存可复用")
        unresolved = int(alignment.get("reviewRequired") or alignment.get("unresolved") or 0)
        locked = int(alignment.get("matched") or 0) + int(alignment.get("constrained") or 0)
        pipeline[2].update(
            state="blocked" if unresolved else "done",
            processed=total,
            total=total,
            detail=f"权威页 {locked} 页，仍有 {unresolved} 页待处理" if unresolved else f"权威页 {locked} 页",
        )
        ocr_pages = int(alignment.get("ocr") or 0)
        pipeline[3].update(state="done", processed=ocr_pages, total=ocr_pages, detail=f"纯 OCR 页 {ocr_pages} 页")
        if status.get("state") == "done":
            for stage in pipeline[4:]:
                stage.update(state="done")
    status["pipeline"] = pipeline
    return status


def update_pipeline_stage(job_id: str, stage_id: str, stage_state: str, **updates) -> dict:
    with status_lock(job_id):
        current = ensure_pipeline(read_full_status(job_id))
        pipeline = current.get("pipeline") or new_pipeline()
        stage = next((item for item in pipeline if item.get("id") == stage_id), None)
        if stage is None:
            raise ValueError(f"未知后台阶段：{stage_id}")
        now = time.time()
        previous_stage_state = str(stage.get("state") or "")
        if stage_state == "running" and previous_stage_state != "running":
            stage["startedAt"] = now
            stage.pop("endedAt", None)
            stage.pop("elapsedSeconds", None)
        elif stage_state == "running" and not stage.get("startedAt"):
            stage["startedAt"] = now
        if stage_state in {"done", "blocked", "error", "paused"}:
            if previous_stage_state != "running":
                stage["startedAt"] = now
            else:
                stage.setdefault("startedAt", now)
            stage["endedAt"] = now
        stage["state"] = stage_state
        for key in ("processed", "total"):
            if key in updates:
                stage[key] = updates[key]
        for key in ("detail", "metrics"):
            if key in updates:
                stage[key] = updates.pop(key)
        started = float(stage.get("startedAt") or 0)
        ended = float(stage.get("endedAt") or now)
        if started:
            stage["elapsedSeconds"] = max(0, round(ended - started, 1))
        updates["pipeline"] = pipeline
        updates["activeStage"] = stage_id
        return write_full_status(job_id, **updates)


def make_output_link(job_id: str, path: Path, name: str, detail: str) -> dict:
    return {
        "name": name,
        "path": str(path),
        "relative": path.resolve().relative_to(job_paths(job_id).root.resolve()).as_posix(),
        "detail": detail,
    }


def cleanup_trial_outputs(job_id: str, keep_page: int | None = None) -> dict:
    paths = job_paths(job_id)
    removed = 0
    if not paths.root.exists():
        return {"removed": 0}
    keep_patterns = set()
    if keep_page is not None:
        keep_patterns = {
            f"page-{keep_page:04d}-guides.png",
            f"page-{keep_page:04d}-trial.pdf",
        }
    for pattern in (
        "page-*-guides.png",
        "page-*-trial.pdf",
        "page-*-trial-report.csv",
        ".render-page-*.png",
        ".text-positioned-full.building.pdf",
        ".*.tmp",
    ):
        for path in paths.root.glob(pattern):
            if path.name in keep_patterns:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return {"removed": removed}


def cleanup_job_cache(job_id: str, keep_final: bool = True, remove_inputs: bool = False) -> dict:
    paths = job_paths(job_id)
    removed = 0
    if not paths.root.exists():
        return {"removed": 0}
    removed += cleanup_trial_outputs(job_id).get("removed", 0)
    page_dir = paths.root / "pages"
    if page_dir.exists():
        shutil.rmtree(page_dir)
        removed += 1
    for name in ("full-processing-report.csv", "full-processing-plan.csv"):
        path = paths.root / name
        if path.exists():
            path.unlink()
            removed += 1
    if not keep_final:
        final_pdf = paths.root / "text-positioned-full.pdf"
        if final_pdf.exists():
            final_pdf.unlink()
            removed += 1
    if remove_inputs:
        for path in (paths.pdf, paths.source):
            if path.exists():
                path.unlink()
                removed += 1
    return {"removed": removed}


def cleanup_old_cache(days: int = 7) -> dict:
    if not JOBS_DIR.exists():
        return {"removed": 0}
    cutoff = time.time() - days * 24 * 60 * 60
    removed = 0
    for job_root in JOBS_DIR.iterdir():
        if not job_root.is_dir():
            continue
        try:
            if job_root.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        job_id = job_id_from_root(job_root)
        if not job_id:
            continue
        removed += cleanup_job_cache(job_id, keep_final=True).get("removed", 0)
    return {"removed": removed}


def manifest_summary(manifest: list[dict] | None, page_count: int) -> dict:
    if not manifest:
        return {"matched": 0, "constrained": 0, "ocr": 0, "blank": 0, "sourceOmitted": 0, "sourceOmittedOcr": 0, "unresolved": page_count, "estimated": 0, "warnings": 0, "boundaryReview": 0, "reviewRequired": page_count, "averageConfidence": 0}
    matched_statuses = {"页首与次页页首锁边", "双头锁边", "跨章节双头锁边", "全文 OCR 边界校准"}
    constrained_statuses = {
        "相邻双锁约束", "章节边界约束", "多页缺口双锚恢复",
        "章节过渡前章约束", "章节过渡后章约束", "页首锁边", "页尾锁边",
    }
    matched = sum(1 for item in manifest if item.get("kind") == "body" and item.get("status") in matched_statuses)
    constrained = sum(1 for item in manifest if item.get("kind") == "body" and item.get("status") in constrained_statuses)
    ocr_pages = sum(1 for item in manifest if item.get("kind") == "ocr")
    blank_pages = sum(1 for item in manifest if item.get("kind") == "blank")
    source_omitted = sum(1 for item in manifest if item.get("kind") == "source-omitted")
    source_omitted_ocr = sum(
        1 for item in manifest
        if item.get("kind") == "source-omitted" and item.get("textOrigin") == "page-ocr"
    )
    unresolved = sum(1 for item in manifest if item.get("kind") == "unresolved")
    estimated = sum(1 for item in manifest if item.get("status") in {"连续估算", "估算"})
    warnings = sum(1 for item in manifest if item.get("continuityWarning"))
    boundary_review = sum(1 for item in manifest if item.get("status") in {"双锁连续去重", "双锁连续补首"})
    scores = [int(item.get("confidence") or 0) for item in manifest]
    average = round(sum(scores) / max(1, len(scores)))
    return {
        "matched": matched,
        "constrained": constrained,
        "ocr": ocr_pages,
        "blank": blank_pages,
        "sourceOmitted": source_omitted,
        "sourceOmittedOcr": source_omitted_ocr,
        "unresolved": unresolved,
        "estimated": estimated,
        "warnings": warnings,
        "boundaryReview": boundary_review,
        "reviewRequired": unresolved + estimated + boundary_review,
        "averageConfidence": average,
    }


def alignment_review_count(alignment: dict | None) -> int:
    alignment = alignment or {}
    return int(alignment.get("reviewRequired") or alignment.get("unresolved") or 0)


def alignment_locked_count(alignment: dict | None) -> int:
    alignment = alignment or {}
    return int(alignment.get("matched") or 0) + int(alignment.get("constrained") or 0)


def alignment_quality_regressed(current: dict, previous: dict | None, page_count: int) -> bool:
    """Refuse to silently replace a materially better page alignment result."""
    if not previous:
        return False
    previous_review = alignment_review_count(previous)
    current_review = alignment_review_count(current)
    previous_locked = alignment_locked_count(previous)
    current_locked = alignment_locked_count(current)
    if previous_review <= 0 and previous_locked <= 0:
        return False
    review_slack = max(50, round(page_count * 0.05))
    locked_slack = max(50, round(page_count * 0.03))
    return (
        current_review > previous_review + review_slack
        and current_locked < previous_locked - locked_slack
    )


def write_alignment_issues(job_id: str, manifest: list[dict]) -> Path:
    report = job_paths(job_id).root / "alignment-issues.csv"
    rows = [
        item for item in manifest
        if item.get("kind") in {"unresolved", "ocr"}
        or item.get("status") in {"双锁连续去重", "双锁连续补首"}
        or item.get("status") in {"连续估算", "估算"}
        or item.get("continuityWarning")
    ]
    temporary = report.with_name(f".{report.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=(
                "page", "kind", "status", "confidence", "reason", "continuityWarning", "startAnchor", "endAnchor"
            ))
            writer.writeheader()
            for item in rows:
                writer.writerow({key: item.get(key, "") for key in writer.fieldnames})
        os.replace(temporary, report)
    finally:
        temporary.unlink(missing_ok=True)
    return report


def build_fast_authoritative_manifest(job: dict, reader: PdfReader, layout: str, status_job_id: str = "") -> list[dict]:
    page_count = len(reader.pages)
    if status_job_id:
        if not precompute_anchor_cache(status_job_id, page_count, layout, first_page=1):
            raise TaskPaused()
        write_full_status(
            status_job_id,
            state="planning",
            processed=0,
            total=page_count,
            message="正在用已缓存锁页结果快速生成全书文字范围。",
        )
    raw_manifest = build_page_manifest(job, reader)
    if not raw_manifest:
        raise ValueError("没有可用的权威正文页范围，请重新检查来源文本。")
    units = load_source_units(job)
    source_title = units[0].title if units else source_title_from_url(str(job.get("sourceOriginal") or "")) or "参考正文"
    source_url = units[0].url if units else str(job.get("sourceOriginal") or "")
    manifest = []
    for index, item in enumerate(raw_manifest):
        confidence = int(item.get("confidence") or 0)
        text = str(item.get("text") or "")
        reliable = bool(text.strip()) and confidence >= 50
        status = str(item.get("status") or "连续估算")
        row = {
            **item,
            "page": index + 1,
            "kind": "body" if reliable else "unresolved",
            "status": status if reliable else "未锁定",
            "text": text if reliable else "",
            "confidence": confidence,
            "sourceTitle": source_title if reliable else "",
            "sourceUrl": source_url if reliable else "",
            "reason": item.get("reason") or ("由全书已缓存锁页结果顺序推定。" if reliable else "置信度过低，未写入文本层。"),
        }
        manifest.append(row)
        if status_job_id and ((index + 1) == 1 or (index + 1) % 100 == 0 or (index + 1) == page_count):
            write_full_status(
                status_job_id,
                state="planning",
                processed=index + 1,
                total=page_count,
                currentPage=index + 1,
                message=f"正在快速生成页文本范围 {index + 1} / {page_count}。",
            )
    return manifest


def validate_page_text_layer(pdf_path: Path, expected_text: str) -> int:
    reader = PdfReader(str(pdf_path), strict=False)
    if len(reader.pages) != 1:
        raise ValueError("中间页文件页数异常，已停止整本输出。")
    extracted = reader.pages[0].extract_text() or ""
    extracted_norm = canonical_output_text(extracted)
    expected_norm = expected_text_layer_norm(expected_text)
    if extracted_norm != expected_norm:
        raise ValueError("中间页文字层与锁定正文不一致，已停止整本输出。")
    if re.search(r"(?:U\+[0-9A-Fa-f]{4,6}){2,}", extracted):
        raise ValueError("中间页文字层出现编码串，已停止整本输出。")
    return len(extracted_norm)


def page_layer_signature(row: dict, layout: str) -> str:
    payload = {
        "engineVersion": LAYOUT_ENGINE_VERSION,
        "layout": layout,
        "page": int(row.get("page") or 0),
        "kind": str(row.get("kind") or ""),
        "text": str(row.get("text") or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def page_layer_cache_valid(page_out: Path, signature_path: Path, signature: str, expected_text: str) -> bool:
    if not page_out.is_file() or not signature_path.is_file():
        return False
    try:
        if signature_path.read_text(encoding="ascii").strip() != signature:
            return False
        validate_page_text_layer(page_out, expected_text)
        return True
    except Exception:
        return False


def write_page_layer(
    reader: PdfReader,
    page_no: int,
    row: dict,
    layout: str,
    page_out: Path,
) -> bool:
    signature = page_layer_signature(row, layout)
    signature_path = page_out.with_suffix(".sha256")
    text = str(row.get("text") or "")
    if page_layer_cache_valid(page_out, signature_path, signature, text):
        return False
    writer = PdfWriter()
    writer.add_page(reader.pages[page_no - 1])
    page = writer.pages[0]
    image_size = None
    blocks = []
    if layout != "horizontal":
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        blocks, image_size = stable_vertical_blocks(width, height, layout)
    remove_text(page, writer)
    overlay = overlay_for_page(page, text, blocks, layout, image_size)
    page.merge_page(overlay)
    write_pdf_atomic(writer, page_out)
    validate_page_text_layer(page_out, text)
    atomic_write_text(signature_path, signature)
    return True


def build_review_pdf(job_id: str, layout: str = "auto") -> dict:
    """Build a clearly labeled inspection draft without relaxing the release gate."""
    paths = job_paths(job_id)
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    manifest_path = paths.root / "page-text-manifest.json"
    if not manifest_path.exists():
        raise ValueError("尚未生成逐页核对清单，无法制作核对预览。")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = payload.get("pages") if isinstance(payload, dict) else payload
    if not isinstance(manifest, list):
        raise ValueError("逐页核对清单格式无效。")
    reader = PdfReader(str(paths.pdf), strict=False)
    page_count = len(reader.pages)
    if len(manifest) != page_count:
        raise ValueError("核对清单页数与扫描 PDF 不一致。")

    review_dir = paths.root / "review-pages"
    review_dir.mkdir(parents=True, exist_ok=True)
    draft_path = paths.root / f"{book_package_slug(str(job.get('pdfOriginal') or 'book'))}-review-draft.pdf"
    confirmed = 0
    page_files = []
    for page_no, row in enumerate(manifest, start=1):
        page_out = review_dir / f"page-{page_no:05d}.pdf"
        scan_marker = page_out.with_suffix(".scan-only")
        text = str(row.get("text") or "")
        if row.get("kind") != "unresolved" and text:
            write_page_layer(reader, page_no, row, selected_layout, page_out)
            scan_marker.unlink(missing_ok=True)
            confirmed += 1
        elif not page_out.exists() or not scan_marker.exists():
            writer = PdfWriter()
            writer.add_page(reader.pages[page_no - 1])
            remove_text(writer.pages[0], writer)
            write_pdf_atomic(writer, page_out)
            page_out.with_suffix(".sha256").unlink(missing_ok=True)
            atomic_write_text(scan_marker, "scan-only-v1")
        page_files.append(page_out)
        if page_no == 1 or page_no % 10 == 0 or page_no == page_count:
            status = read_full_status(job_id)
            pipeline = status.get("pipeline") or []
            review_stage = next((item for item in pipeline if item.get("id") == "review"), None)
            if review_stage:
                review_stage.update(
                    state="running",
                    processed=page_no,
                    total=page_count,
                    detail=f"已整理 {page_no} / {page_count} 页核对预览",
                )
            write_full_status(
                job_id,
                state="reviewing",
                activeStage="review",
                processed=page_no,
                total=page_count,
                message=f"正在生成核对预览 {page_no} / {page_count}。",
                pipeline=pipeline,
            )
    assemble_page_pdfs(page_files, draft_path)
    if len(PdfReader(str(draft_path), strict=False).pages) != page_count:
        raise ValueError("核对预览页数异常，已停止输出。")

    status = read_full_status(job_id)
    pipeline = status.get("pipeline") or []
    review_stage = next((item for item in pipeline if item.get("id") == "review"), None)
    if review_stage:
        review_stage.update(
            state="done",
            processed=page_count,
            total=page_count,
            detail=f"核对预览完成；含 {confirmed} 页已确认文字层",
        )
    issue_report = paths.root / "alignment-issues.csv"
    outputs = []
    if issue_report.exists():
        outputs.append(make_output_link(job_id, issue_report, "待核对页清单", "CSV"))
    outputs.append(make_output_link(job_id, draft_path, "整本核对预览（非正式成品）", "PDF · 未确认页仅保留扫描图"))
    return write_full_status(
        job_id,
        state="error",
        activeStage="align",
        processed=page_count,
        total=page_count,
        message=f"核对预览已生成：{confirmed} 页含已确认文字层，其余页面仅保留扫描图；正式发布门禁仍未通过。",
        outputs=outputs,
        pipeline=pipeline,
    )


def validate_full_output(source_pdf: Path, output_pdf: Path, manifest: list[dict], status_job_id: str = "") -> dict:
    source_reader = PdfReader(str(source_pdf), strict=False)
    output_reader = PdfReader(str(output_pdf), strict=False)
    page_count = len(source_reader.pages)
    if len(output_reader.pages) != page_count or len(manifest) != page_count:
        raise ValueError("整本输出页数与源 PDF 不一致，已停止发布。")
    if status_job_id:
        update_pipeline_stage(
            status_job_id, "text-check", "running", state="running",
            processed=0, total=page_count, detail="逐页核对复制文字与权威正文",
            message="整本已合并，正在验证连续搜索和复制文字。",
        )
    extracted_chars = 0
    extracted_stream = hashlib.sha256()
    expected_stream = hashlib.sha256()
    for index, row in enumerate(manifest):
        extracted = output_reader.pages[index].extract_text() or ""
        extracted_norm = canonical_output_text(extracted)
        expected_norm = expected_text_layer_norm(str(row.get("text") or ""))
        if extracted_norm != expected_norm:
            raise ValueError(f"第 {index + 1} 页复制文字与锁定正文不一致，已停止发布。")
        if re.search(r"(?:U\+[0-9A-Fa-f]{4,6}){2,}", extracted):
            raise ValueError(f"第 {index + 1} 页出现编码串，已停止发布。")
        extracted_stream.update(extracted_norm.encode("utf-8"))
        expected_stream.update(expected_norm.encode("utf-8"))
        extracted_chars += len(extracted_norm)
        if status_job_id and ((index + 1) % 50 == 0 or index + 1 == page_count):
            update_pipeline_stage(
                status_job_id, "text-check", "running", state="running",
                processed=index + 1, total=page_count, detail=f"已验证第 {index + 1} 页",
            )

    continuous_text_hash = extracted_stream.hexdigest()
    if continuous_text_hash != expected_stream.hexdigest():
        raise ValueError("整本连续文字流与逐页权威正文顺序不一致，已停止发布。")
    if status_job_id:
        update_pipeline_stage(
            status_job_id, "text-check", "done", state="running",
            processed=page_count, total=page_count, detail=f"文字层一致，共 {extracted_chars} 个检索字符",
            metrics={"extractedChars": extracted_chars, "continuousTextHash": continuous_text_hash},
        )

    visual_pages = set(sample_pages(page_count))
    for index in range(1, page_count):
        if manifest[index - 1].get("kind") != manifest[index].get("kind"):
            visual_pages.update((index, index + 1))
    checked = 0
    pages_to_check = sorted(visual_pages)[:12]
    if status_job_id:
        update_pipeline_stage(
            status_job_id, "visual-check", "running", state="running",
            processed=0, total=len(pages_to_check), detail="抽样进行像素级扫描画面对比",
            message="文字层验证通过，正在确认扫描画面没有变化。",
        )
    for page_no in pages_to_check:
        source_image = render_page_image(source_pdf, page_no, dpi=72)
        output_image = render_page_image(output_pdf, page_no, dpi=72)
        if source_image.size != output_image.size or ImageChops.difference(source_image, output_image).getbbox() is not None:
            raise ValueError(f"第 {page_no} 页扫描画面发生变化，已停止发布。")
        checked += 1
        if status_job_id:
            update_pipeline_stage(
                status_job_id, "visual-check", "running", state="running",
                processed=checked, total=len(pages_to_check), detail=f"已核验扫描页 {page_no}",
            )
    if status_job_id:
        update_pipeline_stage(
            status_job_id, "visual-check", "done", state="running",
            processed=checked, total=len(pages_to_check), detail=f"{checked} 个关键页像素一致",
            metrics={"pixelCheckedPages": checked},
        )
    return {
        "pages": page_count,
        "extractedChars": extracted_chars,
        "continuousTextHash": continuous_text_hash,
        "pixelCheckedPages": checked,
    }


def build_full_pdf(job_id: str, layout: str, stop_after: int | None = None) -> dict:
    paths = job_paths(job_id)
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    pdf_path = Path(job["pdf"])
    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    reader = PdfReader(str(pdf_path), strict=False)
    page_count = len(reader.pages)
    page_dir = paths.root / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    final_pdf = paths.root / "text-positioned-full.pdf"
    stale_pdf = paths.root / ".text-positioned-full.stale.pdf"
    building_pdf = paths.root / ".text-positioned-full.building.pdf"
    manifest_path = paths.root / "page-text-manifest.json"
    if final_pdf.exists():
        stale_pdf.unlink(missing_ok=True)
        os.replace(final_pdf, stale_pdf)
    update_pipeline_stage(
        job_id, "input", "done", state="planning",
        processed=1, total=1, detail=f"PDF {page_count} 页，EPUB {int(job.get('sourceUnitCount') or 0)} 个内容单元",
        message="PDF 与权威来源检查完成。",
    )
    ensure_text_font()
    streamed_pages: set[int] = set()
    verified_pages: set[int] = set()
    update_pipeline_stage(
        job_id,
        "layer",
        "waiting",
        state="planning",
        processed=0,
        total=page_count,
        detail="等待相邻页反向确认与全书边界恢复",
        metrics={"streamedPages": 0, "waitingForFinalAlignment": True},
    )

    def commit_stream_page(page_no: int, row: dict) -> None:
        page_out = page_dir / f"page-{page_no:05d}.pdf"
        write_page_layer(reader, page_no, row, selected_layout, page_out)
        streamed_pages.add(page_no)
        verified_pages.add(page_no)
        if len(streamed_pages) == 1 or len(streamed_pages) % 10 == 0:
            update_pipeline_stage(
                job_id,
                "layer",
                "waiting",
                state="planning",
                processed=len(streamed_pages),
                total=page_count,
                currentPage=page_no,
                detail=f"已安全预写 {len(streamed_pages)} 页；其余页面等待全书边界恢复",
                metrics={"streamedPages": len(streamed_pages), "waitingForFinalAlignment": True},
                message="OCR、严格对齐和已确认页面写入正在并行推进。",
            )

    manifest = None
    manifest_reused = False
    cached_payload = None
    cached_pages = None
    cached_summary = None
    previous_status = read_full_status(job_id)
    previous_alignment = previous_status.get("previousAlignment") or previous_status.get("alignment") or {}
    if manifest_path.exists():
        try:
            cached_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            cached_pages = cached_payload.get("pages") if isinstance(cached_payload, dict) else None
            if (
                isinstance(cached_pages, list)
                and len(cached_pages) == page_count
                and cached_payload.get("layout") == selected_layout
                and cached_payload.get("inputFingerprint") == job.get("inputFingerprint")
            ):
                cached_summary = manifest_summary(cached_pages, page_count)
            if (
                isinstance(cached_pages, list)
                and len(cached_pages) == page_count
                and cached_payload.get("engineVersion") == LAYOUT_ENGINE_VERSION
                and cached_payload.get("layout") == selected_layout
                and cached_payload.get("inputFingerprint") == job.get("inputFingerprint")
            ):
                manifest = cached_pages
                manifest_reused = True
        except (OSError, json.JSONDecodeError):
            pass
    if manifest is None:
        # There is only one production alignment path. Estimated/fast manifests
        # are intentionally never used for authoritative page text.
        manifest = build_strict_page_manifest(
            job,
            reader,
            selected_layout,
            status_job_id=job_id,
            commit_callback=commit_stream_page,
        )
    for row in manifest:
        if row.get("kind") != "body" or not row.get("text"):
            continue
        row["text"] = str(row["text"])
    alignment = manifest_summary(manifest, page_count)
    regression_reference = cached_summary if alignment_quality_regressed(alignment, cached_summary, page_count) else None
    if not regression_reference and alignment_quality_regressed(alignment, previous_alignment, page_count):
        regression_reference = previous_alignment
    if not manifest_reused and regression_reference:
        candidate_path = paths.root / "page-text-manifest-rejected-regression.json"
        atomic_write_json(candidate_path, {
            "engineVersion": LAYOUT_ENGINE_VERSION,
            "inputFingerprint": job.get("inputFingerprint", ""),
            "layout": selected_layout,
            "pages": manifest,
            "alignment": alignment,
            "previousAlignment": regression_reference,
            "rejectedReason": "alignment-quality-regression",
        })
        if cached_payload is not None:
            atomic_write_json(manifest_path, cached_payload)
        write_full_status(
            job_id,
            state="error",
            activeStage="align",
            processed=page_count,
            total=page_count,
            message=(
                "新一轮严格核对结果明显差于已保存结果，已阻止覆盖正式清单。"
                f" 当前待核对 {alignment_review_count(alignment)} 页，上一结果待核对 {alignment_review_count(regression_reference)} 页。"
            ),
            alignment=regression_reference,
            validation={"qualityRegressionBlocked": True, "candidateManifest": candidate_path.name},
            outputs=[],
        )
        return
    atomic_write_json(manifest_path, {
        "engineVersion": LAYOUT_ENGINE_VERSION,
        "inputFingerprint": job.get("inputFingerprint", ""),
        "layout": selected_layout,
        "pages": manifest,
    })
    if manifest_reused:
        unresolved = int(alignment.get("reviewRequired") or alignment.get("unresolved") or 0)
        locked = int(alignment.get("matched") or 0) + int(alignment.get("constrained") or 0)
        update_pipeline_stage(
            job_id, "ocr", "done", state="planning",
            processed=page_count, total=page_count, detail="页边 OCR 缓存未变化，直接复用",
        )
        update_pipeline_stage(
            job_id, "align", "blocked" if unresolved else "done", state="planning",
            processed=page_count, total=page_count,
            detail=f"已复用严格核对清单：锁定 {locked} 页，待核对 {unresolved} 页",
            metrics={"manifestReused": True, "locked": locked, "unresolved": unresolved},
        )
    blockers = []
    if alignment["unresolved"] and not ALLOW_UNRESOLVED_OUTPUT:
        blockers.append(f"{alignment['unresolved']} 页未锁定")
    if alignment["estimated"] and not ALLOW_ESTIMATED_OUTPUT:
        blockers.append(f"{alignment['estimated']} 页仍是估算范围")
    if alignment["boundaryReview"] and not ALLOW_UNRESOLVED_OUTPUT:
        blockers.append(f"{alignment['boundaryReview']} 页旧式页界结果待复核")
    if blockers:
        if streamed_pages:
            update_pipeline_stage(
                job_id,
                "layer",
                "paused",
                state="planning",
                processed=len(streamed_pages),
                total=page_count,
                detail=f"已安全预写 {len(streamed_pages)} 页；严格门禁未通过，未合并发布",
                metrics={"streamedPages": len(streamed_pages), "published": False},
            )
        return write_full_status(
            job_id,
            state="error",
            activeStage="align",
            processed=page_count,
            total=page_count,
            message=f"处理未完成：{'、'.join(blockers)}。非权威页应自动转为整页 OCR，请重试任务。",
            alignment=alignment,
            validation={},
            outputs=[],
        )
    if alignment["unresolved"]:
        unresolved_pages = [str(item.get("page")) for item in manifest if item.get("kind") == "unresolved"]
        write_full_status(
            job_id,
            state="planning",
            processed=page_count,
            total=page_count,
            currentPage=page_count,
            message=f"全书核对完成，{alignment['unresolved']} 页未可靠锁定，将保留空文本层继续生成：{', '.join(unresolved_pages[:18])}{'…' if len(unresolved_pages) > 18 else ''}。",
            alignment=alignment,
        )

    manifest_hash = hashlib.sha1(
        f"{LAYOUT_ENGINE_VERSION}\n{json.dumps(manifest, ensure_ascii=False, sort_keys=True)}".encode("utf-8")
    ).hexdigest()
    build_key_path = paths.root / "build-manifest-hash.txt"
    atomic_write_text(build_key_path, manifest_hash)

    cached_pages = len(list(page_dir.glob("page-*.pdf")))
    update_pipeline_stage(
        job_id,
        "layer",
        "running",
        state="running",
        total=page_count,
        processed=cached_pages,
        detail=f"已复用 {cached_pages} 个中间页",
        message="整本任务正在本地运行；已优先按页首线索对齐参考文本。",
        alignment=alignment,
        outputs=[],
    )

    # Final publication only trusts pages revalidated against the finalized manifest.
    verified_pages.clear()
    for page_no in range(1, page_count + 1):
        page_out = page_dir / f"page-{page_no:05d}.pdf"
        manifest_row = manifest[page_no - 1]
        write_page_layer(reader, page_no, manifest_row, selected_layout, page_out)
        verified_pages.add(page_no)

        if page_no == 1 or page_no % 10 == 0 or page_no == page_count:
            update_pipeline_stage(
                job_id,
                "layer",
                "running",
                state="running",
                processed=page_no,
                total=page_count,
                currentPage=page_no,
                detail=f"已确认并写入第 {page_no} 页",
                message=f"正在完成剩余页面 {page_no} / {page_count}。",
                alignment=alignment,
            )
        if read_full_status(job_id).get("pauseRequested"):
            break
        if stop_after and page_no >= stop_after:
            break

    page_files = [page_dir / f"page-{page_no:05d}.pdf" for page_no in range(1, page_count + 1)]
    complete = len(verified_pages) == page_count
    if complete:
        update_pipeline_stage(
            job_id, "layer", "done", state="running",
            processed=page_count, total=page_count, detail="全部页面文字层已写入并逐页验证",
        )
        building_pdf.unlink(missing_ok=True)
        try:
            update_pipeline_stage(
                job_id, "assemble", "running", state="running",
                processed=0, total=page_count, detail="正在合并逐页 PDF",
                message="逐页文字层已完成，正在合并整本 PDF。",
            )
            assembly_backend = assemble_page_pdfs(page_files, building_pdf)
            update_pipeline_stage(
                job_id, "assemble", "done", state="running",
                processed=page_count, total=page_count, detail=f"合并完成：{assembly_backend}",
                metrics={"backend": assembly_backend},
            )
            validation = validate_full_output(pdf_path, building_pdf, manifest, status_job_id=job_id)
            validation["assembly"] = assembly_backend
            os.replace(building_pdf, final_pdf)
            stale_pdf.unlink(missing_ok=True)
        finally:
            building_pdf.unlink(missing_ok=True)
        outputs = [
            make_output_link(job_id, final_pdf, "整本文字定位 PDF", "PDF"),
        ]
        cleanup_job_cache(job_id, keep_final=True, remove_inputs=False)
        return write_full_status(
            job_id,
            state="done",
            processed=page_count,
            total=page_count,
            message="整本已经生成完成。",
            alignment=alignment,
            validation=validation,
            outputs=outputs,
            engineVersion=LAYOUT_ENGINE_VERSION,
            outputLayout=selected_layout,
        )

    update_pipeline_stage(
        job_id, "layer", "paused", state="paused",
        processed=len([path for path in page_files if path.exists()]), total=page_count,
        detail="已完成页面和缓存均已保留",
    )
    return write_full_status(
        job_id,
        state="paused",
        processed=len([path for path in page_files if path.exists()]),
        total=page_count,
        message="任务已暂停，下次点击生成整本会从已完成页面继续。",
        alignment=alignment,
        outputs=[],
    )


def validate_trial_output(
    source_pdf: Path,
    page_no: int,
    trial_pdf: Path,
    expected_text: str,
    blocks: list[dict] | None = None,
    image_size: tuple[int, int] | None = None,
) -> dict:
    extracted = PdfReader(str(trial_pdf), strict=False).pages[0].extract_text() or ""
    extracted_norm = canonical_output_text(extracted)
    expected_norm = expected_text_layer_norm(expected_text)
    if expected_norm and len(extracted_norm) < max(4, round(len(expected_norm) * 0.8)):
        raise ValueError("试页文字层没有完整写入，已停止输出。")
    if expected_norm and extracted_norm != expected_norm:
        raise ValueError("试页复制文字与本页锁定正文不一致，已停止输出。")
    if re.search(r"(?:U\+[0-9A-Fa-f]{4,6}){2,}", extracted):
        raise ValueError("试页文字层出现编码串，已停止输出。")
    source_image = render_page_image(source_pdf, page_no, dpi=96)
    trial_image = render_page_image(trial_pdf, 1, dpi=96)
    if source_image.size != trial_image.size or ImageChops.difference(source_image, trial_image).getbbox() is not None:
        raise ValueError("试页画面与原扫描页不一致，已停止输出。")
    return {"extractedChars": len(extracted_norm), "pixelIdentical": True, "wordStyleFrame": True}


def make_trial(job_id: str, page_no: int, layout: str) -> dict:
    paths = job_paths(job_id)
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    pdf_path = Path(job["pdf"])
    reader_for_count = PdfReader(str(pdf_path), strict=False)
    page_no = max(1, min(page_no, len(reader_for_count.pages)))

    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    preview_path = paths.root / f"page-{page_no:04d}-guides.png"
    trial_pdf = paths.root / f"page-{page_no:04d}-trial.pdf"
    preview_path.unlink(missing_ok=True)
    trial_pdf.unlink(missing_ok=True)
    image = render_page_image(pdf_path, page_no)
    blocks = detect_horizontal_block(image) if selected_layout == "horizontal" else detect_vertical_blocks(image, selected_layout)

    reader = PdfReader(str(pdf_path), strict=False)
    resolved = resolve_trial_page_by_next_start(job, reader, page_no, selected_layout)
    if resolved.get("kind") == "unresolved":
        start_sample = normalize_for_match(str(resolved.get("startAnchor") or ""))[0][:18]
        end_sample = normalize_for_match(str(resolved.get("endAnchor") or ""))[0][-18:]
        detail = " / ".join(value for value in (start_sample, end_sample) if value)
        raise ValueError(f"本页页首与次页页首尚未在校准文本中唯一、顺序锁定{f'：{detail}' if detail else ''}。整本任务会把这一页改为纯 OCR。")
    if resolved.get("kind") == "blank":
        raise ValueError("这一页被判断为空白或纯图像页，不适合作为正文校准页。")

    writer = PdfWriter()
    writer.add_page(reader.pages[page_no - 1])
    source_page = writer.pages[0]
    remove_text(source_page, writer)
    text = str(resolved.get("text") or "")
    if not text.strip():
        raise ValueError("这一页没有得到可写入的文字，已停止生成。")
    if selected_layout != "horizontal":
        blocks = attach_ocr_column_geometry(job, page_no, image, blocks, text)
    draw_guides(image, blocks, preview_path)
    packet = io.BytesIO()
    width, height = float(source_page.mediabox.width), float(source_page.mediabox.height)
    overlay_canvas = canvas.Canvas(packet, pagesize=(width, height), pageCompression=1)
    draw_word_style_authoritative_text(overlay_canvas, text, width, height, selected_layout)
    overlay_canvas.showPage()
    overlay_canvas.save()
    packet.seek(0)
    overlay = PdfReader(packet).pages[0]
    source_page.merge_page(overlay)
    write_pdf_atomic(writer, trial_pdf)

    validation = validate_trial_output(pdf_path, page_no, trial_pdf, text, blocks, image.size)
    if resolved.get("kind") == "body":
        job["lastTrial"] = {
            "page": page_no,
            "layout": selected_layout,
            "sourceTitle": resolved.get("sourceTitle", ""),
            "sourceUrl": resolved.get("sourceUrl", ""),
            "confidence": resolved.get("confidence", 0),
            "validatedAt": time.time(),
            "role": "preview-only",
        }
        atomic_write_json(paths.meta, job)

    cleanup_trial_outputs(job_id, keep_page=page_no)

    return {
        "preview": preview_path,
        "trialPdf": trial_pdf,
        "page": page_no,
        "layout": selected_layout,
        "kind": resolved.get("kind"),
        "status": resolved.get("status"),
        "sourceTitle": resolved.get("sourceTitle", ""),
        "confidence": resolved.get("confidence", 0),
        "validation": validation,
    }


def make_full_plan(job_id: str, layout: str) -> dict:
    paths = job_paths(job_id)
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    pdf_path = Path(job["pdf"])
    page_count = len(PdfReader(str(pdf_path), strict=False).pages)
    report = paths.root / "full-processing-plan.csv"
    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["item", "value"])
        writer.writeheader()
        writer.writerow({"item": "PDF 页数", "value": page_count})
        writer.writerow({"item": "处理版式", "value": layout_label(selected_layout)})
        writer.writerow({"item": "当前能力", "value": "已支持单页试运行；整本任务将按后台队列处理"})
        writer.writerow({"item": "下一步", "value": "继续补齐自动分页后，可稳定生成整本 PDF"})
    return {
        "report": report,
        "messages": [
            "已经准备好整本任务的处理记录。",
            "大书会更适合放在后台慢慢跑，关掉网页也不该影响结果；这一块后续继续加固。",
        ],
    }


def create_job(pdf_field, source_field=None, source_url: str = "", layout: str = "auto") -> dict:
    pdf_original = safe_name(getattr(pdf_field, "filename", "pdf"), "source.pdf")
    source_original = ""
    if source_field is not None and getattr(source_field, "filename", ""):
        source_original = safe_name(source_field.filename, "source.txt")
    elif source_url:
        source_original = source_url.strip()
    pdf_digest = upload_sha256(pdf_field)
    source_identity = (
        f"file:{upload_sha256(source_field)}"
        if source_field is not None and source_original
        else (f"url:{source_original}" if source_original else "none")
    )
    fingerprint = job_input_fingerprint(pdf_digest, source_identity, layout)
    existing_job_id = reusable_job_id(
        fingerprint, pdf_original, source_original, pdf_digest, source_identity, layout
    )
    if existing_job_id:
        paths = job_paths(existing_job_id)
        if not paths.pdf.exists():
            write_upload(pdf_field, paths.pdf)
        inspected = inspect_pdf(paths.pdf, layout)
        inspected.update({"jobId": existing_job_id, "reused": True})
        inspected["messages"].append("检测到 PDF、参考文本和版式均未变化，已恢复原任务和已有缓存。")
        return inspected

    job_id = hashlib.sha1(fingerprint.encode("ascii")).hexdigest()[:16]
    if job_paths(job_id).root.exists():
        job_id = make_job_id(pdf_original)
    paths = new_job_paths(job_id, pdf_original)
    paths.root.mkdir(parents=True, exist_ok=True)
    write_upload(pdf_field, paths.pdf)
    source_text = ""
    source_units: list[SourceUnit] = []
    if source_field is not None and getattr(source_field, "filename", ""):
        source_upload = paths.root / source_original
        write_upload(source_field, source_upload)
        source_units = source_file_to_units(source_upload)
        source_text = "\n\n".join(unit.text for unit in source_units if unit.text.strip())
    elif source_url:
        source_original = source_url
        source_units = fetch_source_bundle(source_url)
        source_text = "\n\n".join(unit.text for unit in source_units if unit.text.strip())
    if source_text:
        atomic_write_text(paths.source, source_text)
    source_quality = assess_source_units(source_units)

    inspected = inspect_pdf(paths.pdf, layout)
    meta = {
        "id": job_id,
        "pdf": str(paths.pdf),
        "pdfOriginal": pdf_original,
        "sourceText": str(paths.source) if source_text else "",
        "sourceOriginal": source_original,
        "sourceArchive": str(source_upload) if source_field is not None and source_original else "",
        "layout": inspected["layout"],
        "requestedLayout": layout,
        "inputFingerprint": fingerprint,
        "pdfSha256": pdf_digest,
        "sourceIdentity": source_identity,
        "sourceQuality": source_quality,
        "createdAt": time.time(),
    }
    atomic_write_json(paths.meta, meta)
    if source_units:
        save_source_units(meta, source_units)
    inspected["jobId"] = job_id
    inspected["sourceQuality"] = source_quality
    if source_text:
        inspected["messages"].append(f"已读入参考文本，约 {len(source_text):,} 个字符。")
        if source_url and source_quality["unitCount"] > 1:
            inspected["messages"].append(f"网页已跟随读取 {source_quality['unitCount']} 个正文单元。")
        inspected["messages"].extend(f"网页来源提醒：{warning}。" for warning in source_quality["warnings"])
    else:
        inspected["messages"].append("没有参考文本时，可以先试一页看看版面定位。")
    return inspected
