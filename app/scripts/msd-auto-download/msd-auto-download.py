import glob
import os
import pathlib
import shutil
import sys
import time as _time_mod
from datetime import datetime as _dt
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time
from dotenv import load_dotenv

load_dotenv()

# =====================================
# CONFIG
# =====================================
LOGIN_URL = (
    "https://lenovo-plrs-prod.crm5.dynamics.com/main.aspx?"
    "appid=00fd771a-9081-e911-a83a-000d3a07fba2&"
    "cmdbar=true&"
    "forceUCI=1&"
    "pagetype=entityrecord&"
    "etn=contact"
)

MAIN_URL = "https://lenovo-plrs-prod.crm5.dynamics.com/main.aspx?"

EMAIL    = os.getenv("DYNAMICS_EMAIL", "")
PASSWORD = os.getenv("DYNAMICS_PASSWORD", "")

# =====================================
# CHROME SETUP
# =====================================

# Download destination — use the user's standard Downloads folder
DOWNLOADS_DIR = os.getenv(
    "DOWNLOADS_DIR",
    r"C:\Users\mifta\Downloads"
)

# Use a dedicated Chrome profile directory for Selenium to persist cookies/login session.
# We store it in the user's home directory (e.g., C:\Users\username\.selenium_chrome_profile)
# to avoid OneDrive sync locks, path spaces, or emoji characters in the workspace folder.
SELENIUM_PROFILE_DIR = os.getenv(
    "SELENIUM_PROFILE_DIR",
    str(pathlib.Path.home() / ".selenium_chrome_profile")
)

# ── Clean up stale lock files left by a previous Ctrl+C or crash ──────────────
# Chrome writes a SingletonLock (and SingletonCookie / SingletonSocket) into the
# profile folder while it is running.  If the process was killed without a clean
# shutdown those files remain and prevent the next Chrome instance from starting
# (crashes with "DevToolsActivePort file doesn't exist").
_profile_path = pathlib.Path(SELENIUM_PROFILE_DIR)
for _lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
    _lock_file = _profile_path / _lock
    if _lock_file.exists() or _lock_file.is_symlink():
        try:
            _lock_file.unlink()
            print(f"Removed stale lock: {_lock_file}")
        except Exception as _e:
            print(f"Warning: could not remove {_lock_file}: {_e}")


def _clear_session_cache(profile_dir: str) -> None:
    """Delete only the session/auth files from *profile_dir* so the next
    Chrome launch gets a clean redirect to the Microsoft login page.

    The rest of the profile (extensions, preferences, etc.) is left intact.
    Must be called AFTER driver.quit() so Chrome has released all file handles.
    """
    profile = pathlib.Path(profile_dir)
    # Files that hold the Microsoft/Dynamics session tokens
    session_files = [
        profile / "Default" / "Cookies",
        profile / "Default" / "Login Data",
        profile / "Default" / "Login Data-journal",
        profile / "Default" / "Web Data",
        profile / "Default" / "Network" / "Cookies",
        profile / "Default" / "Network" / "Cookies-journal",
        # Trust-tokens and related auth state
        profile / "Default" / "Trust Tokens",
        profile / "Default" / "Shared Dictionary" / "db",
    ]
    removed = []
    for f in session_files:
        if f.exists():
            try:
                f.unlink()
                removed.append(f.name)
            except Exception as _e:
                print(f"Warning: could not remove session file {f.name}: {_e}")
    if removed:
        print(f"Session cache cleared ({', '.join(removed)}) — next launch will re-authenticate.")
    else:
        print("No session cache files found to clear.")


def _kill_chrome_on_profile(profile_dir: str) -> None:
    """Terminate any Chrome process that is using *profile_dir* as its
    user-data-dir.  This closes the window WITHOUT wiping the profile on disk,
    so cookies and the saved session are preserved for the next launch.

    Called automatically by _make_driver() before spawning a new Chrome so
    that two instances never race on the same profile (which causes the
    'DevToolsActivePort file doesn't exist' crash).
    """
    try:
        import psutil
    except ImportError:
        print("Warning: psutil not installed — cannot auto-close previous Chrome window.")
        return

    # Normalise to lowercase for case-insensitive comparison on Windows
    target = os.path.normcase(os.path.normpath(profile_dir))
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "chrome" not in name:
                continue
            cmdline = proc.info["cmdline"] or []
            for arg in cmdline:
                if "--user-data-dir=" in arg:
                    arg_path = os.path.normcase(
                        os.path.normpath(arg.split("=", 1)[1])
                    )
                    if arg_path == target:
                        print(f"Closing previous Chrome window (pid {proc.pid})...")
                        proc.terminate()
                        killed += 1
                        break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if killed:
        # Give Chrome a moment to release the profile lock files
        _time_mod.sleep(2)
        # Remove any residual lock files the terminated process left behind
        for _lk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            _lf = _profile_path / _lk
            if _lf.exists() or _lf.is_symlink():
                try:
                    _lf.unlink()
                except Exception:
                    pass
        print(f"Previous Chrome window closed ({killed} process(es) terminated).")
    else:
        print("No previous Chrome window found on this profile.")

options = Options()
options.add_argument("--start-maximized")
options.add_argument("--force-device-scale-factor=0.6")
options.add_argument(f"--user-data-dir={SELENIUM_PROFILE_DIR}")

# No "detach" — on normal exit the browser closes; on Ctrl+C only the window
# is closed so the Chrome profile (cookies/session) is preserved on disk.
options.add_experimental_option("prefs", {
    "download.default_directory":         DOWNLOADS_DIR,
    "download.prompt_for_download":       False,
    "download.directory_upgrade":         True,
    "safebrowsing.enabled":               True,
})

def _make_driver():
    """Create a fresh Chrome driver using the shared options.
    Kills any existing Chrome process on the same profile first (so the new
    instance never crashes with 'DevToolsActivePort file doesn't exist'),
    without deleting the profile data (cookies/session are preserved).
    Returns (driver, wait) tuple.
    """
    _kill_chrome_on_profile(SELENIUM_PROFILE_DIR)
    _drv = webdriver.Chrome(options=options)
    _wt  = WebDriverWait(_drv, 30)
    return _drv, _wt

driver, wait = _make_driver()

DYNAMICS_HOST = "lenovo-plrs-prod.crm5.dynamics.com"


# =====================================
# HELPER: Wait for Visible Element (Handles React/Fluent UI animations)
# =====================================
def wait_for_visible_element(xpath: str, timeout: int = 30):
    """
    Robustly waits for at least one element matching `xpath` to exist in DOM,
    then dynamically polls until that element is physically visible (is_displayed() == True).
    Gracefully handles fade-in/fade-out transitions.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Check presence in DOM first
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, xpath)))
            elements = driver.find_elements(By.XPATH, xpath)
            for el in elements:
                if el.is_displayed():
                    return el
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutException(f"Timed out waiting for a visible element matching: {xpath}")


# =====================================
# HELPER: navigate to Work Orders
# =====================================
def open_work_orders():
    # Force window to maximize and bring to focus to prevent OS background/minimized sleep
    try:
        driver.maximize_window()
        driver.switch_to.window(driver.current_window_handle)
        print("Chrome window maximized and focused.")
    except Exception as e:
        print(f"Warning focusing window: {e}")

    print("Navigating to Dynamics main page...")
    driver.get(MAIN_URL)

    # Wait for the app shell to load
    time.sleep(5)

    print("Looking for Work Orders button...")

    work_orders_btn = wait_for_visible_element(
        "//*[normalize-space()='Work Orders' and (self::span or self::div or self::li or self::a)]",
        timeout=30
    )

    print(f"Work Orders button found: '{work_orders_btn.text.strip()}'")

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        work_orders_btn
    )

    time.sleep(1)

    driver.execute_script(
        "arguments[0].click();",
        work_orders_btn
    )

    print("Work Orders clicked.")

    # Wait for the Work Orders list page to load
    time.sleep(5)

    print("Current URL:")
    print(driver.current_url)

    # =====================================
    # CLICK VIEW SELECTOR BUTTON
    # =====================================
    # The split-button that shows the current view name (e.g.
    # "[new] WO Header Status - Labor Vendor Updated - ID").
    # We locate it by the splitbuttonprimary automation id.
    print("Looking for view selector button...")

    view_selector = wait_for_visible_element(
        "//*[@data-automationid='splitbuttonprimary']",
        timeout=30
    )

    print(f"View selector found: '{view_selector.text.strip()}'")

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        view_selector
    )
    time.sleep(1)
    driver.execute_script("arguments[0].click();", view_selector)

    print("View selector clicked. Waiting for dropdown...")
    time.sleep(2)

    # =====================================
    # SELECT "WO Status - IBM TLS MSD" FROM DROPDOWN
    # =====================================
    print("Looking for 'WO Status - IBM TLS MSD' option...")

    miftah_option = wait_for_visible_element(
        "//span[contains(@class,'ms-ContextualMenu-itemText') and normalize-space()='WO Status - IBM TLS MSD']",
        timeout=15
    )

    print("'WO Status - IBM TLS MSD' option found. Clicking...")

    driver.execute_script("arguments[0].click();", miftah_option)

    print("'WO Status - IBM TLS MSD' selected.")

    # =====================================
    # WAIT FOR DATA TO LOAD
    # =====================================
    # Wait until the grid rows are visible — indicates data has loaded.
    print("Waiting for data to load...")

    WebDriverWait(driver, 60).until(
        EC.presence_of_element_located(
            (
                By.XPATH,
                "//*[contains(@class,'ms-DetailsList') or "
                "contains(@class,'ag-row') or "
                "contains(@role,'row')]"
            )
        )
    )

    # Settle time so all rows render completely before clicking export
    print("Grid detected. Waiting 30 seconds for all data rows to fully render...")
    for remaining in range(30, 0, -1):
        if remaining % 10 == 0 or remaining == 1:
            mins, secs = divmod(remaining, 60)
            label = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            print(f"  Waiting for data rows to render... {label} remaining")
        time.sleep(1)
    print("Data fully loaded.")

    # =====================================
    # CLICK "Export to Excel" (command bar button)
    # =====================================
    # The visible label and icon live inside aria-hidden spans — those are
    # not clickable themselves.  We locate the button by finding the img with
    # alt="Export to Excel" and walking UP to the nearest ancestor that is
    # actually a button / has role="button" / has a click handler (li or a).
    print("Looking for 'Export to Excel' button...")

    export_btn = wait_for_visible_element(
        "//img[@alt='Export to Excel']/ancestor::*[@role='button' or self::button or self::a or self::li][1]",
        timeout=30
    )

    print(f"'Export to Excel' button found (tag={export_btn.tag_name}). Clicking...")

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});",
        export_btn
    )
    time.sleep(1)
    driver.execute_script("arguments[0].click();", export_btn)

    print("Export to Excel clicked. Waiting for download to complete...")

    # ── Snapshot what already exists so we only detect NEW files
    _before = set(glob.glob(f"{DOWNLOADS_DIR}/*"))

    download_timeout = 120   # seconds — large exports can take a while
    poll_secs        = 3
    elapsed          = 0
    downloaded_file  = None

    _last_progress_print = 0
    while elapsed < download_timeout:
        time.sleep(poll_secs)
        elapsed += poll_secs

        # All files currently in the folder
        after = set(glob.glob(f"{DOWNLOADS_DIR}/*"))
        new_files = after - _before

        # Also rescan system Downloads in case Chrome ignored the prefs
        system_downloads = str(pathlib.Path.home() / "Downloads")
        if system_downloads != DOWNLOADS_DIR:
            sys_new = {
                f for f in glob.glob(f"{system_downloads}/*")
                if pathlib.Path(f).stat().st_mtime > (time.time() - elapsed - 5)
            }
            new_files |= sys_new

        for f in new_files:
            # Skip partial Chrome downloads
            if f.endswith(".crdownload") or f.endswith(".tmp"):
                print(f"  Partial download in progress: {pathlib.Path(f).name}")
                continue
            # Accept .xlsx or .zip (Dynamics sometimes zips the export)
            if f.endswith(".xlsx") or f.endswith(".zip") or f.endswith(".csv"):
                downloaded_file = f
                break

        if downloaded_file:
            break

        # Print progress at most once every 15 seconds
        if elapsed - _last_progress_print >= 15:
            _last_progress_print = elapsed
            elapsed_m, elapsed_s = divmod(elapsed, 60)
            total_m,  total_s   = divmod(download_timeout, 60)
            e_label = f"{elapsed_m}m {elapsed_s:02d}s" if elapsed_m else f"{elapsed_s}s"
            t_label = f"{total_m}m {total_s:02d}s"     if total_m  else f"{total_s}s"
            print(f"  Waiting for download... ({e_label} / {t_label})")

    if downloaded_file:
        fname = pathlib.Path(downloaded_file).name
        print(f"Export complete. File saved: {downloaded_file}")
        
        # Move the file to the project MSD output directory
        SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
        TARGET_DIR = SCRIPT_DIR.parent.parent.parent / "files" / "msd-auto-download"
        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        
        dest_file = TARGET_DIR / fname
        try:
            shutil.move(downloaded_file, dest_file)
            print(f"File successfully moved to local workspace: {dest_file}")
            print(f"✅ Work Orders exported & moved to local workspace: {fname}")
        except Exception as move_err:
            print(f"Error moving file: {move_err}")
            print(f"✅ Work Orders exported: {fname} (failed to move: {move_err})")

        # Rolling retention: keep only 5 newest files in local workspace Downloads
        try:
            files = [f for f in TARGET_DIR.glob("*") if f.is_file()]
            files.sort(key=lambda x: x.stat().st_mtime)
            if len(files) > 5:
                num_to_delete = len(files) - 5
                for i in range(num_to_delete):
                    old_file = files[i]
                    print(f"Removing oldest file (rolling retention): {old_file.name}")
                    old_file.unlink()
        except Exception as retention_err:
            print(f"Error in rolling retention: {retention_err}")
    else:
        # Last resort — check if a .crdownload is still active (file exists but not done)
        partial = [f for f in glob.glob(f"{DOWNLOADS_DIR}/*") if f.endswith(".crdownload")]
        if partial:
            print("Download started but still in progress after timeout.")
            print("⚠️ Export file is still downloading — check the Downloads folder.")
        else:
            print("Export started but no download file detected.")
            print("⚠️ Export to Excel clicked but no download file was found.")

            # ── Fallback: scan C:\Users\mifta\Downloads for a matching Excel file ──
            # "Match" means the first 10 whitespace-separated words of the stem are
            # the same as those of any file already in the workspace Downloads folder.
            print("Attempting fallback: scanning system Downloads for a matching Excel file...")

            SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
            TARGET_DIR = SCRIPT_DIR.parent.parent.parent / "files" / "msd-auto-download"
            TARGET_DIR.mkdir(parents=True, exist_ok=True)

            # Build a set of first-10-word prefixes from files already in TARGET_DIR
            def _first10(name: str) -> str:
                return " ".join(pathlib.Path(name).stem.split()[:10]).lower()

            existing_prefixes = {_first10(f.name) for f in TARGET_DIR.glob("*") if f.is_file()}

            fallback_src = pathlib.Path(r"C:\Users\mifta\Downloads")
            fallback_found = None

            if fallback_src.exists():
                candidates = sorted(
                    fallback_src.glob("*.xlsx"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True  # newest first
                )
                for candidate in candidates:
                    if _first10(candidate.name) in existing_prefixes:
                        fallback_found = candidate
                        print(f"Fallback match found: {candidate.name}")
                        break

            if fallback_found:
                dest_file = TARGET_DIR / fallback_found.name
                try:
                    shutil.copy2(str(fallback_found), str(dest_file))
                    print(f"Fallback file copied to workspace: {dest_file}")
                except Exception as copy_err:
                    print(f"Error copying fallback file: {copy_err}")

                # Rolling retention: keep only 5 newest files
                try:
                    files = [f for f in TARGET_DIR.glob("*") if f.is_file()]
                    files.sort(key=lambda x: x.stat().st_mtime)
                    if len(files) > 5:
                        for old_file in files[:len(files) - 5]:
                            print(f"Removing oldest file (rolling retention): {old_file.name}")
                            old_file.unlink()
                except Exception as retention_err:
                    print(f"Error in rolling retention: {retention_err}")
            else:
                print("No matching Excel file found in system Downloads. Skipping fallback.")

# =====================================
# LOGIN FUNCTION  (called at startup and after session expiry)
# =====================================
def _is_session_expired() -> bool:
    """Return True if the current browser page indicates the Dynamics session
    has expired — either by redirecting to microsoftonline.com OR by showing
    the 'Sign in to continue' overlay on the Dynamics domain itself."""
    try:
        current_url = driver.current_url
    except Exception:
        return False
    if "microsoftonline.com" in current_url:
        return True
    if current_url.startswith(f"https://{DYNAMICS_HOST}"):
        # Check for the "Sign in to continue" / "Sign in" modal overlay
        for xpath in (
            # Title text of the dialog
            "//*[contains(@class,'ms-Dialog') or contains(@class,'dialog')]"
            "//*[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'))='sign in to continue'"
            " or normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'))='sign in']",
            # Primary "Sign in" action button inside the overlay
            "//button[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            " 'abcdefghijklmnopqrstuvwxyz'))='sign in']",
        ):
            try:
                el = driver.find_element(By.XPATH, xpath)
                if el.is_displayed():
                    return True
            except Exception:
                pass
    return False


def do_login():
    """Navigate to LOGIN_URL and complete the full Microsoft login flow.
    Safe to call multiple times on the same driver/Chrome instance."""

    # ── Navigate ────────────────────────────────────────────────────────
    print("Opening Dynamics URL...")
    driver.get(LOGIN_URL)

    # Poll until page settles — either lands on Dynamics or redirects to login
    print("Waiting for page to settle...")
    for _ in range(30):
        time.sleep(1)
        current_url = driver.current_url
        if "microsoftonline.com" in current_url or current_url.startswith(f"https://{DYNAMICS_HOST}"):
            break

    current_url = driver.current_url
    print(f"Landed on: {current_url}")

    # ── Already logged in — but check for "Sign in to continue" overlay ─
    # When the cached session has expired, Dynamics can load at the DYNAMICS_HOST
    # URL but immediately show a modal overlay asking to sign in.
    # _is_session_expired() catches both that overlay and a full MS-login redirect.
    if current_url.startswith(f"https://{DYNAMICS_HOST}"):
        if _is_session_expired():
            print("⚠️  'Sign in to continue' overlay detected — session has expired.")
            print("Navigating directly to Microsoft login page to force re-authentication...")
            driver.get(
                "https://login.microsoftonline.com/5c7d0b28-bdf8-410c-aa93-4df372b16203/"
                "oauth2/authorize?client_id=00000007-0000-0000-c000-000000000000"
                "&response_type=code%20id_token&scope=openid%20profile"
                "&redirect_uri=https%3A%2F%2Fsg1--apjcrmlivesg614.crm5.dynamics.com%2F"
                "&response_mode=form_post&sso_reload=true"
            )
            # Fall through to the full login flow below
        else:
            print("Already authenticated. No login required.")
            print("✅ Dynamics session already active — starting download loop.")
            return

    # ── Full login flow ──────────────────────────────────────────────────
    print("Redirected to login page. Starting login flow...")

    # =====================================
    # INPUT EMAIL
    # =====================================
    print("Finding email field...")

    email_box = wait.until(
        EC.visibility_of_element_located(
            (
                By.XPATH,
                "//input[@type='email' or @name='loginfmt' or @id='i0116']"
            )
        )
    )

    print("Email field found.")

    driver.execute_script("arguments[0].scrollIntoView(true);", email_box)
    time.sleep(1)
    email_box.click()
    driver.execute_script("arguments[0].value='';", email_box)
    driver.execute_script("arguments[0].value = arguments[1];", email_box, EMAIL)
    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input',  { bubbles: true }));
        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
    """, email_box)
    print("Email entered.")
    time.sleep(2)

    # =====================================
    # CLICK NEXT (EMAIL)
    # =====================================
    print("Finding Next button...")
    next_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
    print("Next button found.")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_button)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", next_button)
    print("Clicked Next.")
    time.sleep(3)

    print("Current URL:")
    print(driver.current_url)
    print("Page title:")
    print(driver.title)

    # =====================================
    # INPUT PASSWORD
    # =====================================
    print("Finding password field...")
    password_box = wait.until(
        EC.visibility_of_element_located(
            (By.XPATH, "//input[@type='password' or @name='passwd' or @id='i0118']")
        )
    )
    print("Password field found.")
    password_box.click()
    driver.execute_script("arguments[0].value='';", password_box)
    driver.execute_script("arguments[0].value = arguments[1];", password_box, PASSWORD)
    driver.execute_script("""
        arguments[0].dispatchEvent(new Event('input',  { bubbles: true }));
        arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
    """, password_box)
    print("Password entered.")
    time.sleep(2)

    # =====================================
    # CLICK SIGN IN (PASSWORD)
    # =====================================
    print("Finding Sign in button...")
    sign_in_button = wait.until(EC.element_to_be_clickable((By.ID, "idSIButton9")))
    print("Sign in button found.")
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", sign_in_button)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", sign_in_button)
    print("Sign in clicked.")
    time.sleep(3)

    # =====================================
    # DETECT WRONG PASSWORD
    # =====================================
    # Microsoft shows an error element with id="passwordError" or a generic
    # "#idA_PWD_ForgotPassword" sibling when the password is incorrect.
    try:
        err_el = driver.find_element(
            By.XPATH,
            "//*[@id='passwordError' or @id='usernameError' "
            "or contains(@class,'alert-error') "
            "or @data-bind='text: error']",
        )
        if err_el.is_displayed() and err_el.text.strip():
            print(
                "❌ LOGIN FAILED — WRONG PASSWORD OR EMAIL: "
                + err_el.text.strip()
                + " — Please update the MSD credentials."
            )
            print("__WRONG_PASSWORD__")
    except Exception:
        pass

    # =====================================
    # HANDLE PHONE VERIFICATION PROMPT
    # =====================================
    try:
        phone_option = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, "//div[@data-value='OneWaySMS']"))
        )
        print("Phone verification prompt detected.")
        print(f"Clicking option: {phone_option.text.strip()}")
        driver.execute_script("arguments[0].click();", phone_option)
        print("Phone option clicked.")
        time.sleep(3)
    except TimeoutException:
        print("No phone selection prompt. Continuing...")

    # =====================================
    # HANDLE OTP CODE INPUT  (with retry on wrong code)
    # =====================================
    try:
        otp_box = WebDriverWait(driver, 10).until(
            EC.visibility_of_element_located(
                (By.XPATH,
                 "//input[@name='otc' or @id='idTxtBx_SAOTCC_OTC' "
                 "or contains(@placeholder,'code') "
                 "or contains(@placeholder,'Code')]")
            )
        )

        print("\n=================================")
        print("VERIFICATION CODE REQUIRED")
        print("Check your SMS for the OTP code.")
        print("=================================\n")

        otp_attempts = 0
        max_otp_attempts = 3

        while otp_attempts < max_otp_attempts:
            otp_attempts += 1
            if otp_attempts == 1:
                otp_code = input("Enter the OTP code from your SMS: ").strip()
            else:
                print(
                    f"❌ The OTP code was incorrect or expired "
                    f"(attempt {otp_attempts}/{max_otp_attempts}).\n"
                    "Please enter the correct code from your SMS."
                )
                otp_code = input("Enter the OTP code: ").strip()

            otp_box = WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located(
                    (By.XPATH,
                     "//input[@name='otc' or @id='idTxtBx_SAOTCC_OTC' "
                     "or contains(@placeholder,'code') "
                     "or contains(@placeholder,'Code')]")
                )
            )
            otp_box.click()
            driver.execute_script("arguments[0].value='';", otp_box)
            driver.execute_script("arguments[0].value = arguments[1];", otp_box, otp_code)
            driver.execute_script("""
                arguments[0].dispatchEvent(new Event('input',  { bubbles: true }));
                arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
            """, otp_box)
            print(f"Code entered (attempt {otp_attempts}).")
            time.sleep(1)

            verify_button = wait.until(
                EC.element_to_be_clickable((By.ID, "idSubmit_SAOTCC_Continue"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", verify_button)
            time.sleep(1)
            driver.execute_script("arguments[0].click();", verify_button)
            print("Verify clicked. Waiting for result...")
            time.sleep(3)

            error_visible = False
            try:
                error_el = driver.find_element(
                    By.XPATH,
                    "//*[@id='idTxtBx_SAOTCC_Error_OTC' or "
                    "contains(@class,'alert-error') or "
                    "contains(@class,'error') and normalize-space(.)!='']"
                )
                if error_el.is_displayed() and error_el.text.strip():
                    print(f"OTP error detected: {error_el.text.strip()}")
                    error_visible = True
            except Exception:
                pass

            if not error_visible:
                print("OTP accepted.")
                break

            if otp_attempts >= max_otp_attempts:
                raise Exception(f"OTP failed after {max_otp_attempts} attempts.")

    except TimeoutException:
        print("No OTP code prompt. Continuing...")

    # =====================================
    # HANDLE "STAY SIGNED IN?" PROMPT
    # =====================================
    try:
        stay_signed_in = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.ID, "idSIButton9"))
        )
        print("'Stay signed in?' prompt detected.")
        try:
            dont_show_checkbox = driver.find_element(
                By.XPATH,
                "//input[@type='checkbox' and @id='KmsiCheckboxField']"
            )
            if not dont_show_checkbox.is_selected():
                driver.execute_script("arguments[0].click();", dont_show_checkbox)
                print("Checked 'Don't show this again'.")
            else:
                print("'Don't show this again' already checked.")
        except Exception:
            print("'Don't show this again' checkbox not found. Continuing...")
        time.sleep(1)
        driver.execute_script("arguments[0].click();", stay_signed_in)
        print("Clicked Yes.")
    except TimeoutException:
        print("No 'Stay signed in?' prompt. Continuing...")

    print("Login completed.")
    print(f"Current URL: {driver.current_url}")
    print("✅ Dynamics login completed — starting download loop.")


# =====================================
# MAIN ENTRY POINT
# =====================================
try:
    do_login()

    # =====================================
    # ACTIVE-WINDOW HELPER
    # =====================================
    def _in_active_window() -> bool:
        """Return True if current local time is Mon–Fri 06:00–20:00."""
        now = _dt.now()
        return now.weekday() < 5 and 6 <= now.hour < 20

    def _secs_until_window() -> int:
        """Return seconds until the next Mon–Fri 06:00 window opens."""
        import math
        now = _dt.now()
        # Build a candidate for 06:00 today
        candidate = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if candidate <= now:
            # 06:00 today already passed — aim for tomorrow 06:00
            candidate = candidate.replace(day=candidate.day + 1)
        # Skip weekend days
        from datetime import timedelta
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return max(1, int((candidate - now).total_seconds()))

    # =====================================
    # REPETITIVE DOWNLOAD LOOP
    # =====================================
    # _MSD_RUN_ONCE = True  → off-hours on-demand: one export then stop (Chrome stays open).
    # _MSD_RUN_ONCE = False → normal in-hours mode: repeat every 30 min.
    _run_once: bool = globals().get("_MSD_RUN_ONCE", False)

    if _run_once:
        # ── OFF-HOURS ON-DEMAND: single export then stop ──────────────────
        # Chrome is NOT closed — session stays alive for next trigger.
        print("ℹ️  Off-hours manual run — exporting once then stopping (Chrome stays open).")
        try:
            print("\n" + "=" * 50)
            print("Starting automatic export...")
            print("=" * 50)
            open_work_orders()
        except Exception as loop_err:
            print(f"❌ Error during automatic export: {loop_err}")
            if _is_session_expired():
                print("\n⚠️  SESSION EXPIRED — Chrome is on the login page.")
                print("Closing Chrome window now...")
                try:
                    driver.quit()
                except Exception:
                    pass
                print("Chrome closed. Clearing session cache for a clean re-authentication...")
                _clear_session_cache(SELENIUM_PROFILE_DIR)
                print("Session cache cleared. Waiting for you to click Re-login in the web UI...")
                input("__RELOGIN_WAIT__")
                print("Re-login confirmed. Reopening Chrome and running login flow...")
                driver, wait = _make_driver()
                do_login()
                # Retry the export once after re-login
                try:
                    open_work_orders()
                except Exception as retry_err:
                    print(f"❌ Export failed after re-login: {retry_err}")
        print("\n✅ Off-hours run complete. Download loop stopped. Chrome remains open.")
        sys.exit(0)   # stops the script thread only — Chrome process is unaffected

    # ── IN-HOURS REPEATING LOOP ───────────────────────────────────────────
    # interval driven by MSD_INTERVAL_SEC in app/config/settings.py
    interval_seconds: int = globals().get("_MSD_INTERVAL_SEC", 1800)
    while True:
        # ── Active-window gate (office hours may end mid-loop) ────────────
        if not _in_active_window():
            secs = _secs_until_window()
            h, rem = divmod(secs, 3600)
            m, s   = divmod(rem, 60)
            print(f"⏸ Outside active hours (Mon–Fri 06:00–20:00). Next window in {h}h {m:02d}m {s:02d}s.")
            while not _in_active_window():
                chunk = min(300, _secs_until_window())
                time.sleep(chunk)
                if not _in_active_window():
                    secs = _secs_until_window()
                    h, rem = divmod(secs, 3600)
                    m2, s2 = divmod(rem, 60)
                    print(f"  Still outside active hours. Next window in {h}h {m2:02d}m {s2:02d}s.")
            print("▶ Active window opened. Reopening Chrome...")
            # Reopen Chrome for the new work session (profile/session preserved on disk)
            driver, wait = _make_driver()
            do_login()

        # _skip_wait is set to True when we recover from a session expiry so
        # the next iteration starts the export immediately without any delay.
        _skip_wait = False

        try:
            print("\n" + "=" * 50)
            print("Starting automatic export...")
            print("=" * 50)
            open_work_orders()
            # ── Export done — close Chrome window, keep session on disk ───
            print("✅ Export complete. Closing Chrome window (session preserved)...")
            try:
                driver.close()
            except Exception as close_err:
                print(f"Warning: could not close Chrome window: {close_err}")
        except Exception as loop_err:
            print(f"❌ Error during automatic export: {loop_err}")
            if _is_session_expired():
                print("\n" + "=" * 50)
                print("⚠️  SESSION EXPIRED / LOGGED OUT DETECTED")
                print("Closing Chrome window now...")
                print("=" * 50 + "\n")
                try:
                    driver.quit()
                except Exception:
                    pass
                print("Chrome closed. Clearing session cache for a clean re-authentication...")
                _clear_session_cache(SELENIUM_PROFILE_DIR)
                print("Session cache cleared. Waiting for you to click Re-login in the web UI...")
                input("__RELOGIN_WAIT__")
                print("Re-login confirmed. Reopening Chrome and running login flow...")
                driver, wait = _make_driver()
                do_login()
                # Skip the inter-download wait — start the next export immediately
                _skip_wait = True

        # ── Countdown — skipped after a session-expiry recovery ──────────
        if _skip_wait:
            print("\n▶ Resuming download immediately after re-login (no wait).")
        else:
            mins_total, secs_total = divmod(interval_seconds, 60)
            print(f"\nNext download in {mins_total}m {secs_total:02d}s. Waiting... (Chrome closed)")
            for remaining in range(interval_seconds, 0, -1):
                if remaining % 300 == 0 or remaining <= 10:
                    mins, secs = divmod(remaining, 60)
                    label = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
                    print(f"  Next download in {label}...")
                time.sleep(1)
            print("Next download starting now!\n")
            # Reopen Chrome for the next export
            print("Reopening Chrome for next export...")
            driver, wait = _make_driver()
            do_login()

except KeyboardInterrupt:
    # Ctrl+C — close the window but keep the Chrome profile / session on disk
    print("\nStopped by user (Ctrl+C).")
    print("Closing browser window (session preserved)...")
    try:
        driver.close()   # closes the window only; does NOT wipe the profile
    except Exception:
        pass
    print("Browser window closed. Your session is still saved.")
    sys.exit(0)

except TimeoutException as e:
    print("Timeout waiting for element.")
    print(e)
    print(f"❌ Dynamics login failed (timeout): {e}")

except Exception as e:
    print("Unexpected error:")
    print(e)
    print(f"❌ Dynamics login failed: {e}")

finally:
    # Only reached on TimeoutException / unexpected Exception.
    # KeyboardInterrupt calls sys.exit(0) above, so it never falls through here.
    print("Closing browser...")
    try:
        driver.quit()
    except Exception:
        pass
    print("Browser closed.")
