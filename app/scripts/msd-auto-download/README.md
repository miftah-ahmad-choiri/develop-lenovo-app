# NGSP / Dynamics Login Automation

Selenium script that logs into [NGSP (Lenovo)](https://ngsp-ap.lenovo.com) via IBM SSO, handles MFA interactively, and navigates to a Work Order page.

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.9 or later |
| Google Chrome | latest stable |
| ChromeDriver | must match your Chrome version |

> **ChromeDriver**: Selenium 4.6+ includes the **Selenium Manager** that downloads a matching ChromeDriver automatically. No manual download is needed if you are on Selenium ≥ 4.6.

---

## 1 — Clone / download the project

```bash
git clone <repo-url>
cd msd-integration-test
```

---

## 2 — Map a clean drive letter (Windows only — required if your path contains emoji)

The repo path contains emoji characters (`💼`, `👨‍💻`). Git Bash and some shell
tools mangle Unicode in `PATH`, breaking `git`, `sed`, and venv activation.
Fix it once by mapping the repo root to a short drive letter in PowerShell:

```powershell
subst Z: "e:\OneDrive-IBM\OneDrive - IBM\IBM💼\LEARNING👨‍💻\github\repository\public\learning\lenovo-apps\msd-integration-test\msd-integration-test"
```

> `subst` maps last only for the current Windows session. To make it permanent,
> add it to your PowerShell `$PROFILE` or create a one-line `.bat` in Startup.

**Always use `Z:\` from this point forward** — in Git Bash, PowerShell, and VS Code terminals.

---

## 3 — Create a virtual environment

### Windows (PowerShell) — from the `Z:` drive

```powershell
python3 -m venv .venv
.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
```

---

## 4 — Activate the virtual environment

### Windows — Git Bash (from `Z:\`)

```bash
cd /z
source .venv/Scripts/activate
```

### Windows — PowerShell (from `Z:\`)

```powershell
Z:\.venv\Scripts\Activate.ps1
```

> If you get an execution-policy error, run once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

### Windows — Command Prompt

```cmd
Z:\.venv\Scripts\activate.bat
```

### macOS / Linux

```bash
source .venv/bin/activate
```

Your prompt will change to show `(.venv)` when the environment is active.

---

## 5 — Install dependencies

```bash
pip install -r requirements.txt
```

---

## 6 — Run the scripts

**NGSP (Lenovo portal):**
```bash
python ngsp-login.py
```

**Dynamics 365 (Lenovo CRM):**
```bash
python msd-auto-download.py
```

You will be prompted interactively for:

```
Enter IBM email: you@ibm.com
Enter password:             ← hidden input, nothing is echoed
Enter Work Order number: 4022687737
```

### MFA step

After the password is submitted, the script will pause with:

```
=================================
MANUAL VERIFICATION REQUIRED
Please complete Lenovo MFA manually.
Approve push / OTP / verification.
=================================

Press ENTER after MFA verification is completed...
```

Complete the push notification / OTP in your authenticator app, then press **ENTER** in the terminal to continue.

---

## 7 — Deactivate the virtual environment (when done)

```bash
deactivate
```

---

## 8 — Using Git Bash after the PATH fix

> **First time only:** close any open Git Bash window and open a fresh one so
> the updated Windows PATH (with `Git\usr\bin`) takes effect.

### Every-day workflow

```bash
# 1. navigate via the clean Z: drive (avoids emoji PATH corruption)
cd /z

# 2. activate the venv
source .venv/Scripts/activate

# 3. work normally — git, ls, sed, python all work
git status
git add .
git commit -m "your message"
git push

# run a script
python ngsp-login.py
python msd-auto-download.py

# 4. deactivate when done
deactivate
```

### Make `Z:` available automatically on every reboot

Add the `subst` command to your PowerShell profile so `Z:` is always mapped
when Windows starts:

```powershell
# Open your PowerShell profile in Notepad
notepad $PROFILE
```

Add this line and save:

```powershell
subst Z: "e:\OneDrive-IBM\OneDrive - IBM\IBM💼\LEARNING👨‍💻\github\repository\public\learning\lenovo-apps\msd-integration-test\msd-integration-test"
```

Now every new PowerShell **and** Git Bash window will have `Z:` / `/z` available.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bash: git: command not found` in Git Bash | Emoji in repo path corrupts `$PATH` when venv activates | Map to `Z:` with `subst` and always work from `Z:\` |
| `bash: sed: command not found` | Same root cause as above | Same fix — use `Z:\` |
| `ModuleNotFoundError: No module named 'selenium'` | venv not activated, or `pip install` not run | Activate the venv and re-run `pip install -r requirements.txt` |
| `selenium.common.exceptions.SessionNotCreatedException` | ChromeDriver version mismatch | Update Chrome, or pin `selenium>=4.6` so Selenium Manager auto-downloads the correct driver |
| `TimeoutException` on Login button | The page structure changed | Inspect the button in DevTools and update the XPath in `ngsp-login2.py` |
| Script advances past MFA too quickly | MFA prompt removed by accident | Ensure the `input("Press ENTER …")` line is **not** commented out |
| `rc_select_1` not found | Ant Design re-rendered the component with a new id | The script falls back to any `input[type='search']` inside the header; no action needed |


---

---

# RESOLV DC Updates — `lenovo_resolve_login.py`

Automates login to [Lenovo Resolve](https://resolve-prod.lenovo.com) using IBM ID credentials, solves the Google reCAPTCHA audio challenge automatically, scrapes GTAP RPL Work Orders, and downloads two Excel exports:

- **Extract-AWB** — scraped work order table, exported directly by the script
- **Extract-DC** — GTAP RPL Report Excel, downloaded via the browser

Both files are saved to `files/resolve-auto-download/` under the project root when run through the web app, or to `app/scripts/resolve-auto-download/Extract-AWB` / `Extract-DC` when run directly.

---

## Prerequisites

### Python ≥ 3.9

```powershell
python --version
```

### Python packages

All three packages below are now included in the project [`requirements.txt`](../../../requirements.txt).  
Install them with:

```powershell
pip install -r requirements.txt
```

| Package | PyPI name | Purpose |
|---|---|---|
| `DrissionPage` | `DrissionPage>=4.0.0` | Controls a real Chrome window (no Selenium/WebDriver needed) |
| `pydub` | `pydub>=0.25.1` | Audio segment handling — converts reCAPTCHA MP3 to WAV |
| `SpeechRecognition` | `SpeechRecognition>=3.10.0` | Transcribes the WAV audio challenge via Google Speech API |
| `imageio-ffmpeg` | `imageio-ffmpeg>=0.4.9` | **Bundles a pre-built ffmpeg binary** — no system install needed |

> **`openpyxl`** (already in `requirements.txt`) is also required — used to write the Extract-AWB Excel file.

---

### ffmpeg — Handled automatically via `imageio-ffmpeg` ✅

`imageio-ffmpeg` is a pip package that **ships its own ffmpeg binary** inside the Python environment. This means:

- ✅ No manual ffmpeg install needed on any laptop
- ✅ No PATH setup required
- ✅ Works identically on every machine after `pip install -r requirements.txt`

The script (`RecaptchaSolver.py`) tries `imageio-ffmpeg` first, then falls back to any system-installed ffmpeg if found.

> **`pip install ffmpeg`** (without `imageio`) is a different, unrelated package that does **not** provide a working binary — do not use it.

#### Verify after installing requirements

```powershell
python -c "import imageio_ffmpeg; print('ffmpeg at:', imageio_ffmpeg.get_ffmpeg_exe())"
```

Expected output:
```
ffmpeg at: C:\Users\...\site-packages\imageio_ffmpeg\binaries\ffmpeg-win64-v7.x.exe
```

---

### Google Chrome

`DrissionPage` drives a **real Google Chrome** window — not Chromium, not Edge.  
Download from <https://www.google.com/chrome/>.  
`DrissionPage` manages the matching ChromeDriver version automatically; no separate ChromeDriver download is needed.

---

## Running the script directly

> **Normal usage**: the script is triggered from the web app at **Admin → Data Import / Export → RESOLV DC Update**.  
> Use the direct run below only for testing / debugging.

### 1 — Activate your virtual environment

```powershell
# from the project root
.venv\Scripts\Activate.ps1
```

### 2 — Run

```powershell
cd develop-lenovo-app\app\scripts\resolve-auto-download
python lenovo_resolve_login.py
```

Chrome will open visibly and the script will:

1. Attempt to reuse a saved session from `session/` (skips login if still valid)
2. If session is expired → navigate to the IBM ID login page, solve the reCAPTCHA automatically
3. Scrape the GTAP RPL Work Orders table (all pages) → save to `Extract-AWB/`
4. Navigate to the GTAP RPL Report → click the download button → save to `Extract-DC/`
5. Close Chrome

### 3 — Output files

| Folder | Contents |
|---|---|
| `Extract-AWB/` | Excel file of scraped work orders (named by timestamp) |
| `Extract-DC/` | Downloaded GTAP RPL Report Excel |

Only the 5 most recent files in each folder are kept; older ones are deleted automatically.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'DrissionPage'` | Package not installed | `pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'pydub'` | Package not installed | `pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'speech_recognition'` | Package not installed | `pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'imageio_ffmpeg'` | Package not installed | `pip install imageio-ffmpeg>=0.4.9` |
| `[ffmpeg] WARNING: ffmpeg not found — audio conversion will fail.` | `imageio-ffmpeg` not installed | `pip install imageio-ffmpeg>=0.4.9` |
| `Captcha detected bot behavior` | Google flagged the IP | Wait 10–15 min and retry; or switch network |
| `Audio challenge failed after 3 attempts` | Speech recognition returned wrong text | Usually transient — rerun the script |
| Chrome opens but stays on the login page | Saved session expired and login failed | Delete `session/` folder and rerun |
| `All 5 attempts failed.` | Persistent reCAPTCHA failure | Check internet connection; verify Chrome is up to date |
