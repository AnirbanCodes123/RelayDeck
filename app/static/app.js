const $ = (selector) => document.querySelector(selector);
const videoInput = $("#videoInput");
const dropzone = $("#dropzone");
const queueSection = $("#queueSection");
const uploadQueue = $("#uploadQueue");
const uploadAllButton = $("#uploadAllButton");
const clearQueueButton = $("#clearQueueButton");
const loopAll = $("#loopAll");
const cpuModel = $("#cpuModel");
const gpuModel = $("#gpuModel");
const streamList = $("#streamList");
const searchInput = $("#searchInput");
const servicePill = $("#servicePill");
const serviceText = $("#serviceText");
const toast = $("#toast");
const queueTemplate = $("#queueItemTemplate");
const streamTemplate = $("#streamItemTemplate");
const liveUrlTemplate = $("#liveUrlItemTemplate");
const liveUrlList = $("#liveUrlList");
const copyAllUrlsButton = $("#copyAllUrlsButton");

let queuedFiles = [];
let streams = [];
let uploading = false;
let toastTimer;
let gpuDefaultApplied = false;
let metricsInFlight = false;
const METRIC_HISTORY = 40;
const metricHistory = { cpu: [], ram: [], gpu: [] };

function slugify(filename) {
  return filename
    .replace(/\.[^.]+$/, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 55) || "camera";
}

function uniqueEndpoint(base) {
  const used = new Set([
    ...streams.map((stream) => stream.stream_name),
    ...queuedFiles.map((item) => item.endpoint),
  ]);
  if (!used.has(base)) return base;
  let suffix = 2;
  while (used.has(`${base}-${suffix}`)) suffix += 1;
  return `${base}-${suffix}`;
}

function humanFileSize(bytes) {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatUptime(seconds = 0) {
  const hours = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const remaining = String(seconds % 60).padStart(2, "0");
  return `${hours}:${minutes}:${remaining}`;
}

function formatGiB(bytes) {
  if (bytes == null) return "—";
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

function loadLevel(percent) {
  if (percent == null) return "off";
  if (percent >= 85) return "hot";
  if (percent >= 70) return "warm";
  return "ok";
}

function pushHistory(key, value) {
  const series = metricHistory[key];
  series.push(value == null ? null : Number(value));
  if (series.length > METRIC_HISTORY) series.shift();
}

function sparkline(values) {
  const usable = values.filter((value) => value != null);
  if (usable.length < 2) return "";
  const width = 72;
  const height = 24;
  const points = values
    .map((value, index) => {
      if (value == null) return null;
      const x = (index / Math.max(1, values.length - 1)) * width;
      const y = height - (Math.min(100, Math.max(0, value)) / 100) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .filter(Boolean)
    .join(" ");
  return `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true"><polyline points="${points}" /></svg>`;
}

function renderMeter(prefix, percent, detail, available = true) {
  const meter = $(`#${prefix}Meter`);
  const bar = $(`#${prefix}Bar`);
  const label = $(`#${prefix}Percent`);
  const footnote = $(`#${prefix}Detail`);
  const spark = $(`#${prefix}Spark`);
  const shown = available ? percent : null;
  meter.classList.remove("load-ok", "load-warm", "load-hot", "load-off");
  meter.classList.add(`load-${loadLevel(shown)}`);
  label.textContent = shown == null ? "—" : `${Math.round(shown)}%`;
  bar.style.width = `${shown == null ? 0 : Math.min(100, Math.max(0, shown))}%`;
  footnote.textContent = detail;
  spark.innerHTML = sparkline(metricHistory[prefix]);
}

function notify(message, isError = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("visible");
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 3000);
}

function runningStreams() {
  return streams.filter((stream) => ["live", "starting"].includes(stream.status));
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    const ok = document.execCommand("copy");
    field.remove();
    return ok;
  }
}

function addFiles(files) {
  const remaining = Math.max(0, 80 - streams.length - queuedFiles.length);
  [...files]
    .filter((file) => file.type.startsWith("video/") || /\.(mkv|ts|m4v)$/i.test(file.name))
    .slice(0, remaining)
    .forEach((file) => {
      queuedFiles.push({
        id: crypto.randomUUID(),
        file,
        endpoint: uniqueEndpoint(slugify(file.name)),
        status: "ready",
        error: "",
      });
    });
  if (files.length > remaining) notify(`Only ${remaining} capacity slots were available.`, true);
  renderQueue();
}

function renderQueue() {
  uploadQueue.replaceChildren();
  queueSection.hidden = queuedFiles.length === 0;
  $("#queueCount").textContent = `${queuedFiles.length} file${queuedFiles.length === 1 ? "" : "s"} ready`;
  for (const item of queuedFiles) {
    const node = queueTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.id = item.id;
    node.classList.add(item.status);
    node.querySelector(".queue-file strong").textContent = item.file.name;
    node.querySelector(".queue-file span").textContent = humanFileSize(item.file.size);
    const input = node.querySelector("input");
    input.value = item.endpoint;
    input.disabled = item.status === "uploading" || item.status === "done";
    input.addEventListener("input", () => {
      item.endpoint = input.value.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
      input.value = item.endpoint;
    });
    const status = node.querySelector(".queue-status");
    status.textContent =
      item.status === "uploading"
        ? "Uploading…"
        : item.status === "done"
          ? "Published"
          : item.status === "failed"
            ? item.error || "Failed"
            : "Ready";
    node.querySelector(".remove-queue").disabled = item.status === "uploading";
    node.querySelector(".remove-queue").addEventListener("click", () => {
      queuedFiles = queuedFiles.filter((candidate) => candidate.id !== item.id);
      renderQueue();
    });
    uploadQueue.append(node);
  }
  uploadAllButton.disabled = uploading || !queuedFiles.some((item) => item.status !== "done");
}

async function uploadOne(item) {
  item.status = "uploading";
  renderQueue();
  const payload = new FormData();
  payload.append("video", item.file);
  payload.append("stream_name", item.endpoint);
  payload.append("loop", String(loopAll.checked));
  payload.append("start_immediately", "true");
  payload.append(
    "transcode_engine",
    document.querySelector('input[name="engine"]:checked').value,
  );
  try {
    const response = await fetch("/api/streams", { method: "POST", body: payload });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Upload failed");
    item.status = "done";
    item.gpuFallback = result.gpu_fallback;
  } catch (error) {
    item.status = "failed";
    item.error = error.message;
  }
  renderQueue();
}

async function uploadAll() {
  const pending = queuedFiles.filter((item) => item.status !== "done");
  if (!pending.length) return;
  if (pending.some((item) => !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(item.endpoint))) {
    notify("Every stream needs a valid, non-empty endpoint.", true);
    return;
  }
  if (new Set(pending.map((item) => item.endpoint)).size !== pending.length) {
    notify("Endpoint names must be unique.", true);
    return;
  }
  uploading = true;
  renderQueue();
  let cursor = 0;
  async function worker() {
    while (cursor < pending.length) {
      const item = pending[cursor];
      cursor += 1;
      await uploadOne(item);
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, pending.length) }, worker));
  uploading = false;
  const failures = pending.filter((item) => item.status === "failed").length;
  if (failures) notify(`${failures} stream${failures === 1 ? "" : "s"} could not be published.`, true);
  else if (pending.some((item) => item.gpuFallback)) {
    notify("NVIDIA GPU was unavailable; affected streams are using CPU.", true);
  }
  else {
    notify(`${pending.length} stream${pending.length === 1 ? "" : "s"} published.`);
    queuedFiles = queuedFiles.filter((item) => item.status !== "done");
  }
  renderQueue();
  await refreshStreams();
}

function renderSummary(data) {
  const aggregate = data.aggregate;
  $("#totalMetric").textContent = aggregate.total;
  $("#liveMetric").textContent = aggregate.live + aggregate.starting;
  $("#errorMetric").textContent = aggregate.error;
  $("#capacityUsed").textContent = aggregate.total;
  $("#capacityMax").textContent = ` / ${aggregate.capacity}`;
  const ready = data.services.ffmpeg && data.services.mediamtx;
  servicePill.classList.toggle("online", ready);
  servicePill.classList.toggle("error", !ready);
  serviceText.textContent = ready
    ? "Media router ready"
    : !data.services.ffmpeg
      ? "FFmpeg unavailable"
      : "MediaMTX offline";
  const gpu = data.services?.nvidia || {};
  cpuModel.textContent = data.services.cpu?.name || "CPU model unavailable";
  gpuModel.classList.toggle("unavailable", !gpu.available);
  gpuModel.title = gpu.error || "";
  gpuModel.textContent = gpu.available
    ? gpu.name || "NVIDIA GPU"
    : gpu.error
      ? gpu.error
      : gpu.name
        ? `${gpu.name} · NVENC unavailable`
        : "Unavailable · streams fall back to CPU";
  if (gpu.available && !gpuDefaultApplied) {
    const nvidiaRadio = document.querySelector('input[name="engine"][value="nvidia"]');
    if (nvidiaRadio) nvidiaRadio.checked = true;
    gpuDefaultApplied = true;
  }
}

function renderStreams() {
  const query = searchInput.value.trim().toLowerCase();
  const visible = streams.filter(
    (stream) =>
      stream.filename.toLowerCase().includes(query) ||
      stream.stream_name.toLowerCase().includes(query),
  );
  streamList.replaceChildren();
  $("#visibleCount").textContent = `${visible.length} stream${visible.length === 1 ? "" : "s"}`;

  for (const stream of visible) {
    const node = streamTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.id = stream.id;
    node.classList.add(stream.status);
    node.querySelector(".source-cell strong").textContent = stream.filename;
    node.querySelector(".source-cell small").textContent = `/${stream.stream_name}`;
    node.querySelector(".endpoint-cell code").textContent = stream.rtsp_url;
    const mode = node.querySelector(".mode-badge");
    mode.textContent = stream.processing_mode;
    mode.classList.toggle(
      "transcode",
      ["cpu", "transcode"].includes(stream.processing_mode),
    );
    mode.classList.toggle("nvidia", stream.processing_mode === "nvidia");
    node.querySelector(".state-badge b").textContent = stream.status;
    node.querySelector(".uptime-cell").textContent = formatUptime(stream.uptime_seconds);
    const active = ["starting", "live", "stopping"].includes(stream.status);
    const toggle = node.querySelector(".stream-toggle");
    toggle.textContent = active ? "Stop" : "Start";
    toggle.disabled = stream.status === "stopping";
    node.querySelector(".copy-stream").addEventListener("click", async () => {
      if (await copyText(stream.rtsp_url)) notify("RTSP URL copied.");
      else notify("Could not copy the RTSP URL.", true);
    });
    toggle.addEventListener("click", () =>
      streamAction(stream.id, active ? "stop" : "start"),
    );
    node.querySelector(".delete-button").addEventListener("click", () => deleteStream(stream));
    node.querySelector(".more-button").addEventListener("click", () => toggleDetails(node, stream));
    streamList.append(node);
  }
  renderLiveUrls();
}

function renderLiveUrls() {
  const live = runningStreams();
  $("#liveUrlCount").textContent = String(live.length);
  $("#liveUrlHeading").textContent = `${live.length} running URL${live.length === 1 ? "" : "s"}`;
  copyAllUrlsButton.disabled = live.length === 0;
  liveUrlList.replaceChildren();
  for (const stream of live) {
    const node = liveUrlTemplate.content.firstElementChild.cloneNode(true);
    node.querySelector("strong").textContent = stream.filename;
    node.querySelector("small").textContent = stream.status;
    node.querySelector("code").textContent = stream.rtsp_url;
    node.querySelector(".copy-stream").addEventListener("click", async () => {
      if (await copyText(stream.rtsp_url)) notify("RTSP URL copied.");
      else notify("Could not copy the RTSP URL.", true);
    });
    liveUrlList.append(node);
  }
}

function showWorkspaceTab(name) {
  document.querySelectorAll(".workspace-tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".workspace-panel").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  if (name === "urls" && window.location.hash !== "#urls") {
    history.replaceState(null, "", "#urls");
  }
  if (name === "control" && window.location.hash === "#urls") {
    history.replaceState(null, "", "#streams");
  }
}

async function toggleDetails(node, stream) {
  const details = node.querySelector(".stream-details");
  details.hidden = !details.hidden;
  if (details.hidden) return;
  details.querySelector("strong").textContent =
    stream.processing_mode === "copy"
      ? "Direct H.264/AAC stream copy"
        : stream.processing_mode === "nvidia"
          ? "NVIDIA NVENC H.264 transcode"
          : "H.264/AAC CPU transcode";
  details.querySelector(".error-detail").textContent = stream.error || "";
  try {
    const response = await fetch(`/api/streams/${stream.id}`);
    const result = await response.json();
    details.querySelector("pre").textContent =
      result.logs?.length ? result.logs.join("\n") : "No FFmpeg messages.";
  } catch {
    details.querySelector("pre").textContent = "Could not load FFmpeg messages.";
  }
}

async function apiAction(url, method = "POST") {
  const response = await fetch(url, { method });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "Request failed");
  return result;
}

async function streamAction(id, action) {
  try {
    await apiAction(`/api/streams/${id}/${action}`);
    await refreshStreams();
  } catch (error) {
    notify(error.message, true);
  }
}

async function deleteStream(stream) {
  if (!window.confirm(`Delete “${stream.filename}” and its stored upload?`)) return;
  try {
    await apiAction(`/api/streams/${stream.id}`, "DELETE");
    notify("Stream deleted.");
    await refreshStreams();
  } catch (error) {
    notify(error.message, true);
  }
}

async function refreshStreams() {
  try {
    const response = await fetch("/api/streams", { cache: "no-store" });
    if (!response.ok) throw new Error("Status request failed");
    const data = await response.json();
    streams = data.streams;
    renderSummary(data);
    renderStreams();
  } catch {
    servicePill.classList.remove("online");
    servicePill.classList.add("error");
    serviceText.textContent = "Backend unavailable";
  }
}

function renderHostMetrics(data) {
  const stamp = $("#metricsStamp");
  stamp.textContent = "Live";
  stamp.classList.remove("stale");
  const cpu = data.cpu || {};
  const memory = data.memory || {};
  const gpu = data.gpu || {};
  pushHistory("cpu", cpu.percent);
  pushHistory("ram", memory.percent);
  pushHistory("gpu", gpu.available ? gpu.percent : null);

  const cpuDetail = [
    cpu.cores ? `${cpu.cores} cores` : null,
    cpu.load1 != null ? `load ${cpu.load1}` : null,
  ]
    .filter(Boolean)
    .join(" · ") || "Sampling…";
  renderMeter("cpu", cpu.percent, cpuDetail, cpu.percent != null);

  const ramDetail =
    memory.used_bytes != null && memory.total_bytes
      ? `${formatGiB(memory.used_bytes)} / ${formatGiB(memory.total_bytes)}`
      : "Memory unavailable";
  renderMeter("ram", memory.percent, ramDetail, memory.percent != null);

  let gpuDetail = "GPU unavailable";
  if (gpu.available) {
    gpuDetail = [
      gpu.name,
      gpu.memory_used_bytes != null && gpu.memory_total_bytes
        ? `${formatGiB(gpu.memory_used_bytes)} / ${formatGiB(gpu.memory_total_bytes)}`
        : null,
      gpu.temperature_c != null ? `${gpu.temperature_c}°C` : null,
    ]
      .filter(Boolean)
      .join(" · ");
  }
  renderMeter("gpu", gpu.percent, gpuDetail, Boolean(gpu.available));
}

async function refreshMetrics() {
  if (metricsInFlight) return;
  metricsInFlight = true;
  try {
    const response = await fetch("/api/metrics", { cache: "no-store" });
    if (!response.ok) throw new Error("metrics failed");
    renderHostMetrics(await response.json());
  } catch {
    $("#metricsStamp").textContent = "Offline";
    $("#metricsStamp").classList.add("stale");
  } finally {
    metricsInFlight = false;
  }
}

videoInput.addEventListener("change", () => {
  addFiles(videoInput.files);
  videoInput.value = "";
});
["dragenter", "dragover"].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  }),
);
["dragleave", "drop"].forEach((name) =>
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
  }),
);
dropzone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
uploadAllButton.addEventListener("click", uploadAll);
clearQueueButton.addEventListener("click", () => {
  if (!uploading) {
    queuedFiles = [];
    renderQueue();
  }
});
searchInput.addEventListener("input", renderStreams);
document.querySelectorAll(".workspace-tab").forEach((tab) => {
  tab.addEventListener("click", () => showWorkspaceTab(tab.dataset.tab));
});
copyAllUrlsButton.addEventListener("click", async () => {
  const urls = runningStreams().map((stream) => stream.rtsp_url);
  if (!urls.length) {
    notify("There are no running RTSP URLs to copy.", true);
    return;
  }
  if (await copyText(urls.join("\n"))) {
    notify(`Copied ${urls.length} running RTSP URL${urls.length === 1 ? "" : "s"}.`);
  } else {
    notify("Could not copy the RTSP URLs.", true);
  }
});
window.addEventListener("hashchange", () => {
  if (window.location.hash === "#urls") showWorkspaceTab("urls");
});
if (window.location.hash === "#urls") showWorkspaceTab("urls");
$("#startAllButton").addEventListener("click", async () => {
  try {
    await apiAction("/api/streams/actions/start-all");
    notify("Start requested for all streams.");
    await refreshStreams();
  } catch (error) {
    notify(error.message, true);
  }
});
$("#stopAllButton").addEventListener("click", async () => {
  try {
    await apiAction("/api/streams/actions/stop-all");
    notify("All streams stopped.");
    await refreshStreams();
  } catch (error) {
    notify(error.message, true);
  }
});

renderQueue();
refreshStreams();
refreshMetrics();
setInterval(() => {
  if (!uploading) refreshStreams();
}, 3000);
setInterval(refreshMetrics, 1000);
