const themeToggleBtn = document.querySelector("#themeToggleBtn");
const environmentEl = document.querySelector("#environment");
const clientEl = document.querySelector("#client");
const fileTypesEl = document.querySelector("#fileTypes");
const rowsEl = document.querySelector("#rows");
const statusEl = document.querySelector("#status");
const resultCountEl = document.querySelector("#resultCount");
const searchBtn = document.querySelector("#searchBtn");
const downloadBtn = document.querySelector("#downloadBtn");
const selectAllBtn = document.querySelector("#selectAllBtn");
const clearFilesBtn = document.querySelector("#clearFilesBtn");
const toggleRows = document.querySelector("#toggleRows");

let lastRows = [];

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
  return [...document.querySelectorAll("input[name='resultRow']:checked")].map((item) => item.value);
}

function payload() {
  return {
    environment: environmentEl.value,
    client: clientEl.value,
    account_numbers: splitValues(document.querySelector("#account_numbers").value),
    encounter_ids: splitValues(document.querySelector("#encounter_ids").value),
    selected_files: selectedFiles(),
    limit: Number(document.querySelector("#limit").value || 100),
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

function renderRows(rows) {
  lastRows = rows;
  rowsEl.innerHTML = "";
  toggleRows.checked = rows.length > 0;
  downloadBtn.disabled = rows.length === 0;
  resultCountEl.textContent = `${rows.length} result${rows.length === 1 ? "" : "s"}`;

  if (!rows.length) {
    rowsEl.innerHTML = '<tr><td class="empty-row" colspan="7">No matching rows found.</td></tr>';
    return;
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" name="resultRow" value="${row.id}" checked /></td>
      <td>${row.account_number ?? ""}</td>
      <td>${row.encounter_id ?? ""}</td>
      <td>${row.client_id ?? ""}</td>
      <td>${row.facility_id ?? ""}</td>
      <td>${row.service_date ?? ""}</td>
      <td>${row.available_file_count ?? 0}</td>
    `;
    rowsEl.appendChild(tr);
  }
}

async function search() {
  if (!clientEl.value) {
    setStatus("Select a client first.", true);
    return;
  }

  setStatus("Searching...");
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
    setStatus("Search complete.");
  } catch (error) {
    renderRows([]);
    setStatus(error.message, true);
  } finally {
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

  setStatus("Preparing ZIP...");
  downloadBtn.disabled = true;

  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      const data = await readResponse(response);
      throw new Error(data.detail || "Download failed.");
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "s3_document_downloads.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);

    const downloaded = response.headers.get("X-Downloaded-Files") || "0";
    const skipped = response.headers.get("X-Skipped-Files") || "0";
    setStatus(`ZIP ready. Downloaded ${downloaded} file(s), skipped ${skipped}.`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
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
  document.querySelectorAll("input[name='resultRow']").forEach((item) => {
    item.checked = toggleRows.checked;
  });
});

searchBtn.addEventListener("click", search);
downloadBtn.addEventListener("click", downloadZip);
environmentEl.addEventListener("change", () => {
  loadClients().catch((error) => setStatus(error.message, true));
});

loadFileTypes().catch((error) => setStatus(error.message, true));
loadClients().catch((error) => setStatus(error.message, true));
