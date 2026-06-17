"""StreamWorker class for managing individual stream watching"""
import json
import os
import shutil
import tempfile
import threading
import time
import random
import re
from urllib.parse import urlparse
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.common.exceptions import TimeoutException, WebDriverException

from utils.helpers import domain_from_url, debug_print, _kick_username_from_url
from .browser import make_chrome_driver, CookieManager
from .api import get_public_api, persist_public_api_token, public_api_available


class StreamWorker(threading.Thread):
    """Manages individual stream watching in a separate thread"""
    
    def __init__(
        self,
        url,
        minutes_target,
        on_update=None,
        on_finish=None,
        stop_event=None,
        driver_path=None,
        extension_path=None,
        hide_player=False,
        mute=False,
        mini_player=False,
        background_browser=False,
        force_160p=False,
        offline_fresh_checks_to_switch=0,
        offline_grace_seconds=60,
        required_category_id=None,
        cumulative_time_callback=None,
    ):
        super().__init__(daemon=True)
        self.url = self._normalize_stream_url(url)
        self.minutes_target = minutes_target
        self.on_update = on_update
        self.on_finish = on_finish
        self.stop_event = stop_event or threading.Event()
        self.elapsed_seconds = 0
        self.driver = None
        self._profile_dir = None
        self.driver_path = driver_path
        self.extension_path = extension_path
        self.hide_player = hide_player
        self.mute = mute
        self.mini_player = mini_player
        self.background_browser = background_browser
        self.force_160p = force_160p
        self.completed = False
        self.ended_because_offline = False
        self.ended_because_wrong_category = False
        self.ended_because_navigation_error = False
        self.error_message = ""
        self.suppress_finish_callback = False
        self.required_category_id = required_category_id
        self.cumulative_time_callback = cumulative_time_callback
        self._offline_fresh_checks = 0
        self.offline_fresh_checks_to_switch = max(0, int(offline_fresh_checks_to_switch or 0))
        self.offline_grace_seconds = max(0, int(offline_grace_seconds or 0))
        self.offline_since = None
        self.offline_elapsed_seconds = 0
        self.offline_grace_remaining_seconds = 0
        self.playback_state = "starting"
        # Anti rate-limit: cache "is live" checks
        self._last_live_check = 0.0
        self._last_live_value = True
        self._live_check_interval = 10  # seconds (reduced for faster detection)
        self._last_live_source = "unknown"  # api | dom | unknown
        # Category check interval (check every 30 seconds)
        self._last_category_check = 0.0
        self._category_check_interval = 30  # seconds

    def _normalize_stream_url(self, url):
        url = (url or "").strip()
        if not url:
            return url
        if url.startswith("kick.com/"):
            return f"https://{url}"
        parsed = urlparse(url)
        if not parsed.scheme and not parsed.netloc:
            return f"https://kick.com/{url.strip('/')}"
        if not parsed.scheme:
            return f"https://{url}"
        return url

    def _safe_get(self, url, timeout=25):
        """Navigate without letting a hanging page load block the worker forever."""
        if not self.driver:
            return False
        try:
            self.driver.set_page_load_timeout(timeout)
        except Exception:
            pass
        try:
            self.driver.get(url)
            return True
        except TimeoutException:
            debug_print(f"DEBUG: Navigation timed out, stopping load: {url}")
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
            return True
        except WebDriverException as e:
            debug_print(f"DEBUG: driver.get failed for {url}: {e}")
            try:
                self.driver.execute_script("window.location.href = arguments[0];", url)
                return True
            except Exception as js_error:
                debug_print(f"DEBUG: JS navigation failed for {url}: {js_error}")
                return False

    def _wait_for_target_url(self, url, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            try:
                current_url = self.driver.current_url or ""
                if self._url_matches_channel(current_url, url):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _force_navigate(self, url):
        """Force navigation through multiple paths and report whether URL changed."""
        if not self.driver:
            return False
        try:
            self.driver.execute_cdp_cmd("Page.enable", {})
            self.driver.execute_cdp_cmd("Page.navigate", {"url": url})
            if self._wait_for_target_url(url, timeout=10):
                return True
        except Exception as e:
            debug_print(f"DEBUG: CDP navigation failed for {url}: {e}")

        self._safe_get(url, timeout=25)
        if self._wait_for_target_url(url, timeout=10):
            return True

        try:
            self.driver.execute_script("window.location.assign(arguments[0]);", url)
            if self._wait_for_target_url(url, timeout=10):
                return True
        except Exception as e:
            debug_print(f"DEBUG: JS assign navigation failed for {url}: {e}")
        return False

    def _url_matches_channel(self, current_url, target_url):
        try:
            expected = "/" + target_url.rstrip("/").split("/")[-1].lower()
            return expected in (current_url or "").lower()
        except Exception:
            return False

    def run(self):
        """Main worker loop"""
        domain = domain_from_url(self.url)
        try:
            # Background mode runs the mining browser headless. Hide player is
            # retained as a page-level video style option for visible runs.
            use_headless = bool(self.background_browser)
            if self.mini_player:
                use_headless = False
            if self.extension_path and self.extension_path.endswith(".crx"):
                use_headless = False

            self._profile_dir = tempfile.mkdtemp(prefix="kickdrops-worker-")
            self.driver = make_chrome_driver(
                headless=use_headless,
                driver_path=self.driver_path,
                extension_path=self.extension_path,
                user_data_dir=self._profile_dir,
            )
            try:
                self.driver.set_page_load_timeout(25)
                self.driver.set_script_timeout(12)
            except Exception:
                pass

            if not use_headless:
                try:
                    if self.mini_player:
                        self.driver.set_window_size(360, 360)
                        self.driver.set_window_position(20, 20)
                    else:
                        # Always bring the main Chrome window back on-screen so it can be moved
                        self.driver.set_window_position(60, 60)
                except Exception:
                    pass

            base = f"https://{domain}" if domain else "about:blank"
            if domain:
                cookies_loaded = CookieManager.load_cookies_via_cdp(self.driver, domain)
                if not cookies_loaded:
                    self._safe_get(base, timeout=15)
                    CookieManager.load_cookies(self.driver, domain)
                
                # Set stream quality in session storage BEFORE navigating to stream URL
                if self.force_160p:
                    try:
                        self.driver.execute_script("sessionStorage.setItem('stream_quality', '160');")
                    except Exception as e:
                        print(f"Error setting stream_quality: {e}")
            
            debug_print(f"DEBUG: Navigating worker to stream URL: {self.url}")
            if not self._force_navigate(self.url):
                current_url = ""
                try:
                    current_url = self.driver.current_url or ""
                except Exception:
                    pass
                self.ended_because_navigation_error = True
                raise RuntimeError(f"Could not navigate to stream URL {self.url}; current URL: {current_url}")
            
            # Wait for page to load (give it time for stream to initialize)
            time.sleep(5)
            try:
                if not self.has_authenticated_session():
                    debug_print("DEBUG: Kick browser session does not look authenticated; drops may not count.")
            except Exception:
                pass

            try:
                self.activate_player()
                self.ensure_player_state()
                self.wait_for_video_playback(timeout=12)
            except Exception:
                pass

            last_report = 0
            while not self.stop_event.is_set():
                prev_live_check = self._last_live_check
                stream_live = self.is_stream_live()
                live = stream_live
                fresh_check = self._last_live_check != prev_live_check
                try:
                    self.ensure_player_state()
                except Exception:
                    pass
                if stream_live and not self.is_video_playing():
                    self.activate_player()
                    self.ensure_player_state()
                    if self.is_offline_now():
                        stream_live = False
                        live = False
                    else:
                        live = True

                if stream_live:
                    self.offline_since = None
                    self.offline_elapsed_seconds = 0
                    self.offline_grace_remaining_seconds = 0
                    self.playback_state = "live"
                else:
                    now = time.time()
                    if self.offline_since is None:
                        self.offline_since = now
                    self.offline_elapsed_seconds = int(now - self.offline_since)
                    self.offline_grace_remaining_seconds = max(
                        0, self.offline_grace_seconds - self.offline_elapsed_seconds
                    )
                    self.playback_state = "offline"

                if fresh_check:
                    if stream_live:
                        self._offline_fresh_checks = 0
                    else:
                        self._offline_fresh_checks += 1

                offline_grace_expired = (
                    not stream_live
                    and self.offline_since is not None
                    and self.offline_elapsed_seconds >= self.offline_grace_seconds
                )
                offline_checks_expired = (
                    not stream_live
                    and self.offline_grace_seconds <= 0
                    and self.offline_fresh_checks_to_switch
                    and self._offline_fresh_checks >= self.offline_fresh_checks_to_switch
                )
                if offline_grace_expired or offline_checks_expired:
                    self.ended_because_offline = True
                    break
                
                # Check category if required (every 30 seconds)
                if self.required_category_id and stream_live:
                    now = time.time()
                    if now - self._last_category_check >= self._category_check_interval:
                        self._last_category_check = now
                        current_category_id = self.get_streamer_category_id()
                        if (
                            current_category_id is not None
                            and str(current_category_id) != str(self.required_category_id)
                        ):
                            debug_print(f"DEBUG: Streamer changed category from {self.required_category_id} to {current_category_id}, switching...")
                            self.ended_because_wrong_category = True
                            break
                
                if live:
                    self.elapsed_seconds += 1
                if time.time() - last_report >= 1:
                    last_report = time.time()
                    if self.on_update:
                        self.on_update(self.elapsed_seconds, live)
                
                # Check completion: for global drops, use cumulative time; otherwise use individual time
                if self.minutes_target:
                    if self.cumulative_time_callback:
                        # Global drop - check cumulative time
                        current_cumulative = self.cumulative_time_callback()
                        if current_cumulative >= self.minutes_target * 60:
                            self.completed = True
                            break
                    else:
                        # Regular drop - use individual time
                        if self.elapsed_seconds >= self.minutes_target * 60:
                            self.completed = True
                            break
                time.sleep(1)
        except Exception as e:
            self.error_message = str(e)
            print("StreamWorker error:", e)
        finally:
            try:
                domain = domain_from_url(self.url)
                if self.driver and domain:
                    CookieManager.save_cookies(self.driver, domain)
            except Exception:
                pass
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
            try:
                if self._profile_dir and os.path.isdir(self._profile_dir):
                    shutil.rmtree(self._profile_dir, ignore_errors=True)
            except Exception:
                pass
            try:
                if self.on_finish and not self.suppress_finish_callback:
                    self.on_finish(self.elapsed_seconds, self.completed)
            except Exception:
                pass

    def stop(self):
        """Stop the worker"""
        self.stop_event.set()
    
    def get_streamer_category_id(self):
        """Get the current category ID of the streamer's livestream"""
        if not self.driver:
            return None
        
        try:
            username = _kick_username_from_url(self.url)
            if not username:
                return None

            if public_api_available():
                try:
                    category_id = get_public_api().category_id_by_slug(username)
                    persist_public_api_token()
                    if category_id is not None:
                        return category_id
                except Exception as e:
                    debug_print(f"DEBUG: Public API category check failed: {e}")
            
            api_url = f"https://kick.com/api/v2/channels/{username}"
            script = """
            const cb = arguments[arguments.length - 1];
            fetch(arguments[0], { credentials: 'include', cache: 'no-store', headers: { 'Accept': 'application/json' } })
              .then(r => r.text())
              .then(t => cb(t))
              .catch(e => cb(JSON.stringify({ error: String(e) })));
            """
            try:
                self.driver.set_script_timeout(10)
            except Exception:
                pass
            text = self.driver.execute_async_script(script, api_url)
            data = json.loads(text) if text else None
            if isinstance(data, dict) and not data.get("error"):
                livestream = data.get("livestream")
                if livestream and livestream.get("is_live"):
                    categories = livestream.get("categories", [])
                    if categories and len(categories) > 0:
                        # Return the first category's ID
                        return categories[0].get("id")
        except Exception as e:
            debug_print(f"DEBUG: Error getting streamer category: {e}")
        return None

    def has_authenticated_session(self):
        """Best-effort check that Kick sees this browser as signed in."""
        if not self.driver:
            return False
        probes = (
            "https://kick.com/api/v1/user",
            "https://kick.com/api/v2/user",
        )
        script = """
        const cb = arguments[arguments.length - 1];
        fetch(arguments[0], { credentials: 'include', cache: 'no-store', headers: { 'Accept': 'application/json' } })
          .then(async r => cb(JSON.stringify({ status: r.status, text: await r.text() })))
          .catch(e => cb(JSON.stringify({ status: 0, error: String(e) })));
        """
        for url in probes:
            try:
                self.driver.set_script_timeout(10)
            except Exception:
                pass
            try:
                raw = self.driver.execute_async_script(script, url)
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    continue
                if int(data.get("status") or 0) != 200:
                    continue
                text = data.get("text") or ""
                parsed = json.loads(text) if text else None
                if isinstance(parsed, dict) and (
                    parsed.get("id")
                    or parsed.get("username")
                    or parsed.get("email")
                    or parsed.get("data")
                ):
                    return True
            except Exception:
                continue
        try:
            cookies = self.driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
            return any(c.get("name") == "session_token" and c.get("value") for c in cookies)
        except Exception:
            return False

    def is_stream_live(self):
        """Check if the stream is currently live"""
        now = time.time()
        # Cache API checks to reduce rate-limit risk
        if now - self._last_live_check < self._live_check_interval:
            return self._last_live_value
        try:
            # Kick is frequently protected (403 from Python). Prefer checking from inside the browser.
            username = _kick_username_from_url(self.url)
            if username:
                if public_api_available():
                    try:
                        status = get_public_api().live_status_by_slug(username)
                        persist_public_api_token()
                        if status is not None:
                            self._last_live_value = bool(status)
                            self._last_live_source = "public_api"
                            return self._last_live_value
                    except Exception as e:
                        debug_print(f"DEBUG: Public API live check failed: {e}")

                try:
                    api_url = f"https://kick.com/api/v2/channels/{username}"
                    script = """
                    const cb = arguments[arguments.length - 1];
                    fetch(arguments[0], { credentials: 'include', cache: 'no-store', headers: { 'Accept': 'application/json' } })
                      .then(r => r.text())
                      .then(t => cb(t))
                      .catch(e => cb(JSON.stringify({ error: String(e) })));
                    """
                    try:
                        self.driver.set_script_timeout(10)
                    except Exception:
                        pass
                    text = self.driver.execute_async_script(script, api_url)
                    data = json.loads(text) if text else None
                    if isinstance(data, dict) and not data.get("error"):
                        livestream = data.get("livestream")
                        is_live = bool(livestream and livestream.get("is_live"))
                        self._last_live_value = is_live
                        self._last_live_source = "browser_api"
                        return is_live
                except Exception:
                    pass

                # Fallback: extract app state from the page (when available) and look for is_live.
                try:
                    state_text = self.driver.execute_script(
                        """
                        try {
                          const next = document.getElementById('__NEXT_DATA__');
                          if (next && next.textContent) return next.textContent;
                          if (window.__NUXT__) return JSON.stringify(window.__NUXT__);
                        } catch (e) {}
                        return null;
                        """
                    )
                    if isinstance(state_text, str) and state_text:
                        m = re.search(r"\"is_live\"\\s*:\\s*(true|false)", state_text, re.IGNORECASE)
                        if m:
                            is_live = m.group(1).lower() == "true"
                            self._last_live_value = is_live
                            self._last_live_source = "page_state"
                            return is_live
                except Exception:
                    pass

            # Last-resort DOM heuristic: only try to detect offline (avoid false positives on generic 'LIVE' text).
            try:
                body = self.driver.find_element(By.TAG_NAME, "body").text.upper()
                offline_markers = (
                    "OFFLINE",
                    "IS OFFLINE",
                    "CHANNEL IS OFFLINE",
                    "NOT LIVE",
                    "HORS LIGNE",
                    "N'EST PAS EN DIRECT",
                )
                if any(m in body for m in offline_markers):
                    self._last_live_value = False
                    self._last_live_source = "dom_offline"
                    return False
            except Exception:
                pass

            self._last_live_source = "unknown"
            return self._last_live_value
        except Exception:
            self._last_live_value = False
            self._last_live_source = "unknown"
            return False
        finally:
            # Add slight jitter to desync multiple workers
            jitter = random.uniform(-3, 3)
            base_interval = 8 if self._last_live_value else 5  # More frequent when offline
            self._live_check_interval = max(4, base_interval + jitter)
            self._last_live_check = now

    def is_offline_now(self):
        """Immediate offline check used when the video is not playing."""
        username = _kick_username_from_url(self.url)
        if username and self.driver:
            try:
                api_url = f"https://kick.com/api/v2/channels/{username}"
                script = """
                const cb = arguments[arguments.length - 1];
                fetch(arguments[0], { credentials: 'include', cache: 'no-store', headers: { 'Accept': 'application/json' } })
                  .then(r => r.text())
                  .then(t => cb(t))
                  .catch(e => cb(JSON.stringify({ error: String(e) })));
                """
                try:
                    self.driver.set_script_timeout(8)
                except Exception:
                    pass
                text = self.driver.execute_async_script(script, api_url)
                data = json.loads(text) if text else None
                if isinstance(data, dict) and not data.get("error"):
                    livestream = data.get("livestream")
                    if not livestream or not livestream.get("is_live"):
                        self._last_live_value = False
                        self._last_live_source = "browser_api_now"
                        self._last_live_check = time.time()
                        return True
            except Exception:
                pass

        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text.upper()
            offline_markers = (
                "OFFLINE",
                "IS OFFLINE",
                "CHANNEL IS OFFLINE",
                "NOT LIVE",
                "NO ESTÁ EN DIRECTO",
                "NO ESTA EN DIRECTO",
                "DESCONECTADO",
                "SIN CONEXIÓN",
                "SIN CONEXION",
                "HORS LIGNE",
                "N'EST PAS EN DIRECT",
            )
            if any(marker in body for marker in offline_markers):
                self._last_live_value = False
                self._last_live_source = "dom_offline_now"
                self._last_live_check = time.time()
                return True
        except Exception:
            pass
        return False

    def ensure_player_state(self):
        """Keep the stream in a state that can count as real viewing."""
        try:
            hide = "true" if self.hide_player else "false"
            muted = "true" if self.mute else "false"
            volume = "0" if self.mute else "1"
            mini = "true" if (not self.hide_player and self.mini_player) else "false"
            js = f"""
            (function(){{
              var v = document.querySelector('video');
              if (v) {{
                try {{ v.muted = {muted}; v.volume = {volume}; }} catch(e) {{}}
                try {{
                  if (v.paused || v.readyState < 2) {{
                    var playPromise = v.play();
                    if (playPromise && playPromise.catch) playPromise.catch(function(){{}});
                  }}
                }} catch(e) {{}}
                if ({hide}) {{
                  v.style.opacity='0';
                  v.style.width='1px';
                  v.style.height='1px';
                  v.style.position='fixed';
                  v.style.bottom='0';
                  v.style.right='0';
                  v.style.pointerEvents='none';
                }} else if ({mini}) {{
                  v.style.opacity='1';
                  v.style.width='100px';
                  v.style.height='100px';
                  v.style.position='fixed';
                  v.style.bottom='6px';
                  v.style.right='6px';
                  v.style.pointerEvents='none';
                  v.style.zIndex='999999';
                }} else {{
                  v.style.opacity='';
                  v.style.width='';
                  v.style.height='';
                  v.style.position='';
                  v.style.bottom='';
                  v.style.right='';
                  v.style.pointerEvents='';
                }}
              }}
            }})();
            """
            self.driver.execute_script(js)
        except Exception:
            pass

    def activate_player(self):
        """Focus the page and click the player once so unmuted playback can start."""
        if not self.driver:
            return False
        try:
            self.driver.execute_cdp_cmd("Page.bringToFront", {})
        except Exception:
            pass
        try:
            self.driver.switch_to.window(self.driver.current_window_handle)
        except Exception:
            pass

        clicked = False
        try:
            video = self.driver.find_element(By.TAG_NAME, "video")
            ActionChains(self.driver).move_to_element(video).click().perform()
            clicked = True
        except Exception:
            try:
                body = self.driver.find_element(By.TAG_NAME, "body")
                ActionChains(self.driver).move_to_element_with_offset(body, 640, 360).click().perform()
                clicked = True
            except Exception:
                pass

        try:
            self.driver.execute_script(
                """
                window.focus();
                const v = document.querySelector('video');
                if (v) {
                  try { v.muted = false; v.volume = 1; } catch(e) {}
                  try {
                    const p = v.play();
                    if (p && p.catch) p.catch(function(){});
                  } catch(e) {}
                }
                """
            )
        except Exception:
            pass
        return clicked

    def wait_for_video_playback(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end and not self.stop_event.is_set():
            if self.is_video_playing():
                return True
            self.activate_player()
            self.ensure_player_state()
            time.sleep(1)
        return False

    def is_video_playing(self):
        """Return True when the browser video appears to be actively playing."""
        if not self.driver:
            return False
        try:
            state = self.driver.execute_script(
                """
                const v = document.querySelector('video');
                if (!v) return { ok: false, reason: 'no-video' };
                return {
                  ok: !v.paused && !v.ended && v.readyState >= 2 && v.currentTime > 0,
                  paused: v.paused,
                  ended: v.ended,
                  readyState: v.readyState,
                  currentTime: v.currentTime,
                  muted: v.muted,
                  volume: v.volume
                };
                """
            )
            if isinstance(state, dict):
                if not state.get("ok"):
                    debug_print(f"DEBUG: Video not actively playing: {state}")
                return bool(state.get("ok"))
        except Exception as e:
            debug_print(f"DEBUG: Video playback check failed: {e}")
        return False
