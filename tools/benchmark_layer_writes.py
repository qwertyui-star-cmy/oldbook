from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from PIL import ImageChops
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import text_layer_engine as engine


def build_subset(source: Path, output: Path, start: int, count: int) -> None:
    reader = PdfReader(str(source), strict=False)
    writer = PdfWriter()
    for index in range(start - 1, min(len(reader.pages), start - 1 + count)):
        writer.add_page(reader.pages[index])
    engine.write_pdf_atomic(writer, output)


def run_mode(job_id: str, rows: list[dict], layout: str, output_dir: Path, workers: int) -> float:
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if workers == 1:
        job = json.loads(engine.job_paths(job_id).meta.read_text(encoding="utf-8"))
        reader = PdfReader(str(job["pdf"]), strict=False)
        for page_no, row in enumerate(rows, 1):
            engine.write_page_layer_resilient(
                job, reader, page_no, row, layout, output_dir / f"page-{page_no:05d}.pdf"
            )
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    engine.write_page_layer_worker,
                    (job_id, page_no, row, layout, str(output_dir / f"page-{page_no:05d}.pdf")),
                )
                for page_no, row in enumerate(rows, 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                page_no, _written, error = future.result()
                if error:
                    raise RuntimeError(f"page {page_no}: {error}")
    return time.perf_counter() - started


def verify_mode(source: Path, rows: list[dict], output_dir: Path) -> None:
    for page_no, row in enumerate(rows, 1):
        engine.validate_page_text_layer(
            output_dir / f"page-{page_no:05d}.pdf", str(row.get("text") or "")
        )
    for page_no in sorted({1, max(1, len(rows) // 2), len(rows)}):
        source_image = engine.render_page_image(source, page_no, dpi=72)
        output_image = engine.render_page_image(output_dir / f"page-{page_no:05d}.pdf", 1, dpi=72)
        if source_image.size != output_image.size or ImageChops.difference(source_image, output_image).getbbox():
            raise RuntimeError(f"page {page_no}: visual mismatch")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_root", type=Path)
    parser.add_argument("--start", type=int, default=101)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    args = parser.parse_args()

    source_job = json.loads((args.job_root / "job.json").read_text(encoding="utf-8"))
    manifest_payload = json.loads((args.job_root / "page-text-manifest.json").read_text(encoding="utf-8"))
    source_rows = manifest_payload["pages"][args.start - 1:args.start - 1 + args.count]
    rows = [
        {**row, "page": index, "kind": "body", "textOrigin": "benchmark-authoritative"}
        for index, row in enumerate(source_rows, 1)
    ]
    layout = str(manifest_payload.get("layout") or source_job.get("layout") or "horizontal")
    job_id = uuid.uuid4().hex[:16]
    root = engine.JOBS_DIR / f"layer-benchmark-{job_id}"
    root.mkdir(parents=True)
    subset = root / "source.pdf"
    try:
        build_subset(Path(source_job["pdf"]), subset, args.start, args.count)
        engine.atomic_write_json(root / "job.json", {
            "id": job_id,
            "pdf": str(subset),
            "pdfOriginal": subset.name,
            "layout": layout,
        })
        results = {}
        for workers in args.workers:
            output_dir = root / f"workers-{workers}"
            elapsed = run_mode(job_id, rows, layout, output_dir, workers)
            verify_mode(subset, rows, output_dir)
            results[str(workers)] = {
                "seconds": round(elapsed, 3),
                "pagesPerSecond": round(len(rows) / max(elapsed, 0.001), 2),
                "textVerifiedPages": len(rows),
                "pixelCheckedPages": 3,
            }
        print(json.dumps({"pages": len(rows), "results": results}, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
