# Current Pipeline Blocker

## Symptom

The frontend shows that every PDF page has been inspected, but the pipeline does not advance to text-layer writing or PDF assembly.

## Current State

- PDF pages: 3372
- Strictly matched or constrained body pages: 2361
- Source-omitted pages: 381
- Unresolved authoritative body pages: 630
- Active stage: `align`
- Backend process: stopped
- Publication state: blocked by the strict release gate

## Expected Behavior

Every authoritative EPUB body segment must map to the correct scanned PDF page. Pages absent from the EPUB may retain the scan with an empty text layer. The final PDF must not be published while authoritative body pages remain unresolved.

## Why The Next Stage Does Not Start

`build_full_pdf()` calls `build_strict_page_manifest()` before writing page PDFs. The strict gate returns early when `manifest_summary()` reports unresolved pages and `TEXT_LAYER_ALLOW_UNRESOLVED_OUTPUT` is disabled. This is intentional data-integrity behavior, but the remaining alignment algorithm cannot yet resolve multi-page gaps inside a source unit or every chapter-boundary run.

## Review Focus

1. Audit multi-page unresolved-run alignment in `build_strict_page_manifest()`.
2. Review `mark_source_omitted_pages()` for conservative classification without treating failed body alignment as omitted source material.
3. Propose a deterministic monotonic alignment algorithm that uses cached page-edge OCR and EPUB chapter boundaries without full-page OCR reruns.
4. Preserve the strict publication gate and page-to-page correspondence.
5. Keep the pipeline status protocol consistent with actual backend thread state.

## Privacy Note

The repository intentionally excludes PDFs, EPUBs, OCR cache files, page anchors, generated outputs, and local job metadata.
