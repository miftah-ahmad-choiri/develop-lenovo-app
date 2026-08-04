# Lenovo ASP Portal

A Flask web application for managing Lenovo After-Sales Partner (ASP) work orders and admin operations, with a dual-portal interface backed by Excel data.

---

## Table of Contents

- [Project Structure](#project-structure)
- [Portal Overview](#portal-overview)
- [Prerequisites](#prerequisites)
- [Local Development](#local-development)
- [Deploy to Render.com](#deploy-to-rendercom)
- [Environment Variables](#environment-variables)
- [File Persistence Note](#file-persistence-note)
- [Windows Troubleshooting](#windows-troubleshooting)
  - [Git Bash: `uname`/`sed`/`git` command not found](#git-bash-uname--sed--git-command-not-found)
  - [PowerShell execution policy blocks activate](#powershell-execution-policy-blocks-venvscriptsactivate)
  - [Activating venv with emoji path](#activating-the-virtual-environment-windows)
  - [ImportError on pip](#importerror-cannot-import-name-_appengine_environ-when-running-pip)
  - [OSError Errno 22](#oserror-errno-22-invalid-argument-when-running-python-runpy)

---

## Project Structure

```
.
├── app/
│   ├── __init__.py                      # App factory — registers all blueprints
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py                  # Flask config (paths, secret key)
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── asp.py                       # ASP Portal routes (/asp/*)
│   │   ├── admin.py                     # Admin Portal routes (/admin/*)
│   │   ├── excel_upload.py              # Legacy /upload-excel routes (backward compat)
│   │   └── ticket.py                    # Legacy ticket form route (backward compat)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── excel_report/
│   │   │   ├── __init__.py
│   │   │   ├── config.py                # Column/sheet config for report reader
│   │   │   └── reader.py                # Loads WO data from compiled Excel report
│   │   ├── upload/
│   │   │   ├── __init__.py
│   │   │   ├── config.py                # Upload folder config
│   │   │   ├── evidence.py              # Evidence file upload handler
│   │   │   └── excel.py                 # Excel file upload/list handler
│   │   └── wo_onsite/
│   │       ├── __init__.py
│   │       ├── config.py                # Pipeline column/sheet config
│   │       ├── pipeline.py              # WO onsite compile pipeline
│   │       └── transforms.py            # Data transformation logic
│   └── templates/
│       ├── base.html                    # Shared layout (topbar + collapsible left sidebar)
│       ├── asp/
│       │   ├── dashboard.html           # ASP Dashboard — stat cards, 5-tab table, modals
│       │   ├── work_orders.html         # Work Orders — Active/Closed/Escalated/Pending
│       │   ├── parts_management.html    # Parts Management — Awaiting/Received/Return
│       │   ├── reschedule.html          # Reschedule Management
│       │   └── escalation.html          # Escalation Center
│       └── admin/
│           ├── dashboard.html           # Admin Dashboard — quick-links overview
│           ├── ticket_management.html   # Ticket Management
│           ├── data_import.html         # Data Import/Export (upload + compile + download)
│           ├── validation_center.html   # Validation Center (AWB & Reschedule)
│           ├── user_management.html     # User & ASP Management
│           └── system_archive.html      # System Archive (masterfiles & uploads)
├── files/
│   ├── upload/
│   │   └── excel/                       # Uploaded source Excel files (auto-created)
│   └── download/
│       └── excel/                       # Compiled masterfile reports (auto-created)
├── render.yaml                          # Render.com deployment config
├── requirements.txt                     # Python dependencies
└── run.py                               # App entry point
```

---

## Portal Overview

The app exposes two portals via a switcher in the topbar. Both share the same `base.html` layout with a collapsible left sidebar (desktop) and a slide-in drawer (mobile).

### ASP Portal (`/asp/*`)

| Page | URL | Status |
|---|---|---|
| Dashboard | `/asp/dashboard` | ✅ Live — WO stat cards, 5-tab data table, modals |
| Work Orders | `/asp/work-orders` | ✅ Live — Active / Closed / Escalated / Pending tabs |
| Parts Management | `/asp/parts` | ✅ Live — Awaiting / Received / Return tabs |
| Reschedule Management | `/asp/reschedule` | 🚧 In development |
| Escalation Center | `/asp/escalation` | 🚧 In development |

### Admin Portal (`/admin/*`)

| Page | URL | Status |
|---|---|---|
| Dashboard | `/admin/dashboard` | 🚧 In development |
| Ticket Management | `/admin/tickets` | 🚧 In development |
| Data Import / Export | `/admin/data-import` | ✅ Live — Excel upload, compile, download masterfile |
| Validation Center | `/admin/validation` | 🚧 In development |
| User & ASP Management | `/admin/users` | 🚧 In development |
| System Archive | `/admin/archive` | ✅ Live — Lists masterfiles and uploaded files |

### Root redirect

`GET /` → redirects to `/asp/dashboard`.

---

## Prerequisites

### Python (macOS via Homebrew)

If you are on macOS and install Python via Homebrew, it will block system-wide `pip install` by default (PEP 668). Always use a virtual environment per project (covered in [Local Development](#local-development) below).

```zsh
brew install python
```

Add Homebrew's unversioned symlinks to your PATH (add to `~/.zshrc`):

```zsh
echo 'export PATH="/usr/local/opt/python@3.14/libexec/bin:$PATH"' >> ~/.zshrc
echo 'export PATH="/usr/local/bin:$PATH"' >> ~/.zshrc
echo "alias py='python3'" >> ~/.zshrc
source ~/.zshrc
```

Verify:

```zsh
python --version   # Python 3.x
python3 --version  # Python 3.x
py --version       # Python 3.x
```

---

## Local Development

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
```

### 2. Create and activate a virtual environment

```bash
Remove-Item -Recurse -Force .venv
$env:PYTHONIOENCODING = "utf-8"; python -m venv .venv
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

> **macOS/Homebrew note:** Running `pip install` outside a virtual environment will fail with an `externally-managed-environment` error. Always activate `.venv` first.

Re-activate each time you return to work on the project:

```zsh
source .venv/bin/activate   # activate
deactivate                  # when done
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the development server

```bash
python run.py
```

The app will be available at **http://127.0.0.1:5000** and will redirect to the ASP Dashboard.

> **Chrome DevTools probe:** You may see `GET /.well-known/appspecific/com.chrome.devtools.json 404` in the console. This is harmless — Chrome sends it automatically on localhost and has no effect on the app.

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

### Step 4 — Deploy

Render automatically builds and deploys on every push to `main`. To trigger manually: **Manual Deploy → Deploy latest commit**.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | **Recommended** | Flask session secret key. Defaults to a hardcoded dev value — always override in production. |
| `PORT` | Auto-set | Injected by Render at runtime. Do not set manually. |

Generate a secure `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## File Persistence Note

Render's free tier uses an **ephemeral filesystem** — uploaded Excel files (`files/upload/excel/`) and compiled reports (`files/download/excel/`) are lost on each redeploy or instance restart.

For persistent storage either:
- Attach a [Render Disk](https://render.com/docs/disks) (paid), or
- Store files in an external object store (e.g. AWS S3, Cloudflare R2)

---

## Windows Troubleshooting

### Git Bash: `uname` / `sed` / `git` command not found

**Symptom:**
```
bash: uname: command not found
bash: sed: command not found
bash: git: command not found
```

**Cause:** The Windows `PATH` has `C:\Users\...\AppData\Local\Microsoft\WindowsApps` (WSL `bash.exe` stub) listed *before* Git's tool directories, so every call to `bash`, `sed`, or `uname` is intercepted by the broken WSL stub.

**Immediate fix** — paste this single line into the broken Git Bash session:
```bash
export PATH="/mingw64/bin:/usr/bin:/bin:$PATH" && uname -a && sed --version && git --version
```

What this does:
- `export PATH="/mingw64/bin:/usr/bin:/bin:$PATH"` — prepends Git's tool directories to the front of `PATH` for the current session, so `git`, `sed`, `uname` etc. resolve to Git Bash's own binaries instead of the broken WSL stub
- `&& uname -a && sed --version && git --version` — immediately verifies all three tools are working; if any command is still missing, it will stop and tell you which one failed

> **Note:** This fix only lasts for the current terminal session. Open a new Git Bash window and it will be gone. Use the permanent fix below to make it stick.

**Permanent fix** — run once in PowerShell (moves Git bins to front of user PATH):
```powershell
$gitBins = @("C:\Program Files\Git\mingw64\bin", "C:\Program Files\Git\usr\bin")
$current = [System.Environment]::GetEnvironmentVariable("PATH", "User") -split ";" |
           Where-Object { $_ -ne "" -and $gitBins -notcontains $_ }
$newPath = ($gitBins + $current) -join ";"
[System.Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
```

Then **close and reopen Git Bash** — the fix will be permanent.

---

### PowerShell execution policy blocks `.venv\Scripts\activate`

**Symptom:**
```
.venv\Scripts\activate : File ... cannot be loaded because running scripts is disabled on this system.
```

**Fix** — run once per user account, in any PowerShell terminal:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

This allows locally-created scripts to run while still blocking unsigned remote scripts. You only need to do this once; the setting persists across sessions.

---

### Activating the virtual environment (Windows)

Because the project path contains emoji characters (`💼`, `👨‍💻`), the Windows console defaults to cp1252 encoding, which causes `pip` to crash with a `UnicodeEncodeError` when it tries to print the path.

**Always activate with:**
```powershell
$env:PYTHONIOENCODING = "utf-8"
.venv\Scripts\Activate.ps1
```

Or as a one-liner:
```powershell
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
```

Then install dependencies normally:
```powershell
pip install -r requirements.txt
```

> **Why `PYTHONIOENCODING`?**
> Setting it to `utf-8` forces Python's stdout/stderr to use UTF-8 instead of the system default (cp1252 on Windows), which cannot encode emoji characters in file paths.

---

### `ImportError: cannot import name '_appengine_environ'` when running pip

**Symptom:**
```
ImportError: cannot import name '_appengine_environ' from 'pip._vendor.urllib3.contrib'
```

**Cause:** The `.venv` folder was created by a different Python version (e.g. 3.11) but is now being run under a newer Python (e.g. 3.14). The vendored `urllib3` inside pip is mismatched.

**Fix** — delete the stale venv and recreate it fresh:
```powershell
Remove-Item -Recurse -Force .venv
python -m venv .venv
$env:PYTHONIOENCODING = "utf-8"; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> The `.venv` folder should never be committed to Git. Confirm `.gitignore` contains `.venv/`.

---

### `OSError: [Errno 22] Invalid argument` when running `python run.py`

**Symptom:**
```
OSError: [Errno 22] Invalid argument
  File "<frozen importlib._bootstrap_external>", line 951, in get_data
```

**Cause:** Stale `__pycache__` folders (compiled `.pyc` bytecode from Python 3.11) were committed to Git and are now invalid under Python 3.14. Windows raises `[Errno 22]` when the bytecode magic number mismatches and the path contains characters it cannot decode.

**Fix** — delete all project-level `__pycache__` directories (excludes `.venv`):
```powershell
Get-ChildItem -Recurse -Filter "__pycache__" -Directory |
  Where-Object { $_.FullName -notlike "*\.venv\*" } |
  Remove-Item -Recurse -Force
```

Python will regenerate fresh `.pyc` files for the current Python version on the next run. To prevent this recurring, ensure `__pycache__/` is in `.gitignore`.
