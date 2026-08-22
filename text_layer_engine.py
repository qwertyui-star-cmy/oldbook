from __future__ import annotations

import csv
import concurrent.futures
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
import zipfile
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from string import punctuation
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.error import HTTPError
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
JOBS_DIR = ROOT / ".cache" / "text-layer-jobs"
POPPLER = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
CN_PUNCT = "，。！？；：、“”‘’（）《》〈〉【】〔〕［］—…·　「」『』﹁﹂﹃﹄"
SKIP_CHARS = set(punctuation + CN_PUNCT)
TEXT_FONT = "HanText"
TEXT_FONT_REGISTERED = False
EXTB_TEXT_FONT = "HanTextExtB"
EXTB_TEXT_FONT_REGISTERED = False
LAYOUT_ENGINE_VERSION = "vertical-local-search-v8-reader-search"
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
OPENCC_S2T_CONVERTER = None
OPENCC_T2S_CONVERTER = None
STATUS_LOCKS: dict[str, threading.RLock] = {}
STATUS_LOCKS_GUARD = threading.Lock()
OCR_IDLE_TIMEOUT_SECONDS = max(60, int(os.environ.get("TEXT_LAYER_OCR_IDLE_TIMEOUT", "600") or "600"))
FULL_OCR_FALLBACK_ENABLED = str(os.environ.get("TEXT_LAYER_FULL_OCR_FALLBACK") or "").strip() in {"1", "true", "yes", "on"}
STRICT_MANIFEST_ENABLED = str(os.environ.get("TEXT_LAYER_STRICT_MANIFEST") or "1").strip().lower() in {"1", "true", "yes", "on"}
FAST_MANIFEST_FUZZY_ENABLED = str(os.environ.get("TEXT_LAYER_FAST_FUZZY") or "").strip() in {"1", "true", "yes", "on"}
ALLOW_UNRESOLVED_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_UNRESOLVED_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_ESTIMATED_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_ESTIMATED_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_OCR_OUTPUT = str(os.environ.get("TEXT_LAYER_ALLOW_OCR_OUTPUT") or "0").strip().lower() in {"1", "true", "yes", "on"}
SEARCH_ALIASES_ENABLED = str(os.environ.get("TEXT_LAYER_SEARCH_ALIASES") or "1").strip().lower() in {"1", "true", "yes", "on"}
SOURCE_UNITS_CACHE: dict[str, tuple[float, int, list["SourceUnit"]]] = {}
SOURCE_UNITS_CACHE_LOCK = threading.Lock()
NORMALIZED_SOURCE_CACHE: dict[int, tuple[str, str, list[int]]] = {}
NORMALIZED_SOURCE_CACHE_LOCK = threading.Lock()
PIPELINE_STAGES = (
    ("input", "来源与任务检查"),
    ("ocr", "页边 OCR 缓存"),
    ("align", "权威正文逐页锁定"),
    ("classify", "未收录页判定"),
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

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(urljoin(self.base_url, href))


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


def safe_name(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._")
    return cleaned or fallback


def make_job_id(seed: str) -> str:
    raw = f"{seed}|{time.time_ns()}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def job_paths(job_id: str) -> JobPaths:
    job_id = str(job_id).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise ValueError("任务编号无效。")
    root = JOBS_DIR / job_id
    return JobPaths(root=root, pdf=root / "source.pdf", source=root / "source.txt", meta=root / "job.json")


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
    with path.open("wb") as handle:
        shutil.copyfileobj(field.file, handle)


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
            manifest = {
                str(item.attrib.get("id") or ""): str(item.attrib.get("href") or "")
                for item in package.findall(".//{*}manifest/{*}item")
            }
            guide_titles = {
                posixpath.normpath(posixpath.join(opf_dir, str(item.attrib.get("href") or ""))):
                str(item.attrib.get("title") or "")
                for item in package.findall(".//{*}guide/{*}reference")
            }
            for itemref in package.findall(".//{*}spine/{*}itemref"):
                href = manifest.get(str(itemref.attrib.get("idref") or ""), "")
                archive_name = posixpath.normpath(posixpath.join(opf_dir, href))
                if archive_name in names and archive_name.lower().endswith((".html", ".xhtml", ".htm")):
                    ordered.append((archive_name, guide_titles.get(archive_name, "")))
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


def opencc_converter(config: str):
    global OPENCC_S2T_CONVERTER, OPENCC_T2S_CONVERTER
    target = "OPENCC_S2T_CONVERTER" if config == "s2t" else "OPENCC_T2S_CONVERTER"
    current = globals()[target]
    if current is None:
        try:
            from opencc import OpenCC
            current = OpenCC(config)
        except Exception:
            current = False
        globals()[target] = current
    return current


def detect_cjk_script(text: str) -> str:
    s2t = opencc_converter("s2t")
    t2s = opencc_converter("t2s")
    if not s2t or not t2s:
        return "unknown"
    chars = [char for char in text if "\u3400" <= char <= "\u9fff"]
    traditional = sum(1 for char in chars if t2s.convert(char) != char)
    simplified = sum(1 for char in chars if s2t.convert(char) != char)
    if traditional >= 2 and traditional > simplified * 1.2:
        return "traditional"
    if simplified >= 2 and simplified > traditional * 1.2:
        return "simplified"
    return "unknown"


def adapt_text_to_scan_script(text: str, scan_text: str) -> tuple[str, str]:
    glyph_adjusted = False
    common_votes = sum(1 for common_variant in COMMON_GLYPH_VARIANTS.values() if common_variant in scan_text)
    source_votes = sum(1 for source_variant in COMMON_GLYPH_VARIANTS if source_variant in scan_text)
    for source_variant, common_variant in COMMON_GLYPH_VARIANTS.items():
        if source_variant in text and (common_variant in scan_text or common_votes > source_votes):
            text = text.replace(source_variant, common_variant)
            glyph_adjusted = True
        elif common_variant in text and (source_variant in scan_text or source_votes > common_votes):
            text = text.replace(common_variant, source_variant)
            glyph_adjusted = True
    for source_variant in ("䃅", "磾", "㻅"):
        safe_variant = COMMON_GLYPH_VARIANTS.get(source_variant)
        if safe_variant and source_variant in text:
            text = text.replace(source_variant, safe_variant)
            glyph_adjusted = True
    target = detect_cjk_script(scan_text)
    source = detect_cjk_script(text)
    if target == "traditional" and source == "simplified":
        converter = opencc_converter("s2t")
        return (converter.convert(text), "简转繁并统一字形") if converter else (text, "已统一常见字形" if glyph_adjusted else "")
    if target == "simplified" and source == "traditional":
        converter = opencc_converter("t2s")
        return (converter.convert(text), "繁转简并统一字形") if converter else (text, "已统一常见字形" if glyph_adjusted else "")
    return text, "已统一常见字形" if glyph_adjusted else ""


def request_bytes(url: str, limit: int = 8 * 1024 * 1024) -> tuple[bytes, str, str]:
    request = Request(url, headers={"User-Agent": "TextLocator/0.2 (+local text alignment)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                content_type = response.headers.get_content_type() or ""
                return response.read(limit), charset, content_type
        except HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                raise
            delay = min(8, max(2, int(exc.headers.get("Retry-After") or 2)))
            time.sleep(delay)
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
    seen = set()
    for link in parser.links:
        parsed = urlparse(link)
        if parsed.netloc != base_host:
            continue
        if not re.search(r"/(?:novel/\d+/chapter|read/\d+)/\d+", parsed.path):
            continue
        clean_link = parsed._replace(query="mode=text" if "/read/" in parsed.path else "", fragment="").geturl()
        if clean_link not in seen:
            seen.add(clean_link)
            chapter_links.append(clean_link)
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
    if end_boundary <= start_pos + 20 or end_boundary - start_pos > 1200:
        return None
    confidence = min(99, 75 + min(len(start_needle), len(end_needle)) * 2)
    return {
        "start": start_pos,
        "end": end_boundary,
        "text": original_slice(source_text, mapping, start_pos, max(start_pos, end_boundary - 1)),
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
        messages.append("扫描页已就绪；试页时会自动识别页首、页尾并定位正文。")
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


def render_page_image(pdf_path: Path, page_no: int, dpi: int = 140) -> Image.Image:
    if not POPPLER.exists():
        raise FileNotFoundError(f"Poppler renderer not found: {POPPLER}")
    identity = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:8]
    out_prefix = pdf_path.parent / f".render-page-{page_no:04d}-{identity}-{os.getpid()}-{threading.get_ident()}"
    result = subprocess.run(
        [str(POPPLER), "-f", str(page_no), "-l", str(page_no), "-png", "-r", str(dpi), str(pdf_path), str(out_prefix)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="ignore").strip() or "页面渲染器没有返回可用信息。"
        raise RuntimeError(f"第 {page_no} 页没有渲染成功：{detail}")
    candidates = sorted(out_prefix.parent.glob(f"{out_prefix.name}-*.png"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"第 {page_no} 页没有生成预览图，请换一页试试。")
    rendered = candidates[0]
    with Image.open(rendered) as opened:
        image = opened.convert("RGB")
    for candidate in candidates:
        candidate.unlink(missing_ok=True)
    return image


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
    kept = []
    depth = 0
    for operands, operator in stream.operations:
        if operator == b"BT":
            depth += 1
            continue
        if operator == b"ET":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            kept.append((operands, operator))
    stream.operations = kept
    page.replace_contents(stream)


def proportional_capacities(raw: list[float], total: int) -> list[int]:
    if not raw or total <= 0:
        return []
    scale = total / max(1.0, sum(raw))
    exact = [max(1.0, value * scale) for value in raw]
    capacities = [max(1, int(value)) for value in exact]
    remainder = total - sum(capacities)
    order = sorted(range(len(exact)), key=lambda index: exact[index] - int(exact[index]), reverse=remainder > 0)
    cursor = 0
    while remainder and order:
        index = order[cursor % len(order)]
        if remainder > 0:
            capacities[index] += 1
            remainder -= 1
        elif capacities[index] > 1:
            capacities[index] -= 1
            remainder += 1
        cursor += 1
        if cursor > len(order) * max(2, total):
            break
    return capacities


def draw_vertical_text(pdf_canvas, text: str, blocks: list[dict], page_w: float, page_h: float, image_w: int, image_h: int) -> None:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return
    sx, sy = page_w / image_w, page_h / image_h
    columns = []
    for block in blocks:
        geometry = block.get("ocrColumns") or []
        if geometry:
            columns.extend(geometry)
            continue
        _, iy0, _, iy1 = block["inner"]
        columns.extend({"x": x, "y0": iy0, "y1": iy1, "recognized": ""} for x in block["cols"])
    if not columns:
        return
    pitch_samples = []
    for column in columns:
        recognized = [char for char in str(column.get("recognized") or "") if not char.isspace()]
        height = max(1.0, float(column["y1"]) - float(column["y0"]))
        if len(recognized) >= 6:
            pitch_samples.append(height / len(recognized))
    pitch_samples.sort()
    pitch = pitch_samples[len(pitch_samples) // 2] if pitch_samples else max(8.0, image_h * 0.019)
    plausible = [value for value in pitch_samples if pitch * 0.55 <= value <= pitch * 1.8]
    if plausible:
        plausible.sort()
        pitch = plausible[len(plausible) // 2]
    raw_capacities = [max(1.0, (float(column["y1"]) - float(column["y0"])) / max(1.0, pitch)) for column in columns]
    capacities = proportional_capacities(raw_capacities, len(chars))
    index = 0
    for column, capacity in zip(columns, capacities):
        if index >= len(chars):
            return
        y0 = float(column["y0"])
        y1 = float(column["y1"])
        usable_h = (y1 - y0) * sy
        step = usable_h / max(1, capacity)
        font_size = min(10.5, max(5.0, step * 0.88))
        x = float(column["x"]) * sx - font_size * .35
        y_start = page_h - y0 * sy - font_size
        chunk = chars[index:index + capacity]
        index += capacity
        for offset, char in enumerate(chunk):
            text_obj = pdf_canvas.beginText()
            text_obj.setTextRenderMode(3)
            text_obj.setFont(ensure_char_font(char), font_size)
            text_obj.setTextOrigin(x, y_start - offset * step)
            text_obj.textOut(char)
            pdf_canvas.drawText(text_obj)


def draw_horizontal_text(pdf_canvas, text: str, page_w: float, page_h: float) -> None:
    chars = [char for char in text if not char.isspace()]
    if not chars:
        return
    margin_x = max(24.0, page_w * 0.075)
    margin_y = max(24.0, page_h * 0.075)
    usable_w = max(40.0, page_w - margin_x * 2)
    usable_h = max(40.0, page_h - margin_y * 2)
    font_size = 10.0
    while font_size > 4.0:
        chars_per_line = max(1, int(usable_w / font_size))
        line_count = (len(chars) + chars_per_line - 1) // chars_per_line
        if line_count * font_size * 1.35 <= usable_h:
            break
        font_size -= 0.5
    chars_per_line = max(1, int(usable_w / font_size))
    line_step = font_size * 1.35
    for line_index, start in enumerate(range(0, len(chars), chars_per_line)):
        y = page_h - margin_y - font_size - line_index * line_step
        if y < margin_y - font_size:
            raise ValueError("本页文字超过横排版心容量，已停止生成，避免截断文字。")
        text_obj = pdf_canvas.beginText(margin_x, y)
        text_obj.setTextRenderMode(3)
        for char in chars[start:start + chars_per_line]:
            text_obj.setFont(ensure_char_font(char), font_size)
            text_obj.textOut(char)
        pdf_canvas.drawText(text_obj)


def searchable_safe_text(text: str) -> str:
    for source_variant, safe_variant in COMMON_GLYPH_VARIANTS.items():
        text = text.replace(source_variant, safe_variant)
    return "".join(
        char
        for char in text
        if char != "\x00" and not ("\ue000" <= char <= "\uf8ff")
    )


def search_aliases(text: str) -> list[str]:
    text = searchable_safe_text(text)
    plain = "".join(char for char in text if not char.isspace() and char not in SKIP_CHARS)
    if not plain:
        return []
    script = detect_cjk_script(plain)
    configs = ("t2s",) if script == "traditional" else (("s2t",) if script == "simplified" else ("t2s", "s2t"))
    candidates = [plain]
    for config in configs:
        converter = opencc_converter(config)
        if converter:
            candidates.append(converter.convert(plain))
    aliases = []
    seen = set()
    for candidate in candidates:
        candidate = "".join(char for char in candidate if not char.isspace() and char not in SKIP_CHARS)
        if candidate and candidate not in seen:
            seen.add(candidate)
            aliases.append(candidate)
    return aliases


def search_alias_fragments(text: str, window: int = 48, stride: int = 24) -> list[tuple[int, int, str]]:
    fragments = []
    for alias_index, alias in enumerate(search_aliases(text)):
        if len(alias) <= window:
            starts = [0]
        else:
            starts = list(range(0, len(alias) - window + 1, stride))
            tail = len(alias) - window
            if starts[-1] != tail:
                starts.append(tail)
        fragments.extend((alias_index, start, alias[start:start + window]) for start in starts)
    return fragments


def draw_search_aliases(
    pdf_canvas,
    text: str,
    blocks: list[dict] | None = None,
    image_size: tuple[int, int] | None = None,
) -> None:
    page_w, page_h = pdf_canvas._pagesize
    font_size = 7.0
    aliases = search_aliases(text)
    columns = []
    if blocks and image_size:
        for block in blocks:
            _, y0, _, y1 = block["inner"]
            columns.extend({"x": x, "y0": y0, "y1": y1} for x in block.get("cols", []))
    sx = float(page_w) / image_size[0] if image_size else 1.0
    sy = float(page_h) / image_size[1] if image_size else 1.0
    column_pitch = 36.0
    if len(columns) >= 2:
        deltas = sorted(abs(float(columns[index]["x"]) - float(columns[index - 1]["x"])) for index in range(1, len(columns)))
        column_pitch = deltas[len(deltas) // 2] * sx

    for alias_index, start, fragment in search_alias_fragments(text):
        alias = aliases[alias_index]
        if columns:
            chars_per_column = max(1, (len(alias) + len(columns) - 1) // len(columns))
            column = columns[min(len(columns) - 1, start // chars_per_column)]
            row_fraction = min(1.0, (start % chars_per_column) / chars_per_column)
            target_width = max(30.0, column_pitch * 2.2)
            x = max(8.0, min(float(page_w) - target_width - 8.0, float(column["x"]) * sx - target_width / 2))
            image_y = float(column["y0"]) + row_fraction * (float(column["y1"]) - float(column["y0"]))
            y = max(8.0, min(float(page_h) - 10.0, float(page_h) - image_y * sy - alias_index * 3.0))
        else:
            target_width = max(50.0, float(page_w) * .72)
            x = max(8.0, (float(page_w) - target_width) / 2)
            y_fraction = start / max(1, len(alias))
            y = max(12.0, float(page_h) * (.84 - y_fraction * .68) - alias_index * 4.0)

        unscaled_width = 0.0
        for char in fragment:
            font = ensure_char_font(char)
            unscaled_width += pdfmetrics.stringWidth(char, font, font_size)
        horizontal_scale = min(100.0, target_width * 100.0 / max(1.0, unscaled_width))
        text_obj = pdf_canvas.beginText(x, y)
        text_obj.setTextRenderMode(3)
        text_obj.setHorizScale(horizontal_scale)
        current_font = ""
        run = []
        for char in fragment:
            font = ensure_char_font(char)
            if current_font and font != current_font:
                text_obj.setFont(current_font, font_size)
                text_obj.textOut("".join(run))
                run = []
            current_font = font
            run.append(char)
        if run:
            text_obj.setFont(current_font, font_size)
            text_obj.textOut("".join(run))
        pdf_canvas.drawText(text_obj)


def expected_text_layer_norm(text: str) -> str:
    primary, _ = normalize_for_match(searchable_safe_text(text))
    if not SEARCH_ALIASES_ENABLED:
        return primary
    alias_norm = "".join(normalize_for_match(fragment)[0] for _, _, fragment in search_alias_fragments(text))
    return primary + alias_norm


def page_text_from_sources(job: dict, page_no: int, layout: str | None = None, require_anchor_for_scan: bool = True) -> str:
    pdf_path = Path(job["pdf"])
    reader = PdfReader(str(pdf_path), strict=False)
    selected_layout = layout or job.get("layout", "auto")
    resolved = resolve_page(job, reader, page_no, selected_layout, allow_discovery=True)
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


def ocr_image_text(image: Image.Image, image_path: Path, layout: str) -> str:
    engine = get_ocr_engine()
    if engine is None:
        return ""
    image.save(image_path)
    result = engine(str(image_path))
    image_path.unlink(missing_ok=True)
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
        image_path = cache_path.with_suffix(".png")
        image.save(image_path)
        result = engine(str(image_path))
        image_path.unlink(missing_ok=True)
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


def ocr_anchor_images(crops: list[Image.Image], image_path: Path, layout: str) -> list[str]:
    if not crops:
        return ["", ""]
    engine = get_ocr_engine()
    if engine is None:
        return ["", ""]
    gap = 36
    widths = [crop.width for crop in crops[:2]]
    height = max(crop.height for crop in crops[:2])
    combined = Image.new("RGB", (sum(widths) + gap, height), "white")
    offsets = [0, widths[0] + gap]
    for index, crop in enumerate(crops[:2]):
        combined.paste(crop, (offsets[index], 0))
    combined.save(image_path)
    result = engine(str(image_path))
    image_path.unlink(missing_ok=True)
    groups = [[], []]
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
        group_index = 0 if cx < widths[0] + gap / 2 else 1
        local_cx = cx - offsets[group_index]
        groups[group_index].append({"text": text, "cx": local_cx, "cy": cy})
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


def ocr_anchor_crops(image: Image.Image, layout: str, units: int = 1) -> list[Image.Image]:
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

    blocks = detect_vertical_blocks(image, layout)
    if not blocks:
        return [image]
    start_block = blocks[0]
    end_block = blocks[-1]
    six0, siy0, six1, siy1 = start_block["inner"]
    eix0, eiy0, eix1, eiy1 = end_block["inner"]
    start_runs = text_column_runs(image, start_block["inner"])
    end_runs = text_column_runs(image, end_block["inner"])
    start_col = start_runs[-1] if start_runs else (six1 - 70, six1)
    end_col = end_runs[0] if end_runs else (eix0, eix0 + 70)
    start_width = max(80, min(190, (start_col[1] - start_col[0]) * units + 34))
    end_width = max(80, min(190, (end_col[1] - end_col[0]) * units + 34))
    start_right = min(six1, max(six0 + 1, start_col[1] + 8))
    start_left = max(six0, min(start_right - 1, start_col[1] - start_width))
    end_left = max(eix0, min(eix1 - 1, end_col[0] - 8))
    end_right = min(eix1, max(end_left + 1, end_col[0] + end_width))
    return [
        image.crop(valid_box(start_left, siy0 - 70, start_right, siy1 + 35)),
        image.crop(valid_box(end_left, eiy0 - 70, end_right, eiy1 + 35)),
    ]


def ocr_page_anchor_pair(job: dict, page_no: int, layout: str) -> tuple[str, str]:
    job_id = str(job.get("id") or "").strip()
    paths = job_paths(job_id) if job_id else None
    cache_path = (paths.root / f"page-{page_no:04d}-ocr-anchors-v8.json") if paths else Path(job["pdf"]).parent / f"page-{page_no:04d}-ocr-anchors-v8.json"
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            start_text = str(payload.get("start") or "")
            end_text = str(payload.get("end") or "")
            if payload.get("complete") or start_text or end_text:
                return start_text, end_text
        except Exception:
            pass
    image = render_page_image(Path(job["pdf"]), page_no, dpi=140)
    primary_crops = ocr_anchor_crops(image, layout, units=1)
    expanded_crops = ocr_anchor_crops(image, layout, units=2)
    chunks = ocr_anchor_images(primary_crops[:2], cache_path.with_suffix(".crops.png"), layout)
    for index, text in enumerate(chunks[:2], 1):
        normalized, _ = normalize_for_match(text)
        side = "start" if index == 1 else "end"
        source_match = any(
            exact_anchor_evidence(normalize_for_match(unit.text)[0], text, side)[0] is not None
            for unit in load_source_units(job)
        )
        if (len(normalized) < 4 or not source_match) and index - 1 < len(expanded_crops):
            text = ocr_image_text(expanded_crops[index - 1], cache_path.with_suffix(f".crop-{index}-wide.png"), layout)
            chunks[index - 1] = text
    while len(chunks) < 2:
        chunks.append("")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    detected_blocks = detect_horizontal_block(image) if layout == "horizontal" else detect_vertical_blocks(image, layout)
    atomic_write_json(cache_path, {
        "complete": True,
        "start": chunks[0],
        "end": chunks[1],
        "imageSize": list(image.size),
        "blocks": detected_blocks,
    })
    return chunks[0], chunks[1]


def precompute_anchor_worker(payload: tuple[str, int, str]) -> tuple[int, bool, str]:
    job_id, page_no, layout = payload
    try:
        paths = job_paths(job_id)
        job = json.loads(paths.meta.read_text(encoding="utf-8"))
        start_text, end_text = ocr_page_anchor_pair(job, page_no, layout)
        return page_no, bool(start_text or end_text), ""
    except Exception as error:
        return page_no, False, str(error)


def anchor_cache_ready(job_id: str, page_no: int) -> bool:
    cache_path = job_paths(job_id).root / f"page-{page_no:04d}-ocr-anchors-v8.json"
    if not cache_path.exists():
        return False
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        return bool(payload.get("complete") or payload.get("start") or payload.get("end"))
    except Exception:
        return False


def adaptive_ocr_workers(requested: int | None = None) -> int:
    override = str(os.environ.get("TEXT_LAYER_OCR_WORKERS") or "").strip()
    if override.isdigit():
        return max(1, min(8, int(override)))
    logical_cpus = os.cpu_count() or 4
    automatic = max(2, min(4, logical_cpus // 3))
    return min(automatic, requested) if requested else automatic


def precompute_anchor_cache(job_id: str, page_count: int, layout: str, first_page: int = 1, workers: int | None = None) -> bool:
    all_pages = list(range(max(1, first_page), page_count + 1))
    pages = [page_no for page_no in all_pages if not anchor_cache_ready(job_id, page_no)]
    cached = len(all_pages) - len(pages)
    if not pages:
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
    worker_count = adaptive_ocr_workers(workers)
    update_pipeline_stage(
        job_id,
        "ocr",
        "running",
        state="planning",
        processed=completed,
        total=page_count,
        detail=f"{worker_count} 路并行",
        message=f"正在逐页进行 OCR 双锁边 {completed} / {len(all_pages)}（{worker_count} 路并行）。",
    )
    with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(precompute_anchor_worker, (job_id, page_no, layout)) for page_no in pages]
        future_pages = {future: page_no for future, page_no in zip(futures, pages)}
        pending = set(futures)
        last_progress = time.time()
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
                    waiting_pages = sorted(future_pages[future] for future in pending)
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
                continue
            last_progress = time.time()
            for future in done:
                future.result()
                completed += 1
                page_no = future_pages.get(future)
                if page_no:
                    update_pipeline_stage(
                        job_id,
                        "ocr",
                        "running",
                        state="planning",
                        processed=completed,
                        total=page_count,
                        currentPage=page_no,
                        detail=f"{worker_count} 路并行，当前完成第 {page_no} 页",
                        message=f"正在逐页进行 OCR 双锁边 {completed} / {len(all_pages)}（{worker_count} 路并行）。",
                    )
                if read_full_status(job_id).get("pauseRequested"):
                    paused = True
                    for pending_future in pending:
                        pending_future.cancel()
                    pending.clear()
                    break
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
    return True


def ocr_page_text(job: dict, page_no: int, layout: str, anchors_only: bool = True) -> str:
    if anchors_only:
        return "\n".join(value for value in ocr_page_anchor_pair(job, page_no, layout) if value.strip())
    job_id = str(job.get("id") or "").strip()
    paths = job_paths(job_id) if job_id else None
    cache_path = (paths.root / f"page-{page_no:04d}-ocr-full-v2.txt") if paths else Path(job["pdf"]).parent / f"page-{page_no:04d}-ocr-full-v2.txt"
    if cache_path.exists():
        text = cache_path.read_text(encoding="utf-8", errors="ignore")
        if text.strip() or get_ocr_engine() is None:
            cleaned = clean_ocr_text(text)
            if cleaned != text:
                atomic_write_text(cache_path, cleaned)
            return cleaned
    image = render_page_image(Path(job["pdf"]), page_no, dpi=160)
    text = ocr_image_text(image, cache_path.with_suffix(".full.png"), layout)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cache_path, text)
    return text


def page_anchor_text(job: dict, reader: PdfReader, page_no: int, layout: str) -> str:
    job_id = str(job.get("id") or "").strip()
    if job_id and anchor_cache_ready(job_id, page_no):
        return ocr_page_text(job, page_no, layout, anchors_only=True)
    extracted = reader.pages[page_no - 1].extract_text() or ""
    normalized, _ = normalize_for_match(extracted)
    if len(normalized) >= 16:
        return extracted
    return ocr_page_text(job, page_no, layout, anchors_only=True)


def page_anchor_pair(job: dict, reader: PdfReader, page_no: int, layout: str) -> tuple[str, str]:
    job_id = str(job.get("id") or "").strip()
    if job_id and anchor_cache_ready(job_id, page_no):
        return ocr_page_anchor_pair(job, page_no, layout)
    extracted = reader.pages[page_no - 1].extract_text() or ""
    normalized, _ = normalize_for_match(extracted)
    if len(normalized) >= 24:
        midpoint = max(1, len(extracted) // 2)
        return extracted[:midpoint], extracted[midpoint:]
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
    candidate_units: list[SourceUnit] | None = None,
) -> dict | None:
    best = None
    for unit in candidate_units if candidate_units is not None else load_source_units(job):
        match = strict_pair_in_text(unit.text, start_text, end_text)
        if not match:
            continue
        candidate = {**match, "sourceTitle": unit.title, "sourceUrl": unit.url, "sourceKind": unit.kind}
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
    for unit in load_source_units(job):
        match = strict_pair_in_text(unit.text, full_text, full_text)
        if not match:
            continue
        if "/" in unit.title and int(match.get("start") or 0) < 500:
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
    unit = next((unit for unit in load_source_units(job) if (unit.url or unit.title) == current_key), None)
    if not unit:
        return current
    source_norm, mapping = normalize_source_cached(unit.text)
    current_end = int(current.get("end") or 0)
    if not source_norm or not mapping or current_end <= previous_end:
        return {
            **current,
            "kind": "unresolved",
            "status": "顺序冲突",
            "text": "",
            "confidence": 0,
            "reason": "本页与上一页正文重叠或倒退，已拒绝写入。",
        }
    if gap < -80 or gap > 500:
        return {
            **current,
            "kind": "unresolved",
            "status": "连续性未通过",
            "text": "",
            "confidence": 0,
            "reason": "本页与上一页权威正文之间存在过大跳跃，已拒绝写入。",
        }
    if gap <= 0:
        current.update({
            "start": previous_end,
            "text": original_slice(unit.text, mapping, previous_end, max(previous_end, current_end - 1)),
            "status": "双锁连续去重",
            "continuityOverlap": abs(gap),
            "reason": "页边锚点略有重叠，已按上一页可靠页尾去除重复文字。",
        })
        return current
    gap_text = original_slice(unit.text, mapping, previous_end, max(previous_end, current_start - 1))
    if re.search(r"(?:卷[之]?[上中下]|卷[一二三四五六七八九十]+|全書[始終]|全书[始终])", gap_text):
        return current
    current.update({
        "start": previous_end,
        "text": original_slice(unit.text, mapping, previous_end, max(previous_end, current_end - 1)),
        "status": "双锁连续补首",
        "continuityFilled": gap,
        "reason": "本页首列 OCR 漏读，已用上一页可靠页尾补齐本页开头。",
    })
    return current


def resolve_page(
    job: dict,
    reader: PdfReader,
    page_no: int,
    layout: str,
    allow_discovery: bool = True,
    candidate_units: list[SourceUnit] | None = None,
) -> dict:
    early_full_text = None
    if FULL_OCR_FALLBACK_ENABLED and page_no <= 12:
        early_full_text = ocr_page_text(job, page_no, layout, anchors_only=False)
        early_decision = classify_page(job, reader, page_no, layout, early_full_text)
        if early_decision["kind"] in {"ocr", "blank"}:
            early_text = prepared_ocr_page_text(early_full_text, early_decision["reason"])
            return {
                "kind": early_decision["kind"],
                "status": "整页 OCR" if early_decision["kind"] == "ocr" else "空白页",
                "page": page_no,
                "text": early_text if early_decision["kind"] == "ocr" else "",
                "confidence": 70 if early_decision["kind"] == "ocr" else 100,
                "sourceTitle": "本页 OCR" if early_decision["kind"] == "ocr" else "",
                "reason": early_decision["reason"],
            }
    start_text, end_text = page_anchor_pair(job, reader, page_no, layout)
    anchor_lines = [normalize_for_match(line)[0] for line in re.split(r"[\r\n]+", f"{start_text}\n{end_text}") if normalize_for_match(line)[0]]
    anchor_short_ratio = sum(1 for line in anchor_lines if len(line) <= 9) / max(1, len(anchor_lines))
    if FULL_OCR_FALLBACK_ENABLED and page_no <= 20 and len(anchor_lines) >= 5 and anchor_short_ratio >= 0.78:
        full_text = early_full_text if early_full_text is not None else ocr_page_text(job, page_no, layout, anchors_only=False)
        return {
            "kind": "ocr",
            "status": "整页 OCR",
            "page": page_no,
            "text": full_text,
            "confidence": 75,
            "sourceTitle": "本页 OCR",
            "reason": "检测到目录式短条目页面",
        }
    match = match_page_source(job, start_text, end_text, allow_discovery=False, candidate_units=candidate_units)
    weak_match = match if match and int(match.get("authorityRank") or 0) < 40 else None
    if match and not weak_match:
        match.update({"kind": "body", "status": "双头锁边", "page": page_no, "startAnchor": start_text, "endAnchor": end_text})
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
    if FULL_OCR_FALLBACK_ENABLED:
        decision = classify_page(job, reader, page_no, layout, full_text)
        if decision["kind"] == "ocr":
            return {
                "kind": "ocr",
                "status": "整页 OCR",
                "page": page_no,
                "text": prepared_ocr_page_text(full_text, decision["reason"]),
                "confidence": 70,
                "sourceTitle": "本页 OCR",
                "reason": decision["reason"],
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


def build_strict_page_manifest(job: dict, reader: PdfReader, layout: str, status_job_id: str = "") -> list[dict]:
    page_count = len(reader.pages)
    if status_job_id:
        if not precompute_anchor_cache(status_job_id, page_count, layout, first_page=1):
            raise TaskPaused()
        update_pipeline_stage(
            status_job_id,
            "align",
            "running",
            state="planning",
            processed=0,
            total=page_count,
            detail="正在按 EPUB 章节顺序逐页双锁",
            message="页边 OCR 已就绪，正在锁定权威正文范围。",
        )
    manifest = []
    source_cursors: dict[str, int] = {}
    units = load_source_units(job)
    unit_indexes = {(unit.url or unit.title): index for index, unit in enumerate(units)}
    active_unit = 0
    for page_no in range(1, page_count + 1):
        if units:
            left = max(0, active_unit - 1)
            right = min(len(units), active_unit + 4)
            candidates = units[left:right]
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
        resolved = apply_previous_page_continuity(job, manifest[-1] if manifest else None, resolved)
        source_key = str(resolved.get("sourceUrl") or resolved.get("sourceTitle") or "")
        if resolved.get("kind") == "body" and source_key:
            active_unit = max(active_unit, unit_indexes.get(source_key, active_unit))
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
                message=f"正在核对第 {page_no} / {page_count} 页。",
            )
            if read_full_status(status_job_id).get("pauseRequested"):
                raise TaskPaused()

    units_by_key = {}
    for unit in units:
        units_by_key[unit.url or unit.title] = unit

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

    for index, item in enumerate(manifest):
        if item.get("kind") != "unresolved" or index == 0 or index >= len(manifest) - 1:
            continue
        previous = manifest[index - 1]
        following = manifest[index + 1]
        previous_key = str(previous.get("sourceUrl") or previous.get("sourceTitle") or "")
        following_key = str(following.get("sourceUrl") or following.get("sourceTitle") or "")
        if previous.get("kind") != "body" or following.get("kind") != "body" or not previous_key or previous_key != following_key:
            continue
        start = int(previous.get("end") or 0)
        end = int(following.get("start") or 0)
        if end <= start or end - start > 5000:
            continue
        unit = units_by_key.get(previous_key)
        if not unit:
            continue
        source_norm, mapping = normalize_source_cached(unit.text)
        if not source_norm or not mapping:
            continue
        item.update({
            "kind": "body",
            "status": "相邻双锁约束",
            "sourceTitle": unit.title,
            "sourceUrl": unit.url,
            "sourceKind": unit.kind,
            "start": start,
            "end": end,
            "text": original_slice(unit.text, mapping, start, max(start, end - 1)),
            "confidence": max(75, min(int(previous.get("confidence") or 0), int(following.get("confidence") or 0)) - 5),
            "reason": "本页 OCR 边缘不足，由前后两页同一来源的可靠双锁边界唯一约束。",
            "adjacentVerified": True,
        })

    for index, item in enumerate(manifest[:-1]):
        next_item = manifest[index + 1]
        if item.get("kind") != "body" or next_item.get("kind") != "body":
            continue
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
            detail=f"正文锁定 {locked} 页，待核对 {unresolved} 页",
            metrics={"locked": locked, "unresolved": unresolved},
        )
        update_pipeline_stage(
            status_job_id,
            "classify",
            "done",
            state="planning",
            processed=omitted,
            total=omitted,
            detail=f"来源未收录 {omitted} 页，保留扫描画面",
            metrics={"sourceOmitted": omitted},
        )
    return manifest


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

        first_page = start_index + 1
        last_page = end_index + 1
        previous_cluster: list[int] = []
        following_cluster: list[int] = []
        previous_accounted = is_leading
        following_accounted = is_trailing
        previous_range = None
        following_range = None

        if previous_unit_index is not None:
            unit = units[previous_unit_index]
            source_norm, _ = normalize_source_cached(unit.text)
            low = int((previous or {}).get("end") or 0)
            high = len(source_norm)
            previous_range = (unit, low, high)
            if high <= low:
                previous_accounted = True
            else:
                evidence = [
                    page_no for page_no in range(first_page, last_page + 1)
                    if bounded_page_anchor_hits(job, reader, page_no, layout, unit, low, high)
                ]
                previous_cluster = edge_evidence_cluster(evidence, first_page, last_page, "start")
                previous_accounted = bool(previous_cluster)

        if following_unit_index is not None:
            unit = units[following_unit_index]
            high = int((following or {}).get("start") or 0)
            following_range = (unit, 0, high)
            if high <= 0:
                following_accounted = True
            else:
                evidence = [
                    page_no for page_no in range(first_page, last_page + 1)
                    if bounded_page_anchor_hits(job, reader, page_no, layout, unit, 0, high)
                ]
                following_cluster = edge_evidence_cluster(evidence, first_page, last_page, "end")
                following_accounted = bool(following_cluster)

        if not previous_accounted or not following_accounted:
            continue
        previous_edge = max(previous_cluster) if previous_cluster else first_page - 1
        following_edge = min(following_cluster) if following_cluster else last_page + 1
        if previous_edge >= following_edge:
            continue

        if len(previous_cluster) == 1 and previous_range:
            set_boundary_body_page(manifest[previous_cluster[0] - 1], *previous_range)
        if len(following_cluster) == 1 and following_range:
            set_boundary_body_page(manifest[following_cluster[0] - 1], *following_range)

        for page_no in range(previous_edge + 1, following_edge):
            manifest[page_no - 1].update({
                "kind": "source-omitted",
                "status": "来源未收录",
                "text": "",
                "confidence": 100,
                "sourceTitle": "",
                "sourceUrl": "",
                "reason": "该页位于相邻权威正文边界之外，来源 EPUB 未收录；保留原扫描画面并留空文字层。",
            })


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
    if layout == "horizontal":
        draw_horizontal_text(overlay_canvas, text, width, height)
    else:
        image_w, image_h = image_size or (round(width), round(height))
        draw_vertical_text(overlay_canvas, text, blocks, width, height, image_w, image_h)
    if SEARCH_ALIASES_ENABLED:
        draw_search_aliases(overlay_canvas, text, blocks, image_size)
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
            detail=f"已锁定 {locked} 页，待核对 {unresolved} 页" if unresolved else f"已锁定 {locked} 页",
        )
        omitted = int(alignment.get("sourceOmitted") or 0)
        pipeline[3].update(state="done", processed=omitted, total=omitted, detail=f"来源未收录 {omitted} 页")
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
        if stage_state == "running" and not stage.get("startedAt"):
            stage["startedAt"] = now
        if stage_state in {"done", "blocked", "error", "paused"}:
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
        job_id = job_root.name
        removed += cleanup_job_cache(job_id, keep_final=True).get("removed", 0)
    return {"removed": removed}


def manifest_summary(manifest: list[dict] | None, page_count: int) -> dict:
    if not manifest:
        return {"matched": 0, "constrained": 0, "ocr": 0, "blank": 0, "sourceOmitted": 0, "unresolved": page_count, "estimated": 0, "warnings": 0, "reviewRequired": page_count, "averageConfidence": 0}
    matched_statuses = {"双头锁边", "双锁连续去重", "双锁连续补首", "全文 OCR 边界校准"}
    constrained_statuses = {"相邻双锁约束", "章节边界约束", "页首锁边", "页尾锁边"}
    matched = sum(1 for item in manifest if item.get("kind") == "body" and item.get("status") in matched_statuses)
    constrained = sum(1 for item in manifest if item.get("kind") == "body" and item.get("status") in constrained_statuses)
    ocr_pages = sum(1 for item in manifest if item.get("kind") == "ocr")
    blank_pages = sum(1 for item in manifest if item.get("kind") == "blank")
    source_omitted = sum(1 for item in manifest if item.get("kind") == "source-omitted")
    unresolved = sum(1 for item in manifest if item.get("kind") == "unresolved")
    estimated = sum(1 for item in manifest if item.get("status") in {"连续估算", "估算"})
    warnings = sum(1 for item in manifest if item.get("continuityWarning"))
    scores = [int(item.get("confidence") or 0) for item in manifest]
    average = round(sum(scores) / max(1, len(scores)))
    return {
        "matched": matched,
        "constrained": constrained,
        "ocr": ocr_pages,
        "blank": blank_pages,
        "sourceOmitted": source_omitted,
        "unresolved": unresolved,
        "estimated": estimated,
        "warnings": warnings,
        "reviewRequired": unresolved + estimated + warnings + ocr_pages,
        "averageConfidence": average,
    }


def write_alignment_issues(job_id: str, manifest: list[dict]) -> Path:
    report = job_paths(job_id).root / "alignment-issues.csv"
    rows = [
        item for item in manifest
        if item.get("kind") in {"unresolved", "ocr"}
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
    extracted_norm, _ = normalize_for_match(extracted)
    expected_norm = expected_text_layer_norm(expected_text)
    if extracted_norm != expected_norm:
        raise ValueError("中间页文字层与锁定正文不一致，已停止整本输出。")
    if re.search(r"(?:U\+[0-9A-Fa-f]{4,6}){2,}", extracted):
        raise ValueError("中间页文字层出现编码串，已停止整本输出。")
    return len(extracted_norm)


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
    for index, row in enumerate(manifest):
        extracted = output_reader.pages[index].extract_text() or ""
        extracted_norm, _ = normalize_for_match(extracted)
        expected_norm = expected_text_layer_norm(str(row.get("text") or ""))
        if extracted_norm != expected_norm:
            raise ValueError(f"第 {index + 1} 页复制文字与锁定正文不一致，已停止发布。")
        if re.search(r"(?:U\+[0-9A-Fa-f]{4,6}){2,}", extracted):
            raise ValueError(f"第 {index + 1} 页出现编码串，已停止发布。")
        extracted_chars += len(extracted_norm)
        if status_job_id and ((index + 1) % 50 == 0 or index + 1 == page_count):
            update_pipeline_stage(
                status_job_id, "text-check", "running", state="running",
                processed=index + 1, total=page_count, detail=f"已验证第 {index + 1} 页",
            )

    if status_job_id:
        update_pipeline_stage(
            status_job_id, "text-check", "done", state="running",
            processed=page_count, total=page_count, detail=f"文字层一致，共 {extracted_chars} 个检索字符",
            metrics={"extractedChars": extracted_chars},
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
    return {"pages": page_count, "extractedChars": extracted_chars, "pixelCheckedPages": checked}


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
    building_pdf = paths.root / ".text-positioned-full.building.pdf"
    manifest_path = paths.root / "page-text-manifest.json"
    calibration = job.get("calibration") or {}
    if not calibration:
        raise ValueError("请先试一页并达到精准锁定，再生成整本。")
    update_pipeline_stage(
        job_id, "input", "done", state="planning",
        processed=1, total=1, detail=f"PDF {page_count} 页，EPUB {int(job.get('sourceUnitCount') or 0)} 个内容单元",
        message="PDF 与权威来源检查完成。",
    )
    manifest = (
        build_strict_page_manifest(job, reader, selected_layout, status_job_id=job_id)
        if STRICT_MANIFEST_ENABLED
        else build_fast_authoritative_manifest(job, reader, selected_layout, status_job_id=job_id)
    )
    for row in manifest:
        if row.get("kind") != "body" or not row.get("text"):
            continue
        page_no = int(row.get("page") or 0)
        scan_sample = "\n".join(ocr_page_anchor_pair(job, page_no, selected_layout))
        row["text"], script_note = adapt_text_to_scan_script(str(row["text"]), scan_sample)
        row["text"] = searchable_safe_text(str(row["text"]))
        if script_note:
            row["scriptAdjustment"] = script_note
    atomic_write_json(manifest_path, {"pages": manifest})
    alignment = manifest_summary(manifest, page_count)
    blockers = []
    if alignment["unresolved"] and not ALLOW_UNRESOLVED_OUTPUT:
        blockers.append(f"{alignment['unresolved']} 页未锁定")
    if alignment["estimated"] and not ALLOW_ESTIMATED_OUTPUT:
        blockers.append(f"{alignment['estimated']} 页仍是估算范围")
    if alignment["ocr"] and not ALLOW_OCR_OUTPUT:
        blockers.append(f"{alignment['ocr']} 页只有 OCR 文字")
    if alignment["warnings"] and not ALLOW_UNRESOLVED_OUTPUT:
        blockers.append(f"{alignment['warnings']} 处连续性警告")
    if blockers:
        issue_report = write_alignment_issues(job_id, manifest)
        return write_full_status(
            job_id,
            state="error",
            activeStage="align",
            processed=page_count,
            total=page_count,
            message=f"严格核对已完成，但还有{'、'.join(blockers)}。已停止发布 PDF，并生成待核对页清单。",
            alignment=alignment,
            validation={},
            outputs=[make_output_link(job_id, issue_report, "待核对页清单", "CSV")],
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
    previous_hash = build_key_path.read_text(encoding="utf-8").strip() if build_key_path.exists() else ""
    if previous_hash and previous_hash != manifest_hash:
        for old_page in page_dir.glob("page-*.pdf"):
            old_page.unlink(missing_ok=True)
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

    ensure_text_font()
    for page_no in range(1, page_count + 1):
        page_out = page_dir / f"page-{page_no:05d}.pdf"
        if page_out.exists() and page_out.stat().st_size > 0:
            try:
                validate_page_text_layer(page_out, str(manifest[page_no - 1].get("text") or ""))
                continue
            except Exception:
                page_out.unlink(missing_ok=True)

        writer = PdfWriter()
        writer.add_page(reader.pages[page_no - 1])
        page = writer.pages[0]
        manifest_row = manifest[page_no - 1]
        text = str(manifest_row.get("text") or "")
        image_size = None
        blocks = []
        if selected_layout == "horizontal":
            blocks = []
        else:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            blocks, image_size = stable_vertical_blocks(width, height, selected_layout)

        remove_text(page, writer)
        overlay = overlay_for_page(page, text, blocks, selected_layout, image_size)
        page.merge_page(overlay)

        write_pdf_atomic(writer, page_out)
        validate_page_text_layer(page_out, text)

        update_pipeline_stage(
            job_id,
            "layer",
            "running",
            state="running",
            processed=page_no,
            total=page_count,
            currentPage=page_no,
            detail=f"正在写入第 {page_no} 页",
            message=f"正在整理第 {page_no} / {page_count} 页。",
            alignment=alignment,
        )
        if read_full_status(job_id).get("pauseRequested"):
            break
        if stop_after and page_no >= stop_after:
            break

    page_files = [page_dir / f"page-{page_no:05d}.pdf" for page_no in range(1, page_count + 1)]
    complete = all(path.exists() and path.stat().st_size > 0 for path in page_files)
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
    extracted_norm, _ = normalize_for_match(extracted)
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
    geometry = {}
    expected_columns = [column for block in (blocks or []) for column in (block.get("ocrColumns") or [])]
    if expected_columns and image_size:
        try:
            import pdfplumber
            with pdfplumber.open(str(trial_pdf)) as document:
                pdf_page = document.pages[0]
                positioned_chars = [char for char in pdf_page.chars if float(char.get("size") or 0) >= 5]
                actual_x = sorted({round(float(char["x0"]), 1) for char in positioned_chars})
                expected_x = [float(column["x"]) * pdf_page.width / image_size[0] for column in expected_columns]
            if abs(len(actual_x) - len(expected_x)) > 1:
                raise ValueError("试页文字列数与扫描正文列数不一致，已停止输出。")
            deltas = sorted(min(abs(value - candidate) for candidate in actual_x) for value in expected_x)
            median_delta = deltas[len(deltas) // 2]
            if median_delta > 12:
                raise ValueError("试页文字列没有贴近扫描正文列，已停止输出。")
            geometry = {"positionedColumns": len(actual_x), "medianColumnDelta": round(median_delta, 2)}
        except ImportError:
            geometry = {"positionedColumns": len(expected_columns)}
    return {"extractedChars": len(extracted_norm), "pixelIdentical": True, **geometry}


def make_trial(job_id: str, page_no: int, layout: str) -> dict:
    paths = job_paths(job_id)
    job = json.loads(paths.meta.read_text(encoding="utf-8"))
    pdf_path = Path(job["pdf"])
    reader_for_count = PdfReader(str(pdf_path), strict=False)
    page_no = max(1, min(page_no, len(reader_for_count.pages)))

    selected_layout = layout if layout != "auto" else job.get("layout", "vertical-double")
    image = render_page_image(pdf_path, page_no)
    blocks = detect_horizontal_block(image) if selected_layout == "horizontal" else detect_vertical_blocks(image, selected_layout)
    preview_path = paths.root / f"page-{page_no:04d}-guides.png"

    reader = PdfReader(str(pdf_path), strict=False)
    resolved = resolve_page(job, reader, page_no, selected_layout, allow_discovery=True)
    if page_no > 1 and resolved.get("kind") == "body":
        previous = resolve_page(job, reader, page_no - 1, selected_layout, allow_discovery=False)
        resolved = apply_previous_page_continuity(job, previous, resolved)
    if resolved.get("kind") == "unresolved":
        start_sample = normalize_for_match(str(resolved.get("startAnchor") or ""))[0][:18]
        end_sample = normalize_for_match(str(resolved.get("endAnchor") or ""))[0][-18:]
        detail = " / ".join(value for value in (start_sample, end_sample) if value)
        raise ValueError(f"OCR 已读到这一页的两端，但还没有在同一可靠正文中双锁成功{f'：{detail}' if detail else ''}。系统没有强行放入错误文字。")
    if resolved.get("kind") == "blank":
        raise ValueError("这一页被判断为空白或纯图像页，不适合作为正文校准页。")

    writer = PdfWriter()
    writer.add_page(reader.pages[page_no - 1])
    source_page = writer.pages[0]
    remove_text(source_page, writer)
    text = str(resolved.get("text") or "")
    if resolved.get("kind") == "body":
        scan_sample = "\n".join(ocr_page_anchor_pair(job, page_no, selected_layout))
        text, script_note = adapt_text_to_scan_script(text, scan_sample)
        text = searchable_safe_text(text)
        if script_note:
            resolved["scriptAdjustment"] = script_note
    if not text.strip():
        raise ValueError("这一页没有得到可写入的文字，已停止生成。")
    if selected_layout != "horizontal":
        blocks = attach_ocr_column_geometry(job, page_no, image, blocks, text)
    draw_guides(image, blocks, preview_path)
    packet = io.BytesIO()
    width, height = float(source_page.mediabox.width), float(source_page.mediabox.height)
    overlay_canvas = canvas.Canvas(packet, pagesize=(width, height), pageCompression=1)
    if selected_layout == "horizontal":
        draw_horizontal_text(overlay_canvas, text, width, height)
    else:
        draw_vertical_text(overlay_canvas, text, blocks, width, height, image.width, image.height)
    draw_search_aliases(overlay_canvas, text, blocks, image.size)
    overlay_canvas.showPage()
    overlay_canvas.save()
    packet.seek(0)
    overlay = PdfReader(packet).pages[0]
    source_page.merge_page(overlay)
    trial_pdf = paths.root / f"page-{page_no:04d}-trial.pdf"
    write_pdf_atomic(writer, trial_pdf)

    validation = validate_trial_output(pdf_path, page_no, trial_pdf, text, blocks, image.size)
    if resolved.get("kind") == "body":
        job["calibration"] = {
            "page": page_no,
            "layout": selected_layout,
            "sourceTitle": resolved.get("sourceTitle", ""),
            "sourceUrl": resolved.get("sourceUrl", ""),
            "confidence": resolved.get("confidence", 0),
            "validatedAt": time.time(),
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
    job_id = make_job_id(pdf_original)
    paths = job_paths(job_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    write_upload(pdf_field, paths.pdf)
    source_text = ""
    source_original = ""
    source_units: list[SourceUnit] = []
    if source_field is not None and getattr(source_field, "filename", ""):
        source_original = safe_name(source_field.filename, "source.txt")
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

    inspected = inspect_pdf(paths.pdf, layout)
    meta = {
        "id": job_id,
        "pdf": str(paths.pdf),
        "pdfOriginal": pdf_original,
        "sourceText": str(paths.source) if source_text else "",
        "sourceOriginal": source_original,
        "sourceArchive": str(source_upload) if source_field is not None and source_original else "",
        "layout": inspected["layout"],
        "createdAt": time.time(),
    }
    atomic_write_json(paths.meta, meta)
    if source_units:
        save_source_units(meta, source_units)
    inspected["jobId"] = job_id
    if source_text:
        inspected["messages"].append(f"已读入参考文本，约 {len(source_text):,} 个字符。")
    else:
        inspected["messages"].append("没有参考文本时，可以先试一页看看版面定位。")
    return inspected
