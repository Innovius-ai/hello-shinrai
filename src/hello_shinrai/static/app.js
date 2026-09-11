const token = document.querySelector('meta[name="hello-shinrai-session"]').content;
const state = {
  bootstrap: null, traces: [], attachments: [], catalog: [], routes: [], chatController: null,
  connection: { shinrai: "offline", llm: false, azure: false, modelCount: null },
};
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Hello-Shinrai-Session", token);
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.json);
    delete options.json;
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const detail = (await response.json()).detail;
      message = typeof detail === "string" ? detail : detail?.message || message;
    } catch (_) {}
    throw new Error(message);
  }
  return response;
}

async function jsonApi(path, options = {}) { return (await api(path, options)).json(); }

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

function pretty(value) { return JSON.stringify(value, null, 2); }
function setStatus(id, text, kind = "") { const node = $(id); node.textContent = text; node.className = `run-status ${kind}`; }
function toast(message, error = false) { const node = $("toast"); node.textContent = message; node.className = error ? "show error" : "show"; clearTimeout(toast.timer); toast.timer = setTimeout(() => node.className = "", 3600); }

function selectTab(name) {
  document.querySelectorAll(".rail-item").forEach((node) => node.classList.toggle("active", node.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((node) => node.classList.toggle("active", node.id === `panel-${name}`));
  if (name === "explorer") loadCatalog();
}

document.querySelectorAll(".rail-item").forEach((node) => node.addEventListener("click", () => selectTab(node.dataset.tab)));
$("open-settings").addEventListener("click", () => $("settings-dialog").showModal());
$("toggle-debug").addEventListener("click", () => document.body.classList.toggle("debug-collapsed"));

function providerDefaults(provider, protocol = "chat") {
  const values = {
    innovius: { models: "https://api.innovius.ai/v1/models", chat: "https://api.innovius.ai/v1/chat/completions", responses: "https://api.innovius.ai/v1/responses" },
    openai: { models: "https://api.openai.com/v1/models", chat: "https://api.openai.com/v1/chat/completions", responses: "https://api.openai.com/v1/responses" },
    kie: { models: "https://api.kie.ai/api/v1/models", chat: "https://api.kie.ai/MODEL/v1/chat/completions", responses: "https://api.kie.ai/api/v1/responses" },
    custom: { models: "", chat: "", responses: "" },
  }[provider];
  return { models: values.models, inference: values[protocol] };
}

function applyProviderDefaults() {
  const defaults = providerDefaults($("llm-provider").value, $("llm-protocol").value);
  $("llm-models-url").value = defaults.models;
  $("llm-inference-url").value = defaults.inference;
  $("model-note").textContent = $("llm-provider").value === "kie"
    ? "Kie Chat Completions URLs can be model-specific. Replace MODEL using that model's Kie documentation."
    : "Catalogue availability does not verify Responses or vision support.";
  setModelCount(null);
}
$("llm-provider").addEventListener("change", applyProviderDefaults);
$("llm-protocol").addEventListener("change", applyProviderDefaults);

async function bootstrap() {
  try {
    const value = await jsonApi("/api/bootstrap");
    state.bootstrap = value;
    state.attachments = value.attachments || [];
    $("shinrai-url").value = value.shinrai.base_url;
    $("llm-provider").value = value.llm.provider;
    $("llm-models-url").value = value.llm.models_url;
    $("llm-inference-url").value = value.llm.inference_url;
    $("llm-protocol").value = value.llm.protocol;
    $("llm-model").value = value.llm.model;
    $("azure-url").value = value.azure.endpoint;
    $("secure-storage-note").textContent = value.secure_storage
      ? "Secure saving uses your operating system's credential store."
      : "No operating-system credential store is available. Keys remain in memory for this run.";
    showCapabilities(value.capabilities, value.usage);
    state.connection.shinrai = value.capabilities ? "online" : value.shinrai.has_key ? "pending" : "offline";
    state.connection.llm = Boolean(value.llm.has_key && value.llm.model);
    state.connection.azure = Boolean(value.azure.has_key && value.azure.endpoint);
    state.connection.modelCount = Number.isInteger(value.llm.model_count) ? value.llm.model_count : null;
    const modelCount = $("model-count");
    modelCount.textContent = Number.isInteger(value.llm.model_count) ? `${value.llm.model_count} models detected` : "Models not loaded";
    modelCount.className = `model-count ${Number.isInteger(value.llm.model_count) ? "loaded" : ""}`;
    renderConnectionStatus();
    renderAttachments();
    await refreshTraces();
  } catch (error) { toast(error.message, true); }
}

function setReadiness(id, text, kind = "") {
  const node = $(id);
  node.textContent = text;
  node.className = `readiness ${kind}`;
}

function setModelCount(count, failed = false) {
  state.connection.modelCount = Number.isInteger(count) ? count : null;
  const node = $("model-count");
  node.textContent = failed ? "Discovery failed" : Number.isInteger(count) ? `${count} model${count === 1 ? "" : "s"} detected` : "Models not loaded";
  node.className = `model-count ${failed ? "error" : Number.isInteger(count) ? "loaded" : ""}`;
  renderConnectionStatus();
}

function renderConnectionStatus() {
  const shinraiChip = $("shinrai-connection-chip");
  shinraiChip.className = `connection-chip ${state.connection.shinrai}`;
  $("shinrai-connection-label").textContent = state.connection.shinrai === "online"
    ? "ShinrAI · connected" : state.connection.shinrai === "pending" ? "ShinrAI · verify key" : "ShinrAI · not connected";
  setReadiness("shinrai-card-status", state.connection.shinrai === "online" ? "Connected" : state.connection.shinrai === "pending" ? "Not yet verified" : "Not connected", state.connection.shinrai);

  const llmChip = $("llm-connection-chip");
  llmChip.className = `connection-chip ${state.connection.llm ? "online" : ""}`;
  const count = state.connection.modelCount;
  $("llm-connection-label").textContent = state.connection.llm
    ? `LLM · ${Number.isInteger(count) ? `${count} models` : "ready"}` : "LLM · not configured";
  setReadiness("llm-card-status", state.connection.llm ? (Number.isInteger(count) ? `Ready · ${count} models` : "Ready") : "Not configured", state.connection.llm ? "online" : "");
  setReadiness("azure-card-status", state.connection.azure ? "Configured" : "Not configured", state.connection.azure ? "online" : "");
}

function showCapabilities(capabilities, usage) {
  const card = $("capability-card");
  if (!capabilities) { card.textContent = "Connect to verify models, balance, and tier access."; return; }
  const tiers = Object.entries(capabilities.tiers || {}).map(([name, row]) => `<span class="tier ${row.allowed ? "allowed" : "denied"}">${escapeHtml(name === "realtime" ? "Real-time · fast" : name)} · ${row.allowed ? "available" : "not entitled"}</span>`).join("");
  const models = (capabilities.models || []).map((model) => `${escapeHtml(model.id)}${model.status ? ` · ${escapeHtml(model.status)}` : ""}`).join("<br>");
  const balance = usage?.balances?.available_records;
  card.innerHTML = `<strong>${balance ?? "Unknown"} records available</strong><div class="tier-list">${tiers}</div><p>${models}</p>`;
}

$("shinrai-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter; button.disabled = true; button.textContent = "Connecting…";
  try {
    const value = await jsonApi("/api/settings/shinrai", { method: "POST", json: { base_url: $("shinrai-url").value, api_key: $("shinrai-key").value, remember: $("remember-shinrai").checked } });
    state.bootstrap.capabilities = value.capabilities; state.bootstrap.usage = value.usage;
    showCapabilities(value.capabilities, value.usage); state.connection.shinrai = "online"; renderConnectionStatus(); $("shinrai-key").value = "";
    toast(value.securely_saved ? "Connected and saved securely." : "ShinrAI connected for this run.");
    await refreshTraces(); await loadCatalog();
  } catch (error) { state.connection.shinrai = "offline"; renderConnectionStatus(); toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "Connect ShinrAI"; }
});

$("llm-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const value = await jsonApi("/api/settings/llm", { method: "POST", json: {
      provider: $("llm-provider").value, models_url: $("llm-models-url").value,
      inference_url: $("llm-inference-url").value, protocol: $("llm-protocol").value,
      api_key: $("llm-key").value, model: $("llm-model").value, remember: $("remember-llm").checked,
    }});
    state.connection.llm = Boolean(value.has_key && value.model); renderConnectionStatus();
    $("llm-key").value = ""; toast(value.securely_saved ? "LLM settings saved securely." : "LLM settings saved for this run.");
  } catch (error) { toast(error.message, true); }
});

$("azure-settings").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const value = await jsonApi("/api/settings/azure", { method: "POST", json: { endpoint: $("azure-url").value, api_key: $("azure-key").value, remember: $("remember-azure").checked } });
    state.connection.azure = Boolean(value.has_key && value.endpoint); renderConnectionStatus();
    $("azure-key").value = ""; toast(value.securely_saved ? "Azure reference saved securely." : "Azure reference saved for this run.");
  } catch (error) { toast(error.message, true); }
});

$("forget-secrets").addEventListener("click", async () => {
  await jsonApi("/api/settings/secrets", { method: "DELETE" });
  $("shinrai-key").value = $("llm-key").value = $("azure-key").value = "";
  state.connection = { shinrai: "offline", llm: false, azure: false, modelCount: null };
  setModelCount(null); renderConnectionStatus(); toast("Saved and in-memory keys were forgotten.");
});

$("load-models").addEventListener("click", async () => {
  const button = $("load-models"); button.disabled = true; button.textContent = "Loading…";
  try {
    const value = await jsonApi("/api/models/discover", { method: "POST", json: { models_url: $("llm-models-url").value, api_key: $("llm-key").value } });
    $("model-options").replaceChildren(...value.models.map((model) => { const option = document.createElement("option"); option.value = model.id; return option; }));
    setModelCount(value.models.length);
    toast(`Loaded ${value.models.length} models. Choose one or keep a manual model ID.`); await refreshTraces();
  } catch (error) { setModelCount(null, true); toast(`${error.message} Manual model entry is still available.`, true); }
  finally { button.disabled = false; button.textContent = "Load models"; }
});

$("text-threshold").addEventListener("input", () => $("threshold-value").textContent = Number($("text-threshold").value).toFixed(2));
$("use-simple-example").addEventListener("click", () => $("text-input").value = "Please send the contract to Ada Lovelace at ada@example.org. Her office is at 12 Analytical Engine Way, London.");
function highlightFindings(text, entities = []) {
  const characters = [...text];
  const usable = entities.filter((item) => Number.isInteger(item.offset) && Number.isInteger(item.length) && item.offset >= 0 && item.length > 0 && item.offset + item.length <= characters.length).sort((a, b) => a.offset - b.offset);
  let cursor = 0; const output = [];
  for (const item of usable) {
    if (item.offset < cursor) continue;
    output.push(escapeHtml(characters.slice(cursor, item.offset).join("")));
    const label = item.category || item.type || item.label || "finding";
    const score = typeof item.confidence_score === "number" ? ` · ${item.confidence_score.toFixed(2)}` : typeof item.score === "number" ? ` · ${item.score.toFixed(2)}` : "";
    output.push(`<mark title="${escapeHtml(label + score)}">${escapeHtml(characters.slice(item.offset, item.offset + item.length).join(""))}<small>${escapeHtml(label)}</small></mark>`);
    cursor = item.offset + item.length;
  }
  output.push(escapeHtml(characters.slice(cursor).join("")));
  return output.join("");
}
$("text-form").addEventListener("submit", async (event) => {
  event.preventDefault(); setStatus("text-status", "Running…");
  try {
    const value = await jsonApi("/api/text", { method: "POST", json: {
      operation: $("text-operation").value, text: $("text-input").value, mode: $("text-mode").value,
      model: $("text-model").value, tier: $("text-tier").value, threshold: Number($("text-threshold").value),
    }});
    $("text-result").classList.remove("hidden");
    const result = value.result;
    const output = result.text || result.results?.map((row) => row.text).join("\n\n") || "Detection complete — see findings.";
    const originals = $("text-operation").value === "batch" ? $("text-input").value.split("\n---\n").map((part) => part.trim()) : [$("text-input").value];
    const rows = result.results || [result];
    $("text-original").innerHTML = originals.map((text, index) => highlightFindings(text, rows[index]?.entities || [])).join("<hr>");
    $("text-output").textContent = output; $("text-json").textContent = pretty(result);
    setStatus("text-status", `${value.trace.elapsed_ms} ms`, "success"); await refreshTraces();
  } catch (error) { setStatus("text-status", error.message, "error"); await refreshTraces(); }
});

const dropzone = $("dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.add("drag"); }));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("drag"); }));
dropzone.addEventListener("drop", (event) => { if (event.dataTransfer.files.length) $("file-input").files = event.dataTransfer.files; });
$("file-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const file = $("file-input").files[0]; if (!file) return;
  const data = new FormData(); data.append("file", file); data.append("mode", $("file-mode").value);
  setStatus("file-status", "Uploading and protecting…");
  try {
    const value = await jsonApi("/api/files", { method: "POST", body: data });
    state.attachments.push(value.attachment); renderAttachments(); setStatus("file-status", "Protected and cleaned up remotely", "success"); await refreshTraces();
  } catch (error) { setStatus("file-status", error.message, "error"); await refreshTraces(); }
});

async function renderAttachments() {
  const list = $("attachment-list"); list.replaceChildren();
  for (const item of state.attachments) {
    const card = document.createElement("article"); card.className = "surface attachment-card";
    card.innerHTML = `<div class="attachment-head"><div><h3>${escapeHtml(item.name)}</h3><div class="attachment-meta">${escapeHtml(item.media_type)} · ${item.pages} preview page(s) · ${escapeHtml(item.cleanup)}</div></div><button class="button danger remove-attachment" data-id="${item.id}" type="button">Remove local copy</button></div><details><summary>Protected text</summary><pre>${escapeHtml(item.protected_text)}</pre></details><div class="preview-strip"></div><div class="actions"><button class="button quiet download-attachment" data-id="${item.id}" data-kind="text">Text</button><button class="button quiet download-attachment" data-id="${item.id}" data-kind="pdf">PDF</button>${item.has_mapping ? `<button class="button quiet download-attachment" data-id="${item.id}" data-kind="mapping">Private map</button>` : ""}</div>`;
    list.appendChild(card);
    const strip = card.querySelector(".preview-strip");
    for (let page = 1; page <= item.pages; page++) {
      try { const response = await api(`/api/attachments/${item.id}/preview/${page}`); const img = document.createElement("img"); img.alt = `Protected page ${page}`; img.src = URL.createObjectURL(await response.blob()); strip.appendChild(img); } catch (_) {}
    }
  }
  renderChatAttachments();
}

$("attachment-list").addEventListener("click", async (event) => {
  const remove = event.target.closest(".remove-attachment");
  if (remove) { await jsonApi(`/api/attachments/${remove.dataset.id}`, { method: "DELETE" }); state.attachments = state.attachments.filter((item) => item.id !== remove.dataset.id); renderAttachments(); return; }
  const download = event.target.closest(".download-attachment");
  if (download) downloadResponse(await api(`/api/attachments/${download.dataset.id}/download/${download.dataset.kind}`));
});

function renderChatAttachments() {
  const area = $("chat-attachments"); area.replaceChildren(...state.attachments.map((item) => {
    const label = document.createElement("label"); label.innerHTML = `<input type="checkbox" value="${item.id}"> ${escapeHtml(item.name)}`; return label;
  }));
}

$("chat-visuals").addEventListener("change", () => $("vision-confirm-wrap").classList.toggle("hidden", !$("chat-visuals").checked));
$("clear-chat").addEventListener("click", async () => { await jsonApi("/api/chat", { method: "DELETE" }); $("chat-messages").innerHTML = '<div class="empty-state">New local conversation started.</div>'; toast("Conversation and replacement map cleared."); });
$("stop-chat").addEventListener("click", () => state.chatController?.abort());

function addMessage(role, text = "") {
  $("chat-messages").querySelector(".empty-state")?.remove();
  const node = document.createElement("div"); node.className = `message ${role}`; node.innerHTML = `<small>${role === "user" ? "You" : "Restored locally"}</small><span></span>`; node.querySelector("span").textContent = text; $("chat-messages").appendChild(node); $("chat-messages").scrollTop = $("chat-messages").scrollHeight; return node.querySelector("span");
}

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const text = $("chat-input").value; const selected = [...$("chat-attachments").querySelectorAll("input:checked")].map((node) => node.value);
  addMessage("user", text); const assistant = addMessage("assistant", "");
  state.chatController = new AbortController(); $("send-chat").classList.add("hidden"); $("stop-chat").classList.remove("hidden"); setStatus("chat-status", "Protecting…");
  try {
    const response = await api("/api/chat", { method: "POST", signal: state.chatController.signal, json: {
      text, attachment_ids: selected, include_visuals: $("chat-visuals").checked, vision_confirmed: $("vision-confirm").checked,
      stream: $("chat-stream").checked, tier: $("chat-tier").value,
    }});
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "", restored = "";
    while (true) {
      const {done, value} = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      const lines = buffer.split("\n"); buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue; const item = JSON.parse(line);
        if (item.type === "protected") setStatus("chat-status", "Calling model…");
        if (item.type === "delta") { restored += item.restored || ""; assistant.textContent = restored; }
        if (item.type === "complete") setStatus("chat-status", `${item.total_ms} ms${item.first_token_ms ? ` · first token ${item.first_token_ms} ms` : ""}`, "success");
        if (item.type === "error") throw new Error(item.message);
      }
      if (done) break;
    }
    $("chat-input").value = ""; await refreshTraces();
  } catch (error) {
    if (error.name === "AbortError") setStatus("chat-status", "Stopped; upstream connection closed", "error");
    else setStatus("chat-status", error.message, "error");
    if (!assistant.textContent) assistant.textContent = "No reply was returned."; await refreshTraces();
  } finally { state.chatController = null; $("send-chat").classList.remove("hidden"); $("stop-chat").classList.add("hidden"); }
});

const azureExamples = {
  "/language/:analyze-text": { kind: "PiiEntityRecognition", analysisInput: { documents: [{ id: "1", language: "en", text: "Email Ada at ada@example.org" }] }, parameters: { modelVersion: "latest" } },
  "/text/analytics/v3.1/entities/recognition/pii": { documents: [{ id: "1", language: "en", text: "Email Ada at ada@example.org" }] },
};
function setAzureExample() { $("azure-body").value = pretty(azureExamples[$("azure-path").value]); }
$("azure-path").addEventListener("change", setAzureExample); setAzureExample();
$("azure-form").addEventListener("submit", async (event) => {
  event.preventDefault(); setStatus("azure-status", "Running…");
  try {
    const value = await jsonApi("/api/azure/compare", { method: "POST", json: { path: $("azure-path").value, body: JSON.parse($("azure-body").value), include_real_azure: $("azure-real").checked } });
    $("azure-results").innerHTML = value.results.map((row) => `<article class="surface comparison-card"><h3>${escapeHtml(row.label)}</h3><div class="timing">${row.elapsed_ms} ms · HTTP ${row.status}</div><pre>${escapeHtml(pretty(row.body ?? row.error))}</pre></article>`).join("");
    setStatus("azure-status", "Comparison complete", "success"); await refreshTraces();
  } catch (error) { setStatus("azure-status", error.message, "error"); await refreshTraces(); }
});

async function loadCatalog() {
  try {
    const value = await jsonApi("/api/explorer/catalog"); state.catalog = value.operations; state.routes = value.discovered_routes;
    const groups = new Map(); value.operations.forEach((item) => { if (!groups.has(item.group)) groups.set(item.group, []); groups.get(item.group).push(item); });
    const select = $("explorer-operation"); select.replaceChildren();
    for (const [name, items] of groups) { const group = document.createElement("optgroup"); group.label = name; for (const item of items) { const option = document.createElement("option"); option.value = item.id; option.textContent = `${item.name} · ${item.availability}`; group.appendChild(option); } select.appendChild(group); }
    if (state.routes.length) { const group = document.createElement("optgroup"); group.label = "Deployment OpenAPI"; const option = document.createElement("option"); option.value = "discovered"; option.textContent = "Use a safely discovered route"; group.appendChild(option); select.appendChild(group); }
    updateOperation();
  } catch (_) {}
}

function updateOperation() {
  const id = $("explorer-operation").value; const discovered = id === "discovered"; $("discovered-fields").classList.toggle("hidden", !discovered);
  if (discovered) {
    const pathSelect = $("explorer-path"); pathSelect.replaceChildren(...state.routes.map((route) => { const option = document.createElement("option"); option.value = route.path; option.dataset.method = route.method; option.textContent = `${route.method} ${route.path}`; return option; }));
    syncDiscoveredMethod(); $("explorer-request-path").value = $("explorer-path").value; $("explorer-body").value = "{}"; $("explorer-query").value = "{}"; $("operation-meta").innerHTML = '<span class="meta-chip good">verified by deployment schema</span>';
    return;
  }
  const item = state.catalog.find((row) => row.id === id); if (!item) return;
  $("explorer-request-path").value = item.path; $("explorer-query").value = pretty(item.query || {}); $("explorer-body").value = item.body === null ? "" : pretty(item.body);
  const availability = item.availability === "available" ? "good" : "bad";
  $("operation-meta").innerHTML = `<span class="meta-chip">${item.method} ${escapeHtml(item.path)}</span><span class="meta-chip ${availability}">${escapeHtml(item.availability)}</span><span class="meta-chip">${escapeHtml(item.auth)} auth</span>${item.requires ? `<span class="meta-chip bad">${escapeHtml(item.requires)}</span>` : ""}`;
}
function syncDiscoveredMethod() { const option = $("explorer-path").selectedOptions[0]; if (option) { $("explorer-method").value = option.dataset.method; $("explorer-request-path").value = option.value; } }
$("explorer-operation").addEventListener("change", updateOperation); $("explorer-path").addEventListener("change", syncDiscoveredMethod);
function explorerPayload(download = false) {
  const discovered = $("explorer-operation").value === "discovered";
  return {
    operation_id: $("explorer-operation").value, method: discovered ? $("explorer-method").value : null,
    path: $("explorer-request-path").value, query: JSON.parse($("explorer-query").value || "{}"),
    body: $("explorer-body").value.trim() ? JSON.parse($("explorer-body").value) : null, download,
  };
}
$("explorer-form").addEventListener("submit", async (event) => {
  event.preventDefault(); setStatus("explorer-status", "Sending once…");
  try {
    const value = await jsonApi("/api/explorer/run", { method: "POST", json: explorerPayload() });
    $("explorer-result").classList.remove("hidden"); $("explorer-json").textContent = pretty({headers: value.headers, body: value.result}); setStatus("explorer-status", `${value.trace.elapsed_ms} ms`, "success"); await refreshTraces();
  } catch (error) { setStatus("explorer-status", error.message, "error"); await refreshTraces(); }
});
$("explorer-download").addEventListener("click", async () => {
  setStatus("explorer-status", "Downloading…");
  try {
    const response = await api("/api/explorer/run", { method: "POST", json: explorerPayload(true) });
    downloadResponse(response); setStatus("explorer-status", "Downloaded", "success"); await refreshTraces();
  } catch (error) { setStatus("explorer-status", error.message, "error"); await refreshTraces(); }
});

async function refreshTraces() {
  try { state.traces = (await jsonApi("/api/traces")).traces; renderTraces(); } catch (_) {}
}
function renderTraces() {
  $("trace-count").textContent = `${state.traces.length} run${state.traces.length === 1 ? "" : "s"}`;
  $("last-time").textContent = state.traces[0]?.elapsed_ms != null ? `${state.traces[0].elapsed_ms} ms last run` : "No timing yet";
  const list = $("trace-list"); if (!state.traces.length) { list.innerHTML = '<div class="empty-state">Your requests will appear here with each stage and timing.</div>'; return; }
  list.innerHTML = state.traces.map((trace, index) => `<details class="trace ${escapeHtml(trace.status)}" ${index === 0 ? "open" : ""}><summary><span class="trace-title">${escapeHtml(trace.operation)} · ${escapeHtml(trace.status)}</span><span class="trace-time">${trace.elapsed_ms ?? "—"} ms</span></summary><pre>${escapeHtml(pretty(trace))}</pre></details>`).join("");
}
$("clear-traces").addEventListener("click", async () => { await jsonApi("/api/traces", { method: "DELETE" }); state.traces = []; renderTraces(); });
$("export-safe").addEventListener("click", async () => downloadResponse(await api("/api/traces/export")));
$("export-full").addEventListener("click", async () => { if (window.confirm("This export includes original content and private replacement maps. Save it only in a trusted location.")) downloadResponse(await api("/api/traces/export?include_sensitive=true")); });

async function downloadResponse(response) {
  const disposition = response.headers.get("content-disposition") || ""; const match = /filename=([^;]+)/.exec(disposition); const name = match ? match[1] : "download";
  const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

bootstrap();

// WebMCP is optional and feature-detected. These tools stage or read the same
// state as the visible interface; they never hide a billable API call.
if (document.modelContext?.registerTool) {
  const tools = [
    {
      name: "read_shinrai_connection",
      title: "Read ShinrAI connection",
      description: "Read whether this local session is connected and which tiers the key may use.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: { readOnlyHint: true, untrustedContentHint: false },
      execute: async () => { const value = await jsonApi("/api/bootstrap"); return { connected: Boolean(value.capabilities), capabilities: value.capabilities }; },
    },
    {
      name: "stage_shinrai_text",
      title: "Stage text protection",
      description: "Open the Text tool and stage text for review before a billable ShinrAI request.",
      inputSchema: { type: "object", properties: { text: { type: "string", minLength: 1 }, operation: { type: "string", enum: ["redact", "analyze", "batch"] } }, required: ["text"], additionalProperties: false },
      annotations: { readOnlyHint: false, untrustedContentHint: true },
      execute: async (input) => { if (!input || typeof input.text !== "string" || !input.text) throw new Error("text is required"); selectTab("text"); $("text-input").value = input.text; if (["redact", "analyze", "batch"].includes(input.operation)) $("text-operation").value = input.operation; return { staged: true, operation: $("text-operation").value }; },
    },
  ];
  for (const tool of tools) Promise.resolve(document.modelContext.registerTool(tool)).catch(() => {});
}
