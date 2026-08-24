# Lenovo ASP Portal

A Flask web application for managing Lenovo After-Sales Partner (ASP) work orders and admin operations, with a dual-portal interface backed by a SQLite database. Includes a REST JSON API used by the companion mobile app.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Portal Overview](#portal-overview)
- [Mobile API Overview](#mobile-api-overview)
- [Prerequisites](#prerequisites)
- [Local Development](#local-development)
- [Deploy to Render.com](#deploy-to-rendercom)
- [Deploy via Cloudflare Tunnel](#deploy-via-cloudflare-tunnel)
- [Environment Variables](#environment-variables)
- [Database](#database)
- [File Persistence Note](#file-persistence-note)
- [Windows Troubleshooting](#windows-troubleshooting)
  - [Git Bash: `uname`/`sed`/`git` command not found](#git-bash-uname--sed--git-command-not-found)
  - [PowerShell execution policy blocks activate](#powershell-execution-policy-blocks-venvscriptsactivate)
  - [Activating venv with emoji path](#activating-the-virtual-environment-windows)
  - [ImportError on pip](#importerror-cannot-import-name-_appengine_environ-when-running-pip)
  - [OSError Errno 22](#oserror-errno-22-invalid-argument-when-running-python-runpy)

---

## Quick Start

> **Windows (PowerShell) — first time setup**

```powershell
# 1. Navigate to project folder
cd path\to\develop-lenovo-app

# 2. Create virtual environment
$env:PYTHONIOENCODING = "utf-8"
python -m venv .venv

# 3. Activate it
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1

# 4. Install dependencies
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 5. Seed database (first time only — place Excel files in files/source-db/ first)
flask seed-db

# 6. Run the app
python run.py
```

Open **http://127.0.0.1:5000** — it will redirect to the login page.

> **Every subsequent session** — just two commands:
> ```powershell
> $env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
> python run.py
> ```

> **If activation is blocked:** run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` once, then retry.

---

## Project Structure

```
.
├── app/
│   ├── __init__.py                      # App factory — registers all blueprints + CORS
│   ├── config/
│   │   ├── file_categories.py           # Upload file-category definitions
│   │   └── settings.py                  # Flask config (paths, secret key)
│   ├── routes/
│   │   ├── asp.py                       # ASP Portal routes (/asp/*)
│   │   ├── admin.py                     # Admin Portal routes (/admin/*) + Monday.com scheduler
│   │   ├── auth.py                      # Login / logout / session (/login, /logout)
│   │   ├── api_mobile.py                # Mobile REST API (/api/v1/*) — JWT-authenticated
│   │   ├── excel_upload.py              # Legacy /upload-excel routes (backward compat)
│   │   └── ticket.py                    # Legacy ticket form route (backward compat)
│   ├── services/
│   │   ├── jwt_service.py               # JWT token generation + @jwt_required decorator
│   │   ├── database/
│   │   │   ├── db.py                    # get_db() — SQLite connection helper
│   │   │   ├── migrate.py               # run_migrations() — schema auto-migration on startup
│   │   │   ├── queries.py               # All read-only query helpers used by routes
│   │   │   ├── schema.sql               # Canonical schema (tables + indexes)
│   │   │   ├── seed.py                  # flask seed-db — populates tables from source Excel files
│   │   │   └── upsert.py                # Upsert helpers for data import
│   │   ├── excel_report/
│   │   │   ├── config.py                # Column/sheet config for report reader
│   │   │   └── reader.py                # Loads WO data from compiled Excel report
│   │   ├── msd_to_db/
│   │   │   ├── explore.ipynb            # Jupyter notebook for MSD data exploration
│   │   │   └── pipeline.md              # MSD → DB pipeline documentation
│   │   └── upload/
│   │       ├── config.py                # Upload folder config
│   │       ├── evidence.py              # Evidence file upload handler
│   │       ├── excel.py                 # Excel file upload/list handler
│   │       ├── excel_to_df.py           # Excel → DataFrame conversion
│   │       ├── meta_cache.py            # Upload metadata cache helper
│   │       └── upload_verification.py   # Upload verification logic
│   └── templates/
│       ├── base.html                    # Shared layout (topbar + collapsible left sidebar)
│       ├── profile.html                 # Profile / password change page
│       ├── asp/
│       │   ├── dashboard.html           # ASP Dashboard — stat cards, 5-tab table, modals
│       │   ├── work_orders.html         # Work Orders — Active/Closed/Escalated/Pending
│       │   ├── parts_management.html    # Parts Management — Awaiting/Received/Return
│       │   ├── reschedule.html          # Reschedule Management
│       │   ├── escalation.html          # Escalation Center
│       │   └── branch_office.html       # Branch office view
│       └── admin/
│           ├── dashboard.html           # Admin Dashboard — quick-links overview
│           ├── ticket_management.html   # Ticket Management
│           ├── data_import.html         # Data Import/Export (upload + compile + download)
│           ├── df_viewer.html           # DataFrame debug viewer
│           ├── monday_collector.html    # Monday.com data collector
│           ├── monday_data.html         # Monday.com data viewer
│           ├── validation_center.html   # Validation Center (AWB & Reschedule)
│           ├── user_management.html     # User & ASP Management
│           ├── user_management/         # Sub-pages: login, asp directory, pw change, counts
│           └── system_archive.html      # System Archive (masterfiles & uploads)
├── files/
│   ├── lenovo_asp.db                    # SQLite database (auto-created on first run)
│   ├── source-db/                       # Source Excel files for flask seed-db (not committed)
│   ├── upload/
│   │   └── excel/                       # Uploaded source Excel files (auto-created)
│   └── download/
│       └── excel/                       # Compiled masterfile reports (auto-created)
├── cloudflared/
│   ├── config.yml                       # Cloudflare Tunnel config (tunnel ID + ingress rules)
│   └── cloudflared.exe                  # cloudflared binary — NOT committed, download manually
├── render.yaml                          # Render.com deployment config
├── requirements.txt                     # Python dependencies
└── run.py                               # App entry point
```

---

## Portal Overview

The app exposes two portals via a switcher in the topbar. Both share the same `base.html` layout with a collapsible left sidebar (desktop) and a slide-in drawer (mobile). The root URL redirects to the login page.

### Root

| Route | Behaviour |
|---|---|
| `GET /` | Redirects to `/login` |
| `GET /login` | Login form — accepts ASP, admin, and superadmin credentials |
| `POST /login` | Processes credentials, sets session, redirects to dashboard |
| `GET /logout` | Clears session, redirects to `/login` |
| `GET /profile` | Profile and password-change page |

### ASP Portal (`/asp/*`)

| Page | URL | Status |
|---|---|---|
| Dashboard | `/asp/dashboard` | ✅ Live — WO stat cards, 5-tab data table, modals |
| Work Orders | `/asp/work-orders` | ✅ Live — Active / Closed / Escalated / Pending tabs |
| Parts Management | `/asp/parts` | ✅ Live — Awaiting / Received / Return tabs |
| Reschedule Management | `/asp/reschedule` | ✅ Live |
| Escalation Center | `/asp/escalation` | ✅ Live |
| Branch Office | `/asp/branch` | ✅ Live — Branch view for ASP HQ accounts |

### Admin Portal (`/admin/*`)

| Page | URL | Status |
|---|---|---|
| Dashboard | `/admin/dashboard` | ✅ Live |
| Ticket Management | `/admin/tickets` | ✅ Live |
| Data Import / Export | `/admin/data-import` | ✅ Live — Excel upload, compile, download masterfile |
| Validation Center | `/admin/validation` | ✅ Live — AWB & reschedule validation |
| User & ASP Management | `/admin/users` | ✅ Live — ASP directory, user accounts, pw change requests |
| System Archive | `/admin/archive` | ✅ Live — Lists masterfiles and uploaded files |
| Monday.com Collector | `/admin/monday` | ✅ Live — Syncs WO data from Monday.com |

---

## Mobile API Overview

A REST JSON API consumed by the **Lenovo ASP Mobile App** (`mobile-lenovo-asp/`). All routes are under `/api/v1/`. Authentication uses **JWT Bearer tokens** — the session-cookie auth used by the web portals is not involved.

CORS is enabled for `*` on all `/api/v1/*` routes, allowing Expo's dev server and any deployed mobile build to call the API.

### Authentication

| Method | Route | Description |
|---|---|---|
| `POST` | `/api/v1/auth/login` | Exchange `{ username, password }` for a JWT. Returns `{ token, role, display_name, username, email, labor_vendor, asp_name }`. |

**Accepted account types:**

| Account type | Table | Role returned | Notes |
|---|---|---|---|
| ASP Staff User | `asp_users` (email-based login) | `asp_user` | Primary mobile user |
| ASP HQ Account | `asp_details` (username login) | `asp` | ASP account login |
| Superadmin | `admin_users` | `superadmin` | Sees all WOs across all ASPs |
| Admin | `admin_users` | — | **Rejected** — admins cannot log in to the mobile app |

JWT payload fields: `sub` (user ID as string), `username`, `role`, `labor_vendor`, `display_name`. Tokens expire after **24 hours**.

### Protected Endpoints

All require `Authorization: Bearer <token>` header. Vendor filtering is automatic — `asp` and `asp_user` accounts only see WOs belonging to their `labor_vendor_related` value. Superadmin sees all.

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v1/mobile/stats` | WO summary stat counts per category |
| `GET` | `/api/v1/mobile/in-prepare` | In-Prepare Follow-Up list (paginated) |
| `GET` | `/api/v1/mobile/cci-followup` | CCI Follow-Up list (paginated) |
| `GET` | `/api/v1/mobile/onsite-followup` | Onsite Follow-Up list (paginated) |
| `GET` | `/api/v1/mobile/return-part` | Return Part list (paginated) |
| `GET` | `/api/v1/mobile/wo/<id>` | Single WO detail + all part lines |

**Common query parameters for list endpoints:**

| Parameter | Default | Description |
|---|---|---|
| `page` | `1` | Page number |
| `per_page` | `20` | Results per page (max 100) |
| `q` | `""` | Search across WO#, serial number, contact name |
| `followup_state` | `""` | Filter by specific follow-up state (optional) |

**Stats response shape:**
```json
{
  "in_prepare_total": 12,
  "cci_followup_total": 5,
  "onsite_followup_total": 3,
  "return_part_total": 8
}
```

---

## Prerequisites

### Python (Windows)

Download and install **Python 3.11** or higher from https://www.python.org/downloads/.
During installation, check **"Add Python to PATH"**.

Verify in a new PowerShell window:
```powershell
python --version   # 3.11.x or higher
```

### Python (macOS via Homebrew)

If you install Python via Homebrew, it blocks system-wide `pip install` by default (PEP 668). Always use a virtual environment per project.

```zsh
brew install python
```

Add Homebrew's unversioned symlinks to your PATH (add to `~/.zshrc`):

```zsh
echo 'export PATH="/usr/local/opt/python@3/libexec/bin:$PATH"' >> ~/.zshrc
echo 'export PATH="/usr/local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

Verify:
```zsh
python --version   # Python 3.x
pip --version      # pip 2x.x
```

---

## Local Development

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd develop-lenovo-app
```

### 2. Create and activate virtual environment

**Windows (PowerShell):**
```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m venv .venv
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

> The project path contains emoji characters — always set `PYTHONIOENCODING=utf-8` on Windows to avoid a `UnicodeEncodeError`. See [Troubleshooting](#activating-the-virtual-environment-windows).

### 3. Install dependencies

**Windows:**
```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux:**
```bash
pip install -r requirements.txt
```

### 4. Seed the database *(first time only)*

Place source Excel files in `files/source-db/`, then:
```powershell
flask seed-db
```

`files/lenovo_asp.db` is auto-created and not committed to Git.

### 5. Run the app

```powershell
python run.py
```

Open **http://127.0.0.1:5000** — redirects to login page.

### 6. Running with the mobile app (same Wi-Fi)

Find your LAN IP and set it in [`mobile-lenovo-asp/services/api.ts`](../mobile-lenovo-asp/services/api.ts):

```powershell
ipconfig | Select-String "IPv4"
```

```typescript
const DEV_DEVICE = "http://192.168.1.X:5000";  // replace X with your IP
```

---

## Deploy to Render.com

### Prerequisites

- A [Render.com](https://render.com) account (free tier is sufficient)
- The repository pushed to GitHub or GitLab

### Step 1 — Push to GitHub

```bash
git add .
git commit -m "your message"
git push origin main
```

### Step 2 — Create a new Web Service on Render

1. Log in to [render.com](https://render.com) and click **New → Web Service**.
2. Connect your GitHub/GitLab account and select this repository.
3. Render will auto-detect `render.yaml` and pre-fill the settings:

   | Setting | Value |
   |---|---|
   | **Runtime** | Python 3.11 |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `gunicorn run:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` |

4. Click **Create Web Service**.

### Step 3 — Set environment variables

In the Render dashboard go to your service → **Environment** tab and add:

| Variable | Value |
|---|---|
| `SECRET_KEY` | A long random string — see [Environment Variables](#environment-variables) |
| `JWT_SECRET_KEY` | A separate long random string for signing mobile JWTs |

### Step 4 — Deploy

Render automatically builds and deploys on every push to `main`. To trigger manually: **Manual Deploy → Deploy latest commit**.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | **Recommended** | Flask session secret key. Defaults to a hardcoded dev value — always override in production. |
| `JWT_SECRET_KEY` | **Recommended** | Secret used to sign mobile JWT tokens. Falls back to `SECRET_KEY` if not set. |
| `PORT` | Auto-set | Injected by Render at runtime. Do not set manually. |

Generate secure keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Run this twice — once for `SECRET_KEY` and once for `JWT_SECRET_KEY`.

---

## Database

The app uses **SQLite** (`files/lenovo_asp.db`). The schema is managed by `app/services/database/migrate.py`, which runs automatically on every startup and applies any pending schema changes.

### Key tables

| Table | Description |
|---|---|
| `wo_summary` | Work order master records |
| `wo_details` | Extended WO detail fields |
| `wo_product_detail` | Part/product lines per WO (MSD + shipment data) |
| `asp_details` | ASP vendor accounts (login + vendor filter) |
| `asp_users` | Individual ASP staff user accounts (email-based login) |
| `admin_users` | Admin and superadmin accounts |

### CLI commands

```powershell
# Seed all tables from source Excel files in files/source-db/
flask seed-db
```

---

## File Persistence Note

Render's free tier uses an **ephemeral filesystem** — uploaded Excel files (`files/upload/excel/`) and compiled reports (`files/download/excel/`) are lost on each redeploy or instance restart.

For persistent storage either:
- Attach a [Render Disk](https://render.com/docs/disks) (paid), or
- Store files in an external object store (e.g. AWS S3, Cloudflare R2)

The SQLite database (`files/lenovo_asp.db`) is also ephemeral on Render's free tier. For a production deployment, migrate to PostgreSQL or attach a Render Disk.

---

## Windows Troubleshooting

### Git Bash: `uname` / `sed` / `git` command not found

**Symptom:**
```
bash: uname: command not found
bash: sed: command not found
bash: git: command not found
```

**Cause:** The Windows `PATH` has `C:\Users\...\AppData\Local\Microsoft\WindowsApps` (WSL `bash.exe` stub) listed *before* Git's tool directories.

**Immediate fix** — paste into the broken Git Bash session:
```bash
export PATH="/mingw64/bin:/usr/bin:/bin:$PATH" && uname -a && sed --version && git --version
```

> This fix only lasts for the current session.

**Permanent fix** — run once in PowerShell (moves Git bins to front of user PATH):
```powershell
$gitBins = @("C:\Program Files\Git\mingw64\bin", "C:\Program Files\Git\usr\bin")
$current = [System.Environment]::GetEnvironmentVariable("PATH", "User") -split ";" |
           Where-Object { $_ -ne "" -and $gitBins -notcontains $_ }
$newPath = ($gitBins + $current) -join ";"
[System.Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
```

Then **close and reopen Git Bash**.

---

### PowerShell execution policy blocks `.venv\Scripts\activate`

**Symptom:**
```
.venv\Scripts\activate : File ... cannot be loaded because running scripts is disabled on this system.
```

**Fix** — run once per user account:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

---

### Activating the virtual environment (Windows)

Because the project path contains emoji characters (`💼`, `👨‍💻`), the Windows console defaults to cp1252 encoding, which causes `pip` to crash with a `UnicodeEncodeError`.

**Always activate with:**
```powershell
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
```

**Always install with:**
```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> Using `.venv\Scripts\python.exe -m pip` instead of bare `pip` bypasses the encoding bug entirely.

---

### `ImportError: cannot import name '_appengine_environ'` when running pip

**Symptom:**
```
ImportError: cannot import name '_appengine_environ' from 'pip._vendor.urllib3.contrib'
```

**Cause:** The `.venv` was created with a different Python version. The vendored `urllib3` inside pip is mismatched.

**Fix** — delete the stale venv and recreate:
```powershell
Remove-Item -Recurse -Force .venv
$env:PYTHONIOENCODING = "utf-8"; python -m venv .venv
.venv\Scripts\Activate.ps1
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> Never commit `.venv/` to Git. Confirm `.gitignore` contains `.venv/`.

---

### `OSError: [Errno 22] Invalid argument` when running `python run.py`

**Symptom:**
```
OSError: [Errno 22] Invalid argument
  File "<frozen importlib._bootstrap_external>", line 951, in get_data
```

**Cause:** Stale `__pycache__` folders from a different Python version were committed to Git. Windows raises `[Errno 22]` when the bytecode magic number mismatches and the path contains emoji characters.

**Fix** — delete all project-level `__pycache__` directories (excludes `.venv`):
```powershell
Get-ChildItem -Recurse -Filter "__pycache__" -Directory |
  Where-Object { $_.FullName -notlike "*\.venv\*" } |
  Remove-Item -Recurse -Force
```

Python will regenerate fresh `.pyc` files on the next run. To prevent recurrence, ensure `__pycache__/` is in `.gitignore`.

---

## Deploy via Cloudflare Tunnel

Exposes the local app publicly at **https://app.ticket-asp.my.id** using a persistent Cloudflare Tunnel — no port-forwarding or cloud hosting required. The mobile app uses this URL in production builds.

> **cloudflared** is stored locally at `cloudflared\cloudflared.exe` — no system-wide install needed.
> Use `.\cloudflared\cloudflared.exe` instead of plain `cloudflared` in every command below.
>
> **Tunnel name:** `asp-ticketing` · **Tunnel ID:** `ce858d75-6e3f-416f-b4f0-d1b5ebb1f016`

---

### Step 1 — Download cloudflared.exe (first time only)

`cloudflared.exe` is not committed to the repo. Download it once:

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "cloudflared\cloudflared.exe"
```

Verify:
```powershell
.\cloudflared\cloudflared.exe --version
```

---

### Step 2 — Authenticate (first time only)

```powershell
.\cloudflared\cloudflared.exe tunnel login
```

A browser window opens — log in to your Cloudflare account and authorise the domain. A credentials file is saved to `C:\Users\<you>\.cloudflared\`.

---

### Step 3 — Create the tunnel (first time only)

> **Already done for this project.** Running the command on the same account prints `tunnel with name already exists`. Skip to Step 4 or verify with:
> ```powershell
> .\cloudflared\cloudflared.exe tunnel list
> ```

To create a brand-new tunnel on a **different** account:

```powershell
.\cloudflared\cloudflared.exe tunnel create asp-ticketing
```

To run the tunnel once everything is configured:

```powershell
# Export a token for the tunnel (replace TUNNEL_ID with your tunnel's UUID)
.\cloudflared\cloudflared.exe tunnel token --cred-file "C:\Users\<you>\.cloudflared\<TUNNEL_ID>.json" <TUNNEL_ID>

# Start the tunnel
.\cloudflared\cloudflared.exe tunnel --config cloudflared\config.yml run
```

Then update [`cloudflared/config.yml`](cloudflared/config.yml) with the printed Tunnel ID:

```yaml
tunnel: <YOUR_TUNNEL_ID>
credentials-file: C:\Users\<you>\.cloudflared\<YOUR_TUNNEL_ID>.json
```

#### Restore missing credentials file

If `tunnel login` was run on a different machine or the `.json` was deleted:

```powershell
.\cloudflared\cloudflared.exe tunnel token `
  --cred-file "C:\Users\010880749\.cloudflared\ce858d75-6e3f-416f-b4f0-d1b5ebb1f016.json" `
  asp-ticketing
```

---

### Step 4 — Add a DNS CNAME record (first time only)

```powershell
.\cloudflared\cloudflared.exe tunnel route dns asp-ticketing app.ticket-asp.my.id
```

This creates a `CNAME` record in your Cloudflare DNS pointing `app.ticket-asp.my.id` to the tunnel.

---

### Step 5 — Review [`cloudflared/config.yml`](cloudflared/config.yml)

```yaml
tunnel: ce858d75-6e3f-416f-b4f0-d1b5ebb1f016
credentials-file: C:\Users\010880749\.cloudflared\ce858d75-6e3f-416f-b4f0-d1b5ebb1f016.json
edge-ip-version: "4"

ingress:
  - hostname: app.ticket-asp.my.id
    service: http://localhost:5000

  - service: http_status:404
```

- The first rule forwards all traffic for `app.ticket-asp.my.id` to the local Flask app on port `5000`.
- The catch-all rule returns `404` for any other hostname.

---

### Step 6 — Run everything (two terminals)

**Terminal 1 — Flask app:**

```powershell
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
python run.py
```

**Terminal 2 — Cloudflare Tunnel:**

```powershell
.\cloudflared\cloudflared.exe tunnel --config cloudflared/config.yml run
```

The app is now publicly accessible at **https://app.ticket-asp.my.id**. The mobile app's production builds point to this URL.

---

### Cloudflare Tunnel — Endpoints

| Route | Description |
|-------|-------------|
| `GET /` | Redirects to `/login` |
| `GET /login` | ASP / admin login page |
| `GET /asp/dashboard` | ASP Portal home |
| `GET /admin/dashboard` | Admin Portal home |
| `POST /api/v1/auth/login` | Mobile app JWT login |
| `GET /api/v1/mobile/stats` | Mobile app WO stats |
| `GET /api/v1/mobile/in-prepare` | Mobile In-Prepare list |
| `GET /api/v1/mobile/cci-followup` | Mobile CCI list |
| `GET /api/v1/mobile/onsite-followup` | Mobile Onsite list |
| `GET /api/v1/mobile/return-part` | Mobile Return Part list |
| `GET /api/v1/mobile/wo/<id>` | Mobile WO detail |

---

### Cloudflare Tunnel — Notes

- `cloudflared.exe` and the credentials file (`*.json`) are excluded from version control via `.gitignore`.
- Credentials file location: `C:\Users\010880749\.cloudflared\ce858d75-6e3f-416f-b4f0-d1b5ebb1f016.json`
- The tunnel stays alive as long as Terminal 2 is running. For always-on deployments, register it as a Windows service:
  ```powershell
  .\cloudflared\cloudflared.exe service install
  ```
- To stop the tunnel, press `Ctrl+C` in Terminal 2 or run:
  ```powershell
  .\cloudflared\cloudflared.exe service stop
  ```
