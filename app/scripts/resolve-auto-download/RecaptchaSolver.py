import os
import shutil
import subprocess
import urllib.request
import random
import speech_recognition
import time
from typing import Optional
from DrissionPage import ChromiumPage

# ── Locate ffmpeg before importing pydub so its internal which() is bypassed ──
def _find_ffmpeg() -> str | None:
    """Return the absolute path to ffmpeg.exe.

    Search order:
      1. imageio-ffmpeg (pip-bundled binary — works on any machine after pip install)
      2. System PATH  (ffmpeg installed globally)
      3. Common Windows install locations (WinGet, Scoop, manual C:\\ffmpeg)
    """
    # 1. imageio-ffmpeg — pip package that ships its own ffmpeg binary.
    #    Install once: pip install imageio-ffmpeg
    #    No manual system install needed on any laptop.
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass

    # 2. ffmpeg already on system PATH (e.g. manually installed or via WinGet)
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 3. Common Windows fallback locations
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            r"Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe",
        ),
        os.path.join(
            os.environ.get("USERPROFILE", ""),
            r"scoop\apps\ffmpeg\current\bin\ffmpeg.exe",
        ),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

_FFMPEG_PATH = _find_ffmpeg()

# Suppress pydub's "Couldn't find ffmpeg" RuntimeWarning — we patch it immediately after.
import warnings as _warnings
with _warnings.catch_warnings():
    _warnings.simplefilter("ignore", RuntimeWarning)
    import pydub
    import pydub.utils as _pydub_utils

if _FFMPEG_PATH:
    _ffprobe = _FFMPEG_PATH.replace("ffmpeg.exe", "ffprobe.exe")
    _orig_which = _pydub_utils.which

    def _patched_which(name):
        if name in ("ffmpeg", "avconv"):
            return _FFMPEG_PATH
        if name in ("ffprobe", "avprobe"):
            return _ffprobe if os.path.isfile(_ffprobe) else _FFMPEG_PATH
        return _orig_which(name)

    _pydub_utils.which = _patched_which
    pydub.AudioSegment.converter = _FFMPEG_PATH
    pydub.AudioSegment.ffmpeg = _FFMPEG_PATH
    pydub.AudioSegment.ffprobe = _ffprobe if os.path.isfile(_ffprobe) else _FFMPEG_PATH
    print(f"[ffmpeg] Using: {_FFMPEG_PATH}")
else:
    print("[ffmpeg] WARNING: ffmpeg not found — audio conversion will fail.")


class RecaptchaSolver:
    """A class to solve reCAPTCHA challenges using audio recognition."""

    TEMP_DIR = os.getenv("TEMP") if os.name == "nt" else "/tmp"
    TIMEOUT_STANDARD = 10
    TIMEOUT_SHORT = 2
    TIMEOUT_DETECTION = 0.05

    def __init__(self, driver: ChromiumPage) -> None:
        self.driver = driver

    def solveCaptcha(self) -> None:
        """Attempt to solve the reCAPTCHA challenge."""

        # ── 1. Click the checkbox ──────────────────────────────────────────────
        self.driver.wait.ele_displayed("@title=reCAPTCHA", timeout=self.TIMEOUT_STANDARD)
        time.sleep(0.3)
        iframe_inner = self.driver("@title=reCAPTCHA")
        iframe_inner.wait.ele_displayed(".rc-anchor-content", timeout=self.TIMEOUT_STANDARD)
        iframe_inner(".rc-anchor-content", timeout=self.TIMEOUT_SHORT).click()

        if self.is_solved():
            return

        # ── 2. Click the audio challenge button ───────────────────────────────
        # Use bframe src to target ONLY the challenge popup, not the checkbox iframe
        time.sleep(1.5)
        iframe = self._get_challenge_iframe()
        iframe.wait.ele_displayed("#recaptcha-audio-button", timeout=self.TIMEOUT_STANDARD)
        iframe("#recaptcha-audio-button", timeout=self.TIMEOUT_SHORT).click()
        time.sleep(0.5)

        if self.is_detected():
            raise Exception("Captcha detected bot behavior")

        # ── 3 & 4. Retry loop: get audio → transcribe → submit ────────────────
        last_error = None
        for attempt in range(1, 4):
            print(f"[captcha] Attempt {attempt}/3...")

            # Always re-query the challenge iframe fresh each attempt
            time.sleep(1.5)
            iframe = self._get_challenge_iframe()

            src = self._get_audio_src(iframe)
            if not src:
                # Click the reload button (↺) and retry
                try:
                    self._get_challenge_iframe()("#recaptcha-reload-button", timeout=self.TIMEOUT_SHORT).click()
                    time.sleep(1)
                except Exception:
                    pass
                last_error = "Could not locate audio source URL"
                continue

            print(f"[captcha] Audio URL: {src[:80]}...")

            try:
                text_response = self._process_audio_challenge(src)
                if not text_response:
                    raise Exception("Speech recognition returned empty result")
                print(f"[captcha] Recognised text: {text_response}")
            except Exception as e:
                last_error = str(e)
                try:
                    self._get_challenge_iframe()("#recaptcha-reload-button", timeout=self.TIMEOUT_SHORT).click()
                    time.sleep(1)
                except Exception:
                    pass
                continue

            # Submit the answer
            try:
                iframe = self._get_challenge_iframe()
                response_box = iframe("#audio-response", timeout=self.TIMEOUT_SHORT)
                response_box.clear()
                response_box.input(text_response.lower())
                iframe("#recaptcha-verify-button").click()
                time.sleep(2)
            except Exception as e:
                last_error = f"Submit failed: {e}"
                continue

            if self.is_solved():
                return

            # Wrong answer — reload for next attempt
            last_error = "Wrong answer after submit"
            try:
                self._get_challenge_iframe()("#recaptcha-reload-button", timeout=self.TIMEOUT_SHORT).click()
                time.sleep(1)
            except Exception:
                pass

        raise Exception(f"Audio challenge failed after 3 attempts: {last_error}")

    def _get_challenge_iframe(self):
        """Return the challenge popup iframe (bframe), not the checkbox iframe."""
        # The challenge iframe src contains 'bframe' — distinct from the
        # checkbox iframe whose title is exactly "reCAPTCHA"
        iframe = self.driver(
            "xpath://iframe[contains(@src,'bframe')]",
            timeout=self.TIMEOUT_STANDARD,
        )
        if iframe:
            return iframe
        # Fallback: second iframe with 'recaptcha' in title (case-insensitive)
        iframes = self.driver.eles("xpath://iframe[contains(@title,'recaptcha')]")
        if len(iframes) >= 2:
            return iframes[1]   # index 1 = challenge iframe
        if iframes:
            return iframes[0]
        raise Exception("Could not find reCAPTCHA challenge iframe")

    def _get_audio_src(self, iframe) -> str | None:
        """Extract the audio challenge MP3 URL from the reCAPTCHA iframe.

        Tries in order:
          1. Download link href  (the ↓ button) — most reliable
          2. <audio src="...">
          3. <source src="..."> child of <audio>
          4. #audio-source legacy id
        """
        # 1. Download button: <a class="rc-audiochallenge-tdownload-link" href="...">
        try:
            dl = iframe(
                "xpath://a[contains(@class,'rc-audiochallenge-tdownload-link') or @id='audio-download']",
                timeout=self.TIMEOUT_SHORT,
            )
            if dl:
                href = dl.attrs.get("href") or dl.attrs.get("download")
                if href and href.startswith("http"):
                    print("[captcha] Source: download link")
                    return href
        except Exception:
            pass

        # 2. <audio src="...">
        try:
            audio_ele = iframe("tag:audio", timeout=self.TIMEOUT_SHORT)
            if audio_ele:
                src = audio_ele.attrs.get("src")
                if src and src.startswith("http"):
                    print("[captcha] Source: <audio src>")
                    return src
        except Exception:
            pass

        # 3. <source src="..."> child of <audio>
        try:
            source_ele = iframe("tag:source", timeout=self.TIMEOUT_SHORT)
            if source_ele:
                src = source_ele.attrs.get("src")
                if src and src.startswith("http"):
                    print("[captcha] Source: <source> child")
                    return src
        except Exception:
            pass

        # 4. Legacy #audio-source id
        try:
            ele = iframe("#audio-source", timeout=self.TIMEOUT_SHORT)
            if ele:
                src = ele.attrs.get("src")
                if src and src.startswith("http"):
                    print("[captcha] Source: #audio-source")
                    return src
        except Exception:
            pass

        return None

    def _process_audio_challenge(self, audio_url: str) -> str:
        """Download the audio, convert to WAV, and transcribe with Google SR."""
        mp3_path = os.path.join(self.TEMP_DIR, f"captcha_{random.randrange(1, 100000)}.mp3")
        wav_path = os.path.join(self.TEMP_DIR, f"captcha_{random.randrange(1, 100000)}.wav")

        try:
            # Download
            req = urllib.request.Request(audio_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as resp, open(mp3_path, "wb") as f:
                f.write(resp.read())

            # Convert mp3 → wav using ffmpeg directly (bypasses pydub path issues)
            if _FFMPEG_PATH:
                result = subprocess.run(
                    [_FFMPEG_PATH, "-y", "-i", mp3_path, wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise Exception("ffmpeg conversion failed")
            else:
                # fallback to pydub (will fail if ffmpeg not on PATH)
                sound = pydub.AudioSegment.from_mp3(mp3_path)
                sound.export(wav_path, format="wav")

            # Transcribe
            recognizer = speech_recognition.Recognizer()
            with speech_recognition.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            return recognizer.recognize_google(audio)

        finally:
            for path in (mp3_path, wav_path):
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    def is_solved(self) -> bool:
        """Check if the captcha checkbox is ticked.

        Success:  <div class="recaptcha-checkbox-checkmark" style="">  → style attr present
        The checkmark div only gets a style attribute when the box is checked.
        Tries multiple strategies to find the element in case the iframe
        reference changed after the audio challenge closed.
        """
        # Strategy 1: look inside @title=reCAPTCHA iframe
        try:
            iframe_inner = self.driver("@title=reCAPTCHA", timeout=self.TIMEOUT_SHORT)
            checkmark = iframe_inner(".recaptcha-checkbox-checkmark", timeout=self.TIMEOUT_SHORT)
            if checkmark is not None and "style" in checkmark.attrs:
                return True
        except Exception:
            pass

        # Strategy 2: search all iframes whose src contains 'api2/anchor'
        try:
            anchor_iframe = self.driver(
                "xpath://iframe[contains(@src,'api2/anchor') or contains(@src,'recaptcha/api2')]",
                timeout=self.TIMEOUT_SHORT,
            )
            checkmark = anchor_iframe(".recaptcha-checkbox-checkmark", timeout=self.TIMEOUT_SHORT)
            if checkmark is not None and "style" in checkmark.attrs:
                return True
        except Exception:
            pass

        return False

    def is_detected(self) -> bool:
        """Check if the bot has been detected."""
        try:
            return (
                self.driver.ele("Try again later", timeout=self.TIMEOUT_DETECTION)
                .states()
                .is_displayed
            )
        except Exception:
            return False

    def get_token(self) -> Optional[str]:
        """Get the reCAPTCHA token if available."""
        try:
            return self.driver.ele("#recaptcha-token").attrs["value"]
        except Exception:
            return None
