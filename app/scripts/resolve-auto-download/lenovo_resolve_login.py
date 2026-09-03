from DrissionPage import ChromiumPage, ChromiumOptions
from RecaptchaSolver import RecaptchaSolver
import threading
import signal
import sys
import time
import re
import os
import glob
from datetime import datetime
from openpyxl import Workbook

# ── Credentials — loaded from shared .env file ────────────────────────────────
# .env lives at:  app/scripts/msd-auto-download/.env
# Keys used:      RESOLVE_USERNAME, RESOLVE_PASSWORD
# When run via the web app, USERNAME/PASSWORD are injected through init_globals
# and override these values before any code here executes.
def _load_env_credentials() -> tuple[str, str]:
    """Read RESOLVE_USERNAME / RESOLVE_PASSWORD from the shared .env file."""
    env_path = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "msd-auto-download", ".env",
        )
    )
    creds = {}
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                creds[_k.strip()] = _v.strip()
    username = creds.get("RESOLVE_USERNAME", "")
    password = creds.get("RESOLVE_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "RESOLVE_USERNAME or RESOLVE_PASSWORD not found in .env.\n"
            f"  Expected file: {env_path}\n"
            "  Add both keys and try again."
        )
    return username, password

USERNAME, PASSWORD = _load_env_credentials()
LOGIN_URL = "https://resolve-prod.lenovo.com/#/login"

# ── Target URLs ────────────────────────────────────────────────────────────────
URL_RPL_GTAP  = "https://resolve-prod.lenovo.com/#/home/rpl-gtap/forward"
URL_GENERATED = "https://resolve-prod.lenovo.com/#/home/rpl-gtap/generated"
URL_REPORT    = "https://resolve-prod.lenovo.com/#/home/report"
URL_GTAP_RPT  = "https://resolve-prod.lenovo.com/#/home/report/gtap-report"

# ── Output directories / session ───────────────────────────────────────────────
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
EXTRACT_AWB_DIR  = os.path.join(BASE_DIR, "Extract-AWB")
EXTRACT_DC_DIR   = os.path.join(BASE_DIR, "Extract-DC")
SESSION_DIR      = os.path.join(BASE_DIR, "session")          # Chrome user-data-dir
SESSION_PROFILE  = os.path.join(SESSION_DIR, "Default")       # Chrome default profile
# Note: makedirs for EXTRACT_AWB_DIR / EXTRACT_DC_DIR are deferred to
# export_to_excel() and move_latest_download() so that init_globals overrides
# (injected by the web app) take effect before the directories are created.
os.makedirs(SESSION_PROFILE, exist_ok=True)

# URLs that mean the session has expired / user is not logged in
LOGGED_OUT_URLS = (
    "https://resolve-prod.lenovo.com/#/",
    "https://resolve-prod.lenovo.com/#/login",
    "https://resolve-prod.lenovo.com/",
)
URL_HOME = "https://resolve-prod.lenovo.com/#/home/work-order/new"

# ── Config ─────────────────────────────────────────────────────────────────────
CAPTCHA_TIMEOUT = 60   # seconds — if reCAPTCHA hangs, kill and retry
MAX_RETRIES     = 5

# ── Chrome launch arguments ────────────────────────────────────────────────────
CHROME_ARGUMENTS = [
    "-no-first-run",
    "-force-color-profile=srgb",
    "-metrics-recording-only",
    "-password-store=basic",
    "-use-mock-keychain",
    "-export-tagged-pdf",
    "-no-default-browser-check",
    "-disable-background-mode",
    "-enable-features=NetworkService,NetworkServiceInProcess",
    "-disable-features=FlashDeprecationWarning",
    "-deny-permission-prompts",
    "-disable-gpu",
    "-accept-lang=en-US",
    "--disable-usage-stats",
    "--disable-crash-reporter",
    "--no-sandbox",
    "--start-maximized",
]


def _write_chrome_prefs() -> None:
    """Write Chrome Preferences file so download dir is always Extract-DC.

    When --user-data-dir is used Chrome reads its prefs from disk and ignores
    set_pref() calls made via ChromiumOptions. Writing the file directly is the
    only reliable way to set the download directory.
    """
    import json
    prefs_path = os.path.join(SESSION_PROFILE, "Preferences")
    # Load existing prefs if present so we don't wipe saved cookies/tokens
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        prefs = {}

    prefs.setdefault("download", {})
    # Pass path as-is — json.dump handles backslash escaping correctly
    prefs["download"]["default_directory"]   = EXTRACT_DC_DIR
    prefs["download"]["prompt_for_download"] = False
    prefs["download"]["directory_upgrade"]   = True
    prefs.setdefault("profile", {})
    prefs["profile"]["default_content_setting_values"] = {"automatic_downloads": 1}

    with open(prefs_path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2)


def make_driver() -> ChromiumPage:
    # Write download prefs into the profile BEFORE Chrome starts so it reads them
    _write_chrome_prefs()

    options = ChromiumOptions()
    for arg in CHROME_ARGUMENTS:
        options.set_argument(arg)
    # Persist cookies/localStorage between runs using a fixed user-data-dir
    options.set_argument(f"--user-data-dir={SESSION_DIR}")
    driver = ChromiumPage(addr_or_opts=options)
    # Maximise window — ensures no buttons are cropped or hidden
    driver.set.window.max()
    return driver


def move_latest_download(src_dir: str, dst_dir: str, after_time: float) -> str | None:
    """Move the most recently downloaded file from src_dir to dst_dir.

    Only considers files whose mtime >= after_time (seconds since epoch).
    Skips partial Chrome downloads (.crdownload, .tmp) and waits for the
    file to stop growing before moving it.
    Falls back to searching ~/Downloads if nothing found in src_dir.
    Returns the destination path or None.
    """
    # Extensions Chrome uses for in-progress downloads — never move these
    _PARTIAL_SUFFIXES = (".crdownload", ".tmp", ".part")

    def _find(directory: str) -> str | None:
        candidates = [
            f for f in glob.glob(os.path.join(directory, "*"))
            if not any(f.endswith(s) for s in _PARTIAL_SUFFIXES)
            and os.path.isfile(f)
            and os.path.getmtime(f) >= after_time - 2
        ]
        return max(candidates, key=os.path.getmtime) if candidates else None

    found = _find(src_dir)

    # Fallback: Chrome ignored our prefs and saved to ~/Downloads
    if not found:
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        found = _find(downloads_dir)
        if found:
            print(f"[!] File landed in Downloads (pref ignored) — moving to Extract-DC...")

    if not found:
        return None

    # ── Wait for file to stabilise before moving ─────────────────────────
    # Chrome finishes writing the .xlsx and then renames the temp file.
    # Poll size every second; once it stays identical for 3 consecutive
    # checks (and size > 0) the file is fully flushed to disk.
    stable_count = 0
    prev_size    = -1
    stability_deadline = time.time() + 30
    while time.time() < stability_deadline:
        try:
            cur_size = os.path.getsize(found)
        except OSError:
            time.sleep(1)
            continue
        if cur_size > 0 and cur_size == prev_size:
            stable_count += 1
            if stable_count >= 3:
                break
        else:
            stable_count = 0
        prev_size = cur_size
        time.sleep(1)
    print(f"[+] File stable at {prev_size:,} bytes — proceeding to move.")

    filename = os.path.basename(found)
    dst_path = os.path.join(dst_dir, filename)
    # Avoid overwriting if same name already exists
    if os.path.exists(dst_path):
        base, ext = os.path.splitext(filename)
        dst_path = os.path.join(dst_dir, f"{base}_{int(after_time)}{ext}")

    os.rename(found, dst_path)
    return dst_path


def set_zoom(driver: ChromiumPage, percent: int = 50) -> None:
    """Apply CSS zoom on the page body so content fits without scrolling."""
    driver.run_js(f"document.body.style.zoom = '{percent}%';")


def keep_latest_files(directory: str, keep: int = 5) -> None:
    """Delete oldest Excel files in directory, keeping only the `keep` most recent."""
    files = sorted(
        [f for f in glob.glob(os.path.join(directory, "*.xlsx")) if os.path.isfile(f)],
        key=os.path.getmtime,
        reverse=True,   # newest first
    )
    for old_file in files[keep:]:
        try:
            os.remove(old_file)
            print(f"[cleanup] Deleted old file: {os.path.basename(old_file)}")
        except OSError as e:
            print(f"[cleanup] Could not delete {old_file}: {e}")


def is_session_valid(driver: ChromiumPage) -> bool:
    """Navigate to the home page and check if the session is still alive.

    Returns True  → already logged in, landed on #/home/...
    Returns False → redirected to #/ or #/login (session expired)
    """
    print("[*] Checking saved session...")
    driver.get(URL_HOME)
    # Give Angular time to redirect if session is invalid
    time.sleep(4)
    current = driver.url.rstrip("/")
    print(f"    Current URL: {current}")

    # Landed on home = session valid
    if "/home/" in current:
        set_zoom(driver)
        print("[+] Session is valid — skipping login.")
        return True

    # Any logged-out URL = session expired
    print("[!] Session expired or not found — will re-login.")
    return False


def clear_session(driver: ChromiumPage) -> None:
    """Clear cookies and localStorage so a stale session doesn't block re-login."""
    try:
        driver.get(LOGIN_URL)
        time.sleep(1)
        driver.run_js("localStorage.clear(); sessionStorage.clear();")
        driver.run_js(
            "document.cookie.split(';').forEach(c => {"
            "  document.cookie = c.trim().split('=')[0]"
            "    + '=;expires=Thu, 01 Jan 1970 00:00:00 UTC;path=/';"
            "});"
        )
        print("[+] Cleared expired session data.")
    except Exception as e:
        print(f"[!] Could not clear session (non-fatal): {e}")


# XPath selectors that do NOT rely on the PrimeNG dynamic table id (pr_id_N).
# The id changes on every re-render / session, so we match by structural
# position inside the paginator's nearest ancestor table instead.
_XPATH_ROWS     = 'xpath://p-paginator/ancestor::div[1]//table//tbody/tr'
_XPATH_FIRST_DC = 'xpath://p-paginator/ancestor::div[1]//table//tbody/tr[1]/td[2]'


def scrape_all_pages(driver: ChromiumPage) -> list[dict]:
    """Scrape all rows from the Generated DC's table across all pages."""
    all_rows = []
    page_num  = 1

    while True:
        # Wait for table rows to be present (id-independent selector)
        driver.wait.ele_displayed(_XPATH_ROWS, timeout=15)

        # Read paginator text
        paginator = driver.ele(
            'xpath://span[contains(@class,"p-paginator-current")]', timeout=10
        )
        pag_text = str(paginator.text) if paginator else ""
        print(f"[scrape] Page {page_num}: {pag_text}")

        # Collect all <tr> rows in tbody
        rows = driver.eles(_XPATH_ROWS, timeout=10)
        for row in rows:
            cells = row.eles('tag:td')
            if len(cells) >= 5:
                all_rows.append({
                    "DC_NO":              str(cells[1].text),
                    "AWB_NO":             str(cells[2].text),
                    "Status":             str(cells[3].text),
                    "DC_Generation_Date": str(cells[4].text),
                })

        # Check if Next button is available (not disabled)
        next_btn = driver.ele(
            'xpath://button[contains(@class,"p-paginator-next")'
            ' and not(contains(@class,"p-disabled"))]',
            timeout=3,
        )
        if not next_btn:
            print(f"[scrape] Last page reached. Total rows: {len(all_rows)}")
            break

        # Read the first DC cell on the current page so we can detect the change
        first_cell_before = str(
            driver.ele(_XPATH_FIRST_DC, timeout=5).text
        )

        # Click Next via JS (avoids zoom/hit-test issues)
        driver.run_js("arguments[0].click();", next_btn)

        # Wait until the first row actually changes — much faster than a fixed sleep
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                first_cell_now = str(
                    driver.ele(_XPATH_FIRST_DC, timeout=2).text
                )
                if first_cell_now != first_cell_before:
                    break
            except Exception:
                pass
            time.sleep(0.3)

        page_num += 1

    return all_rows


def export_to_excel(rows: list[dict]) -> str:
    """Write rows to an Excel file in the Extract-AWB directory.

    Returns the full path of the saved file.
    """
    out_dir = EXTRACT_AWB_DIR
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(out_dir, f"Generated_DCs_{timestamp}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "Generated DCs"

    # Header
    headers = ["DC NO", "AWB NO", "Status", "DC Generation Date"]
    ws.append(headers)

    # Style header row bold
    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)

    # Data rows
    for row in rows:
        ws.append([
            row["DC_NO"],
            row["AWB_NO"],
            row["Status"],
            row["DC_Generation_Date"],
        ])

    # Auto-fit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max_len + 4

    wb.save(filepath)
    return filepath


def solve_captcha_with_timeout(solver: RecaptchaSolver) -> bool:
    """Run solveCaptcha() in a thread; return False if it exceeds CAPTCHA_TIMEOUT."""
    result = {"ok": False, "error": None}

    def _run():
        try:
            solver.solveCaptcha()
            result["ok"] = True
        except Exception as e:
            result["error"] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=CAPTCHA_TIMEOUT)

    if t.is_alive():
        print(f"[!] reCAPTCHA timed out after {CAPTCHA_TIMEOUT}s.")
        return False
    if not result["ok"]:
        raise Exception(result["error"])
    return True


def attempt_login(driver: ChromiumPage) -> bool:
    solver = RecaptchaSolver(driver)

    # 1. Open login page
    print("[*] Opening login page...")
    driver.get(LOGIN_URL)

    # 2. Fill username
    print("[*] Filling username...")
    driver.ele('xpath://input[@formcontrolname="username"]', timeout=15).input(USERNAME, clear=True)

    # 3. Fill password
    print("[*] Filling password...")
    driver.ele('xpath://input[@formcontrolname="password"]', timeout=10).input(PASSWORD, clear=True)

    # 4. Solve reCAPTCHA (with timeout)
    print("[*] Solving reCAPTCHA...")
    t0 = time.time()
    if not solve_captcha_with_timeout(solver):
        return False   # caller will restart Chrome
    print(f"[+] reCAPTCHA solved in {time.time() - t0:.2f}s")

    # 5. Wait 5s then click Login
    for remaining in range(5, 0, -1):
        print(f"    Clicking Login in {remaining}s...", end="\r")
        time.sleep(1)
    print("[*] Clicking Login now...            ")

    driver.ele('xpath://button[@type="submit" and contains(.,"Login")]', timeout=10).click()
    print("[+] Login submitted.")

    # 6. Wait for Angular to redirect away from login page
    print("[*] Waiting for home page...")
    driver.wait.url_change(LOGIN_URL, timeout=20)
    time.sleep(3)
    set_zoom(driver)
    print(f"[+] Arrived at: {driver.url}")

    run_work_steps(driver)
    return True


def run_work_steps(driver: ChromiumPage) -> None:
    """All post-login steps: navigate, scrape, download report, close Chrome.

    Called both from a fresh login and from a restored session.
    """
    # 1. Click GTAP RPL Work Orders menu
    print("[*] Clicking 'GTAP RPL Work Orders'...")
    driver.ele(
        'xpath://span[@class="menu-text" and contains(.,"GTAP RPL Work Orders")]',
        timeout=15,
    ).click()
    print("[+] Navigating to GTAP RPL page...")

    # 2. Wait for rpl-gtap/forward + 5s for table render
    driver.wait.url_change(URL_RPL_GTAP, timeout=15)
    time.sleep(5)
    set_zoom(driver)
    print(f"[+] Arrived at: {driver.url}")

    # 3. Click "Generated DC's" tab
    print("[*] Clicking 'Generated DC\\'s' tab...")
    driver.ele(
        'xpath://button[contains(@class,"nav-link") and contains(.,"Generated DC")]',
        timeout=15,
    ).click()
    print("[+] Clicked Generated DC's tab.")
    time.sleep(3)
    set_zoom(driver)
    print(f"[+] Arrived at: {driver.url}")

    # 4. Open page-size dropdown and select 100 rows — retry up to 3x
    for _try in range(3):
        print(f"[*] Setting 100 rows per page (attempt {_try + 1})...")

        # Click the dropdown trigger via JS to avoid hit-test failures from zoom
        paginator_area = driver.ele('xpath://p-paginator', timeout=15)
        trigger = paginator_area.ele(
            'xpath:.//div[@role="button" and @aria-label="dropdown trigger"]',
            timeout=10,
        )
        driver.run_js("arguments[0].click();", trigger)
        time.sleep(1.5)

        # Click the "100" option via JS
        option_100 = driver.ele(
            'xpath://li[@role="option" and @aria-label="100"]',
            timeout=8,
        )
        if option_100:
            driver.run_js("arguments[0].click();", option_100)
        time.sleep(3)

        # Verify paginator shows 100 rows
        paginator = driver.ele(
            'xpath://span[contains(@class,"p-paginator-current")]',
            timeout=10,
        )
        pag_text = paginator.text if paginator else ""
        print(f"[+] Paginator: {pag_text}")
        if "to 100" in pag_text or "of 100" in pag_text:
            break
        print("[!] 100 rows not confirmed yet — retrying dropdown...")
        time.sleep(1)

    # 7. Scrape all pages
    print("\n[*] Starting full table scrape across all pages...")
    all_data = scrape_all_pages(driver)
    print(f"[+] Scraped {len(all_data)} total rows.")

    # 8. Export to Excel in Extract-AWB, then keep only 5 latest files
    print("[*] Exporting to Excel...")
    excel_path = export_to_excel(all_data)
    print(f"[+] Saved to: {excel_path}")
    keep_latest_files(EXTRACT_AWB_DIR)

    # 9. Click the "Report" menu item
    print("\n[*] Clicking 'Report' menu...")
    driver.ele(
        'xpath://span[@class="menu-text" and contains(.,"Report")]',
        timeout=15,
    ).click()
    driver.wait.url_change(URL_GENERATED, timeout=15)
    time.sleep(2)
    set_zoom(driver)
    print(f"[+] Arrived at: {driver.url}")

    # 10. Click "GTAP RPL Report" tab
    print("[*] Clicking 'GTAP RPL Report' tab...")
    driver.ele(
        'xpath://button[contains(@class,"nav-link") and contains(.,"GTAP RPL Report")]',
        timeout=15,
    ).click()
    time.sleep(2)
    set_zoom(driver)
    print(f"[+] Arrived at: {driver.url}")

    # 11. Click the Excel download button
    print("[*] Clicking Excel download button...")
    driver.ele(
        'xpath://button[contains(@class,"p-button-outlined") and contains(@class,"p-button-secondary") and .//span[contains(@class,"ri-download-2-line")]]',
        timeout=15,
    ).click()
    print("[+] Download triggered.")

    # 12. Wait for the downloaded file — check Extract-DC first, fallback to ~/Downloads
    print("[*] Waiting for file to download...")
    click_time = time.time()
    deadline   = click_time + 60
    downloaded_file = None
    while time.time() < deadline:
        downloaded_file = move_latest_download(EXTRACT_DC_DIR, EXTRACT_DC_DIR, click_time)
        if downloaded_file:
            print(f"[+] File saved to: {downloaded_file}")
            break
        time.sleep(1)

    if downloaded_file:
        keep_latest_files(EXTRACT_DC_DIR)
    else:
        print("[!] Warning: download did not complete within 60s — check Extract-DC / Downloads manually.")

    # 13. Close Chrome
    # Wait 3 s to let any in-progress download finish writing to disk,
    # then dismiss the "Download is in progress — Exit anyway?" dialog
    # that Chrome shows when a file is still being saved.
    print("\n[*] Waiting 3s before closing Chrome...")
    time.sleep(3)
    print("[*] Closing Chrome...")
    # Use quit() instead of close() — quit() sends a SIGTERM / taskkill to the
    # Chrome process directly, bypassing the "Download is in progress" OS dialog
    # that blocks close().  All tabs and the browser process are terminated.
    try:
        driver.quit()
    except Exception:
        # Fallback: if quit() isn't available on this DrissionPage version, use
        # close() and then kill any lingering Chrome PID on the session profile.
        try:
            driver.close()
        except Exception:
            pass
        # Give Chrome 1 s to exit on its own, then force-kill if still running
        time.sleep(1)
        try:
            import psutil as _ps
            session = SESSION_DIR
            for _proc in _ps.process_iter(["name", "cmdline"]):
                try:
                    if "chrome" not in (_proc.info["name"] or "").lower():
                        continue
                    for _arg in (_proc.info["cmdline"] or []):
                        if "--user-data-dir=" in _arg and os.path.normcase(
                            os.path.normpath(_arg.split("=", 1)[1])
                        ) == os.path.normcase(os.path.normpath(session)):
                            _proc.kill()
                            break
                except Exception:
                    pass
        except ImportError:
            pass
    print("[+] Done.")


# ── Graceful shutdown on Ctrl+C ────────────────────────────────────────────────
# Closes Chrome without touching the session/ directory so the next run
# can reuse the saved session.
_driver_ref: ChromiumPage | None = None

def _shutdown(signum=None, frame=None) -> None:
    print("\n[!] Interrupted — closing Chrome (session kept)...")
    if _driver_ref is not None:
        try:
            _driver_ref.close()
        except Exception:
            pass
    sys.exit(0)

# Only register signal handlers when running on the main thread
# (i.e. direct execution).  When launched via runpy from a Flask
# background thread, signal.signal() raises ValueError and is skipped.
import threading as _threading
if _threading.current_thread() is _threading.main_thread():
    signal.signal(signal.SIGINT,  _shutdown)   # Ctrl+C
    signal.signal(signal.SIGTERM, _shutdown)   # kill / task-manager

# ── Main entry point ───────────────────────────────────────────────────────────
driver  = make_driver()
_driver_ref = driver      # give the shutdown handler access
success = False

try:
    # ── Step A: Try saved session first ───────────────────────────────────────
    if is_session_valid(driver):
        try:
            run_work_steps(driver)
            success = True
        except Exception as e:
            print(f"[!] Work steps failed on saved session: {e}")
            try:
                driver.close()
            except Exception:
                pass
            driver = None
            _driver_ref = None

    # ── Step B: Re-login if session was invalid or work steps failed ──────────
    if not success:
        for attempt in range(1, MAX_RETRIES + 1):
            print(f"\n{'='*50}")
            print(f"[*] Login attempt {attempt}/{MAX_RETRIES}")
            print(f"{'='*50}")

            if driver is None:
                driver = make_driver()
                _driver_ref = driver

            clear_session(driver)

            try:
                if attempt_login(driver):
                    success = True
                    break
                else:
                    print("[!] Captcha timed out — restarting Chrome...")
            except Exception as e:
                print(f"[!] Attempt {attempt} failed: {e}")

            try:
                driver.close()
            except Exception:
                pass
            driver = None
            _driver_ref = None
            if attempt < MAX_RETRIES:
                print("[*] Retrying in 3 seconds...")
                time.sleep(3)

except KeyboardInterrupt:
    _shutdown()

if success:
    print("\n[+] All steps completed successfully!")
else:
    print(f"\n[!] All {MAX_RETRIES} attempts failed.")
    if driver is not None:
        try:
            driver.close()
        except Exception:
            pass
