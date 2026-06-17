const state = {
  videos: [],
  reports: [],
  jobs: [],
  projects: [],
  users: [],
  artifacts: [],
  templates: [],
  capabilities: null,
  uploadPolicy: null,
  selectedJob: null,
  jobTimer: null,
};

const $ = (id) => document.getElementById(id);

window.addEventListener("unhandledrejection", (event) => {
  const message = String(event.reason?.message || event.reason || "");
  if (message.includes("tabs:outgoing.message.ready")) {
    event.preventDefault();
    return;
  }
  console.warn("WebUI background promise failed:", event.reason);
});

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("需要登录");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch (_) {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  const contentType = response.headers.get("content-type") || "";
  return contentType.includes("application/json") ? response.json() : response.text();
}

function showLogin() {
  $("loginView").hidden = false;
  $("appView").hidden = true;
}

function showApp(user) {
  $("loginView").hidden = true;
  $("appView").hidden = false;
  $("sessionUser").textContent = user ? `已登录：${user}` : "";
  const loginButton = $("loginButton");
  if (loginButton) {
    loginButton.disabled = false;
    loginButton.textContent = "登录";
  }
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(el.timer);
  el.timer = setTimeout(() => {
    el.hidden = true;
  }, 4200);
}

async function bootstrap() {
  bindEvents();
  try {
    const session = await api("/api/session");
    if (session.authenticated) {
      showApp(session.user);
      refreshAll();
    } else {
      showLogin();
    }
  } catch (_) {
    showLogin();
  }
}

function bindEvents() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("loginError").textContent = "";
    const loginButton = $("loginButton");
    loginButton.disabled = true;
    loginButton.textContent = "登录中...";
    try {
      const payload = {
        username: $("loginUser").value,
        password: $("loginPassword").value,
      };
      const result = await api("/api/login", { method: "POST", body: JSON.stringify(payload) });
      loginButton.textContent = "已登录，正在加载...";
      showApp(result.user);
      refreshAll();
    } catch (error) {
      $("loginError").textContent = error.message;
      loginButton.disabled = false;
      loginButton.textContent = "登录";
    }
  });

  $("logoutBtn").addEventListener("click", async () => {
    await api("/api/logout", { method: "POST", body: "{}" }).catch(() => {});
    showLogin();
  });

  $("refreshBtn").addEventListener("click", () => {
    refreshAll();
  });

  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => showTab(button.dataset.tab));
  });

  $("jobForm").addEventListener("submit", startJob);
  $("uploadForm").addEventListener("submit", uploadFiles);
  $("projectForm").addEventListener("submit", createProject);
  $("folderForm").addEventListener("submit", createFolder);
  $("jobProject").addEventListener("change", renderFolderOptions);
  $("jobTemplate").addEventListener("change", applySelectedTemplate);
  ["jobSourceLanguage", "jobSubtitleLanguage", "jobTtsLanguage"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", renderLanguageCapabilities);
  });
  $("saveTemplateBtn").addEventListener("click", saveCurrentTemplate);
  $("userForm").addEventListener("submit", createUser);
  $("refreshArtifactsBtn").addEventListener("click", loadArtifacts);
  $("cleanupDryRunBtn").addEventListener("click", cleanupDryRun);
  $("saveTuningBtn").addEventListener("click", saveTuning);
  $("reloadConfigBtn").addEventListener("click", loadRawConfig);
  $("saveConfigBtn").addEventListener("click", saveRawConfig);
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach((el) => el.classList.toggle("active", el.id === `${name}Tab`));
  if (name === "jobs") refreshJobs();
  if (name === "projects") {
    loadProjects();
    loadUsers();
  }
  if (name === "artifacts") loadArtifacts();
  if (name === "settings") {
    loadTuning();
    loadRawConfig();
  }
}

async function refreshAll() {
  const results = await Promise.allSettled([loadDashboard(), loadVideos(), loadReports(), refreshJobs(), loadProjects(), loadUsers(), loadArtifacts(), loadTemplates(), loadTuning()]);
  const failed = results.filter((item) => item.status === "rejected");
  if (failed.length) {
    console.warn("Partial refresh failed:", failed.map((item) => item.reason));
    toast(`部分数据刷新失败：${failed[0].reason?.message || failed[0].reason}`);
  }
  if (!state.jobTimer) {
    state.jobTimer = setInterval(() => {
      refreshJobs().catch((error) => console.warn("Job refresh failed:", error));
    }, 5000);
  }
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  $("pathLine").textContent = `输入：${data.input_dir}    输出：${data.output_dir}`;
  $("metricVideos").textContent = data.video_count;
  $("metricReports").textContent = data.report_count;
  $("metricTts").textContent = data.tts.backend || "-";
  $("metricLlm").textContent = data.llm.available ? data.llm.model : "未连接";
  const gpu = (data.metrics?.gpu || [])[0] || {};
  $("metricGpu").textContent = gpu.available ? `${Math.round(gpu.util_percent)}% / ${Math.round(gpu.memory_used_percent)}%` : "未检测";
  $("metricQuota").textContent = data.quota
    ? `远端 ${formatBytes(data.quota.remote_used_bytes || 0)} / ${formatBytes(data.quota.remote_quota_bytes || 0)} · 本地 ${formatBytes(data.quota.local_used_bytes || 0)} / ${formatBytes(data.quota.local_quota_bytes || 0)}`
    : "-";
  const workerMetric = $("metricWorker");
  workerMetric.textContent = workerLabel(data.worker);
  workerMetric.title = data.worker?.message || "";
  $("metricDisk").textContent = data.metrics?.disk ? `${Math.round(data.metrics.disk.used_percent)}%` : "-";
  state.uploadPolicy = data.upload_policy || null;
  renderUploadPolicy();
  state.capabilities = data.capabilities || null;
  renderLanguageCapabilities();
  state.projects = data.projects || [];
  renderProjectOptions();
  renderFolderOptions();
  renderProjects();
  state.reports = data.latest_reports || [];
  renderReports();
}

function workerLabel(worker) {
  if (!worker) return "local";
  if (worker.execution_mode === "worker_queue") {
    if (worker.heartbeat_online) return worker.age_seconds != null ? `online ${worker.age_seconds}s` : "online";
    if (worker.status === "offline") return "离线等待";
    return "等待 worker";
  }
  return worker.status === "online" ? "online" : "local";
}

function renderUploadPolicy() {
  const policy = state.uploadPolicy || {};
  const enabled = policy.enabled !== false;
  const message = $("uploadPolicyMessage");
  if (message) {
    message.textContent = policy.message || "文件保存到 _localizer_output/uploads，不覆盖原始课程文件。";
  }
  const input = $("uploadFiles");
  const button = $("uploadForm")?.querySelector('button[type="submit"]');
  if (input) input.disabled = !enabled;
  if (button) {
    button.disabled = !enabled;
    button.textContent = enabled ? "上传" : "远端上传已禁用";
  }
  const result = $("uploadResult");
  if (result && !enabled && !result.textContent.trim()) {
    result.textContent = policy.message || "";
  }
}

function renderLanguageCapabilities() {
  const el = $("languageCapabilityLine");
  if (!el) return;
  const caps = state.capabilities;
  if (!caps) {
    el.textContent = "语言能力：未加载";
    return;
  }
  const source = $("jobSourceLanguage")?.value || "auto";
  const subtitle = $("jobSubtitleLanguage")?.value || "zh-CN";
  const tts = $("jobTtsLanguage")?.value || "zh-CN";
  const asr = languageCapabilityStatus(caps.asr, source, { autoOk: true });
  const translation = translationCapabilityStatus(caps.translation, subtitle);
  const speech = languageCapabilityStatus(caps.tts, tts, { autoOk: false });
  el.innerHTML = [
    capabilityPill("ASR", source, asr),
    capabilityPill("字幕", subtitle, translation),
    capabilityPill("TTS", tts, speech),
  ].join("");
}

function translationCapabilityStatus(cap, language) {
  if (!cap?.available) return { ok: false, warn: false, text: "LLM 未连接" };
  if (languageSupported(cap.supported_target_languages || [], language)) return { ok: true, warn: false, text: cap.model || cap.backend || "local LLM" };
  if (cap.supports_arbitrary_targets) return { ok: true, warn: true, text: "可尝试，需 QA" };
  return { ok: false, warn: false, text: "未列入支持" };
}

function languageCapabilityStatus(cap, language, options = {}) {
  if (!cap) return { ok: false, warn: false, text: "未知" };
  if (options.autoOk && normalizeLanguage(language) === "auto" && cap.auto_detect) {
    return { ok: true, warn: false, text: "自动识别" };
  }
  const ok = languageSupported(cap.supported_languages || [], language);
  return {
    ok,
    warn: false,
    text: ok ? cap.backend || "支持" : "未列入支持",
  };
}

function capabilityPill(label, language, status) {
  const cls = status.ok ? (status.warn ? "warn" : "ok") : "bad";
  return `<span class="capability-pill ${cls}"><strong>${escapeHtml(label)}</strong>${escapeHtml(language)} · ${escapeHtml(status.text)}</span>`;
}

function languageSupported(list, requested) {
  const target = normalizeLanguage(requested);
  return (list || []).some((item) => {
    const supported = normalizeLanguage(item);
    if (supported === "*" || supported === "any") return true;
    if (target === supported) return true;
    const targetBase = target.split("-")[0];
    const supportedBase = supported.split("-")[0];
    return targetBase === supportedBase && ["zh", "en", "ja", "ko"].includes(targetBase);
  });
}

function normalizeLanguage(value) {
  const text = String(value || "").trim().replaceAll("_", "-").toLowerCase();
  const aliases = {
    mandarin: "zh-cn",
    cmn: "zh-cn",
    cantonese: "yue",
    "zh-hans": "zh-cn",
    "zh-cn": "zh-cn",
    "zh-sg": "zh-cn",
    "zh-hant": "zh-tw",
    "zh-tw": "zh-tw",
    "zh-hk": "zh-hk",
  };
  return aliases[text] || text;
}

async function loadVideos() {
  const data = await api("/api/videos");
  state.videos = data.videos || [];
  state.uploadPolicy = data.upload_policy || state.uploadPolicy;
  renderUploadPolicy();
  renderVideoOptions();
  renderVideos();
}

async function loadReports() {
  const data = await api("/api/reports");
  state.reports = data.reports || [];
  renderReportOptions();
  renderReports();
}

function renderVideoOptions() {
  const select = $("jobVideo");
  select.innerHTML = "";
  for (const video of state.videos) {
    const option = document.createElement("option");
    option.value = video.path;
    option.dataset.name = video.name || "";
    option.textContent = `${video.worker_ref ? "[Worker] " : video.uploaded ? "[上传] " : ""}${video.name}`;
    select.appendChild(option);
  }
}

function renderReportOptions() {
  const select = $("jobReport");
  select.innerHTML = "";
  for (const report of state.reports) {
    const option = document.createElement("option");
    option.value = report.path;
    option.textContent = `${report.pass ? "PASS" : "WARN"} · ${report.name}`;
    select.appendChild(option);
  }
}

async function loadProjects() {
  try {
    const data = await api("/api/projects");
    state.projects = data.projects || [];
    renderProjectOptions();
    renderFolderOptions();
    renderProjects();
  } catch (error) {
    console.warn("Project load failed:", error);
  }
}

async function loadTemplates() {
  try {
    const data = await api("/api/templates");
    state.templates = data.templates || [];
    renderTemplateOptions();
  } catch (error) {
    console.warn("Template load failed:", error);
  }
}

function renderTemplateOptions() {
  const select = $("jobTemplate");
  if (!select) return;
  const selected = select.value;
  select.innerHTML = "";
  for (const template of state.templates) {
    const option = document.createElement("option");
    option.value = template.id;
    option.textContent = `${template.shared ? "[共享] " : ""}${template.name}`;
    select.appendChild(option);
  }
  if (selected && state.templates.some((item) => item.id === selected)) {
    select.value = selected;
  }
  if (!select.value && state.templates.length) {
    select.value = state.templates[0].id;
    applySelectedTemplate();
  }
}

function applySelectedTemplate() {
  const template = state.templates.find((item) => item.id === $("jobTemplate").value);
  if (!template) return;
  applyTemplateParams(template.params || {});
}

function applyTemplateParams(params) {
  setValue("jobSourceLanguage", params.source_language);
  setValue("jobSubtitleLanguage", params.target_subtitle_language);
  setValue("jobTtsLanguage", params.target_tts_language);
  setValue("jobQualityMode", params.quality_mode);
  setValue("jobStyle", params.style);
  setValue("jobTtsSpeed", params.tts_speed);
  setValue("jobTtsEmotion", params.tts_emotion);
  setValue("jobEndGap", params.tts_end_gap_seconds);
  setValue("jobMinAudioGap", params.tts_min_audio_gap_seconds);
  setValue("jobSpeakerGender", params.tts_speaker_gender);
  setValue("jobMaxLineChars", params.max_subtitle_line_chars);
  setChecked("jobHardSubtitle", params.mux_hard_subtitle);
  setChecked("jobSoftSubtitle", params.mux_soft_subtitle);
}

function setValue(id, value) {
  if (value === undefined || value === null || value === "") return;
  const el = $(id);
  if (el) el.value = value;
}

function setChecked(id, value) {
  if (value === undefined || value === null || value === "") return;
  const el = $(id);
  if (el) el.checked = Boolean(value);
}

function renderProjectOptions() {
  const selects = [$("jobProject"), $("folderProject")].filter(Boolean);
  for (const select of selects) select.innerHTML = "";
  for (const project of state.projects) {
    for (const select of selects) {
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = `${project.owner ? `${project.owner} / ` : ""}${project.name}`;
      select.appendChild(option);
    }
  }
}

function renderFolderOptions() {
  const select = $("jobFolder");
  if (!select) return;
  const project = state.projects.find((item) => item.id === $("jobProject").value) || state.projects[0];
  select.innerHTML = "";
  for (const folder of project?.folders || [{ id: "root", name: "Root" }]) {
    const option = document.createElement("option");
    option.value = folder.id;
    option.textContent = folder.name || folder.id;
    select.appendChild(option);
  }
}

function renderProjects() {
  const list = $("projectsList");
  if (!list) return;
  list.innerHTML = "";
  if (!state.projects.length) {
    list.textContent = "暂无项目";
    return;
  }
  for (const project of state.projects) {
    const item = document.createElement("div");
    item.className = "job-item";
    item.innerHTML = `
      <div class="job-title">
        <span>${escapeHtml(project.name)}</span>
        <span class="status ok">${escapeHtml(project.owner || "")}</span>
      </div>
      <div class="job-meta">${escapeHtml(project.id)} · ${escapeHtml(project.description || "")}</div>
      <div class="job-meta">project quota ${formatBytes(project.project_used_bytes || 0)} / ${formatBytes(project.quota_project_bytes || 0)}</div>
      <div class="job-meta">folders ${(project.folders || []).map((folder) => escapeHtml(folder.name || folder.id)).join(" · ")}</div>
    `;
    list.appendChild(item);
  }
}

async function createProject(event) {
  event.preventDefault();
  try {
    const payload = {
      name: $("projectName").value,
      description: $("projectDescription").value,
      quota_project_gb: Number($("projectQuota").value || 500),
    };
    await api("/api/projects", { method: "POST", body: JSON.stringify(payload) });
    $("projectName").value = "";
    $("projectDescription").value = "";
    toast("项目已创建");
    await loadProjects();
  } catch (error) {
    toast(`创建项目失败：${error.message}`);
  }
}

async function createFolder(event) {
  event.preventDefault();
  try {
    const projectId = $("folderProject").value;
    const payload = { name: $("folderName").value };
    const result = await api(`/api/projects/${encodeURIComponent(projectId)}/folders`, { method: "POST", body: JSON.stringify(payload) });
    state.projects = result.projects || state.projects;
    $("folderName").value = "";
    toast("文件夹已创建");
    renderProjectOptions();
    renderFolderOptions();
    renderProjects();
  } catch (error) {
    toast(`创建文件夹失败：${error.message}`);
  }
}

async function loadUsers() {
  try {
    const data = await api("/api/users");
    state.users = data.users || [];
    renderUsers();
  } catch (error) {
    state.users = [];
    renderUsers("当前账号不是管理员，不能查看用户列表。");
  }
}

function renderUsers(message = "") {
  const list = $("usersList");
  if (!list) return;
  list.innerHTML = "";
  if (message) {
    list.textContent = message;
    return;
  }
  if (!state.users.length) {
    list.textContent = "暂无用户或无权限";
    return;
  }
  for (const user of state.users) {
    const item = document.createElement("div");
    item.className = "job-item";
    const localGb = bytesToGb(user.quota_local_bytes || 0);
    const remoteGb = bytesToGb(user.quota_remote_bytes || 0);
    item.innerHTML = `
      <div class="job-title">
        <span>${escapeHtml(user.username)}</span>
        <span class="status ${user.disabled ? "bad" : "ok"}">${user.disabled ? "disabled" : escapeHtml(user.role || "user")}</span>
      </div>
      <div class="job-meta">local quota ${formatBytes(user.quota_local_bytes || 0)} · remote quota ${formatBytes(user.quota_remote_bytes || 0)}</div>
      <div class="user-edit">
        <label>role <select data-user-role="${escapeHtml(user.username)}"><option value="user">user</option><option value="admin">admin</option></select></label>
        <label>local GB <input data-user-local="${escapeHtml(user.username)}" type="number" min="1" value="${localGb}" /></label>
        <label>remote GB <input data-user-remote="${escapeHtml(user.username)}" type="number" min="1" value="${remoteGb}" /></label>
        <label class="check-row"><input data-user-disabled="${escapeHtml(user.username)}" type="checkbox" ${user.disabled ? "checked" : ""} /><span>禁用</span></label>
      </div>
      <div class="job-actions"></div>
    `;
    item.querySelector(`[data-user-role="${cssEscape(user.username)}"]`).value = user.role || "user";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "secondary";
    save.textContent = "保存用户";
    save.addEventListener("click", () => saveUserSettings(user.username));
    item.querySelector(".job-actions").appendChild(save);
    list.appendChild(item);
  }
}

async function saveUserSettings(username) {
  try {
    const payload = {
      role: document.querySelector(`[data-user-role="${cssEscape(username)}"]`).value,
      disabled: document.querySelector(`[data-user-disabled="${cssEscape(username)}"]`).checked,
      quota_local_gb: Number(document.querySelector(`[data-user-local="${cssEscape(username)}"]`).value || 1),
      quota_remote_gb: Number(document.querySelector(`[data-user-remote="${cssEscape(username)}"]`).value || 1),
    };
    const result = await api(`/api/users/${encodeURIComponent(username)}`, { method: "PATCH", body: JSON.stringify(payload) });
    state.users = result.users || state.users;
    renderUsers();
    toast("用户已更新");
  } catch (error) {
    toast(`保存用户失败：${error.message}`);
  }
}

async function createUser(event) {
  event.preventDefault();
  try {
    const payload = {
      username: $("newUsername").value,
      password: $("newPassword").value,
      quota_local_gb: Number($("newQuotaLocal").value || 500),
      quota_remote_gb: Number($("newQuotaRemote").value || 10),
    };
    await api("/api/users", { method: "POST", body: JSON.stringify(payload) });
    $("newUsername").value = "";
    $("newPassword").value = "";
    toast("用户已创建");
    await loadUsers();
  } catch (error) {
    toast(`创建用户失败：${error.message}`);
  }
}

function bytesToGb(bytes) {
  const value = Number(bytes || 0) / 1024 / 1024 / 1024;
  return Number.isFinite(value) ? Math.max(1, Math.round(value * 100) / 100) : 1;
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value));
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function currentTemplateParams() {
  return {
    source_language: $("jobSourceLanguage").value || "auto",
    target_subtitle_language: $("jobSubtitleLanguage").value || "zh-CN",
    target_tts_language: $("jobTtsLanguage").value || "zh-CN",
    quality_mode: $("jobQualityMode").value || "best_quality",
    style: $("jobStyle").value || "",
    tts_speed: Number($("jobTtsSpeed").value || 1.0),
    tts_emotion: $("jobTtsEmotion").value || "clear_engaged_teaching",
    tts_end_gap_seconds: Number($("jobEndGap").value || 0.2),
    tts_min_audio_gap_seconds: Number($("jobMinAudioGap").value || 0.08),
    tts_speaker_gender: $("jobSpeakerGender").value || "auto",
    mux_keep_original_audio: false,
    mux_original_audio_volume: 0.08,
    mux_hard_subtitle: $("jobHardSubtitle").checked,
    mux_soft_subtitle: $("jobSoftSubtitle").checked,
    max_subtitle_line_chars: Number($("jobMaxLineChars").value || 22),
  };
}

async function saveCurrentTemplate() {
  const name = $("templateName").value.trim();
  if (!name) {
    toast("请先填写模板名");
    return;
  }
  try {
    const result = await api("/api/templates", { method: "POST", body: JSON.stringify({ name, params: currentTemplateParams() }) });
    state.templates = result.templates || state.templates;
    $("templateName").value = "";
    renderTemplateOptions();
    $("jobTemplate").value = result.template.id;
    toast("模板已保存");
  } catch (error) {
    toast(`保存模板失败：${error.message}`);
  }
}

function renderReports() {
  const table = $("reportsTable");
  table.innerHTML = "";
  table.append(row(["状态", "报告", "问题", "TTS"], true));
  for (const report of state.reports.slice(0, 14)) {
    const status = document.createElement("span");
    status.className = `status ${report.pass ? "ok" : "warn"}`;
    status.textContent = report.pass ? "PASS" : "CHECK";
    const name = document.createElement("div");
    name.innerHTML = `<strong>${escapeHtml(report.name)}</strong><div class="cell-path">${escapeHtml(report.path)}</div>`;
    table.append(row([status, name, String(report.issues ?? "-"), report.tts || "-"]));
  }
}

function renderVideos() {
  const table = $("videosTable");
  table.innerHTML = "";
  table.append(row(["来源", "视频", "大小", "操作"], true));
  for (const video of state.videos) {
    const name = document.createElement("div");
    const displayPath = video.display_path || video.path;
    name.innerHTML = `<strong>${escapeHtml(video.name)}</strong><div class="cell-path">${escapeHtml(displayPath)}</div>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "secondary";
    btn.textContent = "选中";
    btn.addEventListener("click", () => {
      $("jobVideo").value = video.path;
      $("jobWorkerVideoPath").value = "";
      showTab("dashboard");
    });
    table.append(row([video.worker_ref ? "Worker" : video.uploaded ? "上传" : "课程", name, formatBytes(video.size), btn]));
  }
}

async function loadArtifacts() {
  try {
    const data = await api("/api/artifacts");
    state.artifacts = data.artifacts || [];
    renderArtifacts();
  } catch (error) {
    console.warn("Artifact load failed:", error);
  }
}

function renderArtifacts() {
  const table = $("artifactsTable");
  if (!table) return;
  table.innerHTML = "";
  table.append(row(["类型", "文件", "大小", "操作"], true));
  if (!state.artifacts.length) {
    const empty = document.createElement("div");
    empty.className = "empty-line";
    empty.textContent = "暂无生成产物";
    table.appendChild(empty);
    return;
  }
  for (const artifact of state.artifacts.slice(0, 120)) {
    const name = document.createElement("div");
    const displayPath = artifact.display_path || artifact.path || "";
    name.className = "artifact-name";
    name.innerHTML = `${artifact.thumbnail_url ? `<img class="artifact-thumb" src="${escapeHtml(artifact.thumbnail_url)}" alt="" />` : ""}<div><strong>${escapeHtml(artifact.name)}</strong><div class="cell-path">${escapeHtml(displayPath)}</div></div>`;
    const actions = document.createElement("div");
    actions.className = "artifact-actions";
    if (artifact.download_url) {
      const download = document.createElement("a");
      download.className = "button-link";
      download.href = artifact.download_url;
      download.textContent = "下载";
      download.target = "_blank";
      actions.appendChild(download);
    }
    if (artifact.request_cache_url) {
      const request = document.createElement("button");
      request.type = "button";
      request.className = "secondary";
      request.textContent = "请求下载";
      request.addEventListener("click", () => requestArtifactCache(artifact));
      actions.appendChild(request);
    }
    if (artifact.preview_url) {
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "secondary";
      preview.textContent = "预览";
      preview.addEventListener("click", () => previewArtifact(artifact));
      actions.appendChild(preview);
    }
    if (artifact.path) {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "danger";
      del.textContent = "删除";
      del.addEventListener("click", () => deleteArtifact(artifact));
      actions.appendChild(del);
    }
    table.append(row([artifact.kind || "-", name, formatBytes(artifact.size || 0), actions]));
  }
}

function previewArtifact(artifact) {
  const panel = $("artifactPreview");
  panel.innerHTML = "";
  const title = document.createElement("h2");
  title.textContent = artifact.name;
  panel.appendChild(title);
  if ((artifact.media_type || "").startsWith("video/")) {
    const video = document.createElement("video");
    video.controls = true;
    video.src = artifact.preview_url;
    if (artifact.thumbnail_url) video.poster = artifact.thumbnail_url;
    panel.appendChild(video);
  } else if ((artifact.media_type || "").startsWith("audio/")) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = artifact.preview_url;
    panel.appendChild(audio);
  } else {
    panel.textContent = "该文件类型不支持内嵌预览。";
  }
}

async function deleteArtifact(artifact) {
  const ok = window.confirm(`删除本地生成文件？\n${artifact.name}`);
  if (!ok) return;
  try {
    await api(`/api/artifacts/${encodeURIComponent(artifact.id)}`, { method: "DELETE" });
    toast("已删除产物");
    await loadArtifacts();
    await loadDashboard();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

async function requestArtifactCache(artifact) {
  try {
    const result = await api(artifact.request_cache_url, { method: "POST", body: "{}" });
    toast(`已创建下载缓存任务：${result.job?.id || ""}`);
    await refreshJobs();
  } catch (error) {
    toast(`请求下载失败：${error.message}`);
  }
}

async function cleanupDryRun() {
  try {
    const result = await api("/api/cleanup", { method: "POST", body: JSON.stringify({ dry_run: true, older_than_days: 7 }) });
    const bytes = formatBytes(result.cleanup?.bytes || 0);
    toast(`清理预估：${result.cleanup?.count || 0} 个文件，${bytes}`);
  } catch (error) {
    toast(`清理预估失败：${error.message}`);
  }
}

function row(items, header = false) {
  const el = document.createElement("div");
  el.className = `row ${header ? "header" : ""}`;
  for (const item of items) {
    const cell = document.createElement("div");
    if (item instanceof Node) {
      cell.appendChild(item);
    } else {
      cell.textContent = item;
    }
    el.appendChild(cell);
  }
  return el;
}

async function uploadFiles(event) {
  event.preventDefault();
  if (state.uploadPolicy && state.uploadPolicy.enabled === false) {
    const message = state.uploadPolicy.message || "远端上传已禁用";
    $("uploadResult").textContent = message;
    toast(message);
    return;
  }
  const files = $("uploadFiles").files;
  if (!files.length) {
    toast("请选择文件");
    return;
  }
  const form = new FormData();
  for (const file of files) form.append("files", file);
  $("uploadResult").textContent = "上传中...";
  try {
    const result = await api("/api/upload", { method: "POST", body: form });
    $("uploadResult").textContent = JSON.stringify(result, null, 2);
    toast("上传完成");
    await loadVideos();
  } catch (error) {
    $("uploadResult").textContent = error.message;
  }
}

async function startJob(event) {
  event.preventDefault();
  const type = $("jobType").value;
  const workerVideoPath = $("jobWorkerVideoPath").value.trim();
  const selectedVideo = $("jobVideo").selectedOptions[0];
  const payload = {
    type,
    video: workerVideoPath || $("jobVideo").value,
    video_name: workerVideoPath ? "" : selectedVideo?.dataset?.name || selectedVideo?.textContent || "",
    seconds: Number($("jobSeconds").value || 90),
    report: $("jobReport").value,
    tag: $("jobTag").value || "webui",
    force: $("jobForce").checked,
    project_id: $("jobProject").value,
    folder_id: $("jobFolder").value || "root",
    source_language: $("jobSourceLanguage").value || "auto",
    target_subtitle_language: $("jobSubtitleLanguage").value || "zh-CN",
    target_tts_language: $("jobTtsLanguage").value || "zh-CN",
    quality_mode: $("jobQualityMode").value || "best_quality",
    style: $("jobStyle").value || "",
    template_id: $("jobTemplate").value || "",
    ...currentTemplateParams(),
  };
  try {
    const result = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    toast(jobSubmitMessage(result));
    state.selectedJob = result.job.id;
    showTab("jobs");
    await refreshJobs();
    await loadJobLog(result.job.id);
  } catch (error) {
    toast(`启动失败：${error.message}`);
  }
}

function jobSubmitMessage(result) {
  const job = result.job || {};
  const title = job.title || job.type || "任务";
  if (result.dispatch?.target === "worker") {
    const worker = result.dispatch.worker || {};
    if (worker.heartbeat_online) return `任务已排队，worker 在线：${title}`;
    return `任务已排队，等待本地 worker：${title}`;
  }
  return `任务已启动：${title}`;
}

async function refreshJobs() {
  try {
    const data = await api("/api/jobs");
    state.jobs = data.jobs || [];
    renderJobs();
    if (state.selectedJob) await loadJobLog(state.selectedJob, false);
  } catch (_) {}
}

function renderJobs() {
  const list = $("jobsList");
  list.innerHTML = "";
  if (!state.jobs.length) {
    list.textContent = "暂无任务";
    return;
  }
  for (const job of state.jobs) {
    const item = document.createElement("div");
    item.className = "job-item";
    const workerLine = workerJobLine(job);
    const progressLine = jobProgressLine(job);
    item.innerHTML = `
      <div class="job-title">
        <span>${escapeHtml(job.title || job.type)}</span>
        <span class="status ${statusClass(job.status)}">${escapeHtml(jobStatusLabel(job))}</span>
      </div>
      <div class="job-meta">${escapeHtml(job.created_at || "")} · ${escapeHtml(job.id)}</div>
      <div class="job-meta">${escapeHtml(job.user || "")} · ${escapeHtml(job.metadata?.project_id || "")} · ${escapeHtml(job.metadata?.quality_mode || "")}</div>
      ${workerLine ? `<div class="job-meta">${escapeHtml(workerLine)}</div>` : ""}
      ${progressLine ? `<div class="job-meta">${escapeHtml(progressLine)}</div>` : ""}
      <div class="job-actions"></div>
    `;
    const actions = item.querySelector(".job-actions");
    if (canCancelJob(job)) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "danger";
      cancel.textContent = "取消";
      cancel.addEventListener("click", (event) => {
        event.stopPropagation();
        cancelJob(job.id);
      });
      actions.appendChild(cancel);
    }
    if (canRetryJob(job.status)) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "secondary";
      retry.textContent = "重试";
      retry.addEventListener("click", (event) => {
        event.stopPropagation();
        retryJob(job.id);
      });
      actions.appendChild(retry);
    }
    if (job.status !== "deleted") {
      const del = document.createElement("button");
      del.type = "button";
      del.className = "danger";
      del.textContent = "删除记录";
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteJob(job.id);
      });
      actions.appendChild(del);
    }
    item.addEventListener("click", () => {
      state.selectedJob = job.id;
      loadJobLog(job.id);
    });
    list.appendChild(item);
  }
}

function jobProgressLine(job) {
  const progress = Number(job.progress);
  if (Number.isFinite(progress)) {
    return `进度：${Math.max(0, Math.min(100, Math.round(progress)))}%`;
  }
  const tail = String(job.log_tail || "").trim().split(/\r?\n/).filter(Boolean);
  if (tail.length && ["claimed", "running", "retrying", "failed"].includes(job.status)) {
    return `日志：${tail[tail.length - 1].slice(0, 120)}`;
  }
  return "";
}

function workerJobLine(job) {
  if (job.dispatch_target !== "worker") return "";
  if (job.cancel_requested) return `worker：取消中，等待 ${job.claimed_by || "本地 worker"} 确认`;
  const submitted = job.metadata?.worker_status_at_submit || job.worker_status_at_submit || {};
  if (job.status === "queued" || job.status === "retrying") {
    if (submitted.heartbeat_online === false) return `worker：等待本地 worker 心跳（提交时 ${submitted.status || "unknown"}）`;
    return "worker：已排队，等待领取";
  }
  if (job.claimed_by) return `worker：${job.claimed_by}`;
  if (submitted.status) return `worker：提交时 ${submitted.status}`;
  return "worker：队列任务";
}

function canRetryJob(status) {
  return ["done", "passed", "failed", "cancelled"].includes(status);
}

function canCancelJob(job) {
  return ["queued", "claimed", "running", "retrying", "paused"].includes(job.status) && !job.cancel_requested;
}

function jobStatusLabel(job) {
  return job.cancel_requested && ["claimed", "running", "paused"].includes(job.status) ? "取消中" : job.status;
}

async function cancelJob(jobId) {
  if (!confirm("取消该任务？正在本地 worker 上运行的任务会在下一次控制轮询时停止。")) return;
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST", body: "{}" });
    if (result.ok) {
      toast(result.message || "取消请求已发送");
      await refreshJobs();
    } else {
      toast(result.message || "当前任务不能取消");
    }
  } catch (error) {
    toast(`取消失败：${error.message}`);
  }
}

async function retryJob(jobId) {
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST", body: "{}" });
    state.selectedJob = result.job.id;
    toast("任务已重新进入队列");
    await refreshJobs();
    await loadJobLog(result.job.id, false);
  } catch (error) {
    toast(`重试失败：${error.message}`);
  }
}

async function deleteJob(jobId) {
  if (!confirm("删除任务记录？这会从历史列表隐藏该任务，但不会直接删除产物文件。")) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    if (state.selectedJob === jobId) {
      state.selectedJob = null;
      $("selectedJobLine").textContent = "未选择任务";
      $("jobLog").textContent = "";
    }
    toast("任务记录已软删除");
    await refreshJobs();
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

async function loadJobLog(jobId, noisy = true) {
  try {
    const text = await api(`/api/jobs/${encodeURIComponent(jobId)}/log?lines=360`);
    $("selectedJobLine").textContent = jobId;
    $("jobLog").textContent = text || "日志为空";
  } catch (error) {
    if (noisy) toast(error.message);
  }
}

async function loadTuning() {
  const data = await api("/api/tuning");
  const form = $("tuningForm");
  form.innerHTML = "";
  for (const field of data.fields || []) {
    const label = document.createElement("label");
    if (field.type === "textarea") label.className = "wide";
    label.textContent = field.label;
    const input = controlForField(field);
    if (!input.matches(".check-row")) input.dataset.path = field.path;
    label.appendChild(input);
    form.appendChild(label);
  }
}

function controlForField(field) {
  if (field.type === "bool") {
    const wrap = document.createElement("label");
    wrap.className = "check-row";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(field.value);
    input.dataset.path = field.path;
    const span = document.createElement("span");
    span.textContent = field.value ? "开启" : "关闭";
    input.addEventListener("change", () => {
      span.textContent = input.checked ? "开启" : "关闭";
    });
    wrap.append(input, span);
    return wrap;
  }
  if (field.type === "choice") {
    const select = document.createElement("select");
    for (const value of field.options || []) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.appendChild(option);
    }
    select.value = field.value ?? "";
    return select;
  }
  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value ?? "";
    return textarea;
  }
  const input = document.createElement("input");
  input.type = field.type === "int" || field.type === "float" ? "number" : "text";
  if (field.type === "float") input.step = "0.01";
  if (field.min !== undefined) input.min = field.min;
  if (field.max !== undefined) input.max = field.max;
  input.value = field.value ?? "";
  return input;
}

async function saveTuning() {
  const values = {};
  for (const control of $("tuningForm").querySelectorAll("[data-path]")) {
    if (control.matches(".check-row")) {
      const input = control.querySelector("input");
      values[input.dataset.path] = input.checked;
    } else if (control.type === "checkbox") {
      values[control.dataset.path] = control.checked;
    } else {
      values[control.dataset.path] = control.value;
    }
  }
  try {
    await api("/api/tuning", { method: "POST", body: JSON.stringify({ values }) });
    toast("常用参数已保存");
    await loadTuning();
    await loadRawConfig();
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

async function loadRawConfig() {
  try {
    $("rawConfig").value = await api("/api/config/raw");
  } catch (error) {
    toast(error.message);
  }
}

async function saveRawConfig() {
  try {
    await api("/api/config/raw", { method: "POST", body: JSON.stringify({ yaml: $("rawConfig").value }) });
    toast("YAML 已保存");
    await refreshAll();
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

function statusClass(status) {
  if (status === "done" || status === "passed") return "ok";
  if (status === "failed" || status === "cancelled" || status === "deleted") return "bad";
  return "warn";
}

function formatBytes(size) {
  if (!Number.isFinite(size)) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let n = size;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

bootstrap();
