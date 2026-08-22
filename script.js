(function () {
  const app = document.querySelector(".app");
  const importForm = document.getElementById("importForm");
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
  const preview = document.getElementById("preview");
  const outputs = document.getElementById("outputs");
  const outputState = document.getElementById("outputState");
  const fullProgress = document.getElementById("fullProgress");
  const progressTitle = document.getElementById("progressTitle");
  const progressText = document.getElementById("progressText");
  const progressBar = document.getElementById("progressBar");
  const progressDetail = document.getElementById("progressDetail");
  const pipelineStages = document.getElementById("pipelineStages");
  let progressMeta = document.getElementById("progressMeta");
  const STORAGE_KEY = "text-layer-rebuilder:last-job";

  let activeSource = "file";
  let currentJob = null;
  let pollTimer = null;
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

  async function api(path, options) {
    const response = await fetch(path, options);
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      throw new Error(payload.error || payload || "本地处理失败");
    }
    return payload;
  }

  async function checkEngine() {
    try {
      const status = await api("/api/status");
      engineDot.classList.add("ready");
      engineTitle.textContent = "本地工具已就绪";
      engineDetail.textContent = status.detail || "断网也能处理本地文件";
    } catch (_) {
      engineTitle.textContent = "请启动本地服务";
      engineDetail.textContent = "运行 start.cmd 后再打开页面";
    }
  }

  function sourceFields() {
    document.querySelectorAll(".source-tab").forEach(tab => {
      tab.classList.toggle("active", tab.dataset.source === activeSource);
    });
    document.querySelectorAll(".source-field").forEach(field => {
      field.classList.toggle("hidden", field.dataset.sourceField !== activeSource);
    });
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

  function renderInspection(data) {
    currentJob = data.jobId;
    jobBadge.textContent = `任务 ${data.jobId.slice(0, 8)}`;
    trialPage.max = data.pageCount || "";
    if (data.trialPage) trialPage.value = data.trialPage;
    if (data.layout) layoutMode.value = data.layout;
    if (data.sourceKind) activeSource = data.sourceKind;
    if (data.sourceUrl) sourceUrl.value = data.sourceUrl;
    sourceFileLabel.textContent = data.sourceName ? `已恢复参考文本：${data.sourceName}` : "用于校准的文本";
    sourceFields();
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
    cleanupJob.disabled = false;
    if (data.previewUrl) {
      preview.innerHTML = `<img src="${escapeHtml(data.previewUrl)}" alt="定位线预览">`;
    }
    if (data.outputs) {
      renderOutputs(data.outputs);
      outputState.textContent = data.outputs.length ? "已恢复输出" : "暂无输出";
    }
    if (data.fullStatus) {
      renderFullStatus(data.fullStatus);
      if (["running", "queued", "planning"].includes(data.fullStatus.state)) {
        startPolling(currentJob);
      }
    }
    rememberState({ jobId: currentJob, pdfName: data.pdfName || "" });
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

  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds || 0)));
    if (value < 60) return `${value} 秒`;
    const minutes = Math.floor(value / 60);
    const rest = value % 60;
    if (minutes < 60) return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
    const hours = Math.floor(minutes / 60);
    return `${hours} 小时 ${minutes % 60} 分`;
  }

  function renderPipeline(pipeline) {
    if (!Array.isArray(pipeline) || !pipeline.length) {
      pipelineStages.hidden = true;
      pipelineStages.innerHTML = "";
      return;
    }
    const stateNames = {
      pending: "等待",
      running: "运行中",
      done: "完成",
      blocked: "待核对",
      paused: "已暂停",
      error: "异常"
    };
    pipelineStages.hidden = false;
    pipelineStages.innerHTML = pipeline.map(stage => {
      const total = Number(stage.total || 0);
      const processed = Number(stage.processed || 0);
      const percent = total ? Math.min(100, Math.round(processed * 100 / total)) : (stage.state === "done" ? 100 : 0);
      const elapsed = Number(stage.elapsedSeconds || 0);
      const progressLabel = total ? `${processed} / ${total}` : "";
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
          </div>
        </div>
      `;
    }).join("");
  }

  function renderFullStatus(payload) {
    const total = Number(payload.total || 0);
    const processed = Number(payload.processed || 0);
    const percent = total ? Math.min(100, Math.round(processed * 100 / total)) : 0;
    const reviewCount = Number(payload.alignment?.reviewRequired || payload.alignment?.unresolved || 0);
    const needsReview = reviewCount > 0;
    const hasBackendSignal = typeof payload.backendActive === "boolean";
    const backendActive = hasBackendSignal
      ? payload.backendActive
      : ["queued", "planning", "running"].includes(payload.state);
    const stateLabel = {
      done: "已完成",
      error: needsReview ? "待核对" : "失败",
      paused: "已暂停",
      planning: "严格核对",
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
    progressBar.value = percent;
    const activeStage = Array.isArray(payload.pipeline)
      ? (payload.pipeline.find(stage => stage.id === payload.activeStage)
        || payload.pipeline.find(stage => ["running", "blocked", "paused", "error"].includes(stage.state)))
      : null;
    const progressVerb = {
      ocr: "已缓存",
      align: "已检查",
      classify: "已判定",
      layer: "已写入",
      assemble: "已合并",
      "text-check": "已验证",
      "visual-check": "已验证"
    }[activeStage?.id] || "已处理";
    progressText.textContent = total ? `${progressVerb} ${processed} / ${total} 页` : "--";
    progressTitle.textContent = {
      done: "整本已完成",
      error: needsReview ? "尚待核对" : "整本未生成",
      paused: "已暂停",
      planning: "正在核对页面",
      queued: "准备开始"
    }[payload.state] || "整本处理中";
    const alignment = payload.alignment;
    const alignmentText = alignment
      ? `严格锁定 ${(alignment.matched || 0) + (alignment.constrained || 0)} 页，来源未收录 ${alignment.sourceOmitted || 0} 页，估算 ${alignment.estimated || 0} 页，待核对 ${alignment.reviewRequired || alignment.unresolved || 0} 页。`
      : "";
    progressDetail.textContent = [payload.message, alignmentText].filter(Boolean).join(" ");
    renderPipeline(payload.pipeline);
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
    progressMeta.innerHTML = `
      <strong class="backend-state ${backendActive ? "is-active" : "is-inactive"}"><i aria-hidden="true"></i>${escapeHtml(backendLabel)}</strong>
      <strong>${escapeHtml(stateLabel)}</strong>
      <span>任务 ${escapeHtml(payload.jobId || currentJob || "--")}</span>
      <span>刷新 ${escapeHtml(updated)}</span>
      ${staleLabel}
    `;
    outputState.textContent = needsReview && !backendActive
      ? `待核对 ${reviewCount} 页`
      : (total ? `${stateLabel} · ${processed} / ${total}` : (payload.message || "整本处理中"));
    document.title = total && !["done", "error", "paused"].includes(payload.state)
      ? `${percent}% ${processed}/${total} · ${APP_TITLE}`
      : APP_TITLE;
    if (payload.outputs) {
      renderOutputs(payload.outputs);
    }
    const canPause = ["running", "queued", "planning"].includes(payload.state) && !payload.pauseRequested;
    pauseFull.disabled = !canPause;
    pauseProgress.disabled = !canPause;
    pauseProgress.hidden = !canPause;
    activateProgress.hidden = backendActive || payload.state === "done";
    activateProgress.disabled = backendActive || !currentJob;
    activateProgress.textContent = needsReview
      ? `重新核对 ${reviewCount} 页`
      : (payload.state === "paused" ? "继续运行" : "开始运行");
    runFull.disabled = backendActive || !currentJob;
    runFull.textContent = needsReview
      ? `重新核对 ${reviewCount} 页`
      : (payload.state === "paused" ? "继续运行" : (payload.state === "done" ? "重新生成" : "生成整本"));
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
          runFull.disabled = false;
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
    setBusy("正在铺开材料");
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
      preview.innerHTML = `<div class="empty-preview"><span class="seal large">停</span><strong>这页没有生成成功</strong><p>${escapeHtml(error.message)}</p></div>`;
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
      messages.innerHTML = "<p>整本任务已经交给本地后台。文件越大越适合让它慢慢跑，页面可以关掉，任务文件会保存在本机。</p>";
      if (payload.state === "done" || payload.state === "error") {
        runFull.disabled = false;
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
  applySavedControls(savedState);
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
