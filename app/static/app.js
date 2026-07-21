const themeToggleBtn = document.querySelector("#themeToggleBtn");
const environmentEl = document.querySelector("#environment");
const clientEl = document.querySelector("#client");
const fileTypesEl = document.querySelector("#fileTypes");
const rowsEl = document.querySelector("#rows");
const statusEl = document.querySelector("#status");
const spinnerEl = document.querySelector("#spinner");
const resultCountEl = document.querySelector("#resultCount");
const searchBtn = document.querySelector("#searchBtn");
const downloadBtn = document.querySelector("#downloadBtn");
const selectAllBtn = document.querySelector("#selectAllBtn");
const clearFilesBtn = document.querySelector("#clearFilesBtn");
const toggleRows = document.querySelector("#toggleRows");
const progressBarEl = document.querySelector("#progressBar");
const progressFillEl = document.querySelector("#progressFill");
const progressCountEl = document.querySelector("#progressCount");
const paginationEl = document.querySelector("#pagination");
const pageInfoEl = document.querySelector("#pageInfo");
const prevPageBtn = document.querySelector("#prevPage");
const nextPageBtn = document.querySelector("#nextPage");
const pageSizeEl = document.querySelector("#pageSize");

let lastRows = [];
let currentPage = 1;
let pageSize = Number(pageSizeEl.value) || 25;
// Selection is tracked here (not in the DOM) so it survives paging: only the
// current page's checkboxes exist at any time.
const selectedIds = new Set();

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setBusy(isBusy) {
  spinnerEl.hidden = !isBusy;
}

function showProgress(fraction, label) {
  progressBarEl.hidden = false;
  const pct = Math.max(0, Math.min(100, Math.round(fraction * 100)));
  progressFillEl.style.width = `${pct}%`;
  progressCountEl.textContent = label;
}

function hideProgress() {
  progressBarEl.hidden = true;
  progressFillEl.style.width = "0%";
  progressCountEl.textContent = "";
}

function splitValues(value) {
  return value
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function selectedFiles() {
  return [...document.querySelectorAll("input[name='fileType']:checked")].map((item) => item.value);
}

function selectedRowIds() {
  return Array.from(selectedIds);
}

function payload() {
  return {
    environment: environmentEl.value,
    client: clientEl.value,
    account_numbers: splitValues(document.querySelector("#account_numbers").value),
    encounter_ids: splitValues(document.querySelector("#encounter_ids").value),
    selected_files: selectedFiles(),
  };
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  statusEl.style.color = isError ? "#b42318" : "#5b6472";
}

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }

  const text = await response.text();
  return { detail: text || response.statusText || "Request failed." };
}

async function loadClients() {
  clientEl.innerHTML = '<option value="">Select a client...</option>';
  downloadBtn.disabled = true;
  renderRows([]);

  const response = await fetch(`/api/clients?environment=${encodeURIComponent(environmentEl.value)}`);
  const clientCodes = await readResponse(response);
  if (!response.ok) {
    throw new Error(clientCodes.detail || "Could not load clients.");
  }

  for (const code of clientCodes) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = code;
    clientEl.appendChild(option);
  }
}

async function loadFileTypes() {
  const response = await fetch("/api/file-types");
  const fileTypes = await readResponse(response);
  if (!response.ok) {
    throw new Error(fileTypes.detail || "Could not load file types.");
  }
  fileTypesEl.innerHTML = "";

  for (const fileType of fileTypes) {
    const label = document.createElement("label");
    label.className = "file-option";
    label.innerHTML = `
      <span>${fileType.label}</span>
      <input type="checkbox" name="fileType" value="${fileType.value}" />
    `;
    fileTypesEl.appendChild(label);
  }
}

function updateToggleState() {
  const total = lastRows.length;
  const selected = selectedIds.size;
  toggleRows.checked = total > 0 && selected === total;
  toggleRows.indeterminate = selected > 0 && selected < total;
}

function renderPage() {
  rowsEl.innerHTML = "";
  const total = lastRows.length;

  if (!total) {
    rowsEl.innerHTML = '<tr><td class="empty-row" colspan="8">No matching rows found.</td></tr>';
    paginationEl.hidden = true;
    updateToggleState();
    return;
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  currentPage = Math.min(Math.max(1, currentPage), totalPages);
  const startIdx = (currentPage - 1) * pageSize;
  const pageRows = lastRows.slice(startIdx, startIdx + pageSize);

  for (const row of pageRows) {
    const tr = document.createElement("tr");
    if (row.is_multidoc) {
      tr.classList.add("multidoc");
    }

    const breakdown = (row.file_breakdown || [])
      .filter((item) => item.count > 0)
      .map((item) => `${item.count} ${item.label}`)
      .join(", ");
    const totalFiles = row.download_file_count ?? 0;
    const filesCell = breakdown
      ? `${breakdown} <span class="file-total">(${totalFiles} total)</span>`
      : "—";
    const multiDoc = row.is_multidoc
      ? `<span class="badge badge-yes">Yes · ${row.document_count} docs</span>`
      : '<span class="badge">No</span>';
    const checked = selectedIds.has(String(row.id)) ? "checked" : "";

    tr.innerHTML = `
      <td><input type="checkbox" name="resultRow" value="${row.id}" ${checked} /></td>
      <td>${row.account_number ?? ""}</td>
      <td>${row.encounter_id ?? ""}</td>
      <td>${row.client_id ?? ""}</td>
      <td>${row.facility_id ?? ""}</td>
      <td>${row.service_date ?? ""}</td>
      <td>${multiDoc}</td>
      <td class="files-cell">${filesCell}</td>
    `;
    rowsEl.appendChild(tr);
  }

  paginationEl.hidden = false;
  const endIdx = startIdx + pageRows.length;
  pageInfoEl.textContent = `${startIdx + 1}–${endIdx} of ${total} · Page ${currentPage} of ${totalPages}`;
  prevPageBtn.disabled = currentPage <= 1;
  nextPageBtn.disabled = currentPage >= totalPages;
  updateToggleState();
}

function renderRows(rows) {
  lastRows = rows;
  currentPage = 1;
  selectedIds.clear();
  for (const row of rows) {
    selectedIds.add(String(row.id));
  }
  downloadBtn.disabled = rows.length === 0;
  resultCountEl.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"}`;
  renderPage();
}

async function search() {
  if (!clientEl.value) {
    setStatus("Select a client first.", true);
    return;
  }

  setStatus("Searching…");
  setBusy(true);
  hideProgress();
  searchBtn.disabled = true;
  downloadBtn.disabled = true;

  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });

    const data = await readResponse(response);
    if (!response.ok) {
      throw new Error(data.detail || "Search failed.");
    }

    renderRows(data.rows);
    const encounters = data.count ?? data.rows.length;
    const docs = data.document_count ?? encounters;
    if (data.rows.length) {
      resultCountEl.textContent =
        `${encounters} encounter${encounters === 1 ? "" : "s"} · ${docs} document${docs === 1 ? "" : "s"}`;
    }
    setStatus(`Search complete — ${encounters} encounter(s), ${docs} document(s).`);
  } catch (error) {
    renderRows([]);
    setStatus(error.message, true);
  } finally {
    setBusy(false);
    searchBtn.disabled = false;
  }
}

async function downloadZip() {
  if (!clientEl.value) {
    setStatus("Select a client first.", true);
    return;
  }

  const body = payload();
  body.result_ids = selectedRowIds();
  body.folder_structure = document.querySelector("#folder_structure").value;

  if (!body.result_ids.length) {
    setStatus("Select at least one result row.", true);
    return;
  }

  setStatus("Preparing download…");
  setBusy(true);
  showProgress(0, "Preparing…");
  downloadBtn.disabled = true;

  try {
    const startRes = await fetch("/api/download/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const start = await readResponse(startRes);
    if (!startRes.ok) {
      throw new Error(start.detail || "Download failed.");
    }

    const jobId = start.job_id;
    let encountersTotal = start.encounters_total || 0;
    showProgress(0, `0 / ${encountersTotal} encounters`);

    let job;
    while (true) {
      const progRes = await fetch(`/api/download/progress/${jobId}`);
      job = await readResponse(progRes);
      if (!progRes.ok) {
        throw new Error(job.detail || "Lost track of the download.");
      }
      encountersTotal = job.encounters_total || encountersTotal;
      const fraction = job.files_total > 0 ? job.files_done / job.files_total : 0;
      const label = `${job.encounters_done} / ${encountersTotal} encounters`;
      showProgress(fraction, label);
      setStatus(`Downloading ${label}…`);
      if (job.status === "done") break;
      if (job.status === "error") throw new Error(job.error || "Download failed.");
      await sleep(250);
    }

    setStatus("Finalizing ZIP…");
    const fileRes = await fetch(`/api/download/file/${jobId}`);
    if (!fileRes.ok) {
      const data = await readResponse(fileRes);
      throw new Error(data.detail || "Download failed.");
    }

    const blob = await fileRes.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "s3_document_downloads.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    setStatus(
      `Done — ${job.encounters_total} encounter(s), ${job.file_count} file(s) downloaded, ${job.skipped_count} skipped.`
    );
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setBusy(false);
    hideProgress();
    downloadBtn.disabled = lastRows.length === 0;
  }
}

function isDarkTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "dark") return true;
  if (explicit === "light") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function updateThemeIcon() {
  themeToggleBtn.textContent = isDarkTheme() ? "☀️" : "🌙";
}

function setTheme(theme) {
  if (theme === "dark" || theme === "light") {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
    localStorage.removeItem("theme");
  }
  updateThemeIcon();
}

themeToggleBtn.addEventListener("click", () => {
  setTheme(isDarkTheme() ? "light" : "dark");
});

updateThemeIcon();

selectAllBtn.addEventListener("click", () => {
  document.querySelectorAll("input[name='fileType']").forEach((item) => {
    item.checked = true;
  });
});

clearFilesBtn.addEventListener("click", () => {
  document.querySelectorAll("input[name='fileType']").forEach((item) => {
    item.checked = false;
  });
});

toggleRows.addEventListener("change", () => {
  selectedIds.clear();
  if (toggleRows.checked) {
    for (const row of lastRows) {
      selectedIds.add(String(row.id));
    }
  }
  renderPage();
});

rowsEl.addEventListener("change", (event) => {
  const checkbox = event.target;
  if (!checkbox || checkbox.name !== "resultRow") return;
  if (checkbox.checked) {
    selectedIds.add(checkbox.value);
  } else {
    selectedIds.delete(checkbox.value);
  }
  updateToggleState();
});

prevPageBtn.addEventListener("click", () => {
  if (currentPage > 1) {
    currentPage -= 1;
    renderPage();
  }
});

nextPageBtn.addEventListener("click", () => {
  const totalPages = Math.max(1, Math.ceil(lastRows.length / pageSize));
  if (currentPage < totalPages) {
    currentPage += 1;
    renderPage();
  }
});

pageSizeEl.addEventListener("change", () => {
  pageSize = Number(pageSizeEl.value) || 25;
  currentPage = 1;
  renderPage();
});

searchBtn.addEventListener("click", search);
downloadBtn.addEventListener("click", downloadZip);
environmentEl.addEventListener("change", () => {
  loadClients().catch((error) => setStatus(error.message, true));
});

loadFileTypes().catch((error) => setStatus(error.message, true));
loadClients().catch((error) => setStatus(error.message, true));
