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
const modeTabsEl = document.querySelector("#modeTabs");
const modeHintEl = document.querySelector("#modeHint");
const commonFiltersEl = document.querySelector("#commonFilters");
const directFiltersEl = document.querySelector("#directFilters");
const clientCodesEl = document.querySelector("#client_codes");
const facilityCodesEl = document.querySelector("#facility_codes");
const downloadCountEl = document.querySelector("#downloadCount");
const accountNumbersEl = document.querySelector("#account_numbers");
const encounterIdsEl = document.querySelector("#encounter_ids");

// One scannable line per tab. The full rules -- how the filters combine, what an
// empty result means, why a wide search is unreliable -- live in the README
// rather than on screen above every search.
const MODE_HINTS = {
  common: "Searches every client in commonDb, then reads the file paths from the selected environment.",
  direct:
    "Searches one client's database directly, by account number or encounter ID — one or the other, not both.",
};

// "common" searches commonDb metadata; "direct" is the original per-client lookup.
// Direct is the default because it only needs the client webdb -- commonDb is a
// separate shared server that is not always up, and nothing here contacts it
// until the metadata tab is actually opened.
let searchMode = "direct";

// commonDb dropdown state: loaded lazily, once, on the first metadata tab click.
// A failure leaves `metadataLoaded` false so clicking the tab again retries.
let metadataLoaded = false;
let metadataLoading = false;

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

// Used only for the two identifier lists, so a token has to contain a digit to
// be one. Without that check, stripping letters out of pasted junk can leave a
// bare "--" behind, which would go to the API as an id and quietly return no
// rows instead of being ignored.
function splitValues(value) {
  return value
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter((item) => /[0-9]/.test(item));
}

function selectedFiles() {
  return [...document.querySelectorAll("input[name='fileType']:checked")].map((item) => item.value);
}

function selectedRowIds() {
  return Array.from(selectedIds);
}

// An empty selection used to mean "every type", which quietly pulled every file
// an encounter has. It now has to be an explicit choice.
function requireFileTypes() {
  if (selectedFiles().length) return true;
  setStatus(
    "Select at least one file output first — otherwise every file for every encounter would be downloaded.",
    true
  );
  return false;
}

function payload() {
  return {
    environment: environmentEl.value,
    client: clientEl.value,
    account_numbers: splitValues(accountNumbersEl.value),
    encounter_ids: splitValues(encounterIdsEl.value),
    selected_files: selectedFiles(),
  };
}

// "Search every client" is a deliberate choice, not what you get by leaving the
// box alone. Blank now means "nothing picked yet" and is rejected before the
// search runs; ALL_OPTION is the explicit opt-in, and it maps back to the empty
// list the API already treats as "no restriction".
const ALL_OPTION = "__all__";
const ALL_CLIENTS = { value: ALL_OPTION, label: "All clients" };
const ALL_FACILITIES = { value: ALL_OPTION, label: "All facilities" };

function metadataFilters() {
  const clientCode = clientCodesEl.value;
  const facilityCode = facilityCodesEl.value;
  const filters = {
    date_range: document.querySelector("#date_range").value,
    client_codes: clientCode && clientCode !== ALL_OPTION ? [clientCode] : [],
    facility_codes: facilityCode && facilityCode !== ALL_OPTION ? [facilityCode] : [],
  };

  const dateFrom = document.querySelector("#date_from").value;
  const dateTo = document.querySelector("#date_to").value;
  if (dateFrom) filters.date_from = dateFrom;
  if (dateTo) filters.date_to = dateTo;
  return filters;
}

// Account numbers and encounter IDs are numeric identifiers, so anything that is
// not a digit is dropped as it is typed or pasted.
//
// Commas and hyphens stay because they are part of the format: account numbers
// look like 9619150-720680, and lists are comma separated. Whitespace stays
// because `splitValues` treats it as a separator -- stripping it would silently
// weld a newline-separated paste out of a spreadsheet into one long, wrong id
// rather than rejecting anything.
const ID_DISALLOWED = /[^0-9,\-\s]/g;
const ID_ALLOWED = /[0-9,\-\s]/g;

function sanitizeIdList(el) {
  const before = el.value;
  const cleaned = before.replace(ID_DISALLOWED, "");
  if (cleaned === before) return;

  // Assigning .value drops the caret to the end, which makes editing the middle
  // of a long pasted list impossible. Put it back where it was, less however many
  // characters were removed ahead of it.
  const caret = el.selectionStart;
  const removedBefore = before.slice(0, caret).replace(ID_ALLOWED, "").length;
  el.value = cleaned;
  const position = caret - removedBefore;
  el.setSelectionRange(position, position);
}

// Direct lookup takes accounts or encounters, never both. They used to combine
// with AND, so pasting a list of accounts beside a list of unrelated encounter
// ids matched nothing at all -- the two lists have to describe the same
// encounters to return anything. Whichever box is used first now disables the
// other, so the empty-result case cannot be reached.
function syncDirectFilters() {
  const usingAccounts = accountNumbersEl.value.trim() !== "";
  const usingEncounters = encounterIdsEl.value.trim() !== "";
  accountNumbersEl.disabled = usingEncounters;
  encounterIdsEl.disabled = usingAccounts;
  // The label dims with its field, so the pair reads as one either/or choice.
  accountNumbersEl.closest(".field").classList.toggle("is-disabled", usingEncounters);
  encounterIdsEl.closest(".field").classList.toggle("is-disabled", usingAccounts);
}

function metadataPayload() {
  return {
    environment: environmentEl.value,
    filters: metadataFilters(),
    selected_files: selectedFiles(),
  };
}

function hasMetadataFilter(filters) {
  const hasList = ["client_codes", "facility_codes"].some((key) => filters[key].length > 0);
  const hasDate = filters.date_range !== "any" || Boolean(filters.date_from) || Boolean(filters.date_to);
  return hasList || hasDate;
}

function setMode(mode) {
  searchMode = mode;
  commonFiltersEl.hidden = mode !== "common";
  directFiltersEl.hidden = mode !== "direct";
  modeHintEl.textContent = MODE_HINTS[mode];

  for (const tab of modeTabsEl.querySelectorAll(".mode-tab")) {
    const isActive = tab.dataset.mode === mode;
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  }
  syncDirectFilters();
  renderRows([]);
  setStatus("");
  if (mode === "common") {
    ensureMetadataOptions();
  }
}

function setStatus(message, isError = false) {
  statusEl.textContent = message;
  // Class, not an inline colour: the palette owns both themes.
  statusEl.classList.toggle("is-error", Boolean(isError));
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

function fillOptions(selectEl, values, placeholder) {
  selectEl.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = placeholder;
  selectEl.appendChild(blank);
  // Entries are either plain strings (clients) or {value, label} objects
  // (facilities, where the label carries the facility description too).
  for (const entry of values) {
    const option = document.createElement("option");
    option.value = typeof entry === "string" ? entry : entry.value;
    option.textContent = typeof entry === "string" ? entry : entry.label;
    selectEl.appendChild(option);
  }
}

// The client and facility lists are long (SCP has 252 facilities) and the codes
// are opaque, so a plain <select> is painful to use: its built-in type-ahead
// matches only the visible label, resets after about a second, and cannot narrow
// across several keystrokes. Each select is therefore hidden and driven by a text
// input that filters as you type -- "s" then "j" narrows to codes starting "sj".
//
// The <select> stays in the DOM as the value store, so `.value`, `.disabled` and
// the `change` listeners elsewhere keep working untouched. A MutationObserver
// picks up `fillOptions` rewriting the options and the `disabled` toggles, so
// none of the existing call sites had to change.
const COMBOBOX_WORD_BREAK = /[\s\-—_/(),.]+/;

function enhanceSelect(selectEl) {
  const wrapper = document.createElement("div");
  wrapper.className = "combobox";
  selectEl.parentNode.insertBefore(wrapper, selectEl);

  const input = document.createElement("input");
  input.type = "text";
  input.className = "combobox-input";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");

  const list = document.createElement("ul");
  list.className = "combobox-list";
  list.hidden = true;
  list.id = `${selectEl.id}_list`;
  list.setAttribute("role", "listbox");
  input.setAttribute("aria-controls", list.id);

  // The input goes in ahead of the select, which this also moves into the
  // wrapper. Order matters: each field is a wrapping <label>, and a label points
  // at the first labelable element inside it. With the hidden select first,
  // clicking the "Facility code" caption would focus nothing at all.
  wrapper.append(input, list, selectEl);

  let items = [];
  let matches = [];
  let activeIndex = -1;
  let typing = false;

  // Matching is prefix-only, never substring: typing "s" should mean "starts
  // with s". A substring match would drag in anything whose description merely
  // contains the letter -- "Tablespace" would answer to "s" -- which buries the
  // codes actually being looked for.
  //
  // Ranked rather than plainly filtered so a code prefix sorts above a
  // description hit, and a word prefix in the description still counts, which is
  // what makes "totowa" find ACP_TOTOWA and PULM_TOTOWA.
  function rank(item, query) {
    if (!query) return item.value === "" ? -1 : 0;
    if (item.value === "") return -1;
    if (item.value.toLowerCase().startsWith(query)) return 0;
    const words = item.label.toLowerCase().split(COMBOBOX_WORD_BREAK);
    if (words.some((word) => word.startsWith(query))) return 1;
    return -1;
  }

  function selectedItem() {
    return items.find((item) => item.value !== "" && item.value === selectEl.value) || null;
  }

  function restore() {
    const current = selectedItem();
    input.value = current ? current.label : "";
    typing = false;
  }

  function render() {
    const query = typing ? input.value.trim().toLowerCase() : "";
    matches = items
      .map((item, order) => ({ item, order, score: rank(item, query) }))
      .filter((entry) => entry.score >= 0)
      .sort((a, b) => a.score - b.score || a.order - b.order)
      .map((entry) => entry.item);

    // Offer the blank option as a way back to "no filter", but only when not
    // actively narrowing -- it would otherwise sit above every real match.
    const blank = items.find((item) => item.value === "");
    if (blank && !query) matches.unshift(blank);

    list.innerHTML = "";
    if (!matches.length) {
      const empty = document.createElement("li");
      empty.className = "combobox-empty";
      empty.textContent = "No match";
      list.appendChild(empty);
      activeIndex = -1;
      return;
    }

    matches.forEach((item, index) => {
      const option = document.createElement("li");
      option.className = "combobox-option";
      option.id = `${list.id}_${index}`;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(item.value === selectEl.value));
      option.textContent = item.label;
      option.addEventListener("click", (event) => {
        event.stopPropagation();
        choose(item);
      });
      list.appendChild(option);
    });

    setActive(matches.length && typing ? 0 : -1);
  }

  function setActive(index) {
    activeIndex = index;
    for (const [position, option] of Array.from(list.children).entries()) {
      option.classList.toggle("is-active", position === index);
    }
    if (index >= 0) {
      input.setAttribute("aria-activedescendant", `${list.id}_${index}`);
      list.children[index].scrollIntoView({ block: "nearest" });
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function open() {
    if (input.disabled) return;
    render();
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    activeIndex = -1;
  }

  function choose(item) {
    selectEl.value = item ? item.value : "";
    close();
    restore();
    selectEl.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function sync() {
    const options = Array.from(selectEl.options);
    const blank = options.find((option) => option.value === "");
    input.placeholder = blank ? blank.textContent : "";
    input.disabled = selectEl.disabled;
    items = options.map((option) => ({ value: option.value, label: option.textContent }));
    restore();
    if (!list.hidden) render();
  }

  input.addEventListener("focus", () => {
    typing = false;
    input.select();
    open();
  });

  input.addEventListener("input", () => {
    typing = true;
    open();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (list.hidden) {
        open();
        return;
      }
      if (!matches.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = (activeIndex + step + matches.length + 1) % (matches.length + 1);
      setActive(next === matches.length ? -1 : next);
    } else if (event.key === "Enter") {
      if (list.hidden) return;
      event.preventDefault();
      // A single remaining match is unambiguous, so Enter takes it even if the
      // user never arrowed down to it.
      if (activeIndex >= 0) choose(matches[activeIndex]);
      else if (matches.length === 1) choose(matches[0]);
    } else if (event.key === "Escape") {
      close();
      restore();
    } else if (event.key === "Tab") {
      close();
      restore();
    }
  });

  // Keeps focus on the input so `blur` never races the option's click handler.
  list.addEventListener("mousedown", (event) => event.preventDefault());

  input.addEventListener("blur", () => {
    close();
    restore();
  });

  new MutationObserver(sync).observe(selectEl, {
    childList: true,
    attributes: true,
    attributeFilter: ["disabled"],
  });

  sync();
}

// The only entry point that contacts commonDb before a search. Called when the
// metadata tab is opened, not on page load, so the app is usable while commonDb
// is down; a failed attempt is retried by clicking the tab again.
async function ensureMetadataOptions() {
  if (metadataLoaded || metadataLoading) return;
  metadataLoading = true;
  facilityCodesEl.disabled = true;
  fillOptions(clientCodesEl, [], "Loading from commonDb…");
  setStatus("Connecting to commonDb — building the client list…");
  setBusy(true);

  try {
    await loadMetadataClients();
    metadataLoaded = true;
    setStatus("");
  } catch (error) {
    fillOptions(clientCodesEl, [], "Unavailable — click the tab again to retry");
    setStatus(error.message, true);
  } finally {
    metadataLoading = false;
    setBusy(false);
  }
}

async function loadMetadataClients() {
  const response = await fetch("/api/metadata/clients");
  const codes = await readResponse(response);
  if (!response.ok) {
    throw new Error(codes.detail || "Could not load client codes from commonDb.");
  }
  fillOptions(clientCodesEl, [ALL_CLIENTS, ...codes], "Select a client…");
  await loadFacilities();
}

// Facilities depend on the chosen client, so the list is reloaded whenever the
// client changes and stays disabled until one is picked.
async function loadFacilities() {
  const clientCode = clientCodesEl.value;
  if (!clientCode) {
    fillOptions(facilityCodesEl, [], "Select a client first");
    facilityCodesEl.disabled = true;
    return;
  }

  if (clientCode === ALL_OPTION) {
    // Facility codes are only meaningful within one client, so there is no list
    // to offer across all of them. "All clients" therefore fixes this to "All
    // facilities" rather than leaving a blank box that silently means the same.
    fillOptions(facilityCodesEl, [ALL_FACILITIES], "Select a facility…");
    facilityCodesEl.value = ALL_OPTION;
    facilityCodesEl.disabled = true;
    return;
  }

  facilityCodesEl.disabled = true;
  fillOptions(facilityCodesEl, [], "Loading…");

  const response = await fetch(`/api/metadata/facilities?client_code=${encodeURIComponent(clientCode)}`);
  const facilities = await readResponse(response);
  if (!response.ok) {
    fillOptions(facilityCodesEl, [], "Select a facility…");
    throw new Error(facilities.detail || "Could not load facilities.");
  }

  // {value, label} objects — the option value stays the bare facility code, so
  // the search request is unchanged. "All facilities" leads, as the explicit way
  // to include every one of them.
  fillOptions(facilityCodesEl, [ALL_FACILITIES, ...facilities], "Select a facility…");
  facilityCodesEl.disabled = false;
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
  // The ZIP contains exactly what is ticked, so the count rides on the button.
  downloadCountEl.textContent = selected ? String(selected) : "";
  downloadBtn.disabled = selected === 0;
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

    const chips = (row.file_breakdown || [])
      .filter((item) => item.count > 0)
      .map((item) => `<span class="fchip"><b>${item.count}</b> ${item.label}</span>`)
      .join("");
    const totalFiles = row.download_file_count ?? 0;
    const filesCell = chips
      ? `<div class="fchips">${chips}<span class="file-total">${totalFiles} total</span></div>`
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
  const isCommon = searchMode === "common";
  const body = isCommon ? metadataPayload() : payload();

  if (!requireFileTypes()) return;

  if (isCommon) {
    // Checked before the request so these read as form errors rather than a
    // round trip that comes back 400 or, worse, quietly returns everything.
    const dateFrom = document.querySelector("#date_from").value;
    const dateTo = document.querySelector("#date_to").value;
    if (Boolean(dateFrom) !== Boolean(dateTo)) {
      setStatus(
        `Enter both From and To — ${dateFrom ? "To" : "From"} is missing. ` +
          "A one-sided date range is not allowed; clear both to use the Service date range instead.",
        true,
      );
      return;
    }
    if (!clientCodesEl.value) {
      setStatus('Pick a client — or choose "All clients" if you really mean every client.', true);
      return;
    }
    if (!facilityCodesEl.disabled && !facilityCodesEl.value) {
      setStatus(
        'Pick a facility — or choose "All facilities" if you really mean every facility for this client.',
        true,
      );
      return;
    }
    if (!hasMetadataFilter(body.filters)) {
      setStatus("Enter at least one filter — a date range, client, facility, account, encounter or document type.", true);
      return;
    }
  } else if (!clientEl.value) {
    setStatus("Select a client first.", true);
    return;
  }

  setStatus("Searching…");
  setBusy(true);
  hideProgress();
  searchBtn.disabled = true;
  downloadBtn.disabled = true;

  try {
    const response = await fetch(isCommon ? "/api/metadata/search" : "/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

    let message = `Search complete — ${encounters} encounter(s), ${docs} document(s).`;
    let warn = false;
    if (isCommon) {
      message = `commonDb matched ${data.metadata_match_count} encounter(s). ${message}`;
      if (data.clients_skipped && data.clients_skipped.length) {
        // These clients exist in overall_data but have no CAPC_APIGATEWAY_*
        // database in this environment, so their files cannot be fetched.
        message += ` Skipped client(s) with no database here: ${data.clients_skipped.join(", ")}.`;
        warn = true;
      }
      if (data.truncated) {
        // Worth being blunt: the cap is applied with no ORDER BY, so the cut is
        // wherever the scan happened to stop. A no-client search can come back
        // covering only a handful of clients and look complete.
        message +=
          ` Result limit reached — this is a partial, arbitrary subset and covers only ` +
          `${data.clients_matched ? data.clients_matched.length : 0} client(s), not necessarily every one that matches. ` +
          "Narrow the date range, client or facility.";
        warn = true;
      }
      if (!data.metadata_match_count) {
        // Almost always a date-range miss: overall_data lags real time, so the
        // newest available service date is the useful thing to report.
        message = data.latest_service_date
          ? `No encounters with a service date in that range. The most recent one for this selection is ${data.latest_service_date.slice(0, 10)} — try widening the date range.`
          : "commonDb matched 0 encounters. Try widening the date range or clearing a filter.";
        warn = true;
      }
    }
    setStatus(message, warn);
  } catch (error) {
    renderRows([]);
    setStatus(error.message, true);
  } finally {
    setBusy(false);
    searchBtn.disabled = false;
  }
}

async function downloadZip() {
  const isCommon = searchMode === "common";
  if (!requireFileTypes()) return;
  if (!isCommon && !clientEl.value) {
    setStatus("Select a client first.", true);
    return;
  }

  const body = isCommon ? metadataPayload() : payload();
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
    const startRes = await fetch(isCommon ? "/api/metadata/download/start" : "/api/download/start", {
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
    updateToggleState();
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

modeTabsEl.addEventListener("click", (event) => {
  const tab = event.target.closest(".mode-tab");
  if (!tab) return;
  if (tab.dataset.mode !== searchMode) {
    setMode(tab.dataset.mode);
  } else if (tab.dataset.mode === "common") {
    // Re-clicking the metadata tab retries a commonDb load that failed.
    ensureMetadataOptions();
  }
});

searchBtn.addEventListener("click", search);
downloadBtn.addEventListener("click", downloadZip);
environmentEl.addEventListener("change", () => {
  loadClients().catch((error) => setStatus(error.message, true));
});

for (const el of [accountNumbersEl, encounterIdsEl]) {
  el.addEventListener("input", () => {
    sanitizeIdList(el);
    syncDirectFilters();
  });
}

clientCodesEl.addEventListener("change", () => {
  loadFacilities().catch((error) => setStatus(error.message, true));
});

// Only the three long, data-driven lists. The short fixed ones (environment,
// date range, folder structure, page size) are better off as native selects.
enhanceSelect(clientEl);
enhanceSelect(clientCodesEl);
enhanceSelect(facilityCodesEl);

setMode(searchMode);
loadFileTypes().catch((error) => setStatus(error.message, true));
loadClients().catch((error) => setStatus(error.message, true));
