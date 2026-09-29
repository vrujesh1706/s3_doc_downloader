# S3 Document Downloader — User Guide

Use this app to find encounter documents and save selected files from S3 as a ZIP. No coding knowledge is needed to use the browser page.

[Setup](#1-setup-once) · [Run and access](#2-run-and-access) · [Search](#3-search) · [Search rules](#4-search-rules) · [Download](#5-download) · [Help](#6-common-problems)

**Already have a shared app URL?** Open it and go straight to [Search](#3-search). You do not need Python or a local `.env` file.

## 1. Setup once

You need the project folder, Python 3.10 or newer, and network access to the company databases and S3. Get the database/AWS settings from the project owner; connect to the company VPN if required.

Open a terminal inside the `s3_doc_downloader` folder. Use the commands for your system.

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Copy `.env.example` only for a new setup; keep your existing `.env` if you already have one. Open `.env` in a text editor and replace the sample values:

| Settings | What to enter |
| --- | --- |
| `PROD_*` | Production database connection and S3 bucket. |
| `STAGING_*` | Staging database connection and S3 bucket. |
| `COMMON_DB_*` | Shared metadata database connection. |
| `AWS_*` | AWS credentials and region supplied by your team. |
| Optional limits | Keep the defaults unless the project owner asks you to change them. |

Fill in both environments and the shared database settings, even if you plan to use only direct lookup. Keep credentials private; do not commit or share your `.env` in the project handoff.

## 2. Run and access

Run from the project folder each time.

**Linux / macOS**

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```


Open **[http://localhost:8000](http://localhost:8000)** in your browser. Keep the terminal running; press **Ctrl+C** to stop. The app loads `.env` automatically; restart after changing settings.

**Shared access:** the person hosting the app uses `--host 0.0.0.0` instead. Colleagues open `http://SERVER_IP:8000` using that computer's IP address, on the same reachable network/VPN. The host must allow port 8000 through its firewall. The app has no login, so shared access should stay on the team's private network.

## 3. Search

First choose **Production** or **Staging**, then use one of these tabs:

| Tab | Steps |
| --- | --- |
| **Direct client lookup** | Choose a client. Paste account numbers **or** encounter IDs into one box. Separate values with commas, spaces, or new lines. |
| **Find by metadata** | Choose a client, then a facility or **All facilities**. Optionally choose a coding date range or enter both **From** and **To**. |

Select at least one **File output**, then click **Search**. Available outputs: Original document, PDF document, PCS XML result, CDP XML result, E/M request/result, CPT request/result, and CMCS result.

Type the start of a client code or a word in a facility label to narrow its dropdown, then click an option or press Enter to select it.

## 4. Search rules

| Rule | What it means |
| --- | --- |
| Use one direct lookup box | Account numbers allow letters, digits, and hyphens; encounter IDs allow digits only. Other characters are removed. Clear the filled box to enable the other. |
| Metadata filters narrow together | A client, facility, and date range must all match. |
| Choose client and facility explicitly | Use **All clients** / **All facilities** when needed. **All clients** selects **All facilities** automatically and requires a date filter. |
| Custom dates come first | Fill both **From** and **To**. They include both days and replace the preset. Clear both to use the preset again. |
| Dates mean coding dates | They refer to when an encounter was last coded. **Service date** in the results is a different date. Last 7/30 days includes today; last week means the previous Monday–Sunday. |
| Metadata has a limited window | Searches cover the current calendar month and previous two months, even with **Any date**. |
| Large searches may be incomplete | Default search cap: 5,000 database rows, which may include several documents per encounter. A metadata limit warning means a partial selection, not necessarily the newest matches. Narrow the filters or split direct ID lists into smaller batches. |

## 5. Download

1. Review the results. **Multi-doc** shows encounters with several documents; **Files to download** lists the selected outputs found in the database.
2. Uncheck anything you do not need. **All results start selected**, including other pages. The table's top checkbox selects or clears every page; your selection stays when paging.
3. Choose the folder structure below, then click **Download ZIP**. Keep the page open until it finishes.
4. Save `s3_document_downloads.zip` and check the final downloaded/skipped counts. Your browser controls the save location.

| Folder structure | Inside the ZIP |
| --- | --- |
| **One folder** + only **Original document** | All originals together in `files/`. |
| **One folder** + any other output selection | A folder per encounter: `encounter_account/`. |
| **Facility wise** | `facility/encounter_account/`. |

Original documents are named `account_encounter_facility_worktype_documentID.txt`; their contents are unchanged. Other outputs keep their S3 filenames. Duplicate paths within an encounter folder are included once; name clashes get numbered suffixes.

Run **Search** again after changing filters or file outputs before downloading. Default download limits are 2,500 files and 1 GiB of source data per ZIP. Extra files are skipped; exceeding the size limit fails the job. Use smaller selections when needed.

## 6. Common problems

| Problem | What to do |
| --- | --- |
| Page will not open | Check the terminal is running and the URL/port is correct. For a shared URL, check VPN/network access with the host. |
| Missing settings or database connection error | Check `.env`, credentials, and VPN; restart the app. Ask the project owner if access is still denied. |
| Metadata unavailable | Use **Direct client lookup** with known IDs. Click the metadata tab again to retry later. |
| No results | Check environment, client, IDs, and dates. If shown, use the latest available coding date to adjust your range. |
| Client skipped | That client has no matching database in the selected environment. Check with the project owner. |
| Files skipped or unavailable | Download fewer files if you reached the cap. For S3 failures, check `FAILED_*.txt` notes in a partial ZIP. If every file fails, the app shows an error instead of a ZIP. |
| Download capacity full | Wait for other downloads to finish. Default capacity is two outstanding jobs; unclaimed completed ZIPs expire after one hour. |
| Unknown or expired job | Start the download again. Server restarts also clear pending jobs. |
| Port 8000 already in use | Run with `--port 8001` and open `http://localhost:8001`. |

For code or configuration changes, see the [development guide](DEVELOPMENT.md).
