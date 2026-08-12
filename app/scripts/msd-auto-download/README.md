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
