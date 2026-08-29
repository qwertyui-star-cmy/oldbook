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

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream

import server
import text_layer_engine as engine


class EngineTests(unittest.TestCase):
    def test_job_id_cannot_escape_cache_root(self):
        with self.assertRaises(ValueError):
            engine.job_paths("../../valuable")

    def test_identical_inputs_reuse_existing_job(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            pdf_packet = io.BytesIO()
            from reportlab.pdfgen import canvas

            pdf_canvas = canvas.Canvas(pdf_packet, pagesize=(200, 300))
            pdf_canvas.showPage()
            pdf_canvas.save()
            pdf_bytes = pdf_packet.getvalue()

            def upload(name: str, payload: bytes):
                return SimpleNamespace(filename=name, file=io.BytesIO(payload))

            first = engine.create_job(
                upload("same.pdf", pdf_bytes), upload("same.txt", "权威正文".encode("utf-8")), "", "vertical-single"
            )
            second = engine.create_job(
                upload("same.pdf", pdf_bytes), upload("same.txt", "权威正文".encode("utf-8")), "", "vertical-single"
            )

            self.assertEqual(first["jobId"], second["jobId"])
            self.assertTrue(second["reused"])
            self.assertEqual(len(list(Path(temporary).glob("*/job.json"))), 1)

    def test_completed_output_is_reused_only_for_current_engine_and_layout(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1122334455667788"
            paths = engine.job_paths(job_id)
            paths.root.mkdir(parents=True)
            engine.atomic_write_json(paths.meta, {"id": job_id, "layout": "vertical-single"})
            (paths.root / "text-positioned-full.pdf").write_bytes(b"pdf")
            engine.write_full_status(
                job_id,
                state="done",
                engineVersion=engine.LAYOUT_ENGINE_VERSION,
                outputLayout="vertical-single",
            )

            self.assertTrue(engine.completed_output_is_current(job_id, "vertical-single"))
            self.assertFalse(engine.completed_output_is_current(job_id, "horizontal"))

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

    def test_common_glyph_variants_normalize_for_matching_only(self):
        self.assertEqual(engine.normalize_for_match("徳宗再𭣣長安")[0], engine.normalize_for_match("德宗再收長安")[0])
        self.assertEqual(engine.normalize_for_match("漢爲國")[0], engine.normalize_for_match("汉为国")[0])

    def test_ocr_worker_count_adapts_but_stays_bounded(self):
        with (
            patch.object(engine.os, "cpu_count", return_value=16),
            patch.object(engine, "available_memory_mb", return_value=20000),
            patch.dict("os.environ", {"TEXT_LAYER_OCR_WORKERS": "7"}),
        ):
            self.assertEqual(engine.adaptive_ocr_workers(), 4)
        with (
            patch.object(engine.os, "cpu_count", return_value=4),
            patch.object(engine, "available_memory_mb", return_value=20000),
            patch.dict("os.environ", {"TEXT_LAYER_OCR_WORKERS": "99"}),
        ):
            self.assertEqual(engine.adaptive_ocr_workers(), 1)

    def test_ocr_pool_keeps_capacity_when_current_memory_allows_one_worker(self):
        with (
            patch.object(engine.os, "cpu_count", return_value=16),
            patch.object(engine, "available_memory_mb", return_value=4000),
            patch.dict("os.environ", {}, clear=True),
        ):
            self.assertEqual(engine.ocr_worker_capacity(), 4)
            self.assertEqual(engine.adaptive_ocr_workers(), 1)

    def test_grouped_ocr_uses_grid_without_mixing_crop_coordinates(self):
        seen_sizes = []

        def fake_ocr(image):
            seen_sizes.append(image.size)
            return SimpleNamespace(
                boxes=[
                    [[10, 10], [30, 10], [30, 30], [10, 30]],
                    [[146, 10], [166, 10], [166, 30], [146, 30]],
                    [[10, 246], [30, 246], [30, 266], [10, 266]],
                    [[146, 246], [166, 246], [166, 266], [146, 266]],
                ],
                txts=["甲", "乙", "丙", "丁"],
                scores=[0.99, 0.99, 0.99, 0.99],
            )

        crops = [Image.new("RGB", (100, 200), "white") for _ in range(4)]
        with patch.object(engine, "get_ocr_engine", return_value=fake_ocr):
            result = engine.ocr_grouped_images(crops, "vertical-single")

        self.assertEqual(result, ["甲", "乙", "丙", "丁"])
        self.assertEqual(seen_sizes, [(236, 436)])

    def test_full_page_ocr_explicitly_restores_detection_mode(self):
        calls = []

        def fake_ocr(_image, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(boxes=[], txts=[], scores=[])

        with patch.object(engine, "get_ocr_engine", return_value=fake_ocr):
            engine.ocr_image_payload(Image.new("RGB", (300, 400), "white"), "horizontal")

        self.assertEqual(calls, [{"use_det": True, "use_cls": True, "use_rec": True}])

    def test_vertical_two_column_fallback_uses_detected_column_edges(self):
        image = Image.new("RGB", (420, 600), "white")
        blocks = [{"inner": (20, 20, 400, 580)}]
        runs = [(45, 75), (120, 150), (245, 275), (330, 360)]
        primary = engine.ocr_anchor_crops(image, "vertical-single", units=1, blocks=blocks, edge_runs=(runs, runs))
        expanded = engine.ocr_anchor_crops(image, "vertical-single", units=2, blocks=blocks, edge_runs=(runs, runs))

        self.assertGreater(expanded[0].width, primary[0].width)
        self.assertGreater(expanded[1].width, primary[1].width)
        self.assertGreaterEqual(expanded[0].width, 120)

    def test_four_character_anchor_uses_two_column_ocr_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf_path = Path(temporary) / "scan.pdf"
            pdf_path.write_bytes(b"placeholder")
            crops = [Image.new("RGB", (10, 10), "white") for _ in range(2)]
            with (
                patch.object(engine, "detect_vertical_blocks", return_value=[{"inner": (0, 0, 10, 10)}]),
                patch.object(engine, "text_column_runs", return_value=[(0, 5), (5, 10)]),
                patch.object(engine, "ocr_anchor_crops", return_value=crops),
                patch.object(engine, "ocr_grouped_images", return_value=["天地玄黄", "页尾文字足够", "天地玄黄宇宙", "扩展页尾文字"]),
                patch.object(engine, "source_search_corpus", return_value="天地玄黄宇宙页尾文字足够"),
            ):
                start, end = engine.ocr_page_anchor_pair(
                    {"pdf": str(pdf_path), "inputFingerprint": "test"}, 1, "vertical-single",
                    rendered_image=Image.new("RGB", (10, 10), "white"),
                )

            self.assertEqual(start, "天地玄黄宇宙")
            self.assertEqual(end, "页尾文字足够")

    def test_pdfium_document_is_opened_once_per_worker(self):
        opened = []

        class FakeBitmap:
            def to_pil(self):
                return Image.new("RGB", (20, 30), "white")

            def close(self):
                pass

        class FakePage:
            def render(self, scale):
                self.scale = scale
                return FakeBitmap()

            def close(self):
                pass

        class FakeDocument:
            def __init__(self, path):
                opened.append(path)

            def __getitem__(self, index):
                return FakePage()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "book.pdf"
            pdf.write_bytes(b"cache identity only")
            engine.close_pdfium_documents()
            with patch.object(engine, "pdfium", SimpleNamespace(PdfDocument=FakeDocument)):
                first = engine.render_page_images_persistent(pdf, [1, 2], dpi=144)
                second = engine.render_page_images_persistent(pdf, [3], dpi=144)
            engine.close_pdfium_documents()

        self.assertEqual(len(opened), 1)
        self.assertEqual([image.size for image in [*first, *second]], [(20, 30)] * 3)

    def test_anchor_cache_is_scoped_to_layout_and_input(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1234567890abcdef"
            root = engine.job_paths(job_id).root
            root.mkdir(parents=True)
            engine.atomic_write_json(root / f"page-0001-ocr-anchors-v{engine.ANCHOR_CACHE_VERSION}.json", {
                "complete": True,
                "start": "天地",
                "end": "玄黄",
                "layout": "vertical-single",
                "inputFingerprint": "book-a",
            })

            self.assertTrue(engine.anchor_cache_ready(job_id, 1, "vertical-single", "book-a"))
            self.assertFalse(engine.anchor_cache_ready(job_id, 1, "horizontal", "book-a"))
            self.assertFalse(engine.anchor_cache_ready(job_id, 1, "vertical-single", "book-b"))

    def test_stream_commit_requires_exact_reverse_confirmed_boundary(self):
        previous = {
            "kind": "body", "status": "双头锁边", "sourceUrl": "chapter-1",
            "rawStart": 0, "rawEnd": 12,
        }
        current = {
            "kind": "body", "status": "双头锁边", "sourceUrl": "chapter-1",
            "rawStart": 12, "rawEnd": 25,
        }

        self.assertTrue(engine.strict_pair_committable(previous, current))
        self.assertFalse(engine.strict_pair_committable(previous, {**current, "rawStart": 13}))
        self.assertFalse(engine.strict_pair_committable(previous, {**current, "sourceUrl": "chapter-2"}))
        self.assertFalse(engine.strict_pair_committable({**previous, "status": "连续区间恢复"}, current))

    def test_corrupt_page_layer_cache_is_rebuilt_instead_of_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "page-00001.pdf"
            signature = root / "page-00001.sha256"
            page.write_bytes(b"not a pdf")
            signature.write_text("expected", encoding="ascii")

            self.assertFalse(engine.page_layer_cache_valid(page, signature, "expected", "正文"))

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
            self.assertEqual(len(result["continuousTextHash"]), 64)

    def test_full_validation_reports_every_sampled_visual_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            output = root / "output.pdf"
            Image.new("RGB", (200, 300), "white").save(source, "PDF", resolution=72)
            Image.new("RGB", (200, 300), "black").save(output, "PDF", resolution=72)

            with self.assertRaises(engine.VisualMismatchError) as raised:
                engine.validate_full_output(
                    source,
                    output,
                    [{"page": 1, "kind": "blank", "text": ""}],
                )
            self.assertEqual(raised.exception.pages, [1])

    def test_production_overlay_contains_one_exact_authoritative_stream(self):
        packet = io.BytesIO()
        from reportlab.pdfgen import canvas

        source_canvas = canvas.Canvas(packet, pagesize=(595, 842), pageCompression=1)
        source_canvas.showPage()
        source_canvas.save()
        packet.seek(0)
        page = PdfReader(packet, strict=False).pages[0]
        expected = "侯。追尊宣王爲宣皇帝，禮樂制度皆如魏舊。"

        overlay = engine.overlay_for_page(page, expected, [], "vertical-single", (595, 842))
        extracted = overlay.extract_text() or ""

        self.assertEqual(engine.canonical_output_text(extracted), engine.canonical_output_text(expected))
        self.assertNotIn("为宣皇帝", extracted)

    def test_pure_ocr_overlay_uses_detected_page_coordinates(self):
        packet = io.BytesIO()
        from reportlab.pdfgen import canvas

        source_canvas = canvas.Canvas(packet, pagesize=(200, 300), pageCompression=1)
        source_canvas.showPage()
        source_canvas.save()
        packet.seek(0)
        page = PdfReader(packet, strict=False).pages[0]
        expected = "侯。追尊宣王爲宣皇帝，禮樂制度皆如魏舊。"
        geometry = {
            "imageSize": [1000, 1500],
            "items": [{
                "text": expected,
                "box": [[800, 200], [860, 200], [860, 1300], [800, 1300]],
                "score": 0.99,
            }],
        }

        overlay = engine.overlay_for_page(page, expected, [], "vertical-single", None, geometry)
        extracted = overlay.extract_text() or ""
        positions = []

        def collect_position(text, _cm, text_matrix, _font, _size):
            if text.strip():
                positions.append(float(text_matrix[4]))

        overlay.extract_text(visitor_text=collect_position)

        self.assertEqual(engine.canonical_output_text(extracted), engine.canonical_output_text(expected))
        self.assertTrue(positions)
        self.assertGreater(min(positions), 150)
        self.assertLess(max(positions), 175)

    def test_pure_ocr_overlay_never_falls_back_to_authoritative_layout(self):
        packet = io.BytesIO()
        from reportlab.pdfgen import canvas

        source_canvas = canvas.Canvas(packet, pagesize=(200, 300), pageCompression=1)
        source_canvas.showPage()
        source_canvas.save()
        packet.seek(0)
        page = PdfReader(packet, strict=False).pages[0]

        with self.assertRaisesRegex(ValueError, "缺少可用文字坐标"):
            engine.overlay_for_page(page, "非权威 OCR 文字", [], "vertical-single", None, {})

    def test_ancient_vertical_coverage_detects_a_missing_ink_column(self):
        image = Image.new("L", (1000, 1400), "white")
        draw = ImageDraw.Draw(image)
        centers = [700, 580, 460, 340]
        for center in centers:
            draw.rectangle((center - 18, 220, center + 18, 1180), fill="black")
        items = []
        for center in centers[:-1]:
            items.append({
                "text": "古籍正文",
                "box": [[center - 20, 210], [center + 20, 210], [center + 20, 1190], [center - 20, 1190]],
                "score": 0.98,
            })

        coverage = engine.evaluate_ocr_coverage(image.convert("RGB"), items, "vertical-single")

        self.assertEqual(coverage["expectedColumns"], 4)
        self.assertEqual(coverage["coveredColumns"], 3)
        self.assertFalse(coverage["complete"])
        self.assertAlmostEqual(coverage["missingColumns"][0]["cx"], 340, delta=3)

    def test_ancient_vertical_coverage_rejects_a_partially_recognized_column(self):
        image = Image.new("L", (700, 1400), "white")
        draw = ImageDraw.Draw(image)
        center = 420
        for top in range(220, 1180, 110):
            draw.rectangle((center - 22, top, center + 22, top + 70), fill="black")
        items = [{
            "text": "古籍正文",
            "box": [[center - 24, 210], [center + 24, 210], [center + 24, 1190], [center - 24, 1190]],
            "score": 0.98,
        }]

        coverage = engine.evaluate_ocr_coverage(image.convert("RGB"), items, "vertical-single")

        self.assertEqual(coverage["expectedColumns"], 1)
        self.assertEqual(coverage["coveredColumns"], 1)
        self.assertEqual(len(coverage["weakColumns"]), 1)
        self.assertFalse(coverage["complete"])

    def test_ancient_vertical_coverage_does_not_count_toc_dots_as_missing_words(self):
        image = Image.new("L", (700, 1400), "white")
        draw = ImageDraw.Draw(image)
        center = 420
        draw.rectangle((center - 22, 210, center + 22, 370), fill="black")
        for top in range(410, 1160, 35):
            draw.ellipse((center - 7, top, center + 7, top + 14), fill="black")
        items = [{
            "text": "目录三六",
            "box": [[center - 24, 200], [center + 24, 200], [center + 24, 1190], [center - 24, 1190]],
            "score": 0.98,
        }]

        coverage = engine.evaluate_ocr_coverage(image.convert("RGB"), items, "vertical-single")

        self.assertTrue(coverage["complete"])
        self.assertTrue(engine.detect_ancient_vertical_columns(image)[0]["dottedLeader"])

    def test_missing_vertical_column_retry_builds_positioned_item(self):
        calls = []

        def fake_ocr(image, **kwargs):
            calls.append((image.size, kwargs))
            return SimpleNamespace(txts=["補識別文字"], scores=[0.92])

        missing = [{"x0": 390, "x1": 430, "y0": 120, "y1": 680, "cx": 410}]
        image = Image.new("RGB", (800, 900), "white")

        with patch.object(engine, "get_ocr_engine", return_value=fake_ocr):
            items = engine.supplement_missing_vertical_columns(image, [], missing)

        self.assertEqual(calls[0][1], {"use_det": False, "use_cls": False, "use_rec": True, "text_score": 0.0})
        self.assertGreaterEqual(calls[0][0][1], engine.MIN_COLUMN_OCR_WIDTH)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "補識別文字")
        self.assertEqual(items[0]["origin"], "missing-column-recognition")
        self.assertGreater(items[0]["box"][0][1], 0)
        self.assertAlmostEqual(items[0]["cx"], 410, delta=12)

    def test_full_page_ocr_retries_only_missing_columns_at_high_dpi(self):
        rendered = []

        def fake_render(_pdf, _pages, dpi):
            rendered.append(dpi)
            return [Image.new("RGB", (dpi * 4, dpi * 6), "white")]

        column = {"x0": 300, "x1": 340, "y0": 100, "y1": 700, "cx": 320}
        incomplete = {"complete": False, "expectedColumns": 8, "missingColumns": [column], "weakColumns": []}
        complete = {"complete": True, "expectedColumns": 8, "missingColumns": [], "weakColumns": []}
        initial = {"text": "初识", "items": [], "coverage": incomplete}
        regional_items = [[{"text": "二次识别", "cx": 320, "cy": 400, "box": [[300, 100], [340, 100], [340, 700], [300, 700]]}],
                          [{"text": "完整识别", "cx": 320, "cy": 400, "box": [[300, 100], [340, 100], [340, 700], [300, 700]]}]]
        with (
            patch.object(engine, "render_page_images", side_effect=fake_render),
            patch.object(engine, "ocr_image_payload", return_value=initial),
            patch.object(engine, "supplement_high_resolution_vertical_columns", side_effect=regional_items) as regional,
            patch.object(engine, "evaluate_ocr_coverage", side_effect=[incomplete, complete]),
        ):
            payload = engine.adaptive_full_page_ocr(Path("book.pdf"), 1, "vertical-single")

        self.assertEqual(rendered, [engine.FULL_OCR_BASE_DPI])
        self.assertEqual([call.kwargs["dpi"] for call in regional.call_args_list], list(engine.FULL_OCR_RETRY_DPIS))
        self.assertEqual(payload["text"], "完整识别")
        self.assertEqual(payload["renderDpi"], engine.FULL_OCR_BASE_DPI)
        self.assertEqual(payload["attemptedDpis"], [engine.FULL_OCR_BASE_DPI, *engine.FULL_OCR_RETRY_DPIS])
        self.assertTrue(payload["adaptiveRetry"])

    def test_high_resolution_column_items_map_back_to_base_page(self):
        base = Image.new("RGB", (1000, 1400), "white")
        column = {"x0": 390, "x1": 430, "y0": 200, "y1": 1200, "cx": 410}
        regional_item = {
            "text": "補識別", "box": [[20, 30], [80, 30], [80, 970], [20, 970]],
            "cx": 50, "cy": 500, "score": .9,
        }
        with (
            patch.object(engine, "render_page_region", return_value=Image.new("RGB", (100, 1000), "white")),
            patch.object(engine, "supplement_missing_vertical_columns", return_value=[regional_item]),
        ):
            items = engine.supplement_high_resolution_vertical_columns(
                Path("book.pdf"), 1, base, [], [column], dpi=230
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["origin"], "missing-column-230dpi")
        self.assertAlmostEqual(items[0]["cx"], 410, delta=25)
        self.assertGreater(items[0]["cy"], column["y0"])
        self.assertLess(items[0]["cy"], column["y1"])

    def test_full_page_ocr_keeps_base_dpi_when_coverage_is_complete(self):
        with (
            patch.object(engine, "render_page_images", return_value=[Image.new("RGB", (600, 900), "white")]) as render,
            patch.object(engine, "ocr_image_payload", return_value={"text": "完整", "coverage": {"complete": True}}),
        ):
            payload = engine.adaptive_full_page_ocr(Path("book.pdf"), 1, "vertical-single")

        render.assert_called_once_with(Path("book.pdf"), [1], dpi=engine.FULL_OCR_BASE_DPI)
        self.assertEqual(payload["renderDpi"], engine.FULL_OCR_BASE_DPI)
        self.assertEqual(payload["attemptedDpis"], [engine.FULL_OCR_BASE_DPI])
        self.assertNotIn("adaptiveRetry", payload)

    def test_complex_retry_reuses_saved_base_ocr(self):
        base_payload = {
            "text": "基础识别",
            "items": [],
            "coverage": {"complete": False, "expectedColumns": 1, "missingColumns": [], "weakColumns": []},
        }
        with (
            patch.object(engine, "render_page_images", return_value=[Image.new("RGB", (600, 900), "white")]) as render,
            patch.object(engine, "ocr_image_payload") as base_ocr,
        ):
            payload = engine.adaptive_full_page_ocr(
                Path("book.pdf"), 1, "vertical-single", base_payload=base_payload,
            )

        base_ocr.assert_not_called()
        render.assert_not_called()
        self.assertEqual(payload["text"], "基础识别")
        self.assertEqual(payload["attemptedDpis"], [engine.FULL_OCR_BASE_DPI])

    def test_saved_column_structure_can_be_rechecked_without_base_image(self):
        column = {"x0": 380, "x1": 420, "y0": 100, "y1": 700, "cx": 400, "estimatedTextChars": 8}
        coverage = {
            "complete": False, "expectedColumns": 1, "coveredColumns": 0,
            "missingColumns": [column], "weakColumns": [], "imageWidth": 800, "inkRatio": .12,
        }
        items = [{"text": "补识别文字足够", "box": [[380, 100], [420, 100], [420, 700], [380, 700]]}]

        updated = engine.reevaluate_saved_ocr_coverage(coverage, items)

        self.assertTrue(updated["complete"])
        self.assertEqual(updated["missingColumns"], [])
        self.assertEqual(updated["coveragePercent"], 100.0)

    def test_page_classification_cache_is_bound_to_ocr_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"pdf": str(Path(temporary) / "book.pdf"), "inputFingerprint": "input-1"}
            decision = {"kind": "blank", "reason": "空白页"}
            engine.save_page_classification(job, 3, "vertical-single", "原文字", decision)

            self.assertEqual(
                engine.cached_page_classification(job, 3, "vertical-single", "原文字"), decision,
            )
            self.assertIsNone(
                engine.cached_page_classification(job, 3, "vertical-single", "变化后的文字"),
            )

    def test_incomplete_ocr_cache_is_retried_unless_page_is_blank(self):
        with tempfile.TemporaryDirectory() as temporary:
            job = {"pdf": str(Path(temporary) / "book.pdf"), "inputFingerprint": "input-1"}
            text = "残缺识别"
            engine.atomic_write_text(engine.full_ocr_cache_path(job, 2, "vertical-single"), text)
            engine.atomic_write_json(engine.full_ocr_layout_path(job, 2, "vertical-single"), {
                "text": text,
                "items": [],
                "imageSize": [600, 900],
                "coverage": {"complete": False, "missingColumns": [{"cx": 300}]},
            })

            self.assertFalse(engine.full_ocr_cache_ready(job, 2, "vertical-single"))
            engine.save_page_classification(
                job, 2, "vertical-single", text, {"kind": "blank", "reason": "空白页"},
            )
            self.assertTrue(engine.full_ocr_cache_ready(job, 2, "vertical-single"))

    def test_word_style_frame_reduces_font_instead_of_overflowing(self):
        usable_w = 595 - 2 * (3 * 72 / 2.54)
        usable_h = 842 - 2 * (3 * 72 / 2.54)

        normal_size = engine.word_frame_font_size(400, usable_w, usable_h)
        reduced_size = engine.word_frame_font_size(4000, usable_w, usable_h)

        self.assertEqual(normal_size, 12.0)
        self.assertLess(reduced_size, 12.0)
        capacity = int(usable_w / reduced_size) * int(usable_h / reduced_size)
        self.assertGreaterEqual(capacity, 4000)

    def test_remove_text_keeps_scanned_image_operations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "scan.png"
            pdf_path = root / "mixed.pdf"
            Image.new("RGB", (40, 40), "black").save(image_path)
            from reportlab.pdfgen import canvas

            source_canvas = canvas.Canvas(str(pdf_path), pagesize=(200, 200), pageCompression=1)
            source_canvas.drawImage(str(image_path), 20, 20, 160, 160)
            source_canvas.drawString(30, 180, "old OCR")
            source_canvas.showPage()
            source_canvas.save()

            reader = PdfReader(str(pdf_path), strict=False)
            writer = PdfWriter()
            writer.add_page(reader.pages[0])
            page = writer.pages[0]
            engine.remove_text(page, writer)
            operators = [operator for _, operator in ContentStream(page.get_contents(), writer).operations]

            self.assertIn(b"Do", operators)
            self.assertNotIn(b"Tj", operators)
            self.assertNotIn(b"TJ", operators)

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

    def test_pipeline_stage_restart_resets_elapsed_time(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "abcdef1234567890"
            engine.job_paths(job_id).root.mkdir(parents=True)
            engine.write_full_status(job_id, state="planning", pipeline=engine.new_pipeline())
            with patch.object(engine.time, "time", return_value=1000.0):
                engine.update_pipeline_stage(job_id, "align", "running", state="planning", processed=1, total=10)
            with patch.object(engine.time, "time", return_value=1010.0):
                engine.update_pipeline_stage(job_id, "align", "blocked", state="planning", processed=10, total=10)
            with patch.object(engine.time, "time", return_value=2000.0):
                status = engine.update_pipeline_stage(job_id, "align", "running", state="planning", processed=0, total=10)

            stage = next(item for item in status["pipeline"] if item["id"] == "align")
            self.assertEqual(stage["startedAt"], 2000.0)
            self.assertNotIn("endedAt", stage)
            self.assertEqual(stage["elapsedSeconds"], 0)
            with patch.object(engine.time, "time", return_value=3000.0):
                status = engine.update_pipeline_stage(job_id, "ocr", "done", state="planning", processed=10, total=10)
            stage = next(item for item in status["pipeline"] if item["id"] == "ocr")
            self.assertEqual(stage["startedAt"], 3000.0)
            self.assertEqual(stage["endedAt"], 3000.0)
            self.assertEqual(stage["elapsedSeconds"], 0)

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
            }
            engine.atomic_write_json(paths.meta, metadata)
            manifest = [
                {"page": 1, "kind": "body", "status": "页首与次页页首锁边", "text": "第一页权威文字", "confidence": 99},
                {"page": 2, "kind": "ocr", "status": "整页 OCR", "text": "第二页OCR文字", "confidence": 70},
            ]
            with patch.object(engine, "build_strict_page_manifest", return_value=manifest):
                result = engine.build_full_pdf(job_id, "horizontal")
            self.assertEqual(result["state"], "done")
            self.assertTrue((paths.root / "text-positioned-full.pdf").is_file())
            self.assertEqual(result["validation"]["pages"], 2)
            self.assertEqual(result["alignment"]["ocr"], 1)
            self.assertNotIn("calibration", metadata)

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

    def test_epub_non_spine_navigation_is_available_before_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            epub = Path(temporary) / "book.epub"
            with zipfile.ZipFile(epub, "w") as archive:
                archive.writestr("META-INF/container.xml", """<?xml version="1.0"?>
                    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                      <rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles>
                    </container>""")
                archive.writestr("OPS/book.opf", """<package xmlns="http://www.idpf.org/2007/opf">
                    <manifest>
                      <item id="nav" href="nav.xhtml" properties="nav"/>
                      <item id="body" href="body.xhtml"/>
                    </manifest>
                    <spine><itemref idref="body"/></spine>
                    </package>""")
                archive.writestr("OPS/nav.xhtml", "<html><title>目录</title><body>帝纪第一帝纪第二</body></html>")
                archive.writestr("OPS/body.xhtml", "<html><title>帝纪第一</title><body>正文天地玄黄</body></html>")

            units = engine.epub_spine_units(epub)

            self.assertEqual([unit.title for unit in units], ["目录", "帝纪第一"])
            self.assertIn("帝纪第一帝纪第二", units[0].text)

    def test_special_page_matches_authority_before_ocr_classification(self):
        authority_match = {
            "start": 0, "end": 20, "rawStart": 0, "rawEnd": 20,
            "text": "目录帝纪第一帝纪第二", "confidence": 95,
            "sourceTitle": "目录", "sourceUrl": "toc", "sourceKind": "epub",
            "authorityRank": 45,
        }
        reader = SimpleNamespace(pages=[SimpleNamespace()])
        with (
            patch.object(engine, "FULL_OCR_FALLBACK_ENABLED", True),
            patch.object(engine, "ocr_page_text", return_value="目录\n帝纪第一\n帝纪第二"),
            patch.object(engine, "classify_page", return_value={"kind": "ocr", "reason": "短条目密集页面"}),
            patch.object(engine, "page_anchor_pair", return_value=("目录帝纪第一", "帝纪第二")),
            patch.object(engine, "match_page_source", return_value=authority_match),
        ):
            result = engine.resolve_page({}, reader, 1, "vertical-single", candidate_units=[])

        self.assertEqual(result["kind"], "body")
        self.assertEqual(result["text"], "目录帝纪第一帝纪第二")

    def test_unmatched_special_page_does_not_publish_ocr_text(self):
        reader = SimpleNamespace(pages=[SimpleNamespace()])
        with (
            patch.object(engine, "FULL_OCR_FALLBACK_ENABLED", True),
            patch.object(engine, "ocr_page_text", return_value="出版说明\n本书整理说明"),
            patch.object(engine, "classify_page", return_value={"kind": "ocr", "reason": "短条目密集页面"}),
            patch.object(engine, "page_anchor_pair", return_value=("出版说明", "本书整理说明")),
            patch.object(engine, "match_page_source", return_value=None),
            patch.object(engine, "match_full_ocr_bounds", return_value=None),
            patch.object(engine, "load_source_units", return_value=[engine.SourceUnit("正文", "body", "正文", kind="epub")]),
        ):
            result = engine.resolve_page({}, reader, 1, "vertical-single", allow_discovery=False, candidate_units=[])

        self.assertEqual(result["kind"], "unresolved")
        self.assertEqual(result["text"], "")
        self.assertEqual(result["status"], "来源内容未锁定")

    def test_ocr_pages_do_not_block_pdf_publication(self):
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
            (paths.root / "text-positioned-full.pdf").write_bytes(b"stale output")
            manifest = [{"page": 1, "kind": "ocr", "status": "整页 OCR", "text": "本页OCR文字", "reason": "test"}]
            with patch.object(engine, "build_strict_page_manifest", return_value=manifest):
                result = engine.build_full_pdf(job_id, "horizontal")
            self.assertEqual(result["state"], "done")
            self.assertTrue((paths.root / "text-positioned-full.pdf").exists())
            self.assertEqual(result["alignment"]["ocr"], 1)

    def test_source_omitted_requires_exhausted_unit_boundaries(self):
        previous_unit = engine.SourceUnit("前章", "chapter-1", "前章正文起点天地玄黄前章末尾唯一文字", kind="epub")
        following_unit = engine.SourceUnit("后章", "chapter-2", "后章开头唯一文字宇宙洪荒后章正文终点", kind="epub")
        previous_norm, _ = engine.normalize_for_match(previous_unit.text)
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": len(previous_norm)},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 3, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 4, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 5, "kind": "body", "sourceUrl": "chapter-2", "start": 0},
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        engine.mark_source_omitted_pages(
            {}, reader, manifest, "vertical-single", [previous_unit, following_unit]
        )
        self.assertEqual([item["kind"] for item in manifest[1:4]], ["source-omitted"] * 3)
        summary = engine.manifest_summary(manifest, len(manifest))
        self.assertEqual(summary["sourceOmitted"], 3)
        self.assertEqual(summary["unresolved"], 0)
        self.assertEqual(summary["reviewRequired"], 0)

    def test_failed_body_alignment_is_not_source_omitted(self):
        unit = engine.SourceUnit("正文", "chapter-1", "正文尚未结束天地玄黄宇宙洪荒", kind="epub")
        source_norm, _ = engine.normalize_for_match(unit.text)
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": len(source_norm) - 4},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])

        engine.mark_source_omitted_pages({}, reader, manifest, "vertical-single", [unit])

        self.assertEqual(manifest[1]["kind"], "unresolved")

    def test_only_verified_source_omitted_pages_receive_same_page_ocr(self):
        manifest = [
            {"page": 1, "kind": "source-omitted", "sourceAbsentVerified": True, "text": ""},
            {"page": 2, "kind": "unresolved", "text": ""},
            {"page": 3, "kind": "source-omitted", "text": ""},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(engine, "full_ocr_cache_path", return_value=Path(temporary) / "missing.txt"),
                patch.object(engine, "ocr_page_text", return_value="本页目录 OCR 文字") as ocr,
            ):
                completed = engine.fill_source_omitted_ocr({}, manifest, "vertical-single")

        self.assertEqual(completed, 1)
        ocr.assert_called_once_with({}, 1, "vertical-single", anchors_only=False)
        self.assertEqual(manifest[0]["status"], "来源未收录·整页 OCR")
        self.assertEqual(manifest[0]["text"], "本页目录 OCR 文字")
        self.assertEqual(manifest[0]["textOrigin"], "page-ocr")
        self.assertEqual(manifest[1]["kind"], "unresolved")
        self.assertEqual(manifest[1]["text"], "")
        self.assertEqual(manifest[2]["text"], "")
        summary = engine.manifest_summary(manifest, len(manifest))
        self.assertEqual(summary["sourceOmitted"], 2)
        self.assertEqual(summary["sourceOmittedOcr"], 1)

    def test_throughput_metrics_reports_rate_eta_and_memory(self):
        with (
            patch.object(engine.time, "time", return_value=160.0),
            patch.object(engine, "available_memory_mb", return_value=2048),
        ):
            metrics = engine.throughput_metrics(100.0, 30, 60, workers=3)

        self.assertEqual(metrics["pagesPerMinute"], 30.0)
        self.assertEqual(metrics["etaSeconds"], 60)
        self.assertEqual(metrics["freeMemoryMB"], 2048)
        self.assertEqual(metrics["workers"], 3)

    def test_adaptive_workers_respects_available_memory(self):
        with (
            patch.object(engine.os, "cpu_count", return_value=16),
            patch.object(engine, "available_memory_mb", return_value=1800),
        ):
            self.assertEqual(engine.adaptive_ocr_workers(), 1)
        with (
            patch.object(engine.os, "cpu_count", return_value=16),
            patch.object(engine, "available_memory_mb", return_value=6000),
        ):
            self.assertEqual(engine.adaptive_ocr_workers(), 3)

    def test_anchor_worker_retries_high_dpi_when_low_dpi_evidence_is_weak(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1122334455667788"
            paths = engine.job_paths(job_id)
            paths.root.mkdir(parents=True)
            engine.atomic_write_json(paths.meta, {"id": job_id, "pdf": "unused.pdf"})
            image = Image.new("RGB", (100, 120), "white")
            with (
                patch.object(engine, "render_page_images_persistent", side_effect=[[image], [image]]) as render,
                patch.object(engine, "ocr_page_anchor_pair", side_effect=[("弱证据", "弱证据"), ("高分辨率页首", "高分辨率页尾")]) as anchors,
                patch.object(engine, "anchor_pair_strong_for_source", return_value=False),
            ):
                result = engine.precompute_anchor_worker((job_id, (1,), "vertical-single"))

            self.assertEqual(result, [(1, True, "")])
            self.assertEqual(
                [call.kwargs["dpi"] for call in render.call_args_list],
                [engine.ANCHOR_BASE_DPI, engine.ANCHOR_RETRY_DPI],
            )
            self.assertEqual(
                [call.kwargs["render_dpi"] for call in anchors.call_args_list],
                [engine.ANCHOR_BASE_DPI, engine.ANCHOR_RETRY_DPI],
            )

    def test_multi_page_unresolved_run_recovers_only_with_internal_boundaries(self):
        unit = engine.SourceUnit(
            "正文",
            "chapter-1",
            "前页正文第一页开头唯一甲甲甲第一页末尾唯一第二页开头唯一乙乙乙第二页末尾唯一后页正文",
            kind="epub",
        )
        source_norm, _ = engine.normalize_for_match(unit.text)
        previous_end = len(engine.normalize_for_match("前页正文")[0])
        following_start = source_norm.index(engine.normalize_for_match("后页正文")[0])
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": previous_end, "confidence": 96},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 3, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 4, "kind": "body", "sourceUrl": "chapter-1", "start": following_start, "confidence": 96},
        ]
        anchors = {
            2: ("第一页开头唯一", "第一页末尾唯一"),
            3: ("第二页开头唯一", "第二页末尾唯一"),
        }
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        with patch.object(engine, "page_anchor_pair", side_effect=lambda _job, _reader, page, _layout: anchors[page]):
            recovered = engine.recover_unresolved_runs(
                {}, reader, manifest, "vertical-single", {"chapter-1": unit}
            )

        self.assertEqual(recovered, 2)
        self.assertEqual([item["kind"] for item in manifest[1:3]], ["body", "body"])
        self.assertLess(manifest[1]["start"], manifest[1]["end"])
        self.assertEqual(manifest[1]["end"], manifest[2]["start"])
        self.assertEqual(manifest[1]["status"], "多页缺口双锚恢复")

    def test_single_character_page_start_is_preserved(self):
        source = "上一页结尾。侯正文开头唯一文字天地玄黄，正文页尾唯一文字。下一页开头"
        result = engine.strict_pair_in_text(
            source,
            "侯\n正文开头唯一文字",
            "正文页尾唯一文字",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["text"].startswith("侯"))
        self.assertFalse(result["text"].startswith("上一页"))

    def test_punctuation_page_start_is_preserved(self):
        source = "上一页结尾。正文开头唯一文字天地玄黄，正文页尾唯一文字。下一页开头"
        result = engine.strict_pair_in_text(
            source,
            "。\n正文开头唯一文字",
            "正文页尾唯一文字",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["text"].startswith("。正文开头"))

    def test_internal_ocr_punctuation_omission_keeps_exact_outer_characters(self):
        source = "汉建安六年，郡举上计掾。魏武纳之，于是务农积谷，国用丰赡。帝又言。"
        result = engine.strict_pair_in_text(
            source,
            "汉建安六年郡举上计掾",
            "于是务农积谷国用丰赡帝又",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["text"].startswith("汉建安六年，"))
        self.assertTrue(result["text"].endswith("帝又"))

    def test_wrong_recognized_outer_punctuation_is_not_silently_replaced(self):
        source = "正文开头唯一文字，正文页尾唯一文字。"
        result = engine.strict_pair_in_text(
            source,
            "正文开头唯一文字",
            "正文页尾唯一文字°",
        )

        self.assertIsNone(result)

    def test_page_can_strictly_span_adjacent_source_units(self):
        units = [
            engine.SourceUnit(
                "前章", "chapter-1", "前章正文天地玄黄前章页尾唯一文字", kind="epub"
            ),
            engine.SourceUnit(
                "后章", "chapter-2", "后章标题后章页首唯一文字宇宙洪荒正文", kind="epub"
            ),
        ]
        windows = engine.source_alignment_windows(units)

        result = engine.match_page_source(
            {}, "前章页尾唯一文字", "后章页首唯一文字", False, windows
        )

        self.assertIsNotNone(result)
        self.assertTrue(result["crossesSourceUnit"])
        self.assertEqual((result["sourceStartOrder"], result["sourceEndOrder"]), (0, 1))
        self.assertEqual((result["sourceStartUrl"], result["sourceEndUrl"]), ("chapter-1", "chapter-2"))
        self.assertIn("前章页尾唯一文字后章标题后章页首唯一文字", result["text"])
        self.assertLess(result["globalStart"], result["globalEnd"])

    def test_cross_unit_global_boundary_can_stream_to_following_unit(self):
        previous = {
            "kind": "body", "status": "跨章节双头锁边",
            "sourceUrl": "cross-unit:0", "globalRawStart": 80, "globalRawEnd": 118,
            "globalStart": 80, "globalEnd": 120,
        }
        current = {
            "kind": "body", "status": "双头锁边",
            "sourceUrl": "chapter-2", "globalRawStart": 120, "globalRawEnd": 160,
            "globalStart": 120, "globalEnd": 160,
        }

        self.assertTrue(engine.strict_pair_committable(previous, current))
        self.assertEqual(engine.enforce_adjacent_page_boundaries([previous, current]), 0)

    def test_continuity_never_overwrites_recognized_page_start(self):
        previous = {"kind": "body", "sourceUrl": "chapter-1", "end": 10}
        current = {
            "kind": "body",
            "sourceUrl": "chapter-1",
            "start": 14,
            "end": 40,
            "text": "侯正文",
            "confidence": 96,
            "startAnchor": "侯",
        }

        result = engine.apply_previous_page_continuity({}, previous, current)

        self.assertEqual(result["kind"], "unresolved")
        self.assertEqual(result["status"], "页界未唯一锁定")
        self.assertEqual(result["boundaryGap"], 4)
        self.assertEqual(result["startAnchor"], "侯")
        self.assertEqual(result["text"], "")

    def test_adjacent_pages_compare_raw_punctuation_boundaries(self):
        manifest = [
            {
                "page": 1, "kind": "body", "sourceUrl": "chapter-1",
                "start": 0, "end": 20, "rawStart": 0, "rawEnd": 22, "text": "前页正文",
            },
            {
                "page": 2, "kind": "body", "sourceUrl": "chapter-1",
                "start": 20, "end": 40, "rawStart": 21, "rawEnd": 43, "text": "。后页正文",
            },
        ]

        conflicts = engine.enforce_adjacent_page_boundaries(manifest)

        self.assertEqual(conflicts, 1)
        self.assertEqual([item["kind"] for item in manifest], ["body", "body"])
        self.assertTrue(all(item.get("continuityWarning") for item in manifest))

    def test_legacy_continuity_results_require_review(self):
        manifest = [
            {"kind": "body", "status": "双锁连续补首", "confidence": 91},
            {"kind": "body", "status": "双锁连续去重", "confidence": 90},
            {"kind": "body", "status": "双头锁边", "confidence": 95},
        ]

        summary = engine.manifest_summary(manifest, len(manifest))

        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["boundaryReview"], 2)
        self.assertEqual(summary["reviewRequired"], 2)

    def test_alignment_quality_regression_is_blocked_only_when_material(self):
        previous = {"matched": 1981, "constrained": 710, "unresolved": 300, "reviewRequired": 300}
        regressed = {"matched": 223, "constrained": 0, "unresolved": 3149, "reviewRequired": 3149}
        slight_change = {"matched": 1960, "constrained": 690, "unresolved": 340, "reviewRequired": 340}

        self.assertTrue(engine.alignment_quality_regressed(regressed, previous, 3372))
        self.assertFalse(engine.alignment_quality_regressed(slight_change, previous, 3372))
        self.assertFalse(engine.alignment_quality_regressed(previous, regressed, 3372))

    def test_authoritative_page_uses_next_start_and_tail_is_nonblocking(self):
        unit = engine.SourceUnit(
            "正文", "chapter-1",
            "第一页开头唯一校准正文甲乙丙丁第一页结尾第二页开头唯一校准正文戊己庚辛第二页结尾第三页开头唯一末页内容",
            kind="epub",
        )
        anchors = [
            ("第一页开头唯一", "错误的本页页尾"),
            ("第二页开头唯一", "第二页结尾"),
            ("第三页开头唯一", "末页内容"),
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in anchors])

        def fake_fallback(_job, _reader, manifest, _layout, _status=""):
            for item in manifest:
                if item["kind"] != "body":
                    item.update(kind="ocr", status="整页 OCR", text="本页OCR", textOrigin="page-ocr")
            return sum(item["kind"] == "ocr" for item in manifest)

        with (
            patch.object(engine, "load_source_units", return_value=[unit]),
            patch.object(engine, "page_anchor_pair", side_effect=anchors),
            patch.object(engine, "fill_non_authoritative_ocr", side_effect=fake_fallback),
        ):
            manifest = engine.build_strict_page_manifest({}, reader, "vertical-single")

        self.assertEqual(manifest[0]["kind"], "body")
        self.assertEqual(manifest[0]["status"], "页首与次页页首锁边")
        self.assertTrue(manifest[0]["text"].startswith("第一页开头唯一"))
        self.assertNotIn("第二页开头唯一", manifest[0]["text"])
        self.assertFalse(manifest[0]["tailCrosscheck"]["passed"])
        self.assertEqual(manifest[-1]["kind"], "ocr")

    def test_cross_chapter_page_is_ocr_not_authoritative(self):
        units = [
            engine.SourceUnit("第一章", "chapter-1", "第一页开头唯一前章结束", kind="epub"),
            engine.SourceUnit("第二章", "chapter-2", "第二章标题第二页开头唯一后章正文", kind="epub"),
        ]
        anchors = [("第一页开头唯一", "前章结束"), ("第二页开头唯一", "后章正文")]
        reader = SimpleNamespace(pages=[SimpleNamespace(), SimpleNamespace()])

        def fake_fallback(_job, _reader, manifest, _layout, _status=""):
            for item in manifest:
                item.update(kind="ocr", status="整页 OCR", text="OCR", textOrigin="page-ocr")
            return len(manifest)

        with (
            patch.object(engine, "load_source_units", return_value=units),
            patch.object(engine, "page_anchor_pair", side_effect=anchors),
            patch.object(engine, "fill_non_authoritative_ocr", side_effect=fake_fallback),
        ):
            manifest = engine.build_strict_page_manifest({}, reader, "vertical-single")

        self.assertEqual([item["kind"] for item in manifest], ["ocr", "ocr"])

    def test_clean_chapter_break_between_pages_keeps_previous_page_authoritative(self):
        units = [
            engine.SourceUnit("第一章", "chapter-1", "第一页开头唯一前章完整结尾", kind="epub"),
            engine.SourceUnit("第二章", "chapter-2", "第二页开头唯一后章正文第三页开头唯一末页", kind="epub"),
        ]
        anchors = [
            ("第一页开头唯一", "前章完整结尾"),
            ("第二页开头唯一", "后章正文"),
            ("第三页开头唯一", "末页"),
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in anchors])

        def fake_fallback(_job, _reader, manifest, _layout, _status=""):
            for item in manifest:
                if item["kind"] != "body":
                    item.update(kind="ocr", status="整页 OCR", text="OCR", textOrigin="page-ocr")
            return 1

        with (
            patch.object(engine, "load_source_units", return_value=units),
            patch.object(engine, "page_anchor_pair", side_effect=anchors),
            patch.object(engine, "fill_non_authoritative_ocr", side_effect=fake_fallback),
        ):
            manifest = engine.build_strict_page_manifest({}, reader, "vertical-single")

        self.assertEqual([item["kind"] for item in manifest], ["body", "body", "ocr"])
        self.assertEqual(manifest[0]["text"], "第一页开头唯一前章完整结尾")

    def test_every_non_authoritative_page_becomes_ocr_or_blank(self):
        manifest = [
            {"page": 1, "kind": "body", "status": "页首与次页页首锁边", "text": "权威正文"},
            {"page": 2, "kind": "unresolved", "text": ""},
            {"page": 3, "kind": "unresolved", "text": ""},
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        with tempfile.TemporaryDirectory() as temporary:
            cached = Path(temporary) / "cached.txt"
            cached.write_text("cached", encoding="utf-8")

            def classify(_job, _reader, page_no, _layout, _text):
                return {"kind": "blank", "reason": "空白页"} if page_no == 3 else {"kind": "body"}

            with (
                patch.object(engine, "full_ocr_cache_path", return_value=cached),
                patch.object(engine, "full_ocr_cache_ready", return_value=True),
                patch.object(engine, "read_full_ocr_layout", return_value={
                    "imageSize": [600, 900],
                    "items": [{"text": "目录 OCR", "box": [[400, 100], [440, 100], [440, 700], [400, 700]]}],
                    "coverage": {"complete": True},
                }),
                patch.object(engine, "ocr_page_text", side_effect=["目录 OCR", ""]),
                patch.object(engine, "classify_page", side_effect=classify),
            ):
                completed = engine.fill_non_authoritative_ocr({}, reader, manifest, "vertical-single")

        self.assertEqual(completed, 2)
        self.assertEqual([item["kind"] for item in manifest], ["body", "ocr", "blank"])
        self.assertEqual(manifest[1]["textOrigin"], "page-ocr")
        self.assertEqual(manifest[2]["text"], "")

    def test_incomplete_ocr_coverage_stays_unresolved(self):
        manifest = [{"page": 1, "kind": "unresolved", "text": ""}]
        reader = SimpleNamespace(pages=[SimpleNamespace()])
        coverage = {
            "complete": False,
            "missingColumns": [{"cx": 320}],
            "weakColumns": [{"cx": 420}],
        }

        with (
            patch.object(engine, "full_ocr_cache_ready", return_value=True),
            patch.object(engine, "read_full_ocr_layout", return_value={
                "imageSize": [600, 900],
                "items": [{"text": "不完整 OCR", "box": [[400, 100], [440, 100], [440, 700], [400, 700]]}],
                "coverage": coverage,
            }),
            patch.object(engine, "ocr_page_text", return_value="不完整 OCR"),
            patch.object(engine, "classify_page", return_value={"kind": "body"}),
        ):
            completed = engine.fill_non_authoritative_ocr({}, reader, manifest, "vertical-single")

        self.assertEqual(completed, 0)
        self.assertEqual(manifest[0]["kind"], "unresolved")
        self.assertEqual(manifest[0]["status"], "OCR 覆盖不完整")
        self.assertEqual(manifest[0]["text"], "")
        self.assertEqual(manifest[0]["textOrigin"], "page-ocr-incomplete")
        self.assertIn("2 条", manifest[0]["reason"])

    def test_release_audit_requires_every_page_to_pass_a_terminal_gate(self):
        manifest = [
            {"page": 1, "kind": "body", "status": "页首与次页页首锁边"},
            {"page": 2, "kind": "blank", "status": "空白页"},
            {"page": 3, "kind": "ocr", "ocrCoverage": {"complete": True}},
            {
                "page": 4, "kind": "unresolved", "textOrigin": "page-ocr-incomplete",
                "ocrAttempts": [150, 190, 230, engine.STUBBORN_FULL_OCR_DPI],
            },
        ]

        audit = engine.manifest_release_audit(manifest, 4)

        self.assertFalse(audit["releaseReady"])
        self.assertEqual(audit["releasablePages"], 3)
        self.assertEqual(audit["states"]["failed-review"], 1)
        self.assertEqual(audit["pages"]["failed-review"], [4])

    def test_chapter_transition_title_page_can_attach_to_next_unit_prefix(self):
        previous_unit = engine.SourceUnit("志第一", "chapter-1", "前章正文结束", kind="epub")
        following_unit = engine.SourceUnit("志第二", "chapter-2", "志第二新章正文开始", kind="epub")
        following_start = engine.normalize_for_match(following_unit.text)[0].index("新章正文")
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": len(engine.normalize_for_match(previous_unit.text)[0]), "confidence": 96},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 3, "kind": "body", "sourceUrl": "chapter-2", "start": following_start, "confidence": 96},
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        with patch.object(engine, "page_anchor_pair", return_value=("志第二", "志第二")):
            recovered = engine.recover_chapter_transition_runs(
                {}, reader, manifest, "vertical-single",
                [previous_unit, following_unit],
                {"chapter-1": 0, "chapter-2": 1},
            )

        self.assertEqual(recovered, 1)
        self.assertEqual(manifest[1]["kind"], "body")
        self.assertEqual(manifest[1]["status"], "章节过渡后章约束")
        self.assertEqual(manifest[1]["sourceUrl"], "chapter-2")

    def test_chapter_transition_overlap_stays_unresolved(self):
        previous_unit = engine.SourceUnit("志第一", "chapter-1", "前章正文结束", kind="epub")
        following_unit = engine.SourceUnit("志第二", "chapter-2", "志第二新章正文开始", kind="epub")
        following_start = engine.normalize_for_match(following_unit.text)[0].index("新章正文")
        manifest = [
            {"page": 1, "kind": "body", "sourceUrl": "chapter-1", "end": 0, "confidence": 96},
            {"page": 2, "kind": "unresolved", "status": "未锁定", "text": ""},
            {"page": 3, "kind": "body", "sourceUrl": "chapter-2", "start": following_start, "confidence": 96},
        ]
        reader = SimpleNamespace(pages=[SimpleNamespace() for _ in manifest])
        with patch.object(engine, "page_anchor_pair", return_value=("志第一志第二", "")):
            recovered = engine.recover_chapter_transition_runs(
                {}, reader, manifest, "vertical-single",
                [previous_unit, following_unit],
                {"chapter-1": 0, "chapter-2": 1},
            )

        self.assertEqual(recovered, 0)
        self.assertEqual(manifest[1]["kind"], "unresolved")


class FileServerTests(unittest.TestCase):
    def test_job_library_returns_completed_pdf_download_link(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(engine, "JOBS_DIR", Path(temporary)), \
                patch.object(server, "JOBS_DIR", Path(temporary)):
            job_id = "aabbccddeeff0011"
            paths = engine.new_job_paths(job_id, "晋书.pdf")
            paths.root.mkdir(parents=True)
            engine.atomic_write_json(paths.meta, {
                "id": job_id,
                "pdfOriginal": "晋书.pdf",
                "pageCount": 2,
            })
            engine.write_full_status(job_id, state="done", processed=2, total=2, outputs=[])
            (paths.root / "text-positioned-full.pdf").write_bytes(b"pdf")

            handler = object.__new__(server.AppHandler)
            jobs = handler.list_jobs()

            self.assertEqual(len(jobs), 1)
            self.assertTrue(jobs[0]["hasOutput"])
            self.assertIn("/jobs/aabbccddeeff0011/text-positioned-full.pdf", jobs[0]["outputDownloadUrl"])
            self.assertTrue(jobs[0]["outputDownloadUrl"].endswith("&download=1"))

    def test_job_diagnostics_summarizes_unresolved_runs(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(engine, "JOBS_DIR", Path(temporary)):
            job_id = "1122334455667788"
            paths = engine.job_paths(job_id)
            paths.root.mkdir(parents=True)
            engine.atomic_write_json(paths.root / "page-text-manifest.json", {"pages": [
                {"page": 1, "kind": "body"},
                {"page": 2, "kind": "unresolved", "reason": "缺少页尾锚点"},
                {"page": 3, "kind": "unresolved", "reason": "缺少页尾锚点"},
                {"page": 4, "kind": "body"},
                {"page": 5, "kind": "unresolved", "status": "章节顺序冲突"},
            ]})
            server.DIAGNOSTICS_CACHE.clear()

            result = server.build_job_diagnostics(job_id, expected_unresolved=3)

            self.assertTrue(result["available"])
            self.assertTrue(result["current"])
            self.assertEqual(result["unresolved"], 3)
            self.assertEqual(result["runCount"], 2)
            self.assertEqual(result["longestRun"], 2)
            self.assertEqual(result["runLengthBuckets"]["one"], 1)
            self.assertEqual(result["runLengthBuckets"]["two"], 1)
            self.assertEqual(result["sourceOrderConflicts"], 1)

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
