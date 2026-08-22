from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from pypdf import PdfReader, PdfWriter

import server
import text_layer_engine as engine


class EngineTests(unittest.TestCase):
    def test_job_id_cannot_escape_cache_root(self):
        with self.assertRaises(ValueError):
            engine.job_paths("../../valuable")

    def test_horizontal_layer_keeps_all_text(self):
        packet = io.BytesIO()
        from reportlab.pdfgen import canvas

        pdf_canvas = canvas.Canvas(packet, pagesize=(595, 842), pageCompression=1)
        expected = "天地玄黄宇宙洪荒" * 80
        engine.draw_horizontal_text(pdf_canvas, expected, 595, 842)
        pdf_canvas.showPage()
        pdf_canvas.save()
        packet.seek(0)
        extracted = PdfReader(packet, strict=False).pages[0].extract_text() or ""
        self.assertEqual(engine.normalize_for_match(extracted)[0], engine.normalize_for_match(expected)[0])

    def test_common_glyph_variants_follow_the_scan(self):
        self.assertEqual(engine.normalize_for_match("徳宗再𭣣長安")[0], engine.normalize_for_match("德宗再收長安")[0])
        adjusted, note = engine.adapt_text_to_scan_script("徳宗再𭣣長安", "德宗自復京闕")
        self.assertEqual(adjusted, "德宗再收長安")
        self.assertTrue(note)

    def test_search_aliases_are_contiguous_and_bilingual(self):
        packet = io.BytesIO()
        from reportlab.pdfgen import canvas

        pdf_canvas = canvas.Canvas(packet, pagesize=(595, 842), pageCompression=1)
        engine.draw_search_aliases(pdf_canvas, "德宗自復，京闕國史")
        pdf_canvas.showPage()
        pdf_canvas.save()
        packet.seek(0)
        extracted = PdfReader(packet, strict=False).pages[0].extract_text() or ""
        self.assertIn("德宗自復京闕國史", extracted)
        self.assertIn("德宗自复京阙国史", extracted)
        self.assertNotIn("，", extracted)

    def test_local_search_fragments_cover_cross_column_phrases(self):
        text = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏" * 4
        fragments = [fragment for alias_index, _, fragment in engine.search_alias_fragments(text) if alias_index == 0]
        for start in range(0, len(text) - 24 + 1):
            self.assertTrue(any(text[start:start + 24] in fragment for fragment in fragments))

    def test_ocr_worker_count_adapts_but_stays_bounded(self):
        with patch.dict("os.environ", {"TEXT_LAYER_OCR_WORKERS": "7"}):
            self.assertEqual(engine.adaptive_ocr_workers(), 7)
        with patch.dict("os.environ", {"TEXT_LAYER_OCR_WORKERS": "99"}):
            self.assertEqual(engine.adaptive_ocr_workers(), 8)

    def test_stable_vertical_blocks_do_not_require_page_ocr(self):
        blocks, image_size = engine.stable_vertical_blocks(595, 842, "vertical-single")
        self.assertEqual(len(blocks), 1)
        self.assertGreaterEqual(len(blocks[0]["cols"]), 10)
        self.assertEqual(image_size[0], 1000)

    def test_full_validation_preserves_scan_and_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            output = root / "output.pdf"
            Image.new("RGB", (600, 800), "white").save(source, "PDF", resolution=150)

            reader = PdfReader(str(source), strict=False)
            writer = PdfWriter()
            writer.add_page(reader.pages[0])
            page = writer.pages[0]
            text = "庄子逍遥游北冥有鱼其名为鲲"
            overlay = engine.overlay_for_page(page, text, [], "horizontal", None)
            page.merge_page(overlay)
            engine.write_pdf_atomic(writer, output)

            result = engine.validate_full_output(
                source,
                output,
                [{"page": 1, "kind": "body", "text": text}],
            )
            self.assertEqual(result["pages"], 1)
            self.assertGreater(result["extractedChars"], 0)

    def test_status_file_remains_valid_under_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "0123456789abcdef"
            engine.job_paths(job_id).root.mkdir(parents=True)
            threads = [
                threading.Thread(target=engine.write_full_status, args=(job_id,), kwargs={"processed": index})
                for index in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            payload = json.loads((engine.job_paths(job_id).root / "full-status.json").read_text(encoding="utf-8"))
            self.assertIn("processed", payload)

    def test_pipeline_stage_persists_independent_progress(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1234567890abcdef"
            engine.job_paths(job_id).root.mkdir(parents=True)
            engine.write_full_status(job_id, state="planning", pipeline=engine.new_pipeline())
            status = engine.update_pipeline_stage(
                job_id,
                "ocr",
                "running",
                state="planning",
                processed=8,
                total=20,
                detail="2 路并行",
            )
            stage = next(item for item in status["pipeline"] if item["id"] == "ocr")
            self.assertEqual((stage["processed"], stage["total"]), (8, 20))
            self.assertEqual(stage["detail"], "2 路并行")
            self.assertEqual(status["processed"], 8)
            self.assertEqual(status["activeStage"], "ocr")

    def test_first_page_is_not_automatically_front_matter(self):
        page = SimpleNamespace(extract_text=lambda: "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜")
        reader = SimpleNamespace(pages=[page])
        decision = engine.classify_page({"pdf": "unused.pdf"}, reader, 1, "horizontal")
        self.assertEqual(decision["kind"], "body")

    def test_full_build_is_validated_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "0011223344556677"
            paths = engine.job_paths(job_id)
            paths.root.mkdir(parents=True)
            images = [Image.new("RGB", (500, 700), "white") for _ in range(2)]
            images[0].save(paths.pdf, "PDF", save_all=True, append_images=images[1:], resolution=150)
            metadata = {
                "id": job_id,
                "pdf": str(paths.pdf),
                "pdfOriginal": "scan.pdf",
                "sourceText": "",
                "sourceOriginal": "",
                "layout": "horizontal",
                "calibration": {"page": 1, "layout": "horizontal"},
            }
            engine.atomic_write_json(paths.meta, metadata)
            manifest = [
                {"page": 1, "kind": "body", "status": "双头锁边", "text": "第一页目录文字", "confidence": 99},
                {"page": 2, "kind": "body", "status": "双头锁边", "text": "第二页目录文字", "confidence": 99},
            ]
            with patch.object(engine, "build_strict_page_manifest", return_value=manifest):
                result = engine.build_full_pdf(job_id, "horizontal")
            self.assertEqual(result["state"], "done")
            self.assertTrue((paths.root / "text-positioned-full.pdf").is_file())
            self.assertEqual(result["validation"]["pages"], 2)

    def test_epub_uses_spine_order_instead_of_filename_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            epub = Path(temporary) / "book.epub"
            with zipfile.ZipFile(epub, "w") as archive:
                archive.writestr("META-INF/container.xml", """<?xml version="1.0"?>
                    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                      <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles>
                    </container>""")
                archive.writestr("OPS/book.opf", """<package xmlns="http://www.idpf.org/2007/opf">
                    <manifest>
                      <item id="c1" href="chapter1.xhtml"/>
                      <item id="c2" href="chapter2.xhtml"/>
                      <item id="c10" href="chapter10.xhtml"/>
                    </manifest>
                    <spine><itemref idref="c1"/><itemref idref="c2"/><itemref idref="c10"/></spine>
                    </package>""")
                archive.writestr("OPS/chapter1.xhtml", "<html><title>一</title><body>第一章</body></html>")
                archive.writestr("OPS/chapter2.xhtml", "<html><title>二</title><body>第二章</body></html>")
                archive.writestr("OPS/chapter10.xhtml", "<html><title>十</title><body>第十章</body></html>")
            units = engine.epub_spine_units(epub)
            self.assertEqual([unit.text for unit in units], ["一\n第一章", "二\n第二章", "十\n第十章"])

    def test_unresolved_pages_block_pdf_publication(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1021324354657687"
            paths = engine.job_paths(job_id)
            paths.root.mkdir(parents=True)
            Image.new("RGB", (500, 700), "white").save(paths.pdf, "PDF", resolution=150)
            engine.atomic_write_json(paths.meta, {
                "id": job_id,
                "pdf": str(paths.pdf),
                "sourceText": "",
                "layout": "horizontal",
                "calibration": {"page": 1, "layout": "horizontal"},
            })
            manifest = [{"page": 1, "kind": "unresolved", "status": "未锁定", "text": "", "reason": "test"}]
            with patch.object(engine, "build_strict_page_manifest", return_value=manifest):
                result = engine.build_full_pdf(job_id, "horizontal")
            self.assertEqual(result["state"], "error")
            self.assertFalse((paths.root / "text-positioned-full.pdf").exists())
            self.assertTrue((paths.root / "alignment-issues.csv").exists())

    def test_source_omitted_pages_are_separate_from_unresolved_body(self):
        previous_unit = engine.SourceUnit("前章", "chapter-1", "前章正文起点天地玄黄前章末尾唯一文字", kind="epub")
        following_unit = engine.SourceUnit("后章", "chapter-2", "后章开头唯一文字宇宙洪荒后章正文终点", kind="epub")
        previous_norm, _ = engine.normalize_for_match(previous_unit.text)
        following_norm, _ = engine.normalize_for_match(following_unit.text)
        previous_end = previous_norm.index("前章末尾唯一文字")
        following_start = following_norm.index("宇宙洪荒")
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": previous_end},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 3, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 4, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 5, "kind": "body", "sourceUrl": "chapter-2", "start": following_start},
        ]
        anchors = {
            2: ("前章末尾唯一文字", "前章末尾唯一文字"),
            3: ("校勘记不在电子书", "校勘记不在电子书"),
            4: ("后章开头唯一文字", "后章开头唯一文字"),
        }
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        with patch.object(engine, "page_anchor_pair", side_effect=lambda _job, _reader, page, _layout: anchors[page]):
            engine.mark_source_omitted_pages(
                {}, reader, manifest, "vertical-single", [previous_unit, following_unit]
            )
        self.assertEqual(manifest[1]["kind"], "body")
        self.assertEqual(manifest[2]["kind"], "source-omitted")
        self.assertEqual(manifest[3]["kind"], "body")
        summary = engine.manifest_summary(manifest, len(manifest))
        self.assertEqual(summary["sourceOmitted"], 1)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["reviewRequired"], 0)


class FileServerTests(unittest.TestCase):
    def test_pdf_supports_range_and_head_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = Path(temporary)
            job_id = "fedcba9876543210"
            job_root = jobs / job_id
            job_root.mkdir()
            payload = b"0123456789" * 1000
            (job_root / "book.pdf").write_bytes(payload)

            with patch.object(server, "JOBS_DIR", jobs):
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{httpd.server_port}/jobs/{job_id}/book.pdf"
                try:
                    request = Request(base, headers={"Range": "bytes=100-199"})
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.headers["Accept-Ranges"], "bytes")
                        self.assertEqual(response.read(), payload[100:200])

                    request = Request(base, method="HEAD")
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(int(response.headers["Content-Length"]), len(payload))

                    request = Request(base, headers={"Range": "bytes=999999-"})
                    with self.assertRaises(HTTPError) as context:
                        urlopen(request, timeout=5)
                    self.assertEqual(context.exception.code, 416)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
