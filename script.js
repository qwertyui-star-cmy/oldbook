(function () {
  const app = document.querySelector(".app");
  const importForm = document.getElementById("importForm");
  const importSubmit = document.getElementById("importSubmit");
  const cacheResult = document.getElementById("cacheResult");
  const sourceQuality = document.getElementById("sourceQuality");
  const pdfFile = document.getElementById("pdfFile");
  const sourceFile = document.getElementById("sourceFile");
  const sourceFileLabel = document.getElementById("sourceFileLabel");
  const sourceUrl = document.getElementById("sourceUrl");
  const pdfName = document.getElementById("pdfName");
  const jobBadge = document.getElementById("jobBadge");
  const summaryCards = document.getElementById("summaryCards");
  const messages = document.getElementById("messages");
  const engineDot = document.getElementById("engineDot");
  const engineTitle = document.getElementById("engineTitle");
  const engineDetail = document.getElementById("engineDetail");
  const layoutMode = document.getElementById("layoutMode");
  const trialPage = document.getElementById("trialPage");
  const runTrial = document.getElementById("runTrial");
  const runFull = document.getElementById("runFull");
  const pauseFull = document.getElementById("pauseFull");
  const pauseProgress = document.getElementById("pauseProgress");
  const activateProgress = document.getElementById("activateProgress");
  const cleanupJob = document.getElementById("cleanupJob");
  const reviewDraft = document.getElementById("reviewDraft");
  const jobLibrary = document.getElementById("jobLibrary");
  const jobLibraryTab = document.getElementById("jobLibraryTab");
  const jobLibraryHeaderButton = document.getElementById("jobLibraryHeaderButton");
  const jobLibraryList = document.getElementById("jobLibraryList");
  const refreshJobLibrary = document.getElementById("refreshJobLibrary");
  const preview = document.getElementById("preview");
  const outputs = document.getElementById("outputs");
  const outputState = document.getElementById("outputState");
  const completionResult = document.getElementById("completionResult");
  const fullProgress = document.getElementById("fullProgress");
  const progressTitle = document.getElementById("progressTitle");
  const progressText = document.getElementById("progressText");
  const progressBar = document.getElementById("progressBar");
  const progressDetail = document.getElementById("progressDetail");
  const workMotion = document.getElementById("workMotion");
  const diagnostics = document.getElementById("diagnostics");
  const pipelineDetails = document.getElementById("pipelineDetails");
  const pipelineStages = document.getElementById("pipelineStages");
  let progressMeta = document.getElementById("progressMeta");
  const STORAGE_KEY = "text-layer-rebuilder:last-job";

  let activeSource = "file";
  let currentJob = null;
  let pollTimer = null;
  let latestFullStatus = null;
  let serviceCapabilities = {};
  let progressSamples = [];
  let progressSampleKey = "";
  let jobLibraryLoading = false;
  let lastJobLibraryLoad = 0;
  const APP_TITLE = document.title;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#039;"
    }[char]));
  }

  function setBusy(label) {
    jobBadge.textContent = label;
    document.body.classList.add("is-busy");
    document.body.style.cursor = "progress";
  }

  function clearBusy() {
    document.body.classList.remove("is-busy");
    document.body.style.cursor = "";
  }

  function renderCards(items) {
    summaryCards.innerHTML = items.map(item => `
      <article>
        <strong>${escapeHtml(item.label)}</strong>
        <span>${escapeHtml(item.value)}</span>
      </article>
    `).join("");
  }

  function renderCacheResult(data) {
    if (!data?.jobId) {
      cacheResult.hidden = true;
      cacheResult.innerHTML = "";
      return;
    }
    const hasCurrentOutput = Boolean(data.fullStatus?.outputCurrent || data.outputCurrent);
    const reused = Boolean(data.reused);
    const restored = Boolean(data.restored);
    const title = reused
      ? (hasCurrentOutput ? "已复用整本" : "已复用原任务")
      : (restored ? "已恢复上次任务" : "已建立新任务");
    const detail = reused
      ? (hasCurrentOutput ? "现有 PDF 与当前处理版本一致" : "OCR 缓存和已完成页面已接续")
      : (restored ? "后台状态和已有输出已恢复" : "未发现相同材料缓存");
    cacheResult.hidden = false;
    cacheResult.className = `cache-result ${reused || restored ? "is-reused" : "is-new"}`;
    cacheResult.innerHTML = `
      <span class="cache-result-mark" aria-hidden="true">${reused || restored ? "✓" : "+"}</span>
      <div>
        <strong>${escapeHtml(title)}</strong>
        <span>${escapeHtml(detail)}</span>
      </div>
    `;
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const error = new Error(payload.error || payload || "本地处理失败");
      error.payload = typeof payload === "object" ? payload : {};
      throw error;
    }
    return payload;
  }

  async function checkEngine() {
    try {
      const status = await api("/api/status");
      serviceCapabilities = status.capabilities || {};
      jobLibrary.hidden = !serviceCapabilities.jobLibrary;
      jobLibraryHeaderButton.disabled = !serviceCapabilities.jobLibrary;
      if (serviceCapabilities.jobLibrary) loadJobLibrary();
      engineDot.classList.add("ready");
      engineTitle.textContent = "本地工具已就绪";
      engineDetail.textContent = status.detail || "断网也能处理本地文件";
    } catch (_) {
      engineTitle.textContent = "请启动本地服务";
      engineDetail.textContent = "运行 start.cmd 后再打开页面";
      jobLibrary.hidden = true;
      jobLibraryHeaderButton.disabled = true;
    }
  }

  function sourceFields() {
    document.querySelectorAll(".source-tab").forEach(tab => {
      tab.classList.toggle("active", tab.dataset.source === activeSource);
    });
    document.querySelectorAll(".source-field").forEach(field => {
      field.classList.toggle("hidden", field.dataset.sourceField !== activeSource);
    });
    sourceFile.required = activeSource === "file";
    sourceUrl.required = activeSource === "url";
  }

  function renderSourceQuality(quality) {
    if (!quality || !Number(quality.unitCount || 0)) {
      sourceQuality.hidden = true;
      sourceQuality.innerHTML = "";
      return;
    }
    const warnings = Array.isArray(quality.warnings) ? quality.warnings : [];
    const state = quality.usable && !warnings.length ? "来源读取正常" : "来源需要确认";
    sourceQuality.hidden = false;
    sourceQuality.className = `source-quality ${warnings.length ? "has-warning" : "is-ready"}`;
    sourceQuality.innerHTML = `
      <div class="source-quality-head">
        <strong>${escapeHtml(state)}</strong>
        <span>${escapeHtml(`${Number(quality.unitCount)} 个正文单元`)}</span>
      </div>
      <div class="source-quality-metrics">
        <span>${escapeHtml(`${Number(quality.totalChars || 0).toLocaleString("zh-CN")} 字符`)}</span>
        <span>${escapeHtml(`重复 ${Number(quality.duplicateUnits || 0)}`)}</span>
        <span>${escapeHtml(`污染 ${Number(quality.suspiciousUnits || 0)}`)}</span>
      </div>
      ${warnings.length ? `<ul>${warnings.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
    `;
  }

  function readSavedState() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      return {};
    }
  }

  function rememberState(extra = {}) {
    const previous = readSavedState();
    const next = {
      ...previous,
      ...extra,
      sourceKind: activeSource,
      sourceUrl: sourceUrl.value.trim(),
      layout: layoutMode.value,
      trialPage: trialPage.value || "1"
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  }

  function applySavedControls(saved) {
    if (!saved || !Object.keys(saved).length) return;
    activeSource = saved.sourceKind || activeSource;
    sourceUrl.value = saved.sourceUrl || "";
    layoutMode.value = saved.layout || layoutMode.value;
    trialPage.value = saved.trialPage || trialPage.value || "1";
    sourceFields();
  }

  function fileLabel(file) {
    if (!file) return "纯扫描或已有文字的 PDF 都可以";
    const mb = file.size / 1024 / 1024;
    return `${file.name} · ${mb >= 100 ? mb.toFixed(0) : mb.toFixed(1)} MB`;
  }

  function jobStateLabel(job) {
    if (job.backendActive) return "运行中";
    return {
      done: "已完成",
      error: "待处理",
      paused: "已暂停",
      planning: "可恢复",
      queued: "等待中",
      running: "可恢复",
      ready: "已载入"
    }[job.state] || "已保存";
  }

  async function loadJobLibrary(force = false) {
    if (!serviceCapabilities.jobLibrary) return;
    const now = Date.now();
    if (jobLibraryLoading || (!force && now - lastJobLibraryLoad < 5000)) return;
    jobLibraryLoading = true;
    try {
      const payload = await api("/api/jobs");
      lastJobLibraryLoad = Date.now();
      const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
      jobLibraryTab.textContent = `任务库 ${jobs.length}`;
      jobLibraryHeaderButton.textContent = `历史任务 ${jobs.length}`;
      if (!jobs.length) {
        jobLibraryList.innerHTML = "<p>还没有保存的书籍任务。</p>";
        return;
      }
      jobLibraryList.innerHTML = jobs.map(job => {
        const total = Number(job.total || 0);
        const processed = Number(job.processed || 0);
        const progress = total ? `${processed} / ${total}` : "尚未运行";
        return `
          <article class="job-library-item ${job.jobId === currentJob ? "is-current" : ""}">
            <div class="job-library-item-head">
              <button class="job-library-book" type="button" data-job-action="folder" data-job-id="${escapeHtml(job.jobId)}" title="打开任务文件夹：${escapeHtml(job.bookName)}" aria-label="打开 ${escapeHtml(job.bookName)} 的任务文件夹">
                <strong>${escapeHtml(job.bookName)}</strong>
              </button>
              <span>${escapeHtml(jobStateLabel(job))}</span>
            </div>
            <div class="job-library-item-meta">
              <span title="${escapeHtml(job.packageName)}">${escapeHtml(job.packageName)}</span>
              <span>${escapeHtml(progress)}</span>
            </div>
            <div class="job-library-item-actions">
              <button type="button" data-job-action="restore" data-job-id="${escapeHtml(job.jobId)}">进入任务</button>
              <button type="button" data-job-action="folder" data-job-id="${escapeHtml(job.jobId)}">打开文件夹</button>
              ${job.outputDownloadUrl ? `<a class="job-result-link" href="${escapeHtml(job.outputDownloadUrl)}" download>下载成品</a>` : ""}
            </div>
          </article>
        `;
      }).join("");
    } catch (error) {
      jobLibraryList.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
    } finally {
      jobLibraryLoading = false;
    }
  }

  function renderInspection(data) {
    stopPolling();
    currentJob = data.jobId;
    jobBadge.textContent = `任务 ${data.jobId.slice(0, 8)}`;
    trialPage.max = data.pageCount || "";
    if (data.trialPage) trialPage.value = data.trialPage;
    if (data.layout) layoutMode.value = data.layout;
    if (data.sourceKind) activeSource = data.sourceKind;
    if (data.sourceUrl) sourceUrl.value = data.sourceUrl;
    sourceFileLabel.textContent = data.sourceName ? `已恢复参考文本：${data.sourceName}` : "用于校准的文本";
    sourceFields();
    renderSourceQuality(data.sourceQuality);
    renderCacheResult(data);
    if (data.pdfName) {
      pdfName.textContent = data.hasCachedPdf === false ? `${data.pdfName} · 源文件已清理` : `已恢复上次 PDF：${data.pdfName}`;
    }
    renderCards([
      { label: "页数", value: data.pageCount || "--" },
      { label: "文字情况", value: data.textLayerLabel || "--" },
      { label: "页面样子", value: data.layoutLabel || "--" }
    ]);
    summaryCards.hidden = false;
    messages.hidden = !(data.messages || []).length;
    messages.innerHTML = (data.messages || []).map(item => `<p>${escapeHtml(item)}</p>`).join("");
    const canRun = data.hasCachedPdf !== false;
    runTrial.disabled = !canRun;
    runFull.disabled = !canRun;
    cleanupJob.disabled = Boolean(data.fullStatus?.backendActive);
    if (data.previewUrl) {
      preview.innerHTML = `<img src="${escapeHtml(data.previewUrl)}" alt="定位线预览">`;
    }
    if (data.outputs) {
      renderOutputs(data.outputs);
      outputState.textContent = data.outputs.length ? "已恢复输出" : "暂无输出";
    }
    if (data.fullStatus) {
      renderFullStatus(data.fullStatus);
      if (["running", "queued", "planning", "reviewing"].includes(data.fullStatus.state)) {
        startPolling(currentJob);
      }
    }
    rememberState({ jobId: currentJob, pdfName: data.pdfName || "" });
    loadJobLibrary(true);
  }

  function renderOutputs(items) {
    if (!items.length) {
      outputs.innerHTML = "<p>暂无输出。</p>";
      return;
    }
    outputs.innerHTML = items.map(item => `
      <a class="output-link" href="${escapeHtml(item.downloadUrl || item.url)}" ${item.downloadUrl ? "download" : "target=\"_blank\" rel=\"noreferrer\""}>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.downloadUrl ? `下载 ${item.detail || "文件"}` : (item.detail || "打开"))}</span>
      </a>
    `).join("");
  }

  function renderCompletionResult(items, state) {
    const pdf = Array.isArray(items) ? items.find(item => item.downloadUrl || item.url) : null;
    if (state !== "done" || !pdf) {
      completionResult.hidden = true;
      completionResult.innerHTML = "";
      return;
    }
    completionResult.hidden = false;
    completionResult.innerHTML = `
      <strong>整本 PDF 已经生成完成</strong>
      <div>
        <a class="completion-download" href="${escapeHtml(pdf.downloadUrl || pdf.url)}" download>下载整本 PDF</a>
        <a class="completion-open" href="${escapeHtml(pdf.url || pdf.downloadUrl)}" target="_blank" rel="noreferrer">在线打开</a>
      </div>
    `;
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds || 0)));
    if (value < 60) return `${value} 秒`;
    const minutes = Math.floor(value / 60);
    const rest = value % 60;
    if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
    const hours = Math.floor(minutes / 60);
    return `${hours} 小时 ${minutes % 60} 分`;
  }

  function estimateProgress(stage, jobId) {
    if (!stage) return { rate: 0, etaSeconds: 0 };
    const processed = Number(stage.processed || 0);
    const total = Number(stage.total || 0);
    const key = `${jobId || ""}:${stage.id || ""}:${total}`;
    const now = Date.now();
    if (key !== progressSampleKey) {
      progressSampleKey = key;
      progressSamples = [];
    }
    progressSamples.push({ at: now, processed });
    progressSamples = progressSamples
      .filter(sample => now - sample.at <= 5 * 60 * 1000)
      .slice(-90);
    const first = progressSamples.find(sample => sample.processed < processed);
    const elapsedMinutes = first ? (now - first.at) / 60000 : 0;
    const sampledRate = first && elapsedMinutes > 0
      ? (processed - first.processed) / elapsedMinutes
      : 0;
    const metrics = stage.metrics || {};
    const rate = Number(metrics.pagesPerMinute || 0) || sampledRate;
    const etaSeconds = Number(metrics.etaSeconds || 0)
      || (rate > 0 && total > processed ? (total - processed) * 60 / rate : 0);
    return { rate, etaSeconds };
  }

  function renderPipeline(pipeline) {
    if (!Array.isArray(pipeline) || !pipeline.length) {
      pipelineStages.hidden = true;
      pipelineStages.innerHTML = "";
      return;
    }
    const stateNames = {
      pending: "等待",
      waiting: "等待边界确认",
      running: "运行中",
      done: "完成",
      blocked: "待处理",
      paused: "已暂停",
      error: "异常"
    };
    const alignStage = pipeline.find(stage => stage.id === "align");
    const displayedPipeline = pipeline.map(stage => {
      const waitingForAlignment = stage.id === "layer"
        && alignStage?.state === "running"
        && (stage.metrics?.waitingForFinalAlignment || Number(stage.processed || 0) <= 1);
      return waitingForAlignment
        ? {
            ...stage,
            state: "waiting",
            detail: Number(stage.processed || 0) > 0
              ? `已安全预写 ${Number(stage.processed)} 页；其余页面等待全书边界恢复`
              : "等待相邻页反向确认与全书边界恢复",
          }
        : stage;
    });
    pipelineStages.hidden = false;
    pipelineStages.innerHTML = displayedPipeline.map(stage => {
      const total = Number(stage.total || 0);
      const processed = Number(stage.processed || 0);
      const percent = total ? Math.min(100, Math.round(processed * 100 / total)) : (stage.state === "done" ? 100 : 0);
      const elapsed = Number(stage.elapsedSeconds || 0);
      const metrics = stage.metrics || {};
      const progressLabel = total ? `${processed} / ${total}` : "";
      const metricLabels = [
        Number(metrics.pagesPerMinute || 0) > 0 ? `${Number(metrics.pagesPerMinute).toFixed(1)} 页/分` : "",
        Number(metrics.etaSeconds || 0) > 0 ? `预计 ${formatDuration(Number(metrics.etaSeconds))}` : "",
        Number(metrics.cachedPages || 0) > 0 ? `缓存 ${Number(metrics.cachedPages)} 页` : "",
        Number(metrics.newlyOcrPages || 0) > 0 ? `新 OCR ${Number(metrics.newlyOcrPages)} 页` : "",
        Number(metrics.freeMemoryMB || 0) > 0 ? `可用内存 ${(Number(metrics.freeMemoryMB) / 1024).toFixed(1)} GB` : "",
      ].filter(Boolean);
      return `
        <div class="pipeline-stage is-${escapeHtml(stage.state || "pending")}">
          <span class="pipeline-dot" aria-hidden="true"></span>
          <div class="pipeline-stage-main">
            <div class="pipeline-stage-head">
              <strong>${escapeHtml(stage.label || stage.id)}</strong>
              <span>${escapeHtml(stateNames[stage.state] || stage.state || "等待")}</span>
            </div>
            <div class="pipeline-stage-detail">${escapeHtml(stage.detail || "尚未开始")}</div>
            ${total ? `<progress max="100" value="${percent}"></progress>` : ""}
          </div>
          <div class="pipeline-stage-meta">
            ${progressLabel ? `<span>${escapeHtml(progressLabel)}</span>` : ""}
            ${elapsed ? `<span>${escapeHtml(formatDuration(elapsed))}</span>` : ""}
            ${metricLabels.map(label => `<span>${escapeHtml(label)}</span>`).join("")}
          </div>
        </div>
      `;
    }).join("");
  }

  function renderDiagnostics(payload, backendActive) {
    const data = payload.diagnostics;
    const unresolved = Number(payload.alignment?.reviewRequired || payload.alignment?.unresolved || 0);
    const qualityRegressionBlocked = Boolean(payload.validation?.qualityRegressionBlocked);
    if (!data?.available || !unresolved) {
      if (!qualityRegressionBlocked) {
        diagnostics.hidden = true;
        diagnostics.innerHTML = "";
        return;
      }
      diagnostics.hidden = false;
      diagnostics.innerHTML = `
        <div class="diagnostics-head">
          <div>
            <strong>质量回归已拦截</strong>
            <p>新一轮核对结果差于已保存结果，系统没有覆盖正式清单。</p>
          </div>
        </div>
      `;
      return;
    }
    const buckets = data.runLengthBuckets || {};
    const runLabel = item => item.start === item.end
      ? `第 ${item.start} 页`
      : `第 ${item.start}-${item.end} 页`;
    const sampleRuns = (data.sampleRuns || []).map(item =>
      `<span>${escapeHtml(runLabel(item))}</span>`
    ).join("");
    const reasons = (data.reasons || []).map(item =>
      `<li><span>${escapeHtml(item.reason)}</span><strong>${escapeHtml(item.count)} 页</strong></li>`
    ).join("");
    const nextAction = backendActive
      ? "后台正在把剩余页面转换为本页整页 OCR。"
      : `仍有 ${unresolved} 页没有完成自动 OCR 转换；重新开始会复用已有缓存。`;
    diagnostics.hidden = false;
    diagnostics.innerHTML = `
      <div class="diagnostics-head">
        <div>
          <strong>非权威页 OCR 尚未完成</strong>
          <p>${escapeHtml(nextAction)}</p>
        </div>
        <span class="diagnostics-count">${escapeHtml(unresolved)} 页待处理</span>
      </div>
      ${qualityRegressionBlocked ? '<p class="diagnostics-warning">质量回归已拦截：新结果没有覆盖已保存清单。</p>' : ''}
      ${data.current === false ? '<p class="diagnostics-warning">诊断清单正在刷新，以顶部实时数量为准。</p>' : ''}
      <div class="diagnostics-grid">
        <div><span>连续区间</span><strong>${escapeHtml(data.runCount || 0)} 段</strong></div>
        <div><span>最长区间</span><strong>${escapeHtml(data.longestRun || 0)} 页</strong></div>
        <div><span>1 / 2 页短段</span><strong>${escapeHtml((buckets.one || 0) + (buckets.two || 0))} 段</strong></div>
        <div><span>章节顺序冲突</span><strong>${escapeHtml(data.sourceOrderConflicts || 0)} 页</strong></div>
      </div>
      ${sampleRuns ? `<div class="diagnostics-runs"><span>前几个区间</span><div>${sampleRuns}</div></div>` : ""}
      ${reasons ? `<ul class="diagnostics-reasons">${reasons}</ul>` : ""}
    `;
  }

  function renderFullStatus(payload) {
    latestFullStatus = payload;
    const total = Number(payload.total || 0);
    const processed = Number(payload.processed || 0);
    const hasBackendSignal = typeof payload.backendActive === "boolean";
    const backendActive = hasBackendSignal
      ? payload.backendActive
      : ["queued", "planning", "running", "reviewing"].includes(payload.state);
    const alignmentStage = Array.isArray(payload.pipeline)
      ? payload.pipeline.find(stage => stage.id === "align")
      : null;
    const alignmentCurrent = ["done", "blocked"].includes(alignmentStage?.state);
    const alignment = alignmentCurrent ? payload.alignment : null;
    const reviewCount = Number(alignment?.reviewRequired || alignment?.unresolved || 0);
    const needsReview = reviewCount > 0;
    const manifestReused = Array.isArray(payload.pipeline)
      && payload.pipeline.some(stage => stage.metrics?.manifestReused);
    const outputMatchesLayout = Boolean(payload.outputCurrent)
      && (layoutMode.value === "auto" || !payload.outputLayout || payload.outputLayout === layoutMode.value);
    const reuseLabel = payload.reusedOutput || outputMatchesLayout
      ? "整本缓存有效"
      : (manifestReused ? "已复用核对清单" : "");
    const stateLabel = {
      done: "已完成",
      error: needsReview ? "待处理" : "失败",
      paused: "已暂停",
      reviewing: "生成预览",
      planning: "后台处理中",
      queued: "排队中",
      running: "写入 PDF"
    }[payload.state] || payload.state || "处理中";
    if (!progressMeta) {
      progressMeta = document.createElement("div");
      progressMeta.id = "progressMeta";
      progressMeta.className = "progress-meta";
      progressDetail.insertAdjacentElement("afterend", progressMeta);
    }
    fullProgress.hidden = false;
    const activeStage = Array.isArray(payload.pipeline)
      ? (payload.pipeline.find(stage => stage.state === "running")
        || payload.pipeline.find(stage => stage.id === payload.activeStage)
        || payload.pipeline.find(stage => ["blocked", "paused", "error"].includes(stage.state)))
      : null;
    const activeStages = Array.isArray(payload.pipeline)
      ? payload.pipeline.filter(stage => {
          if (stage.state !== "running") return false;
          if (stage.id !== "layer") return true;
          return alignmentStage?.state !== "running"
            || (!stage.metrics?.waitingForFinalAlignment && Number(stage.processed || 0) > 1);
        })
      : [];
    const activeMetrics = activeStage?.metrics || {};
    const displayedTotal = Number(activeStage?.total || total);
    const displayedProcessed = Number(activeStage?.processed ?? processed);
    const percent = displayedTotal
      ? Math.min(100, Math.round(displayedProcessed * 100 / displayedTotal))
      : 0;
    const estimate = estimateProgress(activeStage, payload.jobId || currentJob);
    const rateLabel = estimate.rate > 0 ? `${estimate.rate.toFixed(1)} 页/分` : "速度计算中";
    const etaLabel = estimate.etaSeconds > 0 ? `预计剩余 ${formatDuration(estimate.etaSeconds)}` : "剩余时间计算中";
    progressBar.value = percent;
    const progressVerb = {
      ocr: "已缓存",
      align: "已检查",
      classify: "已判定",
      layer: "已写入",
      assemble: "已合并",
      "text-check": "已验证",
      "visual-check": "已验证"
    }[activeStage?.id] || "已处理";
    progressText.textContent = displayedTotal
      ? `${displayedProcessed} / ${displayedTotal} 页 · ${percent}%`
      : "--";
    progressTitle.textContent = {
      done: "整本已完成",
      error: needsReview ? "部分页面尚未处理" : "整本未生成",
      paused: "已暂停",
      reviewing: "正在生成核对预览",
      planning: "后台运行中",
      queued: "准备开始"
    }[payload.state] || (backendActive ? "后台运行中" : "整本处理中");
    const alignmentText = alignment
      ? `权威文本页 ${(alignment.matched || 0) + (alignment.constrained || 0)} 页，纯 OCR 页 ${alignment.ocr || 0} 页，空白页 ${alignment.blank || 0} 页，未完成 ${alignment.unresolved || 0} 页。`
      : "";
    const alignmentPendingText = alignmentStage?.state === "running"
      ? "权威文本页、纯 OCR 页和空白页数量将在逐页处理后公布。"
      : "";
    progressDetail.textContent = backendActive
      ? `${rateLabel} · ${etaLabel}`
      : [payload.message, alignmentText || alignmentPendingText].filter(Boolean).join(" ");
    workMotion.classList.toggle("is-running", backendActive);
    workMotion.classList.toggle("is-stalled", Boolean(payload.stalled));
    renderDiagnostics({ ...payload, alignment }, backendActive);
    renderPipeline(payload.pipeline);
    reviewDraft.hidden = true;
    reviewDraft.disabled = true;
    if (pipelineDetails && (payload.state === "error" || needsReview || payload.stalled)) {
      pipelineDetails.open = true;
    }
    const updated = payload.updatedAt
      ? new Date(Number(payload.updatedAt) * 1000).toLocaleTimeString("zh-CN", { hour12: false })
      : new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const staleSeconds = Number(payload.staleSeconds || 0);
    const staleLabel = payload.stalled && staleSeconds
      ? `<span class="stall-warning">疑似卡住 ${Math.round(staleSeconds)} 秒</span>`
      : "";
    const backendLabel = backendActive
      ? (payload.state === "queued" ? "后台正在启动" : "后台正在运行")
      : "后台未运行";
    progressMeta.innerHTML = backendActive ? `
      <strong class="backend-state is-active"><i aria-hidden="true"></i>${escapeHtml(backendLabel)}</strong>
      ${staleLabel}
    ` : `
      <strong class="backend-state is-inactive"><i aria-hidden="true"></i>${escapeHtml(backendLabel)}</strong>
      <strong>${escapeHtml(stateLabel)}</strong>
      <span>刷新 ${escapeHtml(updated)}</span>
      ${reuseLabel ? `<strong class="reuse-state">${escapeHtml(reuseLabel)}</strong>` : ""}
      ${staleLabel}
    `;
    outputState.textContent = payload.reusedOutput
      ? "已复用现有整本"
      : (needsReview && !backendActive
      ? `待处理 ${reviewCount} 页`
      : (total ? `${stateLabel} · ${processed} / ${total}` : (payload.message || "整本处理中")));
    document.title = total && !["done", "error", "paused"].includes(payload.state)
      ? `${percent}% ${displayedProcessed}/${displayedTotal} · ${APP_TITLE}`
      : APP_TITLE;
    if (payload.outputs) {
      renderOutputs(payload.outputs);
    }
    renderCompletionResult(payload.outputs || [], payload.state);
    const canPause = ["running", "queued", "planning"].includes(payload.state) && !payload.pauseRequested;
    runTrial.disabled = backendActive || !currentJob;
    cleanupJob.disabled = backendActive || !currentJob;
    pauseFull.disabled = !canPause;
    pauseProgress.disabled = !canPause;
    pauseProgress.hidden = !canPause;
    activateProgress.hidden = backendActive || payload.state === "done";
    activateProgress.disabled = backendActive || !currentJob;
    activateProgress.textContent = needsReview
      ? `继续处理 ${reviewCount} 页`
      : (payload.state === "paused" ? "继续运行" : "开始运行");
    runFull.disabled = backendActive || !currentJob || outputMatchesLayout;
    runFull.textContent = needsReview
      ? `继续处理 ${reviewCount} 页`
      : (outputMatchesLayout ? "整本已是最新" : (payload.state === "paused" ? "继续运行" : (payload.state === "done" ? "按当前版式生成" : "生成整本")));
  }

  function stopPolling() {
    if (pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling(jobId) {
    stopPolling();
    const tick = async () => {
      try {
        const payload = await api(`/api/job/${encodeURIComponent(jobId)}`);
        renderFullStatus(payload);
        if (["done", "error", "paused"].includes(payload.state)) {
          stopPolling();
          runFull.disabled = Boolean(payload.outputCurrent);
          pauseFull.disabled = true;
          clearBusy();
        }
      } catch (error) {
        progressDetail.textContent = error.message;
      }
    };
    tick();
    pollTimer = window.setInterval(tick, 2500);
  }

  pdfFile.addEventListener("change", () => {
    pdfName.textContent = fileLabel(pdfFile.files[0]);
    if (pdfFile.files[0]) {
      rememberState({ pdfName: pdfFile.files[0].name });
    }
  });

  document.querySelectorAll(".source-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      activeSource = tab.dataset.source;
      sourceFields();
      rememberState();
    });
  });

  [sourceUrl, layoutMode, trialPage].forEach(control => {
    control.addEventListener("change", () => rememberState());
    control.addEventListener("input", () => rememberState());
  });

  document.addEventListener("click", event => {
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "theme") {
      app.dataset.theme = app.dataset.theme === "dark" ? "" : "dark";
    }
  });

  importForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (!pdfFile.files[0]) return;
    setBusy("正在比对材料与缓存");
    importSubmit.disabled = true;
    cacheResult.hidden = true;
    const formData = new FormData();
    formData.append("pdf", pdfFile.files[0]);
    formData.append("layout", layoutMode.value);
    formData.append("trial_page", trialPage.value || "1");
    formData.append("source_kind", activeSource);
    if (activeSource === "file" && sourceFile.files[0]) {
      formData.append("source_file", sourceFile.files[0]);
    }
    if (activeSource === "url") {
      formData.append("source_url", sourceUrl.value.trim());
    }

    try {
      const data = await api("/api/inspect", { method: "POST", body: formData });
      renderInspection(data);
      rememberState({ jobId: data.jobId, pdfName: pdfFile.files[0].name });
    } catch (error) {
      jobBadge.textContent = "没有整理成功";
      messages.hidden = false;
      messages.textContent = error.message;
    } finally {
      importSubmit.disabled = false;
      clearBusy();
    }
  });

  runTrial.addEventListener("click", async () => {
    if (!currentJob) return;
    setBusy("正在试放文字");
    runTrial.disabled = true;
    preview.innerHTML = `
      <div class="empty-preview loading-preview">
        <div class="flow-mark"><span></span><span></span><span></span></div>
        <strong>文字正在沿着版面落位</strong>
        <p>先生成一页看看，不满意可以换页或换版式。</p>
      </div>
    `;
    try {
      const payload = await api("/api/trial", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobId: currentJob,
          page: Number(trialPage.value) || 1,
          layout: layoutMode.value
        })
      });
      preview.innerHTML = `<img src="${escapeHtml(payload.previewUrl)}" alt="定位线预览">`;
      outputState.textContent = payload.trialStatus || "试页已生成";
      renderOutputs(payload.outputs || []);
      messages.hidden = false;
      messages.innerHTML = `<p>${escapeHtml(payload.message || "试页已生成并通过检查。")}</p>`;
      rememberState({ jobId: currentJob });
      cleanupJob.disabled = false;
    } catch (error) {
      const suggestions = Array.isArray(error.payload?.suggestedPages)
        ? error.payload.suggestedPages
        : [];
      const suggestionButtons = suggestions.map(page =>
        `<button type="button" class="trial-suggestion" data-page="${page}">试第 ${page} 页</button>`
      ).join("");
      preview.innerHTML = `
        <div class="empty-preview">
          <span class="seal large">停</span>
          <strong>这一页不适合作为权威文字预览</strong>
          <p>${escapeHtml(error.message)}</p>
          <p>生成整本时，这一页会自动改用整页 OCR；无需更换试页，也不会阻止整本任务。</p>
          ${suggestionButtons ? `<div class="trial-suggestions">${suggestionButtons}</div>` : ""}
        </div>
      `;
      preview.querySelectorAll(".trial-suggestion").forEach(button => {
        button.addEventListener("click", () => {
          trialPage.value = button.dataset.page;
          rememberState();
          runTrial.click();
        });
      });
      outputState.textContent = "试页未生成；仍可直接生成整本";
    } finally {
      runTrial.disabled = false;
      clearBusy();
    }
  });

  async function startFullRun() {
    if (!currentJob) return;
    setBusy("正在启动后台");
    runFull.disabled = true;
    activateProgress.disabled = true;
    fullProgress.hidden = false;
    renderOutputs([]);
    try {
      const payload = await api("/api/full", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jobId: currentJob, layout: layoutMode.value })
      });
      renderFullStatus(payload);
      rememberState({ jobId: currentJob });
      messages.hidden = false;
      messages.innerHTML = payload.reusedOutput
        ? "<p>材料和处理版本没有变化，已直接使用现有整本 PDF。</p>"
        : "<p>整本任务已经交给本地后台。文件越大越适合让它慢慢跑，页面可以关掉，任务文件会保存在本机。</p>";
      if (payload.state === "done" || payload.state === "error") {
        runFull.disabled = Boolean(payload.outputCurrent);
        activateProgress.disabled = false;
        pauseFull.disabled = true;
        clearBusy();
      } else {
        pauseFull.disabled = false;
        startPolling(currentJob);
      }
    } catch (error) {
      outputState.textContent = "整本没有准备好";
      messages.hidden = false;
      messages.textContent = error.message;
      runFull.disabled = false;
      activateProgress.disabled = false;
      pauseFull.disabled = true;
      clearBusy();
    } finally {
    }
  }

  runFull.addEventListener("click", startFullRun);
  activateProgress.addEventListener("click", startFullRun);

  layoutMode.addEventListener("change", () => {
    rememberState();
    if (!currentJob || latestFullStatus?.backendActive) return;
    const sameLayout = layoutMode.value === "auto"
      || !latestFullStatus?.outputLayout
      || latestFullStatus.outputLayout === layoutMode.value;
    if (latestFullStatus?.outputCurrent && !sameLayout) {
      runFull.disabled = false;
      runFull.textContent = "按当前版式生成";
      cacheResult.hidden = true;
    }
  });

  cleanupJob.addEventListener("click", async () => {
    if (!currentJob) return;
    cleanupJob.disabled = true;
    outputState.textContent = "正在清理";
    try {
      const payload = await api("/api/cleanup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jobId: currentJob, keepFinal: true })
      });
      renderFullStatus(payload);
      rememberState({ jobId: currentJob });
      if (!payload.outputs || !payload.outputs.length) {
        renderOutputs([]);
      }
      outputState.textContent = "已清理缓存";
    } catch (error) {
      outputState.textContent = "清理失败";
      messages.hidden = false;
      messages.textContent = error.message;
    } finally {
      cleanupJob.disabled = false;
    }
  });

  reviewDraft.addEventListener("click", async () => {
    if (!currentJob || latestFullStatus?.backendActive) return;
    reviewDraft.disabled = true;
    try {
      const payload = await api("/api/review-pdf", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jobId: currentJob, layout: layoutMode.value })
      });
      renderFullStatus(payload);
      startPolling(currentJob);
    } catch (error) {
      messages.hidden = false;
      messages.textContent = error.message;
      reviewDraft.disabled = false;
    }
  });

  function toggleJobLibrary() {
    const open = jobLibrary.classList.toggle("is-open");
    jobLibraryTab.setAttribute("aria-expanded", String(open));
    jobLibraryHeaderButton.setAttribute("aria-expanded", String(open));
    if (open) loadJobLibrary(true);
  }

  jobLibraryTab.addEventListener("click", toggleJobLibrary);
  jobLibraryHeaderButton.addEventListener("click", toggleJobLibrary);

  jobLibrary.addEventListener("mouseenter", () => loadJobLibrary());
  refreshJobLibrary.addEventListener("click", () => loadJobLibrary(true));

  jobLibraryList.addEventListener("click", async event => {
    const button = event.target.closest("button[data-job-action]");
    if (!button) return;
    const jobId = button.dataset.jobId;
    button.disabled = true;
    try {
      if (button.dataset.jobAction === "folder") {
        await api("/api/open-job-folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ jobId })
        });
      } else {
        const payload = await api(`/api/restore/${encodeURIComponent(jobId)}`);
        renderInspection(payload);
        jobLibrary.classList.remove("is-open");
        jobLibraryTab.setAttribute("aria-expanded", "false");
        jobLibraryHeaderButton.setAttribute("aria-expanded", "false");
      }
    } catch (error) {
      messages.hidden = false;
      messages.textContent = error.message;
      loadJobLibrary(true);
    } finally {
      button.disabled = false;
    }
  });

  async function requestPause() {
    if (!currentJob) return;
    pauseFull.disabled = true;
    pauseProgress.disabled = true;
    progressDetail.textContent = "正在请求暂停，当前页完成后会停下。";
    try {
      const payload = await api("/api/pause", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ jobId: currentJob })
      });
      renderFullStatus(payload);
    } catch (error) {
      progressDetail.textContent = error.message;
    }
  }

  pauseFull.addEventListener("click", requestPause);
  pauseProgress.addEventListener("click", requestPause);

  renderCards([
    { label: "页数", value: "--" },
    { label: "文字情况", value: "--" },
    { label: "页面样子", value: "--" }
  ]);
  const savedState = readSavedState();
  try {
    applySavedControls(savedState);
  } catch (_) {
    // A stale browser preference must never prevent the service check, task
    // library, or latest-job restoration from starting.
    sourceFields();
  }
  checkEngine();
  const restorePath = "/api/restore/latest";
  if (restorePath) {
    jobBadge.textContent = "正在恢复上次任务";
    api(restorePath)
      .then(renderInspection)
      .catch(() => {
        jobBadge.textContent = "未创建任务";
      });
  }
}());
