from __future__ import annotations

import cgi
import json
import mimetypes
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from text_layer_engine import JOBS_DIR, TaskPaused, build_full_pdf, cleanup_job_cache, cleanup_old_cache, create_job, ensure_pipeline, inspect_pdf, job_paths, layout_label, make_trial, new_pipeline, read_full_status, write_full_status


ROOT = Path(__file__).resolve().parent
LAST_JOB_FILE = JOBS_DIR.parent / "last-job.json"
RUNNING_TASKS: dict[str, threading.Thread] = {}
RUNNING_LOCK = threading.Lock()


class UploadField:
    def __init__(self, filename: str, file_object, owner=None):
        self.filename = filename
        self.file = file_object
        self.owner = owner


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_json({
                "ok": True,
                "detail": "断网也能处理本地 PDF 和本地文本",
                "root": str(ROOT),
            })
            return
        if parsed.path.startswith("/api/job/"):
            job_id = parsed.path.rsplit("/", 1)[-1].strip()
            self.send_json(self.public_job_status(job_id))
            return
        if parsed.path == "/api/restore/latest":
            job_id = self.latest_job_id()
            result = self.public_restore(job_id) if job_id else None
            if result is None:
                self.send_json({"error": "没有找到可恢复的任务，请重新导入一次。"}, status=404)
            else:
                self.send_json(result)
            return
        if parsed.path.startswith("/api/restore/"):
            job_id = parsed.path.rsplit("/", 1)[-1].strip()
            result = self.public_restore(job_id)
            if result is None:
                self.send_json({"error": "没有找到上次任务，请重新导入一次。"}, status=404)
            else:
                self.send_json(result)
            return
        if parsed.path.startswith("/jobs/"):
            self.send_job_file(parsed.path, download="download" in parse_qs(parsed.query))
            return
        super().do_GET()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/jobs/"):
            self.send_job_file(parsed.path, head_only=True, download="download" in parse_qs(parsed.query))
            return
        super().do_HEAD()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/inspect":
                self.receive_inspect()
                return
            if parsed.path == "/api/trial":
                self.receive_trial()
                return
            if parsed.path == "/api/full":
                self.receive_full()
                return
            if parsed.path == "/api/pause":
                self.receive_pause()
                return
            if parsed.path == "/api/cleanup":
                self.receive_cleanup()
                return
        except Exception as error:
            self.send_json({"error": str(error)}, status=500)
            return
        self.send_error(404)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return {}, {}
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            },
            keep_blank_values=True,
        )
        fields = {}
        files = {}
        for item in form.list or []:
            if not item.name:
                continue
            if item.filename:
                item.file.seek(0)
                files[item.name] = UploadField(item.filename, item.file, owner=item)
            else:
                fields[item.name] = str(item.value or "")
        return fields, files

    def receive_inspect(self):
        fields, files = self.read_form()
        pdf_field = files.get("pdf")
        if pdf_field is None or not getattr(pdf_field, "filename", ""):
            self.send_json({"error": "请先选择 PDF。"}, status=400)
            return
        source_kind = fields.get("source_kind", "none")
        source_field = files.get("source_file") if source_kind == "file" else None
        source_url = fields.get("source_url", "") if source_kind == "url" else ""
        layout = fields.get("layout", "auto")
        result = create_job(pdf_field, source_field, source_url, layout)
        self.record_last_job(result["jobId"])
        self.send_json(result)

    def receive_trial(self):
        payload = self.read_json()
        job_id = str(payload.get("jobId", "")).strip()
        page = int(payload.get("page", 1) or 1)
        layout = str(payload.get("layout", "auto"))
        result = make_trial(job_id, page, layout)
        job_root = job_paths(job_id).root
        if result.get("kind") == "body":
            trial_message = f"第 {result['page']} 页已精准锁定：{result.get('sourceTitle') or '参考正文'}。"
        else:
            trial_message = f"第 {result['page']} 页已按整页 OCR 处理。"
        self.send_json({
            "previewUrl": self.job_url(job_root, result["preview"]),
            "message": trial_message,
            "trialStatus": result.get("status"),
            "sourceTitle": result.get("sourceTitle", ""),
            "confidence": result.get("confidence", 0),
            "outputs": [
                {"name": "定位线预览图", "url": self.job_url(job_root, result["preview"]), "detail": "PNG"},
                {"name": "单页试运行 PDF", "url": self.job_url(job_root, result["trialPdf"]), "detail": "PDF"},
            ],
        })

    def receive_full(self):
        payload = self.read_json()
        job_id = str(payload.get("jobId", "")).strip()
        layout = str(payload.get("layout", "auto"))
        paths = job_paths(job_id)
        if not paths.meta.exists():
            self.send_json({"error": "找不到这个任务，请重新导入。"}, status=404)
            return

        with RUNNING_LOCK:
            task = RUNNING_TASKS.get(job_id)
            if task and task.is_alive():
                self.send_json(self.public_job_status(job_id))
                return
            write_full_status(
                job_id,
                state="queued",
                message="整本任务已排队，马上开始。",
                pauseRequested=False,
                validation={},
                outputs=[],
                activeStage="input",
                pipeline=new_pipeline(),
            )
            task = threading.Thread(target=self.run_full_task, args=(job_id, layout), daemon=True)
            RUNNING_TASKS[job_id] = task
            task.start()
        self.send_json(self.public_job_status(job_id))

    def receive_pause(self):
        payload = self.read_json()
        job_id = str(payload.get("jobId", "")).strip()
        paths = job_paths(job_id)
        if not paths.meta.exists():
            self.send_json({"error": "找不到这个任务，请重新导入。"}, status=404)
            return
        current = read_full_status(job_id)
        if current.get("state") not in {"queued", "planning", "running"}:
            self.send_json(self.public_job_status(job_id))
            return
        status = write_full_status(
            job_id,
            pauseRequested=True,
            message="已收到暂停请求；当前页完成后会停下。",
        )
        self.send_json(self.public_job_status(job_id))

    def receive_cleanup(self):
        payload = self.read_json()
        job_id = str(payload.get("jobId", "")).strip()
        keep_final = bool(payload.get("keepFinal", True))
        paths = job_paths(job_id)
        if not paths.meta.exists():
            self.send_json({"error": "找不到这个任务，请重新导入。"}, status=404)
            return
        with RUNNING_LOCK:
            task = RUNNING_TASKS.get(job_id)
            if task and task.is_alive():
                self.send_json({"error": "任务还在运行，先暂停或等它完成后再清理。"}, status=409)
                return
        result = cleanup_job_cache(job_id, keep_final=keep_final)
        status = read_full_status(job_id)
        alignment = status.get("alignment") or {}
        verified_final = (
            status.get("state") == "done"
            and not int(alignment.get("reviewRequired") or 0)
            and not int(alignment.get("estimated") or 0)
        )
        if keep_final and verified_final and (paths.root / "text-positioned-full.pdf").exists():
            status = write_full_status(
                job_id,
                outputs=[{
                    "name": "整本文字定位 PDF",
                    "path": str(paths.root / "text-positioned-full.pdf"),
                    "relative": "text-positioned-full.pdf",
                    "detail": "PDF",
                }],
                message=f"已清理 {result['removed']} 项临时文件，保留最终 PDF。",
            )
        elif status.get("outputs"):
            status = write_full_status(
                job_id,
                message=f"已清理 {result['removed']} 项临时文件；核对清单仍保留。",
            )
        else:
            status = write_full_status(
                job_id,
                outputs=[],
                message=f"已清理 {result['removed']} 项临时文件。",
            )
        public = self.public_job_status(job_id)
        public["removed"] = result["removed"]
        self.send_json(public)

    def run_full_task(self, job_id: str, layout: str):
        try:
            build_full_pdf(job_id, layout)
        except TaskPaused:
            current = read_full_status(job_id)
            if current.get("state") != "paused":
                write_full_status(job_id, state="paused", message="任务已暂停，进度和缓存均已保存。", pauseRequested=False)
        except Exception as error:
            write_full_status(job_id, state="error", message=f"整本任务停止：{error}")

    def public_job_status(self, job_id: str) -> dict:
        status = ensure_pipeline(read_full_status(job_id))
        status["backendActive"] = False
        if status.get("state") in {"queued", "planning", "running"}:
            with RUNNING_LOCK:
                task = RUNNING_TASKS.get(job_id)
                active = bool(task and task.is_alive())
            stale_seconds = time.time() - float(status.get("updatedAt") or 0)
            if not active and stale_seconds > 90:
                status = write_full_status(
                    job_id,
                    state="error",
                    message="后台任务已中断；已完成的 OCR 缓存仍保留，点击“生成整本”会从缓存继续。",
                    pauseRequested=False,
                )
            else:
                status["backendActive"] = active
                status["staleSeconds"] = round(stale_seconds, 1)
                if active and stale_seconds > 120:
                    status["stalled"] = True
                    status["message"] = f"{status.get('message') or '后台任务运行中'}（已 {int(stale_seconds)} 秒没有进度更新，可能卡在当前页。）"
        status["backendActive"] = bool(status.get("backendActive", False))
        paths = job_paths(job_id)
        public_outputs = []
        for item in status.get("outputs", []):
            relative = item.get("relative")
            if relative:
                output_path = paths.root / relative
                if not output_path.is_file():
                    continue
                url = self.job_url(paths.root, output_path)
                public_outputs.append({
                    "name": item.get("name", "输出文件"),
                    "url": url,
                    "downloadUrl": f"{url}&download=1",
                    "detail": item.get("detail", "打开"),
                })
        status["outputs"] = public_outputs
        return status

    def record_last_job(self, job_id: str) -> None:
        LAST_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = LAST_JOB_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"jobId": job_id}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(LAST_JOB_FILE)

    def latest_job_id(self) -> str:
        active = []
        resumable = []
        for status_path in JOBS_DIR.glob("*/full-status.json"):
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if status.get("state") in {"queued", "planning", "running"}:
                    active.append((float(status.get("updatedAt") or 0), status_path.parent.name))
                elif status.get("state") in {"paused", "error", "done"} and int(status.get("total") or 0) > 0:
                    score = float(status.get("updatedAt") or 0) + max(0, int(status.get("processed") or 0))
                    resumable.append((score, status_path.parent.name))
            except Exception:
                continue
        if active:
            active.sort(reverse=True)
            for _, job_id in active:
                if job_paths(job_id).meta.exists():
                    return job_id
        if resumable:
            resumable.sort(reverse=True)
            for _, job_id in resumable:
                if job_paths(job_id).meta.exists():
                    return job_id
        if LAST_JOB_FILE.exists():
            try:
                saved = json.loads(LAST_JOB_FILE.read_text(encoding="utf-8"))
                job_id = str(saved.get("jobId", "")).strip()
                if job_id and job_paths(job_id).meta.exists():
                    return job_id
            except Exception:
                pass
        metas = sorted(JOBS_DIR.glob("*/job.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for meta in metas:
            job_id = meta.parent.name
            if job_paths(job_id).meta.exists():
                return job_id
        return ""

    def public_restore(self, job_id: str) -> dict:
        paths = job_paths(job_id)
        if not paths.meta.exists():
            return None
        job = json.loads(paths.meta.read_text(encoding="utf-8"))
        has_cached_pdf = paths.pdf.exists()
        if has_cached_pdf:
            data = inspect_pdf(paths.pdf, job.get("layout", "auto"))
        else:
            data = {
                "pageCount": job.get("pageCount", ""),
                "textLayerLabel": "--",
                "layout": job.get("layout", "auto"),
                "layoutLabel": layout_label(job.get("layout", "auto")),
                "messages": ["上次任务的源 PDF 已被清理；如果需要重新试页或生成整本，请重新导入。"],
            }

        source_original = job.get("sourceOriginal", "")
        source_kind = "none"
        source_url = ""
        if source_original.startswith(("http://", "https://")):
            source_kind = "url"
            source_url = source_original
        elif job.get("sourceText") or paths.source.exists():
            source_kind = "file"

        preview_files = sorted(paths.root.glob("page-*-guides.png"), key=lambda item: item.stat().st_mtime, reverse=True)
        trial_files = sorted(paths.root.glob("page-*-trial.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)
        preview_url = self.job_url(paths.root, preview_files[0]) if preview_files else ""
        trial_page = 1
        if preview_files:
            match = re.search(r"page-(\d+)-guides\.png$", preview_files[0].name)
            if match:
                trial_page = int(match.group(1))

        full_status = self.public_job_status(job_id)
        outputs = list(full_status.get("outputs") or [])
        if not outputs and (preview_files or trial_files):
            if preview_files:
                outputs.append({"name": "定位线预览图", "url": self.job_url(paths.root, preview_files[0]), "detail": "PNG"})
            if trial_files:
                outputs.append({"name": "单页试运行 PDF", "url": self.job_url(paths.root, trial_files[0]), "detail": "PDF"})

        restore_message = f"已恢复上次任务：{job.get('pdfOriginal') or '已缓存 PDF'}。"
        messages = [restore_message, *(data.get("messages") or [])]
        data.update({
            "jobId": job_id,
            "pdfName": job.get("pdfOriginal") or "已缓存 PDF",
            "sourceKind": source_kind,
            "sourceName": source_original if source_kind == "file" else "",
            "sourceUrl": source_url,
            "layout": data.get("layout") or job.get("layout", "auto"),
            "layoutLabel": data.get("layoutLabel") or layout_label(job.get("layout", "auto")),
            "trialPage": trial_page,
            "hasCachedPdf": has_cached_pdf,
            "previewUrl": preview_url,
            "outputs": outputs,
            "fullStatus": full_status,
            "messages": messages,
        })
        return data

    def job_url(self, job_root: Path, path: Path) -> str:
        relative = path.resolve().relative_to(job_root.resolve()).as_posix()
        version = path.stat().st_mtime_ns if path.exists() else 0
        return f"/jobs/{job_root.name}/{relative}?v={version}"

    def send_job_file(self, request_path: str, head_only: bool = False, download: bool = False):
        parts = [unquote(part) for part in request_path.split("/") if part]
        if len(parts) < 3:
            self.send_error(404)
            return
        job_id = parts[1]
        if not re.fullmatch(r"[0-9a-f]{16}", job_id):
            self.send_error(404)
            return
        relative = Path(*parts[2:])
        root = (JOBS_DIR / job_id).resolve()
        path = (root / relative).resolve()
        if root not in [path, *path.parents] or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start = 0
        end = max(0, size - 1)
        status = 200
        range_header = self.headers.get("Range", "")
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip()) if range_header else None
        if range_header and not match:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        if match:
            first, last = match.groups()
            if not first and not last:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if first:
                start = int(first)
                end = min(end, int(last)) if last else end
            else:
                suffix = min(size, int(last))
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        content_length = 0 if size == 0 else end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Accept-Ranges", "bytes")
        disposition = "attachment" if download else "inline"
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{quote(path.name)}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if head_only or content_length == 0:
            return
        remaining = content_length
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main():
    cleanup_old_cache(days=7)
    for status_path in JOBS_DIR.glob("*/full-status.json"):
        try:
            job_id = status_path.parent.name
            status = read_full_status(job_id)
            if status.get("state") in {"queued", "planning", "running"}:
                write_full_status(
                    job_id,
                    state="paused",
                    pauseRequested=False,
                    message="上次运行被中断；点击生成整本后会从已完成页面继续。",
                    outputs=[],
                )
        except Exception:
            continue
    server = ThreadingHTTPServer(("127.0.0.1", 8787), AppHandler)
    if sys.stdout:
        print("文本精准定位古籍: http://127.0.0.1:8787/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
