"""Main application UI for KickDropsMiner"""
import json
import os
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, simpledialog, filedialog
from urllib.parse import urlparse
import urllib.request
from io import BytesIO
import customtkinter as ctk
from PIL import Image

from core import (
    Config,
    StreamWorker,
    CookieManager,
    make_chrome_driver,
    configure_public_api,
    kick_is_live_by_api,
    kick_live_status_by_api,
    kick_channel_category_id_by_api,
    fetch_drops_progress,
    fetch_drops_progress_direct,
    fetch_drops_campaigns_and_progress,
    fetch_live_streamers_by_category,
    is_campaign_expired
)
from core.kick_public_api import KickPublicAPI
from utils.helpers import (
    APP_DIR,
    deduplicate,
    domain_from_url,
    cookie_file_for_domain,
    debug_print,
    set_debug_config
)
from utils.backoff import ExponentialBackoff
from utils.translations import translate, TRANSLATIONS


DONATION_URL = "https://paypal.me/thuliotria"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Kick Drop Miner")
        self.geometry("1000x750")
        self.minsize(900, 700)

        self.config_data = Config()
        if self.config_data.language != "es":
            self.config_data.language = "es"
            self.config_data.save()
        changed_playback_config = False
        if self.config_data.mute:
            self.config_data.mute = False
            changed_playback_config = True
        if self.config_data.hide_player:
            self.config_data.hide_player = False
            changed_playback_config = True
        if self.config_data.background_browser:
            self.config_data.background_browser = False
            changed_playback_config = True
        if changed_playback_config:
            self.config_data.save()
        self._configure_kick_public_api()
        # Set global debug config reference
        set_debug_config(self.config_data)
        self.workers = {}
        self._interactive_driver = None  # Chrome pour capture de cookies
        self._settings_window = None
        self._pending_theme_after = None
        self.queue_running = False
        self.queue_current_idx = None
        self._queue_starting = False
        self._start_generation = 0
        self._live_status_cache = {}
        self._live_status_ttl = 25
        self.metric_vars = {}
        self.drop_progress_by_campaign = {}
        self._drop_progress_last_fetch = 0
        self._drop_progress_fetching = False
        self._drop_progress_interval = 5

        # Helper traduction
        def _t(key: str, **kwargs):
            return translate(self.config_data.language, key).format(**kwargs)

        self.t = _t

        # Appearance / theme
        ctk.set_appearance_mode("Dark" if self.config_data.dark_mode else "Light")
        ctk.set_default_color_theme("dark-blue")
        try:
            self.configure(fg_color=self._palette()["bg"])
        except Exception:
            pass
        self.status_var = tk.StringVar(value=self.t("status_ready"))
        self.status_progress_var = tk.StringVar(value="Drop: --")

        # Layout principal: 2 colonnes (sidebar gauche, contenu droit)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = ctk.CTkFrame(self, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        # Leave free space at the bottom to avoid cutting off controls
        # (uses a high empty row to serve as expandable space)
        self.sidebar.grid_rowconfigure(99, weight=1)

        self._build_sidebar()

        # Contenu principal
        self.content = ctk.CTkFrame(self, corner_radius=12)
        self.content.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        self.content.grid_rowconfigure(2, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

        self._build_content()

        # Status bar
        self.status_frame = ctk.CTkFrame(
            self,
            fg_color=self._palette()["panel"],
            corner_radius=8,
            border_width=1,
            border_color=self._palette()["border"],
        )
        self.status_frame.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10)
        )
        self.status_frame.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(
            self.status_frame,
            textvariable=self.status_var,
            anchor="w",
            height=34,
            padx=12,
            text_color=self._palette()["text"],
            font=ctk.CTkFont(size=12),
        )
        self.status.grid(row=0, column=0, sticky="ew")
        self.status_progress = ctk.CTkLabel(
            self.status_frame,
            textvariable=self.status_progress_var,
            anchor="e",
            width=190,
            height=34,
            padx=14,
            text_color=self._palette()["accent"],
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.status_progress.grid(row=0, column=1, sticky="e")

        self._sort_items_by_priority_and_completion()
        self.refresh_list()
        self.after(300, lambda: self._refresh_real_drop_progress_async(force=True))
        
        # Start offline retry monitor
        self._start_offline_retry_monitor()
        
        # Auto-start queue if enabled
        if self.config_data.auto_start and self.config_data.items:
            # Delay slightly to let UI finish loading
            self.after(1000, self._auto_start_queue)
        
        # Properly close all browsers when closing the app
        try:
            self.protocol("WM_DELETE_WINDOW", self.on_close)
        except Exception:
            pass

    def _available_languages(self):
        codes = list(TRANSLATIONS.keys())
        ordered = []
        for preferred in ("es", "en", "fr"):
            if preferred in codes:
                ordered.append(preferred)
        for code in sorted(c for c in codes if c not in ordered):
            ordered.append(code)
        return ordered

    def _language_label(self, lang_code):
        label_key = f"language_{lang_code}"
        label = translate(self.config_data.language, label_key)
        if label == label_key:
            label = translate(lang_code, label_key)
        if label == label_key:
            label = lang_code
        return label

    def _get_language_choices(self):
        codes = self._available_languages()
        if self.config_data.language not in codes and codes:
            self.config_data.language = codes[0]
            self.config_data.save()
        labels = {code: self._language_label(code) for code in codes}
        self.lang_display_to_code = {label: code for code, label in labels.items()}
        return [labels[code] for code in codes]

    def _configure_kick_public_api(self):
        def save_token(access_token, expires_at):
            self.config_data.kick_api_access_token = access_token or ""
            self.config_data.kick_api_token_expires_at = expires_at or 0
            self.config_data.save()

        configure_public_api(
            self.config_data.kick_api_client_id,
            self.config_data.kick_api_client_secret,
            self.config_data.kick_api_access_token,
            self.config_data.kick_api_token_expires_at,
            save_callback=save_token,
        )

    def save_kick_api_settings(self, client_id, client_secret, status_label=None):
        self.config_data.kick_api_client_id = client_id.strip()
        self.config_data.kick_api_client_secret = client_secret.strip()
        self.config_data.kick_api_access_token = ""
        self.config_data.kick_api_token_expires_at = 0
        self.config_data.save()
        self._configure_kick_public_api()
        if status_label:
            status_label.configure(text="Guardado. Usa Probar API para validar.")
        self._update_overview()

    def test_kick_public_api_settings(self, status_label=None):
        client = KickPublicAPI(
            self.config_data.kick_api_client_id,
            self.config_data.kick_api_client_secret,
            self.config_data.kick_api_access_token,
            self.config_data.kick_api_token_expires_at,
        )
        ok = False
        try:
            ok = client.ensure_token()
        except Exception as e:
            if status_label:
                status_label.configure(text=f"Error con el token API: {e}")
            return
        if ok:
            self.config_data.kick_api_access_token = client.access_token
            self.config_data.kick_api_token_expires_at = client.expires_at
            self.config_data.save()
            self._configure_kick_public_api()
            if status_label:
                status_label.configure(text="Token API listo. API oficial activada.")
        elif status_label:
            status_label.configure(text="Error con el token API. Revisa client id/secret.")

    def open_donation_link(self):
        if not DONATION_URL:
            messagebox.showinfo(self.t("btn_donate"), self.t("donation_not_configured"))
            return
        try:
            webbrowser.open_new_tab(DONATION_URL)
            self.status_var.set(self.t("donation_opened"))
        except Exception as e:
            messagebox.showerror(self.t("error"), self.t("donation_open_fail", e=e))

    def _palette(self):
        if self.config_data.dark_mode:
            return {
                "bg": "#070B10",
                "panel": "#0E151D",
                "panel_alt": "#151F2A",
                "subtle": "#8EA0B5",
                "text": "#ECF4FF",
                "border": "#26384A",
                "accent": "#55FF8A",
                "accent_dark": "#18B957",
                "success": "#22D66F",
                "warning": "#FFB020",
                "danger": "#FF4D5E",
                "info": "#36D7FF",
                "purple": "#9B7BFF",
            }
        return {
            "bg": "#EEF4F9",
            "panel": "#FFFFFF",
            "panel_alt": "#E7EEF6",
            "subtle": "#5E6D80",
            "text": "#0B1520",
            "border": "#C8D5E4",
            "accent": "#18B957",
            "accent_dark": "#0D8F3F",
            "success": "#129C54",
            "warning": "#B7791F",
            "danger": "#D7263D",
            "info": "#007EA7",
            "purple": "#6554C0",
        }

    def _make_metric(self, parent, column, key, label, color):
        palette = self._palette()
        frame = ctk.CTkFrame(
            parent,
            corner_radius=6,
            fg_color=palette["panel_alt"],
            border_width=1,
            border_color=palette["border"],
        )
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        frame.grid_columnconfigure(0, weight=1)
        value_var = tk.StringVar(value="-")
        self.metric_vars[key] = value_var
        ctk.CTkLabel(
            frame,
            text=label,
            text_color=palette["subtle"],
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
        ctk.CTkLabel(
            frame,
            textvariable=value_var,
            text_color=color,
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

    def _set_status_alert(self, alert=False):
        if not hasattr(self, "status"):
            return
        try:
            self.status.configure(
                text_color=self._palette()["danger"] if alert else self._palette()["text"]
            )
        except Exception:
            pass

    def _priority_label(self, priority):
        return "Alta" if bool(priority is True or str(priority).lower() == "high") else "-"

    def _priority_rank(self, priority):
        return 0 if bool(priority is True or str(priority).lower() == "high") else 1

    def _toggle_priority(self, priority):
        return not bool(priority is True or str(priority).lower() == "high")

    def _item_is_complete_for_order(self, item):
        return bool(item.get("finished") or self._is_real_drop_complete(item))

    def _sort_items_by_priority_and_completion(self):
        if not getattr(self, "config_data", None) or not self.config_data.items:
            return False
        if self.workers:
            return False

        indexed_items = list(enumerate(self.config_data.items))

        def sort_key(pair):
            original_idx, item = pair
            complete = self._item_is_complete_for_order(item)
            if complete:
                group = 2
            elif self._priority_rank(item.get("priority")) == 0:
                group = 0
            else:
                group = 1
            return (group, original_idx)

        sorted_items = sorted(indexed_items, key=sort_key)
        old_to_new = {old_idx: new_idx for new_idx, (old_idx, _item) in enumerate(sorted_items)}
        if all(old_idx == new_idx for old_idx, new_idx in old_to_new.items()):
            return False

        self.config_data.items = [item for _old_idx, item in sorted_items]
        if self.queue_current_idx is not None:
            self.queue_current_idx = old_to_new.get(self.queue_current_idx)
        self.config_data.save()
        return True

    def _has_kick_cookies(self):
        return os.path.exists(cookie_file_for_domain("kick.com"))

    def _update_overview(self):
        if not getattr(self, "metric_vars", None):
            return
        total = len(self.config_data.items)
        finished = sum(1 for item in self.config_data.items if item.get("finished"))
        active_url = "-"
        if self.queue_current_idx is not None and 0 <= self.queue_current_idx < total:
            active_url = self.config_data.items[self.queue_current_idx]["url"].rstrip("/").split("/")[-1]
        elif self.workers:
            idx = next(iter(self.workers.keys()))
            if 0 <= idx < total:
                active_url = self.config_data.items[idx]["url"].rstrip("/").split("/")[-1]

        values = {
            "queue": f"{finished}/{total}",
            "active": active_url,
            "cookies": self._auth_status_label(),
        }
        for key, value in values.items():
            if key in self.metric_vars:
                self.metric_vars[key].set(value)
        if hasattr(self, "queue_badge_var"):
            if self.queue_running:
                self.queue_badge_var.set("EN MARCHA")
            elif self.workers:
                self.queue_badge_var.set("ACTIVO")
            else:
                self.queue_badge_var.set("INACTIVO")

    def _auth_status_label(self):
        has_api = bool(
            self.config_data.kick_api_client_id
            and self.config_data.kick_api_client_secret
            and self.config_data.kick_api_access_token
        )
        has_cookies = self._has_kick_cookies()
        if has_api and has_cookies:
            return "API+Cookie"
        if has_api:
            return "API"
        if has_cookies:
            return "Cookie"
        return "Missing"

    # ----------- UI construction -----------
    def _build_sidebar(self):
        palette = self._palette()
        try:
            self.sidebar.configure(
                fg_color=palette["panel"],
                border_width=1,
                border_color=palette["border"],
            )
        except Exception:
            pass

        header = ctk.CTkFrame(self.sidebar, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, padx=12, pady=(14, 12), sticky="ew")
        header.grid_columnconfigure(1, weight=1)

        # Logo (assets/logo.png) + title
        try:
            logo_path = os.path.join(APP_DIR, "assets", "logo.png")
            img = Image.open(logo_path)
            self._logo_img = ctk.CTkImage(
                light_image=img, dark_image=img, size=(30, 30)
            )
            logo_lbl = ctk.CTkLabel(header, image=self._logo_img, text="")
            logo_lbl.grid(row=0, column=0, rowspan=2, padx=(4, 8), pady=4, sticky="w")
        except Exception:
            pass

        title = ctk.CTkLabel(
            header,
            text="KICK DROPS",
            text_color=palette["text"],
            font=ctk.CTkFont(size=17, weight="bold"),
        )
        title.grid(row=0, column=1, padx=0, pady=(2, 0), sticky="w")
        subtitle = ctk.CTkLabel(
            header,
            text="MINER CORE",
            text_color=palette["accent"],
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        subtitle.grid(row=1, column=1, padx=0, pady=(0, 4), sticky="w")

        # Main actions
        btn_add = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_add"),
            command=self.add_link,
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_add.grid(row=1, column=0, padx=14, pady=6, sticky="w")

        btn_remove = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_remove"),
            width=180,
            height=34,
            corner_radius=6,
        )
        # Bind to the underlying tkinter widget to detect Ctrl key
        # We'll handle both normal and Ctrl+click in the bound function
        btn_remove.bind("<Button-1>", self.on_remove_button_click)
        btn_remove.grid(row=2, column=0, padx=14, pady=6, sticky="w")

        btn_start = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_start"),
            command=self.start_selected,
            width=180,
            height=36,
            corner_radius=6,
        )
        btn_start.grid(row=3, column=0, padx=14, pady=(6, 2), sticky="w")

        btn_priority = ctk.CTkButton(
            self.sidebar,
            text="Prioridad alta",
            command=self.toggle_selected_priority,
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_priority.grid(row=4, column=0, padx=14, pady=6, sticky="w")

        btn_stop = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_stop_sel"),
            command=self.stop_selected,
            fg_color="#E74C3C",
            hover_color="#C0392B",
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_stop.grid(row=5, column=0, padx=14, pady=6, sticky="w")

        btn_signin = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_signin"),
            command=self.connect_to_kick,
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_signin.grid(row=6, column=0, padx=14, pady=6, sticky="w")

        btn_drops = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_drops"),
            command=self.show_drops_window,
            fg_color="#9b59b6",
            hover_color="#8e44ad",
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_drops.grid(row=7, column=0, padx=14, pady=6, sticky="w")

        # Settings button
        btn_settings = ctk.CTkButton(
            self.sidebar,
            text="Ajustes",
            command=self.show_settings_window,
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_settings.grid(row=8, column=0, padx=14, pady=(18, 6), sticky="w")

        btn_donate = ctk.CTkButton(
            self.sidebar,
            text=self.t("btn_donate"),
            command=self.open_donation_link,
            width=180,
            height=34,
            corner_radius=6,
        )
        btn_donate.grid(row=9, column=0, padx=14, pady=6, sticky="w")
        try:
            btn_settings.configure(text="Ajustes")
            btn_add.configure(fg_color=palette["accent_dark"], hover_color=palette["accent"])
            btn_start.configure(fg_color=palette["accent_dark"], hover_color=palette["accent"])
            btn_priority.configure(fg_color=palette["warning"], hover_color="#E09A13", text_color="#101820")
            btn_stop.configure(fg_color=palette["danger"], hover_color="#B91C1C")
            btn_drops.configure(fg_color=palette["info"], hover_color="#0369A1")
            btn_donate.configure(fg_color=palette["warning"], hover_color="#E09A13", text_color="#101820")
            for secondary in (btn_remove, btn_signin, btn_settings):
                secondary.configure(
                    fg_color=palette["panel_alt"],
                    hover_color=palette["border"],
                    text_color=palette["text"],
                )
        except Exception:
            pass

        # Initialize toggle variables (used in settings window)
        self.mute_var = tk.BooleanVar(value=bool(self.config_data.mute))
        self.hide_player_var = tk.BooleanVar(value=bool(self.config_data.hide_player))
        self.mini_player_var = tk.BooleanVar(value=bool(self.config_data.mini_player))
        self.background_browser_var = tk.BooleanVar(value=bool(self.config_data.background_browser))
        self.force_160p_var = tk.BooleanVar(value=bool(self.config_data.force_160p))
        self.auto_start_var = tk.BooleanVar(value=bool(self.config_data.auto_start))
        self.theme_var = tk.StringVar(
            value=self.t("theme_dark")
            if self.config_data.dark_mode
            else self.t("theme_light")
        )
        language_choices = self._get_language_choices()
        current_label = self._language_label(self.config_data.language)
        if current_label not in language_choices and language_choices:
            current_label = language_choices[0]
        self.lang_var = tk.StringVar(value=current_label)

    def _build_content(self):
        palette = self._palette()
        try:
            self.content.configure(fg_color="transparent")
        except Exception:
            pass

        header = ctk.CTkFrame(
            self.content,
            corner_radius=6,
            fg_color=palette["panel"],
            border_width=1,
            border_color=palette["border"],
        )
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)

        title = ctk.CTkLabel(
            header,
            text="Panel de canales",
            text_color=palette["text"],
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        title.grid(row=0, column=0, sticky="w", padx=14, pady=(10, 0))
        subtitle = ctk.CTkLabel(
            header,
            text="Prioridad, estado del drop y actividad en tiempo real",
            text_color=palette["subtle"],
            font=ctk.CTkFont(size=11),
        )
        subtitle.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

        self.queue_badge_var = tk.StringVar(value="INACTIVO")
        queue_badge = ctk.CTkLabel(
            header,
            textvariable=self.queue_badge_var,
            text_color="#06100A",
            fg_color=palette["accent_dark"],
            corner_radius=6,
            width=105,
            height=26,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        queue_badge.grid(row=0, column=1, rowspan=2, sticky="e", padx=14, pady=10)

        metrics = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        metrics.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 10))
        for col in range(3):
            metrics.grid_columnconfigure(col, weight=1)
        self.metric_vars = {}
        self._make_metric(metrics, 0, "queue", "PROGRESO", palette["accent"])
        self._make_metric(metrics, 1, "active", "ACTIVO", palette["info"])
        self._make_metric(metrics, 2, "cookies", "SESIÓN", palette["success"])

        # Tableau (ttk.Treeview) dans un CTkFrame
        table_frame = ctk.CTkFrame(
            self.content,
            corner_radius=6,
            fg_color=palette["panel"],
            border_width=1,
            border_color=palette["border"],
        )
        table_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        # Automatic light/dark theme
        if ctk.get_appearance_mode() == "Dark":
            style.theme_use("clam")
            style.configure(
                "Treeview",
                background=palette["panel"],
                fieldbackground=palette["panel"],
                foreground=palette["text"],
                rowheight=34,
                bordercolor=palette["border"],
                borderwidth=0,
                font=("Segoe UI", 10),
            )
            style.configure(
                "Treeview.Heading",
                background="#0A1118" if ctk.get_appearance_mode() == "Dark" else palette["panel_alt"],
                foreground=palette["text"],
                font=("Segoe UI", 10, "bold"),
                relief="flat",
            )
            sel_bg = palette["accent_dark"]
            style.map(
                "Treeview",
                background=[("selected", sel_bg)],
                foreground=[("selected", "white")],
            )
        else:
            style.theme_use("clam")
            style.configure(
                "Treeview",
                background=palette["panel"],
                fieldbackground=palette["panel"],
                foreground=palette["text"],
                rowheight=34,
                bordercolor=palette["border"],
                borderwidth=0,
                font=("Segoe UI", 10),
            )
            style.configure(
                "Treeview.Heading",
                background=palette["panel_alt"],
                foreground=palette["text"],
                font=("Segoe UI", 10, "bold"),
                relief="flat",
            )
            sel_bg = palette["accent_dark"]
            style.map(
                "Treeview",
                background=[("selected", sel_bg)],
                foreground=[("selected", "white")],
            )

        self.tree = ttk.Treeview(
            table_frame,
            columns=("drop", "url", "priority", "elapsed"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("drop", text="Drop")
        self.tree.heading("url", text="URL")
        self.tree.heading("priority", text="Prioridad")
        self.tree.heading("elapsed", text=self.t("col_elapsed"))
        self.tree.column("drop", width=60, anchor="center")
        self.tree.column("url", width=470, anchor="w")
        self.tree.column("priority", width=110, anchor="center")
        self.tree.column("elapsed", width=140, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        yscroll.grid(row=0, column=1, sticky="ns")
        
        # Bind double-click to edit minutes
        self.tree.bind("<Double-Button-1>", self.on_tree_double_click)

        # Colored rows via tags
        try:
            self.tree.tag_configure(
                "odd",
                background="#0B1117" if ctk.get_appearance_mode() == "Dark" else "#F8FAFC",
            )
            self.tree.tag_configure(
                "even",
                background="#101821" if ctk.get_appearance_mode() == "Dark" else palette["panel"],
            )
            self.tree.tag_configure(
                "redo",
                background="#33240C" if ctk.get_appearance_mode() == "Dark" else "#FEF3C7",
            )
            self.tree.tag_configure(
                "paused",
                background="#35151C" if ctk.get_appearance_mode() == "Dark" else "#FEE2E2",
            )
            self.tree.tag_configure(
                "finished",
                background=palette["panel"],
            )
        except Exception:
            pass
        self._update_overview()

    # ----------- Theme -----------
    def show_settings_window(self):
        """Open settings window with all toggles and dropdowns"""
        existing_window = getattr(self, "_settings_window", None)
        try:
            if existing_window is not None and existing_window.winfo_exists():
                existing_window.lift()
                existing_window.focus_force()
                return
        except Exception:
            self._settings_window = None

        # Create settings window
        settings_window = ctk.CTkToplevel(self)
        settings_window.title("Ajustes")
        settings_window.geometry("450x650")
        settings_window.resizable(False, False)
        settings_window.transient(self)

        self._settings_window = settings_window

        def close_settings_window():
            try:
                settings_window.grab_release()
            except Exception:
                pass
            try:
                settings_window.destroy()
            finally:
                if getattr(self, "_settings_window", None) is settings_window:
                    self._settings_window = None

        settings_window.protocol("WM_DELETE_WINDOW", close_settings_window)
        
        # Center the window
        settings_window.update_idletasks()
        x = (settings_window.winfo_screenwidth() // 2) - (450 // 2)
        y = (settings_window.winfo_screenheight() // 2) - (700 // 2)
        settings_window.geometry(f"450x700+{x}+{y}")
        
        # Consistent theme
        ctk.set_appearance_mode("Dark" if self.config_data.dark_mode else "Light")
        
        # Main frame with padding
        main_frame = ctk.CTkFrame(settings_window)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Title
        title_label = ctk.CTkLabel(
            main_frame,
            text="Ajustes",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        title_label.pack(pady=(0, 20))
        
        # Scrollable frame for settings
        scrollable_frame = ctk.CTkScrollableFrame(main_frame)
        scrollable_frame.pack(fill="both", expand=True)
        
        # Player Settings Section
        player_section = ctk.CTkFrame(scrollable_frame)
        player_section.pack(fill="x", pady=(0, 15))
        
        player_title = ctk.CTkLabel(
            player_section,
            text="Reproductor",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        player_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Mute toggle
        sw_mute = ctk.CTkSwitch(
            player_section,
            text=self.t("switch_mute"),
            command=self.on_toggle_mute,
            variable=self.mute_var,
        )
        sw_mute.pack(anchor="w", padx=15, pady=5)
        
        # Hide player toggle
        sw_hide = ctk.CTkSwitch(
            player_section,
            text=self.t("switch_hide"),
            command=self.on_toggle_hide,
            variable=self.hide_player_var,
        )
        sw_hide.pack(anchor="w", padx=15, pady=5)
        
        # Mini player toggle
        sw_mini = ctk.CTkSwitch(
            player_section,
            text=self.t("switch_mini"),
            command=self.on_toggle_mini,
            variable=self.mini_player_var,
        )
        sw_mini.pack(anchor="w", padx=15, pady=5)
        
        # Force 160p toggle
        sw_force_160p = ctk.CTkSwitch(
            player_section,
            text=self.t("switch_force_160p"),
            command=self.on_toggle_force_160p,
            variable=self.force_160p_var,
        )
        sw_force_160p.pack(anchor="w", padx=15, pady=(5, 15))
        
        # Queue Settings Section
        queue_section = ctk.CTkFrame(scrollable_frame)
        queue_section.pack(fill="x", pady=(0, 15))
        
        queue_title = ctk.CTkLabel(
            queue_section,
            text="Arranque automático",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        queue_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Auto-start toggle
        sw_auto_start = ctk.CTkSwitch(
            queue_section,
            text="Iniciar automáticamente al abrir",
            command=self.on_toggle_auto_start,
            variable=self.auto_start_var,
        )
        sw_auto_start.pack(anchor="w", padx=15, pady=(5, 15))
        
        # Appearance Settings Section
        appearance_section = ctk.CTkFrame(scrollable_frame)
        appearance_section.pack(fill="x", pady=(0, 15))
        
        appearance_title = ctk.CTkLabel(
            appearance_section,
            text="Apariencia",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        appearance_title.pack(anchor="w", padx=15, pady=(15, 10))
        
        # Theme dropdown
        theme_label = ctk.CTkLabel(appearance_section, text=self.t("label_theme"))
        theme_label.pack(anchor="w", padx=15, pady=(5, 5))
        theme_menu = ctk.CTkOptionMenu(
            appearance_section,
            values=[self.t("theme_dark"), self.t("theme_light")],
            command=lambda choice: self.change_theme(choice, settings_window),
            variable=self.theme_var,
            width=350,
        )
        theme_menu.pack(anchor="w", padx=15, pady=(0, 10))
        
        # Language dropdown
        language_choices = self._get_language_choices()
        lang_label = ctk.CTkLabel(appearance_section, text=self.t("label_language"))
        lang_label.pack(anchor="w", padx=15, pady=(5, 5))
        lang_menu = ctk.CTkOptionMenu(
            appearance_section,
            values=language_choices,
            command=self.change_language,
            variable=self.lang_var,
            width=350,
        )
        lang_menu.pack(anchor="w", padx=15, pady=(0, 15))

        # Kick Public API Settings Section
        api_section = ctk.CTkFrame(scrollable_frame)
        api_section.pack(fill="x", pady=(0, 15))

        api_title = ctk.CTkLabel(
            api_section,
            text="Kick Public API",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        api_title.pack(anchor="w", padx=15, pady=(15, 10))

        api_help = ctk.CTkLabel(
            api_section,
            text="Opcional. Activa comprobaciones oficiales de directos, canales y categorías.",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            anchor="w",
        )
        api_help.pack(anchor="w", padx=15, pady=(0, 8))

        client_id_var = tk.StringVar(value=self.config_data.kick_api_client_id)
        client_secret_var = tk.StringVar(value=self.config_data.kick_api_client_secret)

        ctk.CTkLabel(api_section, text="Client ID").pack(anchor="w", padx=15, pady=(4, 2))
        client_id_entry = ctk.CTkEntry(api_section, textvariable=client_id_var, width=350)
        client_id_entry.pack(anchor="w", padx=15, pady=(0, 8))

        ctk.CTkLabel(api_section, text="Client Secret").pack(anchor="w", padx=15, pady=(4, 2))
        client_secret_entry = ctk.CTkEntry(api_section, textvariable=client_secret_var, width=350, show="*")
        client_secret_entry.pack(anchor="w", padx=15, pady=(0, 8))

        api_status = ctk.CTkLabel(
            api_section,
            text="Configurada" if self.config_data.kick_api_client_id else "No configurada",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            anchor="w",
        )
        api_status.pack(anchor="w", padx=15, pady=(0, 8))

        api_buttons = ctk.CTkFrame(api_section, fg_color="transparent")
        api_buttons.pack(fill="x", padx=15, pady=(0, 15))
        ctk.CTkButton(
            api_buttons,
            text="Guardar API",
            width=165,
            command=lambda: self.save_kick_api_settings(
                client_id_var.get(), client_secret_var.get(), api_status
            ),
        ).pack(side="left", padx=(0, 10))
        ctk.CTkButton(
            api_buttons,
            text="Probar API",
            width=165,
            command=lambda: self.test_kick_public_api_settings(api_status),
        ).pack(side="left")
        
        # Browser Settings Section
        browser_section = ctk.CTkFrame(scrollable_frame)
        browser_section.pack(fill="x", pady=(0, 15))
        
        browser_title = ctk.CTkLabel(
            browser_section,
            text="Navegador",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        browser_title.pack(anchor="w", padx=15, pady=(15, 10))

        sw_background_browser = ctk.CTkSwitch(
            browser_section,
            text="Segundo plano/headless (puede no contar)",
            command=self.on_toggle_background_browser,
            variable=self.background_browser_var,
        )
        sw_background_browser.pack(anchor="w", padx=15, pady=(0, 10))

        background_help = ctk.CTkLabel(
            browser_section,
            text="Para drops fiables, deja el navegador visible y el reproductor con audio activo.",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray60"),
            anchor="w",
        )
        background_help.pack(anchor="w", padx=15, pady=(0, 10))
        
        # ChromeDriver button
        def choose_chromedriver_wrapper():
            self.choose_chromedriver()
            settings_window.lift()
            settings_window.focus_force()
            # Refresh the window to update labels
            close_settings_window()
            self.show_settings_window()
        
        btn_chromedriver = ctk.CTkButton(
            browser_section,
            text=self.t("btn_chromedriver"),
            command=choose_chromedriver_wrapper,
            width=350,
        )
        btn_chromedriver.pack(anchor="w", padx=15, pady=5)
        
        # Show current chromedriver path if set
        chromedriver_label = ctk.CTkLabel(
            browser_section,
            text=f"Current: {os.path.basename(self.config_data.chromedriver_path) if self.config_data.chromedriver_path else 'Not set'}",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray50")
        )
        chromedriver_label.pack(anchor="w", padx=15, pady=(0, 10))
        
        # Chrome Extension button
        def choose_extension_wrapper():
            self.choose_extension()
            settings_window.lift()
            settings_window.focus_force()
            # Refresh the window to update labels
            close_settings_window()
            self.show_settings_window()
        
        btn_extension = ctk.CTkButton(
            browser_section,
            text=self.t("btn_extension"),
            command=choose_extension_wrapper,
            width=350,
        )
        btn_extension.pack(anchor="w", padx=15, pady=5)
        
        # Show current extension path if set
        extension_label = ctk.CTkLabel(
            browser_section,
            text=f"Current: {os.path.basename(self.config_data.extension_path) if self.config_data.extension_path else 'Not set'}",
            font=ctk.CTkFont(size=11),
            text_color=("gray50", "gray50")
        )
        extension_label.pack(anchor="w", padx=15, pady=(0, 15))
        
        # Close button
        close_btn = ctk.CTkButton(
            settings_window,
            text="Close",
            command=close_settings_window,
            width=200,
        )
        close_btn.pack(pady=15)

    def change_theme(self, choice, source_window=None):
        # CTkOptionMenu invokes this while Tk is still closing the native menu.
        # Applying the global theme immediately can leave Windows/Tk menu state stale.
        if getattr(self, "_pending_theme_after", None) is not None:
            try:
                self.after_cancel(self._pending_theme_after)
            except Exception:
                pass
        self._pending_theme_after = self.after(
            150, lambda selected=choice, window=source_window: self._apply_theme(selected, window)
        )

    def _apply_theme(self, choice, source_window=None):
        self._pending_theme_after = None
        self._release_any_grab()
        reopen_settings = self._destroy_settings_for_theme_change(source_window)
        dark_values = {"Sombre", "Dark"}
        try:
            dark_values.update(
                translate(lang, "theme_dark") for lang in TRANSLATIONS.keys()
            )
        except Exception:
            pass
        dark = choice in dark_values
        self.config_data.dark_mode = dark
        self.config_data.save()
        self.theme_var.set(self.t("theme_dark") if dark else self.t("theme_light"))
        ctk.set_appearance_mode("Dark" if dark else "Light")
        try:
            self.status.configure(fg_color=self._palette()["panel"])
        except Exception:
            pass
        for w in self.sidebar.winfo_children():
            w.destroy()
        self._build_sidebar()
        # Rebuild content (to recalculate Treeview styles)
        for w in self.content.winfo_children():
            w.destroy()
        self._build_content()
        self.refresh_list()
        if reopen_settings:
            self.after(50, self.show_settings_window)

    def _release_any_grab(self):
        try:
            grabbed = self.grab_current()
            if grabbed is not None:
                grabbed.grab_release()
        except Exception:
            pass
        try:
            grabbed_name = self.tk.call("grab", "current")
            if grabbed_name:
                self.tk.call("grab", "release", grabbed_name)
        except Exception:
            pass

    def _destroy_settings_for_theme_change(self, source_window=None):
        settings_window = source_window or getattr(self, "_settings_window", None)
        try:
            if settings_window is not None and settings_window.winfo_exists():
                settings_window.destroy()
                if getattr(self, "_settings_window", None) is settings_window:
                    self._settings_window = None
                self.update_idletasks()
                return True
        except Exception:
            self._settings_window = None
        return False

    # ----------- Language -----------
    def change_language(self, choice):
        mapping = getattr(self, "lang_display_to_code", {})
        new_lang = None

        if isinstance(choice, str):
            new_lang = mapping.get(choice)
            if not new_lang:
                # Fallback: case-insensitive match
                for label, code in mapping.items():
                    if label.lower() == choice.lower():
                        new_lang = code
                        break

        if not new_lang:
            return

        if new_lang == self.config_data.language:
            return  # No change needed

        self.config_data.language = new_lang
        self.config_data.save()

        # Rebuild sidebar & content to refresh text
        try:
            for w in self.sidebar.winfo_children():
                w.destroy()
            self._build_sidebar()
        except Exception:
            pass

        try:
            for w in self.content.winfo_children():
                w.destroy()
            self._build_content()
        except Exception:
            pass

        # Update status bar if it's at the initial text
        try:
            ready_variants = [translate(lang, "status_ready") for lang in TRANSLATIONS]
            if self.status_var.get() in ready_variants:
                self.status_var.set(self.t("status_ready"))
        except Exception:
            pass

    # ----------- Actions -----------
    def on_tree_double_click(self, event):
        """Handle double-click on tree to edit minutes"""
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        
        column = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        
        if not row_id:
            return
        
        if column == "#3":
            idx = int(row_id)
            if idx >= len(self.config_data.items):
                return
            self.tree.selection_set(str(idx))
            self.toggle_selected_priority()
    
    def refresh_list(self):
        self._sort_items_by_priority_and_completion()
        for r in self.tree.get_children():
            self.tree.delete(r)
        for i, item in enumerate(self.config_data.items):
            elapsed = self.workers[i].elapsed_seconds if i in self.workers else 0
            tags = ["odd" if i % 2 else "even"]
            is_complete = bool(item.get("finished") or self._is_real_drop_complete(item))
            if is_complete:
                tags.append("finished")
            self.tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    "✅" if is_complete else "",
                    item["url"],
                    self._priority_label(item.get("priority")),
                    f"{elapsed}s",
                ),
                tags=tuple(tags),
            )
        self._update_overview()

    def add_link(self):
        url = simpledialog.askstring(
            self.t("prompt_live_url_title"), self.t("prompt_live_url_msg")
        )
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        minutes = simpledialog.askinteger(
            self.t("prompt_minutes_title"), self.t("prompt_minutes_msg"), minvalue=0
        )
        self.config_data.add(url, minutes or 0)
        self.refresh_list()
        self.status_var.set(self.t("status_link_added"))
        # Auto-start if enabled and queue not running
        if self.config_data.auto_start and not self.queue_running:
            self.after(500, self._auto_start_queue)

    def on_remove_button_click(self, event):
        """Handle remove button click - check for Ctrl key"""
        # Check if Ctrl key is pressed (state & 0x4 is Control modifier)
        ctrl_pressed = (event.state & 0x4) != 0
        
        if ctrl_pressed:
            # Ctrl is pressed - show clear all dialog
            self.after(0, self.clear_all_items)
        else:
            # Normal remove action
            self.after(0, self.remove_selected)
    
    def clear_all_items(self):
        """Clear all items from the list after confirmation"""
        if not self.config_data.items:
            return  # Nothing to clear
        
        # Show confirmation dialog
        result = messagebox.askyesno(
            "Clear All Items",
            f"Are you sure you want to remove all {len(self.config_data.items)} item(s) from the list?",
            icon="warning"
        )
        
        if result:
            # Stop all running workers
            for idx in list(self.workers.keys()):
                self._stop_worker_at(idx)
            
            # Clear all items
            self.config_data.items = []
            self.config_data.save()
            
            # Refresh UI
            self.refresh_list()
            self.status_var.set("All items cleared")
            debug_print(f"DEBUG: Cleared all items from list")
    
    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx in self.workers:
            self._stop_worker_at(idx)
        self.config_data.remove(idx)
        reindexed_workers = {}
        for old_i, worker in self.workers.items():
            if old_i < idx:
                reindexed_workers[old_i] = worker
            elif old_i > idx:
                reindexed_workers[old_i - 1] = worker
        self.workers = reindexed_workers
        self.refresh_list()
        self.status_var.set(self.t("status_link_removed"))

    def start_selected(self):
        sel = self.tree.selection()
        if not sel:
            self.status_var.set("Selecciona un canal para iniciar")
            return
        idx = int(sel[0])
        self.queue_running = False
        self.queue_current_idx = None
        self._queue_starting = False
        self._start_generation += 1
        self._start_index(idx)

    def toggle_selected_priority(self):
        sel = self.tree.selection()
        if not sel:
            self.status_var.set("Selecciona un canal para cambiar prioridad")
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self.config_data.items):
            return
        if idx in self.workers:
            messagebox.showwarning(
                self.t("warning"),
                self.t("cannot_edit_active_stream")
            )
            return
        item = self.config_data.items[idx]
        item["priority"] = self._toggle_priority(item.get("priority"))
        self.config_data.save()
        self._sort_items_by_priority_and_completion()
        new_idx = self._find_item_index(item)
        if new_idx is None:
            new_idx = idx
        self.refresh_list()
        self.tree.selection_set(str(new_idx))
        self.status_var.set(
            "Prioridad alta activada y canal movido al inicio"
            if item.get("priority")
            else "Prioridad alta desactivada"
        )

    def _stop_worker_at(self, idx, wait=False):
        worker = self.workers.pop(idx, None)
        if not worker:
            return
        try:
            worker.suppress_finish_callback = True
            worker.stop()
        except Exception:
            pass
        if wait:
            try:
                worker.join(timeout=1.5)
            except Exception:
                pass
        try:
            if getattr(worker, "driver", None):
                worker.driver.quit()
        except Exception:
            pass

    def _stop_running_workers(self):
        """Stop current stream workers before starting another stream."""
        if len(self.workers) > 0:
            for running_idx in list(self.workers.keys()):
                self._stop_worker_at(running_idx, wait=False)
                if running_idx < len(self.config_data.items):
                    self.config_data.items[running_idx]["finished"] = False

    def _mark_retry(self, idx, elapsed=None):
        """Show that an item was skipped and can be retried later."""
        self._set_status_alert(True)
        if idx < 0 or idx >= len(self.config_data.items):
            return
        try:
            values = list(self.tree.item(str(idx), "values"))
            values[3] = f"{elapsed}s ({self.t('retry')})" if elapsed is not None else self.t("retry")
            current_tags = set(self.tree.item(str(idx), "tags") or [])
            current_tags.add("redo")
            current_tags.discard("paused")
            current_tags.discard("finished")
            self.tree.item(str(idx), values=values, tags=tuple(current_tags))
        except Exception:
            pass
        self.status_var.set(self.t("offline_wait_retry", url=self.config_data.items[idx]["url"]))
        self._update_overview()

    def _campaign_channel_urls(self, item):
        urls = []
        for ch in item.get("campaign_channels", []):
            ch_url = ch.get("url") if isinstance(ch, dict) else ch
            if ch_url:
                urls.append(ch_url)
        if item.get("url") and item["url"] not in urls:
            urls.append(item["url"])
        return deduplicate(urls)

    def _cached_live_status(self, url, ttl=None):
        ttl = self._live_status_ttl if ttl is None else ttl
        now = time.time()
        cached = self._live_status_cache.get(url)
        if cached and now - cached[0] < ttl:
            return cached[1]
        status = kick_live_status_by_api(url)
        self._live_status_cache[url] = (now, status)
        return status

    def _category_matches_required(self, url, required_category_id):
        if not required_category_id:
            return True
        current_category_id = kick_channel_category_id_by_api(url)
        if current_category_id is None:
            return None
        return str(current_category_id) == str(required_category_id)

    def _has_cookies_for_item_noninteractive(self, item):
        domain = domain_from_url(item.get("url", ""))
        if not domain:
            return False
        if CookieManager.has_saved_session(domain):
            return True
        try:
            return bool(
                CookieManager.import_from_browser(domain)
                and CookieManager.has_saved_session(domain)
            )
        except Exception:
            return False

    def _find_live_campaign_alternative(self, idx):
        """Background-safe alternative selection for campaign channels."""
        if idx < 0 or idx >= len(self.config_data.items):
            return False
        item = self.config_data.items[idx]
        campaign_channels = item.get("campaign_channels", [])
        if not campaign_channels:
            return False

        tried_channels = item.get("tried_channels", [])
        current_url = item.get("url", "")
        if current_url and current_url not in tried_channels:
            tried_channels.append(current_url)

        all_channel_urls = self._campaign_channel_urls(item)
        if all_channel_urls and len(tried_channels) >= len(all_channel_urls):
            tried_channels.clear()
            debug_print(f"DEBUG: Reset tried_channels for campaign {item.get('campaign_id')}")

        for alt_channel in campaign_channels:
            alt_url = alt_channel.get("url") if isinstance(alt_channel, dict) else alt_channel
            if not alt_url or alt_url == current_url or alt_url in tried_channels:
                continue
            if self._cached_live_status(alt_url) is not True:
                continue
            category_match = self._category_matches_required(
                alt_url, item.get("required_category_id")
            )
            if category_match is not True:
                debug_print(
                    f"DEBUG: Skipping {alt_url}; wrong category for drop "
                    f"required={item.get('required_category_id')} match={category_match}"
                )
                continue
            self.config_data.items[idx]["url"] = alt_url
            tried_channels.append(alt_url)
            self.config_data.items[idx]["tried_channels"] = tried_channels
            self.config_data.save()
            debug_print(f"DEBUG: Switched to live campaign alternative: {alt_url}")
            return True

        item["tried_channels"] = tried_channels
        self.config_data.save()
        return False

    def _preflight_item_for_start(self, idx):
        """Run slow start checks outside the Tk thread."""
        if idx < 0 or idx >= len(self.config_data.items):
            return "invalid"

        item = self.config_data.items[idx]
        if item.get("campaign_id"):
            self._refresh_real_drop_progress_sync()
            item = self.config_data.items[idx]
            if self._is_real_drop_complete(item):
                return "complete"

        if item.get("finished"):
            return "skip"

        live_status = self._cached_live_status(item["url"])
        if live_status is False:
            if self._find_live_campaign_alternative(idx):
                item = self.config_data.items[idx]
                if item.get("campaign_id") and self._is_real_drop_complete(item):
                    return "complete"
            else:
                return "offline"

        if live_status is True:
            category_match = self._category_matches_required(
                item["url"], item.get("required_category_id")
            )
            if category_match is False:
                if self._find_live_campaign_alternative(idx):
                    item = self.config_data.items[idx]
                    if item.get("campaign_id") and self._is_real_drop_complete(item):
                        return "complete"
                else:
                    return "wrong_category"

        if not self._has_cookies_for_item_noninteractive(item):
            return "cookies"

        return "ready"

    def _finish_start_failure(self, idx, reason, manual=False):
        self._queue_starting = False
        if idx < 0 or idx >= len(self.config_data.items):
            return
        if reason == "complete":
            self._complete_drop_and_advance(idx, advance=True)
            return
        if reason == "skip":
            self.status_var.set("Canal ya completado, buscando el siguiente drop pendiente")
            self._advance_to_next_pending_drop(idx)
            return
        if reason == "cookies":
            item = self.config_data.items[idx]
            self._mark_retry(idx)
            if manual:
                domain = domain_from_url(item.get("url", ""))
                if domain and messagebox.askyesno(self.t("cookies_missing_title"), self.t("cookies_missing_msg")):
                    self.obtain_cookies_interactively(item["url"], domain)
                    if CookieManager.has_saved_session(domain):
                        self._start_index(idx)
            return
        if reason == "offline":
            self._mark_retry(idx)
            return
        if reason == "wrong_category":
            self._mark_retry(idx)
            self.status_var.set(
                f"Categoría incorrecta para el drop: {self.config_data.items[idx]['url']}"
            )
            return
        self._update_overview()

    def _switch_to_live_campaign_channel(self, idx):
        """Switch campaign item to a live alternative channel when one exists."""
        item = self.config_data.items[idx]
        campaign_channels = item.get("campaign_channels", [])
        if not campaign_channels:
            return False

        tried_channels = item.get("tried_channels", [])
        current_url = item["url"]
        if current_url not in tried_channels:
            tried_channels.append(current_url)

        all_channel_urls = self._campaign_channel_urls(item)
        if all_channel_urls and len(tried_channels) >= len(all_channel_urls):
            tried_channels.clear()
            debug_print(f"DEBUG: Reset tried_channels for campaign {item.get('campaign_id')}")

        for alt_channel in campaign_channels:
            alt_url = alt_channel.get("url") if isinstance(alt_channel, dict) else alt_channel
            if not alt_url or alt_url == current_url or alt_url in tried_channels:
                continue
            if self._cached_live_status(alt_url) is not True:
                continue
            category_match = self._category_matches_required(
                alt_url, item.get("required_category_id")
            )
            if category_match is not True:
                debug_print(
                    f"DEBUG: Skipping {alt_url}; wrong category for drop "
                    f"required={item.get('required_category_id')} match={category_match}"
                )
                continue
            self.config_data.items[idx]["url"] = alt_url
            tried_channels.append(alt_url)
            self.config_data.items[idx]["tried_channels"] = tried_channels
            self.config_data.save()
            self.refresh_list()
            self.status_var.set(f"Switched to {alt_url.split('/')[-1]}")
            debug_print(f"DEBUG: Switched to live campaign alternative: {alt_url}")
            return True

        item["tried_channels"] = tried_channels
        self.config_data.save()
        return False

    def _ensure_cookies_for_item(self, item):
        domain = domain_from_url(item["url"])
        if not domain:
            messagebox.showerror(self.t("error"), self.t("invalid_url"))
            return False

        if CookieManager.has_saved_session(domain):
            return True

        try:
            if CookieManager.import_from_browser(domain) and CookieManager.has_saved_session(domain):
                return True
        except Exception:
            pass

        if self.config_data.auto_start or self.queue_running:
            self.status_var.set(f"Skipping {item['url']} - no cookies")
            return False

        if messagebox.askyesno(self.t("cookies_missing_title"), self.t("cookies_missing_msg")):
            self.obtain_cookies_interactively(item["url"], domain)
            return CookieManager.has_saved_session(domain)
        return False

    def _cumulative_time_callback_for_item(self, item):
        if not item.get("is_global_drop"):
            return None
        campaign_id = item.get("campaign_id")
        if not campaign_id:
            return None

        def get_cumulative_time():
            total = 0
            for other_item in self.config_data.items:
                if other_item.get("campaign_id") == campaign_id:
                    total += other_item.get("cumulative_time", 0)
            return total

        return get_cumulative_time

    def _store_drop_progress_data(self, progress_data):
        if not isinstance(progress_data, list):
            return False
        for progress in progress_data:
            if not isinstance(progress, dict):
                continue
            campaign_id = progress.get("id")
            if campaign_id:
                self.drop_progress_by_campaign[campaign_id] = progress
                self.drop_progress_by_campaign[str(campaign_id)] = progress
        self._drop_progress_last_fetch = time.time()
        return self._sync_finished_items_from_real_progress()

    def _sync_finished_items_from_real_progress(self):
        changed = False
        for item in self.config_data.items:
            campaign_id = item.get("campaign_id")
            if not campaign_id:
                continue
            if (
                campaign_id not in self.drop_progress_by_campaign
                and str(campaign_id) not in self.drop_progress_by_campaign
            ):
                continue
            is_complete = self._is_real_drop_complete(item)
            if bool(item.get("finished")) != is_complete:
                item["finished"] = is_complete
                if is_complete:
                    item["tried_channels"] = []
                changed = True
        if changed:
            self.config_data.save()
            self._sort_items_by_priority_and_completion()
        return changed

    def _store_progress_and_refresh_list(self, progress_data):
        if self._store_drop_progress_data(progress_data):
            self.refresh_list()

    def _format_required_units(self, units):
        try:
            units = int(units or 0)
        except (TypeError, ValueError):
            units = 0
        if units <= 0:
            return ""
        hours, minutes = divmod(units, 60)
        if hours and minutes:
            return f"{hours} h {minutes} min"
        if hours:
            return f"{hours} h"
        return f"{minutes} min"

    def _format_real_drop_progress(self, item):
        if not item:
            return ""
        campaign_id = item.get("campaign_id")
        if not campaign_id:
            return ""
        campaign = self.drop_progress_by_campaign.get(campaign_id) or self.drop_progress_by_campaign.get(str(campaign_id))
        if not isinstance(campaign, dict):
            return ""

        reward = self._current_real_drop_reward(campaign)
        if reward:
            percent = self._reward_progress_percent(reward)
            required = self._format_required_units(reward.get("required_units"))
            return f"Drop: {percent}% de {required}" if required else f"Drop: {percent}%"

        return ""

    def _current_real_drop_reward(self, campaign):
        rewards = [reward for reward in campaign.get("rewards", []) if isinstance(reward, dict)]
        if not rewards:
            return None
        open_rewards = [reward for reward in rewards if not reward.get("claimed")]
        in_progress_rewards = [
            reward
            for reward in open_rewards
            if self._reward_progress_value(reward) > 0
        ]
        candidates = in_progress_rewards or open_rewards or rewards
        return max(candidates, key=self._reward_progress_value)

    def _reward_progress_value(self, reward):
        try:
            return float(reward.get("progress") or 0)
        except (TypeError, ValueError):
            return 0.0

    def _reward_progress_percent(self, reward):
        raw_progress = self._reward_progress_value(reward)
        percent = raw_progress if raw_progress > 1 else raw_progress * 100
        return min(100, max(0, int(round(percent))))

    def _is_real_drop_complete(self, item):
        campaign_id = item.get("campaign_id") if item else None
        if not campaign_id:
            return False
        campaign = self.drop_progress_by_campaign.get(campaign_id) or self.drop_progress_by_campaign.get(str(campaign_id))
        if not isinstance(campaign, dict):
            return False
        rewards = [reward for reward in campaign.get("rewards", []) if isinstance(reward, dict)]
        if not rewards:
            return False
        unclaimed = [reward for reward in rewards if not reward.get("claimed")]
        if not unclaimed:
            return True
        current_reward = self._current_real_drop_reward(campaign)
        return bool(current_reward and self._reward_progress_percent(current_reward) >= 100)

    def _item_drop_key(self, item):
        if not isinstance(item, dict):
            return None
        campaign_id = item.get("campaign_id")
        if campaign_id:
            return f"campaign:{campaign_id}"
        url = item.get("url")
        return f"url:{url}" if url else None

    def _next_drop_start_index(self, current_idx):
        total = len(self.config_data.items)
        if total <= 0:
            return 0
        return (current_idx + 1) % total

    def _advance_to_next_pending_drop(self, current_idx):
        """Continue mining from the next not-completed drop, even after manual Start."""
        self.queue_running = True
        self.queue_current_idx = None
        self._queue_starting = False
        self.after(
            250,
            lambda: self._run_queue_from(
                0, wrap=True, retry_when_busy=True
            ),
        )

    def _mark_campaign_finished(self, campaign_id):
        changed = False
        for item in self.config_data.items:
            if item.get("campaign_id") == campaign_id:
                item["finished"] = True
                item["tried_channels"] = []
                changed = True
        if changed:
            self.config_data.save()
            self.refresh_list()

    def _complete_drop_and_advance(self, idx, advance=True):
        if idx < 0 or idx >= len(self.config_data.items):
            return
        item = self.config_data.items[idx]
        campaign_id = item.get("campaign_id")
        if campaign_id:
            self._mark_campaign_finished(campaign_id)
        elif idx < len(self.config_data.items):
            self.config_data.items[idx]["finished"] = True
            self.config_data.save()
            self.refresh_list()
        if idx in self.workers:
            self._stop_worker_at(idx)
        self.status_var.set("Drop al 100%, buscando el siguiente drop pendiente")
        self._set_status_progress()
        if advance:
            self._advance_to_next_pending_drop(idx)

    def _refresh_real_drop_progress_async(self, force=False, item=None):
        if self._drop_progress_fetching:
            return
        if item is not None and not item.get("campaign_id"):
            return
        if not force and time.time() - self._drop_progress_last_fetch < self._drop_progress_interval:
            return

        self._drop_progress_fetching = True

        def fetch_and_store():
            try:
                result = fetch_drops_progress_direct()
                progress_data = result.get("progress", [])
                progress_data = [p for p in progress_data if isinstance(p, dict)]

                def apply_progress():
                    changed = self._store_drop_progress_data(progress_data)
                    if changed:
                        self.refresh_list()
                    if item is not None:
                        self._set_status_progress(item)
                        idx = self._find_item_index(item)
                        if idx is not None and self._is_real_drop_complete(self.config_data.items[idx]):
                            self._complete_drop_and_advance(idx)

                self.after(0, apply_progress)
            except Exception as e:
                debug_print(f"DEBUG: Real drop progress refresh failed: {e}")
            finally:
                self._drop_progress_fetching = False

        threading.Thread(target=fetch_and_store, daemon=True).start()

    def _find_item_index(self, item):
        for idx, candidate in enumerate(self.config_data.items):
            if candidate is item:
                return idx
        item_url = item.get("url") if isinstance(item, dict) else None
        item_campaign_id = item.get("campaign_id") if isinstance(item, dict) else None
        for idx, candidate in enumerate(self.config_data.items):
            if candidate.get("url") == item_url and candidate.get("campaign_id") == item_campaign_id:
                return idx
        return None

    def _refresh_real_drop_progress_sync(self):
        try:
            result = fetch_drops_progress_direct()
            progress_data = result.get("progress", [])
            progress_data = [p for p in progress_data if isinstance(p, dict)]
            self._store_drop_progress_data(progress_data)
            return True
        except Exception as e:
            debug_print(f"DEBUG: Real drop progress sync failed: {e}")
            return False

    def _set_status_progress(self, item=None, seconds=0):
        real_progress = self._format_real_drop_progress(item)
        if item is not None and item.get("campaign_id"):
            self._refresh_real_drop_progress_async(item=item)
        if real_progress:
            self.status_progress_var.set(real_progress)
            return
        if item is not None:
            self.status_progress_var.set("Drop: cargando..." if item.get("campaign_id") else "Drop: --")
            return
        self.status_progress_var.set("Drop: --")

    def _start_worker_for_item(self, idx):
        item = self.config_data.items[idx]
        stop_event = threading.Event()
        worker = StreamWorker(
            item["url"],
            item["minutes"],
            on_update=lambda s, live: self.on_worker_update(idx, s, live),
            on_finish=lambda e, c: self.on_worker_finish(idx, e, c),
            stop_event=stop_event,
            driver_path=self.config_data.chromedriver_path,
            extension_path=self.config_data.extension_path,
            hide_player=bool(self.hide_player_var.get()),
            mute=bool(self.mute_var.get()),
            mini_player=bool(self.mini_player_var.get()),
            background_browser=bool(self.background_browser_var.get()),
            force_160p=bool(self.config_data.force_160p),
            required_category_id=item.get("required_category_id"),
            cumulative_time_callback=self._cumulative_time_callback_for_item(item),
        )
        self.workers[idx] = worker
        worker.start()
        self.tree.selection_set(str(idx))
        self._set_status_alert(False)
        self.status_var.set(self.t("status_playing", url=item["url"]))
        self._set_status_progress(item, 0)
        self._refresh_real_drop_progress_async(force=True, item=item)
        self._update_overview()
        return True

    def _start_index(self, idx, manual=True):
        """Start one item without blocking the Tk thread."""
        if idx < 0 or idx >= len(self.config_data.items):
            return False
        if self._queue_starting:
            return False

        self._stop_running_workers()
        self._queue_starting = True
        self._start_generation += 1
        start_generation = self._start_generation
        self.status_var.set(f"Preparando {self.config_data.items[idx]['url']}...")
        self._update_overview()

        def preflight():
            try:
                reason = self._preflight_item_for_start(idx)
            except Exception as e:
                debug_print(f"DEBUG: Start preflight failed: {e}")
                reason = "error"

            def apply_result():
                if start_generation != self._start_generation:
                    return
                if reason != "ready":
                    self._finish_start_failure(idx, reason, manual=manual)
                    return
                self._queue_starting = False
                if idx < 0 or idx >= len(self.config_data.items):
                    return
                self.refresh_list()
                self.tree.selection_set(str(idx))
                self._start_worker_for_item(idx)

            self.after(0, apply_result)

        threading.Thread(target=preflight, daemon=True).start()
        return True

    def _start_index_after_switch(self, idx):
        """Compatibility wrapper for older delayed switch callbacks."""
        return self._start_index(idx)

    def start_all_in_order(self):
        self.queue_running = True
        self.queue_current_idx = None
        self._run_queue_from(0)

    def _queue_scan_indices(self, start_idx, wrap=False):
        total = len(self.config_data.items)
        if total <= 0:
            return []
        indices = list(range(total))
        indices.sort(
            key=lambda i: (
                self._priority_rank(self.config_data.items[i].get("priority")),
                i,
            )
        )
        return indices

    def _run_queue_from(self, start_idx: int, wrap=False, retry_when_busy=False):
        """Run queue ensuring only one stream at a time."""
        if len(self.workers) > 0:
            if retry_when_busy:
                self.after(
                    500,
                    lambda idx=start_idx, w=wrap: self._run_queue_from(
                        idx, wrap=w, retry_when_busy=True
                    ),
                )
            return
        if self._queue_starting:
            if retry_when_busy:
                self.after(
                    500,
                    lambda idx=start_idx, w=wrap: self._run_queue_from(
                        idx, wrap=w, retry_when_busy=True
                    ),
                )
            return

        self._queue_starting = True
        self._start_generation += 1
        start_generation = self._start_generation
        self.status_var.set("Preparando canales...")
        self._update_overview()

        def preflight_queue():
            try:
                selected_idx = None
                completed_idxs = []
                retry_idxs = []
                seen_drop_keys = set()
                for i in self._queue_scan_indices(start_idx, wrap=wrap):
                    if i < 0 or i >= len(self.config_data.items):
                        continue
                    item = self.config_data.items[i]
                    if item.get("finished"):
                        continue
                    drop_key = self._item_drop_key(item)
                    if drop_key and drop_key in seen_drop_keys:
                        continue
                    if drop_key:
                        seen_drop_keys.add(drop_key)
                    reason = self._preflight_item_for_start(i)
                    if reason == "ready":
                        selected_idx = i
                        break
                    if reason == "complete":
                        completed_idxs.append(i)
                        continue
                    if reason in ("offline", "cookies", "wrong_category"):
                        retry_idxs.append(i)
                        continue
            except Exception as e:
                debug_print(f"DEBUG: Queue preflight failed: {e}")
                selected_idx = None
                completed_idxs = []
                retry_idxs = []

            def apply_queue_result():
                if start_generation != self._start_generation:
                    return
                self._queue_starting = False
                for completed_idx in completed_idxs:
                    self._complete_drop_and_advance(completed_idx, advance=False)
                for retry_idx in retry_idxs:
                    self._mark_retry(retry_idx)
                if selected_idx is None:
                    self.queue_running = False
                    self.queue_current_idx = None
                    self._set_status_alert(False)
                    self.status_var.set(self.t("queue_finished_status"))
                    self._set_status_progress()
                    self._update_overview()
                    return

                self.refresh_list()
                self.queue_current_idx = selected_idx
                self.tree.selection_set(str(selected_idx))
                if self._start_worker_for_item(selected_idx):
                    self.status_var.set(
                        self.t("queue_running_status", url=self.config_data.items[selected_idx]["url"])
                    )
                else:
                    self.queue_current_idx = None
                    self._run_queue_from(self._next_drop_start_index(selected_idx), wrap=True)

            self.after(0, apply_queue_result)

        threading.Thread(target=preflight_queue, daemon=True).start()

    def stop_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self.queue_running = False
        self.queue_current_idx = None
        self._queue_starting = False
        self._start_generation += 1
        if idx in self.workers:
            self._stop_worker_at(idx)
            self._set_status_alert(False)
            self.status_var.set(self.t("status_stopped"))
            self._set_status_progress()
            # Update the display
            if str(idx) in self.tree.get_children():
                values = list(self.tree.item(str(idx), "values"))
                values[3] = f"{values[3]} ({self.t('tag_stop')})"
                self.tree.item(str(idx), values=values)
            self._update_overview()

    def obtain_cookies_interactively(self, url, domain):
        try:
            drv = make_chrome_driver(
                headless=False,
                driver_path=self.config_data.chromedriver_path,
                extension_path=self.config_data.extension_path,
            )
            self._interactive_driver = drv
        except Exception as e:
            messagebox.showerror(self.t("error"), self.t("chrome_start_fail", e=e))
            return
        drv.get(url)
        messagebox.showinfo(self.t("action_required"), self.t("sign_in_and_click_ok"))
        try:
            CookieManager.save_cookies(drv, domain)
            if CookieManager.has_saved_session(domain):
                messagebox.showinfo(
                    self.t("ok"), self.t("cookies_saved_for", domain=domain)
                )
            else:
                messagebox.showwarning(
                    self.t("error"),
                    "Cookies saved, but no Kick session_token was found. Make sure you are fully signed in before pressing OK.",
                )
        except Exception as e:
            messagebox.showerror(self.t("error"), self.t("cannot_save_cookies", e=e))
        finally:
            try:
                drv.quit()
            except Exception:
                pass
            finally:
                self._interactive_driver = None

    def on_close(self):
        # Stop the queue and close all browser windows
        try:
            self.queue_running = False
            self._queue_starting = False
            self._start_generation += 1
        except Exception:
            pass

        # Close Chrome cookie import window if open
        try:
            if self._interactive_driver:
                try:
                    self._interactive_driver.quit()
                except Exception:
                    pass
                self._interactive_driver = None
        except Exception:
            pass

        # Stop and close all Selenium drivers from workers
        for idx, w in list(self.workers.items()):
            try:
                w.stop()
            except Exception:
                pass
            try:
                if getattr(w, "driver", None):
                    try:
                        w.driver.quit()
                    except Exception:
                        pass
            except Exception:
                pass

        # Wait briefly for threads to stop
        for idx, w in list(self.workers.items()):
            try:
                w.join(timeout=2.5)
            except Exception:
                pass

        # Close the application
        try:
            self.destroy()
        except Exception:
            os._exit(0)

    def connect_to_kick(self):
        sel = self.tree.selection()
        if sel:
            idx = int(sel[0])
            url = self.config_data.items[idx]["url"]
            domain = domain_from_url(url)
        else:
            url = "https://kick.com"
            domain = "kick.com"
        # Attempt automatic cookie import from browser
        try:
            if CookieManager.import_from_browser(domain) and CookieManager.has_saved_session(domain):
                messagebox.showinfo(
                    self.t("ok"), self.t("cookies_saved_for", domain=domain)
                )
                return
        except Exception:
            pass
        # Otherwise, fall back to existing interactive method
        if messagebox.askyesno(
            self.t("connect_title"), self.t("open_url_to_get_cookies", url=url)
        ):
            self.obtain_cookies_interactively(url, domain)

    def choose_chromedriver(self):
        path = filedialog.askopenfilename(
            title=self.t("pick_chromedriver_title"),
            filetypes=[(self.t("executables_filter"), "*.exe;*")],
        )
        if not path:
            return
        self.config_data.chromedriver_path = path
        self.config_data.save()
        messagebox.showinfo(self.t("ok"), self.t("chromedriver_set", path=path))

    def choose_extension(self):
        path = filedialog.askopenfilename(
            title=self.t("pick_extension_title"),
            filetypes=[("CRX", "*.crx"), (self.t("all_files_filter"), "*.*")],
        )
        if not path:
            return
        self.config_data.extension_path = path
        self.config_data.save()
        messagebox.showinfo(self.t("ok"), self.t("extension_set", path=path))

    def show_drops_window(self):
        """Opens a window to display and select drop campaigns"""
        drops_window = ctk.CTkToplevel(self)
        drops_window.title(self.t("drops_title"))
        drops_window.geometry("1000x700")
        drops_window.minsize(900, 600)
        
        # Keep window on top
        drops_window.attributes('-topmost', True)
        drops_window.lift()
        drops_window.focus_force()

        # Consistent theme
        ctk.set_appearance_mode("Dark" if self.config_data.dark_mode else "Light")

        # Main frame with background color
        main_frame = ctk.CTkFrame(drops_window, fg_color=("gray92", "gray14"))
        main_frame.pack(fill="both", expand=True, padx=0, pady=0)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(1, weight=1)

        # Header with refresh button
        header_frame = ctk.CTkFrame(main_frame, fg_color=("gray86", "gray17"), corner_radius=0, height=60)
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)
        header_frame.grid_propagate(False)

        status_label = ctk.CTkLabel(
            header_frame, text=self.t("drops_loading"), 
            font=ctk.CTkFont(size=16, weight="bold")
        )
        status_label.grid(row=0, column=0, sticky="w", padx=20, pady=15)

        scrollable_frame = ctk.CTkScrollableFrame(
            main_frame, 
            label_text="",
            fg_color=("gray92", "gray14")
        )
        scrollable_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=15)
        scrollable_frame.grid_columnconfigure(0, weight=1)

        refresh_btn = ctk.CTkButton(
            header_frame,
            text=self.t("btn_refresh_drops"),
            width=130,
            height=35,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("#3b82f6", "#2563eb"),
            hover_color=("#2563eb", "#1d4ed8"),
            command=lambda: self._refresh_drops(scrollable_frame, status_label),
        )
        refresh_btn.grid(row=0, column=1, padx=20, pady=15)

        # Refresh function for buttons
        def refresh_callback():
            threading.Thread(target=lambda: self._refresh_drops(scrollable_frame, status_label), daemon=True).start()
        
        # Store reference for buttons
        self._current_drops_refresh = refresh_callback
        
        # Load initial campaigns in a separate thread
        def load_and_focus():
            self._refresh_drops(scrollable_frame, status_label)
            # Bring window to front after loading
            try:
                drops_window.lift()
                drops_window.focus_force()
            except:
                pass
        
        threading.Thread(target=load_and_focus, daemon=True).start()

    def _refresh_drops(self, scrollable_frame, status_label):
        """Refreshes the list of drop campaigns with integrated progress"""

        # Clean the frame
        def clear_frame():
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            status_label.configure(text=self.t("drops_loading"))

        self.after(0, clear_frame)

        def display_campaigns():
            driver = None
            try:
                # Fetch both campaigns and progress using a single Chrome instance
                result = fetch_drops_campaigns_and_progress()
                campaigns = result.get("campaigns", [])
                progress_data = result.get("progress", [])
                progress_data = [p for p in progress_data if isinstance(p, dict)]
                driver = result.get("driver")
                self.after(0, lambda data=progress_data: self._store_progress_and_refresh_list(data))
                
                if not campaigns:
                    status_label.configure(text=self.t("drops_error"))
                    no_data_label = ctk.CTkLabel(
                        scrollable_frame,
                        text=self.t("drops_error"),
                        font=ctk.CTkFont(size=12),
                        text_color="gray",
                    )
                    no_data_label.grid(row=0, column=0, pady=20)
                    return

                # Create a progress lookup by campaign ID
                progress_by_id = {}
                for prog in progress_data:
                    if not isinstance(prog, dict):
                        continue  # Skip unexpected progress entries
                    campaign_id = prog.get("id")
                    if campaign_id:
                        progress_by_id[campaign_id] = prog
                
                # Merge progress data into campaigns
                for campaign in campaigns:
                    campaign_id = campaign.get("id")
                    if campaign_id in progress_by_id:
                        # Campaign has progress - merge progress info
                        prog = progress_by_id[campaign_id]
                        campaign["progress_data"] = prog
                        campaign["progress_status"] = prog.get("status", "not_started")
                        campaign["progress_units"] = prog.get("progress_units", 0)
                        
                        # Merge category from progress data if not already in campaign
                        if "category" in prog and "category" not in campaign:
                            campaign["category"] = prog["category"]
                        elif "category" in prog:
                            # Update category if progress has more complete data
                            campaign["category"] = prog["category"]
                        
                        # Merge reward progress
                        reward_progress = {}
                        for reward in prog.get("rewards", []):
                            reward_id = reward.get("id")
                            if reward_id:
                                reward_progress[reward_id] = {
                                    "progress": reward.get("progress", 0.0),
                                    "claimed": reward.get("claimed", False),
                                    "required_units": reward.get("required_units", 0),
                                }
                        
                        # Attach progress to each reward in campaign
                        for reward in campaign.get("rewards", []):
                            reward_id = reward.get("id")
                            if reward_id in reward_progress:
                                reward["progress"] = reward_progress[reward_id]["progress"]
                                reward["claimed"] = reward_progress[reward_id]["claimed"]
                                reward["progress_required_units"] = reward_progress[reward_id]["required_units"]
                    else:
                        # Campaign has no progress - not started
                        campaign["progress_data"] = None
                        campaign["progress_status"] = "not_started"
                        campaign["progress_units"] = 0
                        for reward in campaign.get("rewards", []):
                            reward["progress"] = 0.0
                            reward["claimed"] = False

                # Filter campaigns into active and expired
                active_campaigns = []
                expired_campaigns = []
                
                for campaign in campaigns:
                    if is_campaign_expired(campaign):
                        expired_campaigns.append(campaign)
                    else:
                        active_campaigns.append(campaign)
                
                # Group active campaigns by game and sort by progress status
                games = {}
                for campaign in active_campaigns:
                    # Double-check: skip if expired (safety check)
                    if is_campaign_expired(campaign):
                        continue
                    game_name = campaign["game"]
                    if game_name not in games:
                        games[game_name] = {
                            "image": campaign.get("game_image", ""),
                            "campaigns": [],
                        }
                    games[game_name]["campaigns"].append(campaign)
                
                # Sort campaigns within each game by progress status
                # Priority: in progress > not started > claimed/completed
                def sort_key(campaign):
                    status = campaign.get("progress_status", "not_started")
                    if status == "in progress":
                        return 0
                    elif status == "not_started":
                        return 1
                    elif status == "claimed":
                        return 2
                    else:
                        return 3
                
                for game_name, game_data in games.items():
                    game_data["campaigns"].sort(key=sort_key)
                
                # Sort games by priority: games with in-progress campaigns first
                def game_priority(game_data):
                    campaigns = game_data["campaigns"]
                    # Check if any campaign is in progress
                    has_in_progress = any(c.get("progress_status") == "in progress" for c in campaigns)
                    if has_in_progress:
                        return 0
                    # Check if any campaign is not started
                    has_not_started = any(c.get("progress_status") == "not_started" for c in campaigns)
                    if has_not_started:
                        return 1
                    return 2
                
                # Convert to list, sort, then back to dict (or use OrderedDict)
                games_list = sorted(games.items(), key=lambda x: game_priority(x[1]))
                games = dict(games_list)

                status_text = self.t("drops_loaded", count=len(active_campaigns))
                if expired_campaigns:
                    status_text += f" ({len(expired_campaigns)} expired)"
                status_label.configure(text=status_text)

                # Add toggle for showing expired campaigns
                if not hasattr(scrollable_frame, "_show_expired_var"):
                    scrollable_frame._show_expired_var = tk.BooleanVar(value=False)
                
                show_expired = scrollable_frame._show_expired_var.get()
                
                # Display each game with its campaigns
                row_idx = 0
                for game_name, game_data in games.items():
                    # Separate campaigns into active and completed
                    game_active_campaigns = []
                    game_completed_campaigns = []
                    
                    for campaign in game_data["campaigns"]:
                        status = campaign.get("progress_status", "not_started")
                        if status == "claimed":
                            game_completed_campaigns.append(campaign)
                        else:
                            game_active_campaigns.append(campaign)
                    # Frame for game (collapsible) - improved style
                    game_frame = ctk.CTkFrame(
                        scrollable_frame, 
                        corner_radius=12,
                        border_width=2,
                        border_color=("#3b82f6", "#2563eb")
                    )
                    game_frame.grid(row=row_idx, column=0, sticky="ew", padx=0, pady=10)
                    game_frame.grid_columnconfigure(0, weight=1)

                    # Variable for toggle collapse
                    is_expanded = tk.BooleanVar(value=True)

                    # Game header (clickable to collapse/expand) - larger and colored
                    game_header = ctk.CTkFrame(
                        game_frame, 
                        fg_color=("#e0f2fe", "#1e3a5f"),
                        cursor="hand2",
                        corner_radius=10
                    )
                    game_header.grid(row=0, column=0, sticky="ew", padx=3, pady=3)
                    # Don't expand any column - let content determine width
                    game_header.grid_columnconfigure(3, weight=1)  # Expand the empty space column

                    # Expand/collapse icon - more visible
                    collapse_icon = ctk.CTkLabel(
                        game_header, 
                        text="▼", 
                        font=ctk.CTkFont(size=14, weight="bold"),
                        text_color=("#3b82f6", "#60a5fa")
                    )
                    collapse_icon.grid(row=0, column=0, padx=(15, 10), pady=12)

                    # Game image (if available) - larger
                    col_offset = 1
                    if game_data["image"]:
                        try:
                            # Download and display game image
                            with urllib.request.urlopen(
                                game_data["image"], timeout=3
                            ) as response:
                                image_data = response.read()
                            game_img = Image.open(BytesIO(image_data))
                            game_img = game_img.resize(
                                (48, 48), Image.Resampling.LANCZOS
                            )
                            game_photo = ctk.CTkImage(
                                light_image=game_img, dark_image=game_img, size=(48, 48)
                            )

                            img_label = ctk.CTkLabel(
                                game_header, image=game_photo, text="", cursor="hand2"
                            )
                            img_label.image = game_photo
                            img_label.grid(row=0, column=1, padx=(0, 12))
                            col_offset = 2
                        except Exception as e:
                            print(f"Could not load game image: {e}")

                    # Game name - larger and colored
                    game_label = ctk.CTkLabel(
                        game_header,
                        text=game_name,
                        font=ctk.CTkFont(size=20, weight="bold"),
                        text_color=("#1e40af", "#93c5fd")
                    )
                    game_label.grid(row=0, column=col_offset, sticky="w", padx=(0, 0))
                    
                    # Spacer column to push badge to the right
                    # (column 3 has weight=1)

                    # Number of campaigns - styled badge, aligned right
                    count_label = ctk.CTkLabel(
                        game_header,
                        text=f"{len(game_data['campaigns'])} campaign{'s' if len(game_data['campaigns']) > 1 else ''}",
                        font=ctk.CTkFont(size=11, weight="bold"),
                        fg_color=("#bfdbfe", "#1e40af"),
                        corner_radius=12,
                        padx=10,
                        pady=4
                    )
                    count_label.grid(row=0, column=4, sticky="e", padx=(15, 15))

                    # Campaigns frame (can be hidden)
                    campaigns_container = ctk.CTkFrame(
                        game_frame, fg_color="transparent"
                    )
                    campaigns_container.grid(row=1, column=0, sticky="ew")
                    campaigns_container.grid_columnconfigure(0, weight=1)

                    # Fonction toggle
                    def toggle_collapse(
                        event=None,
                        icon=collapse_icon,
                        container=campaigns_container,
                        var=is_expanded,
                    ):
                        if var.get():
                            container.grid_remove()
                            icon.configure(text="▶")
                            var.set(False)
                        else:
                            container.grid()
                            icon.configure(text="▼")
                            var.set(True)

                    # Make header clickable
                    game_header.bind("<Button-1>", toggle_collapse)
                    game_label.bind("<Button-1>", toggle_collapse)
                    collapse_icon.bind("<Button-1>", toggle_collapse)
                    count_label.bind("<Button-1>", toggle_collapse)
                    # Bind img_label if it exists
                    for widget in game_header.winfo_children():
                        if isinstance(widget, ctk.CTkLabel) and hasattr(
                            widget, "image"
                        ):
                            widget.bind("<Button-1>", toggle_collapse)

                    # Display active campaigns first
                    camp_idx = 0
                    for campaign in game_active_campaigns:
                        self._create_campaign_display(campaigns_container, campaign, camp_idx, scrollable_frame, game_data, status_label)
                        camp_idx += 1
                    
                    # Display completed campaigns in a collapsible section
                    if game_completed_campaigns:
                        # Add separator if there are active campaigns
                        if active_campaigns:
                            separator = ctk.CTkFrame(campaigns_container, fg_color="transparent", height=2)
                            separator.grid(row=camp_idx, column=0, sticky="ew", padx=8, pady=6)
                            camp_idx += 1
                        
                        # Collapsible header for completed campaigns
                        completed_header_frame = ctk.CTkFrame(
                            campaigns_container,
                            fg_color=("gray85", "#2d3748"),
                            corner_radius=8,
                            cursor="hand2"
                        )
                        completed_header_frame.grid(row=camp_idx, column=0, sticky="ew", padx=8, pady=6)
                        completed_header_frame.grid_columnconfigure(1, weight=1)
                        
                        completed_expanded = tk.BooleanVar(value=False)  # Collapsed by default
                        
                        completed_collapse_icon = ctk.CTkLabel(
                            completed_header_frame,
                            text="▶",
                            font=ctk.CTkFont(size=12, weight="bold"),
                            text_color=("gray60", "gray40")
                        )
                        completed_collapse_icon.grid(row=0, column=0, padx=(12, 8), pady=8)
                        
                        completed_header_label = ctk.CTkLabel(
                            completed_header_frame,
                            text=f"{self.t('drops_completed_campaigns')} ({len(game_completed_campaigns)})",
                            font=ctk.CTkFont(size=12, weight="bold"),
                            text_color=("gray60", "gray40")
                        )
                        completed_header_label.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=8)
                        
                        # Container for completed campaigns
                        completed_container = ctk.CTkFrame(
                            campaigns_container,
                            fg_color="transparent"
                        )
                        completed_container.grid(row=camp_idx + 1, column=0, sticky="ew")
                        completed_container.grid_columnconfigure(0, weight=1)
                        completed_container.grid_remove()  # Hidden by default
                        
                        def toggle_completed(event=None):
                            if completed_expanded.get():
                                completed_container.grid_remove()
                                completed_collapse_icon.configure(text="▶")
                                completed_expanded.set(False)
                            else:
                                completed_container.grid()
                                completed_collapse_icon.configure(text="▼")
                                completed_expanded.set(True)
                        
                        completed_header_frame.bind("<Button-1>", toggle_completed)
                        completed_collapse_icon.bind("<Button-1>", toggle_completed)
                        completed_header_label.bind("<Button-1>", toggle_completed)
                        
                        # Display completed campaigns
                        for comp_idx, campaign in enumerate(game_completed_campaigns):
                            self._create_campaign_display(completed_container, campaign, comp_idx, scrollable_frame, game_data, status_label)
                        
                        camp_idx += 2  # Skip header and container rows
                    
                    row_idx += 1
                
                # Display expired campaigns section if toggle is on
                if expired_campaigns and hasattr(scrollable_frame, "_show_expired_var") and scrollable_frame._show_expired_var.get():
                        expired_separator = ctk.CTkFrame(scrollable_frame, fg_color=("gray70", "gray30"), height=2)
                        expired_separator.grid(row=row_idx, column=0, sticky="ew", padx=0, pady=15)
                        row_idx += 1
                        
                        expired_label = ctk.CTkLabel(
                            scrollable_frame,
                            text=f"⏰ Expired Campaigns ({len(expired_campaigns)})",
                            font=ctk.CTkFont(size=14, weight="bold"),
                            text_color=("#6b7280", "#9ca3af"),
                        )
                        expired_label.grid(row=row_idx, column=0, sticky="w", padx=15, pady=10)
                        row_idx += 1
                        
                        for exp_idx, campaign in enumerate(expired_campaigns):
                            self._create_campaign_display(scrollable_frame, campaign, exp_idx, scrollable_frame, {"image": ""}, status_label)
                            row_idx += 1
                
                # Force update
                scrollable_frame.update_idletasks()
            except Exception as e:
                status_label.configure(text=f"Error: {str(e)}")
                import traceback
                traceback.print_exc()
            finally:
                # Close driver after displaying all campaigns
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass

        # Call on UI thread in background to avoid blocking
        threading.Thread(target=display_campaigns, daemon=True).start()

    def _get_campaign_category_id(self, campaign):
        """Return the Kick category ID attached to a campaign, when available."""
        category = campaign.get("category", {})
        if isinstance(category, dict) and category.get("id"):
            return category.get("id")

        progress_data = campaign.get("progress_data", {})
        if isinstance(progress_data, dict):
            progress_category = progress_data.get("category", {})
            if isinstance(progress_category, dict) and progress_category.get("id"):
                return progress_category.get("id")

        return campaign.get("category_id")

    def _auto_find_streamers_for_game(self, campaign, category_id, scrollable_frame, status_label):
        """Auto-find and add live streamers for a global drop campaign"""
        def find_and_add():
            game_name = campaign.get('game', 'game')
            debug_print(f"DEBUG: Starting search for live streamers")
            debug_print(f"DEBUG: Campaign: {campaign.get('name', 'unknown')}")
            debug_print(f"DEBUG: Game: {game_name}")
            debug_print(f"DEBUG: Category ID: {category_id}")
            
            status_label.configure(text=f"🔍 Searching for live streamers of {game_name}...")
            
            # Use existing driver from drops window if available, or create new one
            driver = None
            try:
                debug_print("DEBUG: Attempting to get driver from drops fetch...")
                # Try to get driver from current drops fetch
                result = fetch_drops_campaigns_and_progress()
                driver = result.get("driver")
                if driver:
                    debug_print("DEBUG: Reusing existing driver")
                else:
                    debug_print("DEBUG: No existing driver, will create new one")
            except Exception as e:
                debug_print(f"DEBUG: Error getting driver: {e}")
                pass
            
            debug_print(f"DEBUG: Calling fetch_live_streamers_by_category with category_id={category_id}")
            streamers = fetch_live_streamers_by_category(category_id, limit=24, driver=driver)
            debug_print(f"DEBUG: Found {len(streamers)} streamers")
            
            if not streamers:
                status_label.configure(text=f"❌ No live streamers found for {game_name}")
                debug_print(f"DEBUG: No streamers found, closing driver if needed")
                if driver:
                    try:
                        driver.quit()
                    except:
                        pass
                return
            
            debug_print(f"DEBUG: Processing {len(streamers)} streamers to add to queue")
            status_label.configure(text=f"📝 Adding {len(streamers)} streamer(s) to queue...")
            
            # Calculate maximum required time from rewards (cumulative drops)
            rewards = campaign.get("rewards", [])
            max_required_minutes = 0
            for reward in rewards:
                required_units = reward.get("required_units", 0)
                if required_units > max_required_minutes:
                    max_required_minutes = required_units
            
            # If no rewards found, default to 120
            if max_required_minutes == 0:
                max_required_minutes = 120
            
            debug_print(f"DEBUG: Campaign has {len(rewards)} rewards, max required: {max_required_minutes} minutes")
            
            # Add all found streamers to queue
            count = 0
            skipped = 0
            campaign_id = campaign.get("id")
            all_streamers = [{"url": s["url"], "username": s["username"]} for s in streamers]
            
            for streamer in streamers:
                try:
                    url = streamer["url"]
                    username = streamer.get("username", "unknown")
                    debug_print(f"DEBUG: Processing streamer: {username} ({url})")
                    
                    if self._is_channel_in_list(url):
                        debug_print(f"DEBUG: Streamer {username} already in list, skipping")
                        skipped += 1
                        continue
                    
                    # Store all streamers as alternatives for each other
                    # Use max_required_minutes for cumulative drops
                    debug_print(f"DEBUG: Adding {username} to queue with target: {max_required_minutes} minutes")
                    self.config_data.add(
                        url, 
                        max_required_minutes, 
                        campaign_id, 
                        all_streamers,
                        required_category_id=category_id,
                        is_global_drop=True
                    )
                    count += 1
                except Exception as e:
                    debug_print(f"DEBUG: Error adding streamer {streamer.get('username', 'unknown')}: {e}")
                    import traceback
                    traceback.print_exc()
            
            debug_print(f"DEBUG: Added {count} streamers, skipped {skipped} (already in list)")
            self.refresh_list()
            status_label.configure(text=f"✅ Added {count} live streamer(s) for {game_name}" + (f" ({skipped} already in list)" if skipped > 0 else ""))
            
            # Auto-start if enabled
            if self.config_data.auto_start and not self.queue_running:
                debug_print("DEBUG: Auto-start enabled, starting queue")
                self.after(500, self._auto_start_queue)
            else:
                debug_print("DEBUG: Auto-start disabled or queue already running")
            
            if driver:
                try:
                    debug_print("DEBUG: Closing driver")
                    driver.quit()
                except Exception as e:
                    debug_print(f"DEBUG: Error closing driver: {e}")
        
        threading.Thread(target=find_and_add, daemon=True).start()

    def _create_campaign_display(self, parent, campaign, camp_idx, scrollable_frame, game_data, status_label=None):
        """Helper function to create a campaign display frame"""
        try:
            campaign_frame = ctk.CTkFrame(
                parent,
                corner_radius=10,
                fg_color=("white", "#1f2937"),
                border_width=1,
                border_color=("#d1d5db", "#374151")
            )
            campaign_frame.grid(
                row=camp_idx, column=0, sticky="ew", padx=8, pady=6
            )
            campaign_frame.grid_columnconfigure(0, weight=1)

            # Campaign header - improved style
            header = ctk.CTkFrame(campaign_frame, fg_color="transparent")
            header.grid(row=0, column=0, sticky="ew", padx=15, pady=(12, 8))
            header.grid_columnconfigure(1, weight=1)
            campaign_channels = campaign.get("channels", [])
            category_id = self._get_campaign_category_id(campaign)
            campaign_has_channels = bool(campaign_channels)
            all_channels_added = (
                campaign_has_channels
                and all(
                    self._is_channel_in_list(ch.get("url") if isinstance(ch, dict) else ch)
                    for ch in campaign_channels
                )
            )

            campaign_name_label = ctk.CTkLabel(
                header,
                text=campaign["name"],
                font=ctk.CTkFont(size=14, weight="bold"),
                anchor="w"
            )
            campaign_name_label.grid(
                row=0, column=0, columnspan=2, sticky="w"
            )

            # Status badge - show progress status if available
            progress_status = campaign.get("progress_status", "not_started")
            if progress_status == "not_started":
                status_text = "ACTIVA" if campaign["status"] == "active" else campaign["status"].upper()
                status_color = ("#10b981", "#059669") if campaign["status"] == "active" else ("#6b7280", "#4b5563")
            elif progress_status == "in progress":
                status_text = "EN PROGRESO"
                status_color = ("#f59e0b", "#d97706")
            elif progress_status == "claimed":
                status_text = "RECLAMADO"
                status_color = ("#10b981", "#059669")
            else:
                status_text = campaign["status"].upper()
                status_color = ("#6b7280", "#4b5563")
            
            status_badge = ctk.CTkLabel(
                header,
                text=status_text,
                font=ctk.CTkFont(size=10, weight="bold"),
                fg_color=status_color,
                text_color="white",
                corner_radius=6,
                padx=10,
                pady=4,
            )
            status_badge.grid(row=0, column=2, sticky="e")

            choose_enabled = campaign_has_channels or bool(category_id)
            choose_btn = ctk.CTkButton(
                header,
                text=self.t("btn_unchoose_campaign") if all_channels_added else "Choose channel",
                width=150,
                height=28,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color=("#ef4444", "#dc2626") if all_channels_added else ("#10b981", "#059669"),
                hover_color=("#dc2626", "#b91c1c") if all_channels_added else ("#059669", "#047857"),
                corner_radius=6,
                state="normal" if choose_enabled else "disabled",
            )
            choose_btn.grid(row=0, column=3, sticky="e", padx=(10, 0))

            def choose_campaign(c=campaign, btn=choose_btn, cid=category_id):
                if c.get("channels"):
                    all_added_now = all(
                        self._is_channel_in_list(ch.get("url") if isinstance(ch, dict) else ch)
                        for ch in c["channels"]
                    )
                    if all_added_now:
                        self._remove_all_campaign_channels(c)
                        btn.configure(
                            text="Choose channel",
                            fg_color=("#10b981", "#059669"),
                            hover_color=("#059669", "#047857"),
                        )
                        if status_label:
                            status_label.configure(
                                text=self.t(
                                    "drops_campaign_unselected",
                                    campaign=c.get("name", "")
                                )
                            )
                    else:
                        self._show_campaign_channel_picker(c, status_label)
                    return

                if not cid:
                    if status_label:
                        status_label.configure(text="Error: No category_id found for this campaign")
                    return

                if status_label:
                    status_label.configure(
                        text=self.t(
                            "drops_campaign_searching",
                            campaign=c.get("name", "")
                        )
                    )
                self._auto_find_streamers_for_game(c, cid, scrollable_frame, status_label)

            choose_btn.configure(command=choose_campaign)

            # Display rewards (drops) with images
            rewards = campaign.get("rewards", [])
            if rewards:
                rewards_frame = ctk.CTkFrame(
                    campaign_frame, 
                    fg_color=("gray90", "#111827"),
                    corner_radius=8
                )
                rewards_frame.grid(
                    row=1, column=0, sticky="ew", padx=15, pady=(0, 10)
                )
                rewards_frame.grid_columnconfigure(1, weight=1)

                rewards_label = ctk.CTkLabel(
                    rewards_frame,
                    text="🎁 Rewards:",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=("#7c3aed", "#a78bfa")
                )
                rewards_label.grid(row=0, column=0, sticky="w", padx=(12, 10), pady=10)

                # Horizontal frame for drop images
                images_frame = ctk.CTkFrame(
                    rewards_frame, fg_color="transparent"
                )
                images_frame.grid(row=0, column=1, sticky="w", pady=10, padx=(0, 12))

                for rew_idx, reward in enumerate(
                    rewards[:6]
                ):  # Max 6 rewards shown
                    try:
                        # Build complete image URL
                        reward_img_url = reward.get("image_url", "")
                        if reward_img_url and not reward_img_url.startswith(
                            "http"
                        ):
                            reward_img_url = (
                                f"https://ext.cdn.kick.com/{reward_img_url}"
                            )

                        if reward_img_url:
                            # CDN images - use simple urllib request with headers
                            try:
                                req = urllib.request.Request(
                                    reward_img_url,
                                    headers={
                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                        "Referer": "https://kick.com/"
                                    }
                                )
                                with urllib.request.urlopen(req, timeout=5) as response:
                                    img_data = response.read()

                                rew_img = Image.open(BytesIO(img_data))
                                rew_img = rew_img.resize(
                                    (50, 50), Image.Resampling.LANCZOS
                                )
                                rew_photo = ctk.CTkImage(
                                    light_image=rew_img,
                                    dark_image=rew_img,
                                    size=(50, 50),
                                )

                                reward_name = reward.get(
                                    "name", "Unknown"
                                )
                                required_mins = reward.get(
                                    "required_units", 0
                                )
                                
                                # Get progress info if available
                                progress = reward.get("progress", 0.0)
                                claimed = reward.get("claimed", False)
                                progress_units = campaign.get("progress_units", 0)
                                
                                # Build tooltip with progress info
                                if progress > 0 or claimed:
                                    progress_percent = int(progress * 100)
                                    if claimed:
                                        tooltip_text = f"{reward_name}\n⏱️ {required_mins} minutes\n✓ CLAIMED ({progress_percent}%)"
                                    else:
                                        tooltip_text = f"{reward_name}\n⏱️ {required_mins} minutes\n📊 {progress_percent}% ({progress_units}/{required_mins})"
                                else:
                                    tooltip_text = f"{reward_name}\n⏱️ {required_mins} minutes\n⏸️ Not started"

                                # Frame with border for each reward - change border color if claimed
                                border_color = ("#10b981", "#059669") if claimed else ("#f59e0b", "#d97706") if progress > 0 else ("#d1d5db", "#374151")
                                border_width = 3 if claimed or progress > 0 else 2
                                
                                rew_container = ctk.CTkFrame(
                                    images_frame,
                                    fg_color=("white", "#0f172a"),
                                    border_width=border_width,
                                    border_color=border_color,
                                    corner_radius=8,
                                    width=60,
                                    height=60
                                )
                                rew_container.grid(row=0, column=rew_idx, padx=4)
                                rew_container.grid_propagate(False)
                                
                                rew_label = ctk.CTkLabel(
                                    rew_container,
                                    image=rew_photo,
                                    text="",
                                )
                                rew_label.image = rew_photo
                                rew_label.place(relx=0.5, rely=0.5, anchor="center")
                                
                                # Add claimed checkmark overlay if claimed
                                if claimed:
                                    claimed_overlay = ctk.CTkLabel(
                                        rew_container,
                                        text="✓",
                                        font=ctk.CTkFont(size=16, weight="bold"),
                                        text_color="#10b981",
                                        fg_color="transparent"
                                    )
                                    claimed_overlay.place(relx=0.85, rely=0.15, anchor="center")

                                # Add tooltip (drop name on hover) - on container for better functionality
                                self._create_tooltip(rew_container, tooltip_text)
                                self._create_tooltip(rew_label, tooltip_text)
                            except Exception:
                                pass  # Silently skip images that fail to load
                    except Exception:
                        pass

            # Participating channels - improved style
            channels_frame = ctk.CTkFrame(
                campaign_frame, fg_color="transparent"
            )
            channels_frame.grid(
                row=2, column=0, sticky="ew", padx=15, pady=(0, 12)
            )
            channels_frame.grid_columnconfigure(0, weight=1)
            
            # Store widget references (defined before if/else to avoid scope error)
            channel_buttons = []

            if not campaign["channels"]:
                # Global drop - show option to auto-find streamers
                global_drop_frame = ctk.CTkFrame(channels_frame, fg_color="transparent")
                global_drop_frame.grid(row=0, column=0, sticky="ew", pady=5)
                global_drop_frame.grid_columnconfigure(0, weight=1)
                
                no_channels_label = ctk.CTkLabel(
                    global_drop_frame,
                    text=self.t("drops_no_channels"),
                    text_color=("#6b7280", "#9ca3af"),
                    font=ctk.CTkFont(size=11, slant="italic"),
                )
                no_channels_label.grid(row=0, column=0, sticky="w")
                
                # Button to auto-find streamers for this game
                # Get category_id from campaign (from progress API or campaigns API)
                # Always show button, but disable if no category_id
                def find_streamers(c=campaign, cid=category_id, sl=status_label):
                    if not cid:
                        if sl:
                            sl.configure(text="Error: No category_id found for this campaign")
                        debug_print(f"DEBUG: Campaign structure: {list(c.keys())}")
                        debug_print(f"DEBUG: Category: {c.get('category')}")
                        debug_print(f"DEBUG: Progress data: {c.get('progress_data', {}).get('category') if isinstance(c.get('progress_data'), dict) else 'N/A'}")
                        return
                    if sl:
                        self._auto_find_streamers_for_game(c, cid, scrollable_frame, sl)
                    else:
                        debug_print("DEBUG: No status_label available")
                
                find_btn = ctk.CTkButton(
                    global_drop_frame,
                    text="🔍 Find Live Streamers",
                    width=180,
                    height=30,
                    font=ctk.CTkFont(size=11, weight="bold"),
                    fg_color=("#10b981", "#059669") if category_id else ("#6b7280", "#4b5563"),
                    hover_color=("#059669", "#047857") if category_id else ("#4b5563", "#374151"),
                    command=find_streamers,
                    state="normal" if category_id else "disabled",
                )
                find_btn.grid(row=0, column=1, padx=(10, 0), sticky="e")
                
                if not category_id:
                    debug_print(f"DEBUG: No category_id found for campaign {campaign.get('name', 'unknown')}")
                    debug_print(f"DEBUG: Campaign keys: {list(campaign.keys())}")
                    debug_print(f"DEBUG: Category value: {campaign.get('category')}")
            else:
                # List of channels with buttons - improved design
                for ch_idx, channel in enumerate(campaign["channels"][:5]):
                    channel_url = channel["url"]
                    is_added = self._is_channel_in_list(channel_url)
                    
                    channel_row = ctk.CTkFrame(
                        channels_frame, 
                        fg_color=("gray95", "#1f2937"),
                        corner_radius=6
                    )
                    channel_row.grid(
                        row=ch_idx, column=0, sticky="ew", pady=3
                    )
                    channel_row.grid_columnconfigure(0, weight=1)

                    # Icon according to state, but text always normal
                    icon = "✓" if is_added else "📺"
                    ch_label = ctk.CTkLabel(
                        channel_row,
                        text=f"{icon} {channel['username']}",
                        font=ctk.CTkFont(size=12),
                        anchor="w"
                    )
                    ch_label.grid(row=0, column=0, sticky="w", padx=(12, 10), pady=8)

                    # Add or Remove button depending on state
                    action_btn = ctk.CTkButton(
                        channel_row,
                        text="✗ Remove" if is_added else "+ Add",
                        width=90,
                        height=28,
                        font=ctk.CTkFont(size=11, weight="bold"),
                        fg_color=("#ef4444", "#dc2626") if is_added else ("#3b82f6", "#2563eb"),
                        hover_color=("#dc2626", "#b91c1c") if is_added else ("#2563eb", "#1d4ed8"),
                        corner_radius=6,
                    )
                    action_btn.grid(row=0, column=1, sticky="e", padx=8, pady=4)
                    
                    # Store reference to this button
                    channel_buttons.append((channel_url, action_btn, ch_label, channel['username']))
                    
                    # Function to toggle button state
                    def toggle_channel(url=channel_url, btn=action_btn, label=ch_label, username=channel['username'], camp=campaign):
                        if self._is_channel_in_list(url):
                            # Remove
                            self._remove_drop_channel(url)
                            # Update button and label (icon only)
                            btn.configure(
                                text="+ Add",
                                fg_color=("#3b82f6", "#2563eb"),
                                hover_color=("#2563eb", "#1d4ed8")
                            )
                            label.configure(text=f"📺 {username}")
                        else:
                            # Add
                            self._add_drop_channel(url, 120, camp)
                            # Update button and label (icon only)
                            btn.configure(
                                text="✗ Remove",
                                fg_color=("#ef4444", "#dc2626"),
                                hover_color=("#dc2626", "#b91c1c")
                            )
                            label.configure(text=f"✓ {username}")
                    
                    action_btn.configure(command=toggle_channel)

                # "Add/Remove All Channels" button - toggle based on state
                add_all_btn = None
                if len(campaign["channels"]) > 1:
                    # Check if all channels are added
                    all_added = all(self._is_channel_in_list(ch['url']) for ch in campaign["channels"])
                    
                    add_all_btn = ctk.CTkButton(
                        channels_frame,
                        text=f"✨ {self.t('btn_remove_all_channels')}" if all_added else f"✨ {self.t('btn_add_all_channels')}",
                        height=32,
                        font=ctk.CTkFont(size=12, weight="bold"),
                        fg_color=("#ef4444", "#dc2626") if all_added else ("#10b981", "#059669"),
                        hover_color=("#dc2626", "#b91c1c") if all_added else ("#059669", "#047857"),
                        corner_radius=8,
                    )
                    add_all_btn.grid(
                        row=len(campaign["channels"][:5]),
                        column=0,
                        sticky="ew",
                        pady=(8, 0),
                    )
                    
                    # Function for add/remove all with individual button updates
                    def toggle_all_channels(c=campaign, bulk_btn=add_all_btn, btn_refs=channel_buttons):
                        # Check if all are added
                        all_added = all(self._is_channel_in_list(ch['url']) for ch in c["channels"])
                        
                        if all_added:
                            # Remove all
                            for ch in c["channels"]:
                                self._remove_drop_channel(ch['url'])
                            # Update bulk button
                            bulk_btn.configure(
                                text=f"✨ {translate(self.config_data.language, 'btn_add_all_channels')}",
                                fg_color=("#10b981", "#059669"),
                                hover_color=("#059669", "#047857")
                            )
                            # Update all displayed individual buttons
                            for url, btn, label, username in btn_refs:
                                btn.configure(
                                    text="+ Add",
                                    fg_color=("#3b82f6", "#2563eb"),
                                    hover_color=("#2563eb", "#1d4ed8")
                                )
                                label.configure(text=f"📺 {username}")
                        else:
                            # Add all
                            self._add_all_campaign_channels(c)
                            # Update bulk button
                            bulk_btn.configure(
                                text=f"✨ {translate(self.config_data.language, 'btn_remove_all_channels')}",
                                fg_color=("#ef4444", "#dc2626"),
                                hover_color=("#dc2626", "#b91c1c")
                            )
                            # Update all displayed individual buttons
                            for url, btn, label, username in btn_refs:
                                btn.configure(
                                    text="✗ Remove",
                                    fg_color=("#ef4444", "#dc2626"),
                                    hover_color=("#dc2626", "#b91c1c")
                                )
                                label.configure(text=f"✓ {username}")
                    
                    add_all_btn.configure(command=toggle_all_channels)
                
                # Now configure individual button commands (with access to bulk_btn)
                for url, btn, label, username in channel_buttons:
                    def make_toggle(url=url, btn=btn, label=label, username=username, c=campaign, bulk_btn=add_all_btn, btn_refs=channel_buttons):
                        def toggle():
                            if self._is_channel_in_list(url):
                                # Remove
                                self._remove_drop_channel(url)
                                btn.configure(
                                    text="+ Add",
                                    fg_color=("#3b82f6", "#2563eb"),
                                    hover_color=("#2563eb", "#1d4ed8")
                                )
                                label.configure(text=f"📺 {username}")
                            else:
                                # Add
                                self._add_drop_channel(url, 120, c)
                                btn.configure(
                                    text="✗ Remove",
                                    fg_color=("#ef4444", "#dc2626"),
                                    hover_color=("#dc2626", "#b91c1c")
                                )
                                label.configure(text=f"✓ {username}")
                            
                            # Check if all channels are now added and update bulk button
                            if bulk_btn:
                                all_now_added = all(self._is_channel_in_list(ch['url']) for ch in c["channels"])
                                if all_now_added:
                                    bulk_btn.configure(
                                        text=f"✨ {translate(self.config_data.language, 'btn_remove_all_channels')}",
                                        fg_color=("#ef4444", "#dc2626"),
                                        hover_color=("#dc2626", "#b91c1c")
                                    )
                                else:
                                    bulk_btn.configure(
                                        text=f"✨ {translate(self.config_data.language, 'btn_add_all_channels')}",
                                        fg_color=("#10b981", "#059669"),
                                        hover_color=("#059669", "#047857")
                                    )
                        return toggle
                    
                    btn.configure(command=make_toggle())
        except Exception as e:
            print(f"Error creating campaign display: {e}")
            import traceback
            traceback.print_exc()

    def _setup_progress_tab(self, parent, drops_window):
        """Sets up the progress tab UI"""
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        
        # Header with refresh button
        header_frame = ctk.CTkFrame(parent, fg_color=("gray86", "gray17"), corner_radius=0, height=60)
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.grid_columnconfigure(0, weight=1)
        header_frame.grid_propagate(False)
        
        status_label = ctk.CTkLabel(
            header_frame, text=self.t("drops_progress_loading"),
            font=ctk.CTkFont(size=16, weight="bold")
        )
        status_label.grid(row=0, column=0, sticky="w", padx=20, pady=15)
        
        refresh_btn = ctk.CTkButton(
            header_frame,
            text=self.t("btn_refresh_progress"),
            width=130,
            height=35,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=("#3b82f6", "#2563eb"),
            hover_color=("#2563eb", "#1d4ed8"),
            command=lambda: self._refresh_progress(scrollable_frame, status_label),
        )
        refresh_btn.grid(row=0, column=1, padx=20, pady=15)
        
        # Scrollable frame for progress
        scrollable_frame = ctk.CTkScrollableFrame(
            parent,
            label_text="",
            fg_color=("gray92", "gray14")
        )
        scrollable_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=15)
        scrollable_frame.grid_columnconfigure(0, weight=1)
        
        # Initial load
        self._refresh_progress(scrollable_frame, status_label)
        
        # Bring window to front after loading
        def load_and_focus():
            try:
                drops_window.lift()
                drops_window.focus_force()
            except:
                pass
        
        threading.Thread(target=load_and_focus, daemon=True).start()

    def _refresh_progress(self, scrollable_frame, status_label):
        """Fetches and displays drop progress"""
        # Clear existing content
        def clear_frame():
            for widget in scrollable_frame.winfo_children():
                widget.destroy()
            status_label.configure(text=self.t("drops_progress_loading"))
        
        self.after(0, clear_frame)
        
        def display_progress():
            try:
                result = fetch_drops_progress()
                progress_data = result.get("progress", [])
                progress_data = [p for p in progress_data if isinstance(p, dict)]
                driver = result.get("driver")
                self.after(0, lambda data=progress_data: self._store_progress_and_refresh_list(data))
                
                try:
                    if not progress_data:
                        def show_error():
                            status_label.configure(text=self.t("drops_progress_error"))
                            no_data_label = ctk.CTkLabel(
                                scrollable_frame,
                                text=self.t("drops_progress_no_data"),
                                font=ctk.CTkFont(size=12),
                                text_color="gray",
                            )
                            no_data_label.grid(row=0, column=0, pady=20)
                        self.after(0, show_error)
                        return
                    
                    # Group by status
                    in_progress = [p for p in progress_data if p.get("status") == "in progress"]
                    claimed = [p for p in progress_data if p.get("status") == "claimed"]
                    
                    total = len(progress_data)
                    active = len(in_progress)
                    
                    def update_ui():
                        status_label.configure(
                            text=self.t("drops_progress_loaded", total=total, active=active)
                        )
                        
                        row_idx = 0
                        
                        # Display in-progress campaigns
                        if in_progress:
                            section_label = ctk.CTkLabel(
                                scrollable_frame,
                                text=self.t("drops_progress_in_progress"),
                                font=ctk.CTkFont(size=14, weight="bold"),
                            )
                            section_label.grid(row=row_idx, column=0, sticky="w", padx=20, pady=(20, 10))
                            row_idx += 1
                            
                            for campaign in in_progress:
                                self._create_progress_card(scrollable_frame, campaign, row_idx)
                                row_idx += 1
                        
                        # Display claimed campaigns
                        if claimed:
                            if in_progress:
                                row_idx += 1  # Spacing
                            
                            section_label = ctk.CTkLabel(
                                scrollable_frame,
                                text=self.t("drops_progress_claimed"),
                                font=ctk.CTkFont(size=14, weight="bold"),
                            )
                            section_label.grid(row=row_idx, column=0, sticky="w", padx=20, pady=(20, 10))
                            row_idx += 1
                            
                            for campaign in claimed:
                                self._create_progress_card(scrollable_frame, campaign, row_idx)
                                row_idx += 1
                    
                    self.after(0, update_ui)
                            
                finally:
                    # Close driver after UI is rendered
                    if driver:
                        try:
                            driver.quit()
                        except:
                            pass
                            
            except Exception as e:
                print(f"Error displaying progress: {e}")
                import traceback
                traceback.print_exc()
                def show_error():
                    status_label.configure(text=self.t("drops_progress_error"))
                self.after(0, show_error)
        
        # Run in thread to avoid blocking UI
        threading.Thread(target=display_progress, daemon=True).start()

    def _create_progress_card(self, parent, campaign, row):
        """Creates a card displaying campaign progress"""
        card_frame = ctk.CTkFrame(parent, corner_radius=10)
        card_frame.grid(row=row, column=0, sticky="ew", padx=20, pady=10)
        card_frame.grid_columnconfigure(0, weight=1)
        
        # Campaign name
        name_label = ctk.CTkLabel(
            card_frame,
            text=campaign.get("name", "Unknown Campaign"),
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        name_label.grid(row=0, column=0, columnspan=2, sticky="w", padx=15, pady=(15, 5))
        
        # Game info
        category = campaign.get("category", {})
        game_label = ctk.CTkLabel(
            card_frame,
            text=f"Game: {category.get('name', 'Unknown')}",
            font=ctk.CTkFont(size=12),
        )
        game_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=15, pady=5)
        
        # Status badge
        status = campaign.get("status", "unknown")
        status_color = "#10b981" if status == "claimed" else "#f59e0b"
        status_label = ctk.CTkLabel(
            card_frame,
            text=status.upper(),
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=status_color,
        )
        status_label.grid(row=2, column=0, sticky="w", padx=15, pady=5)
        
        # Rewards with progress
        rewards = campaign.get("rewards", [])
        for i, reward in enumerate(rewards):
            reward_frame = ctk.CTkFrame(card_frame, fg_color=("gray90", "gray16"))
            reward_frame.grid(row=3 + i, column=0, columnspan=2, sticky="ew", padx=15, pady=5)
            reward_frame.grid_columnconfigure(1, weight=1)
            
            # Reward name
            reward_name = ctk.CTkLabel(
                reward_frame,
                text=reward.get("name", "Unknown Reward"),
                font=ctk.CTkFont(size=11),
            )
            reward_name.grid(row=0, column=0, sticky="w", padx=10, pady=5)
            
            # Progress information
            progress = reward.get("progress", 0.0)
            required = reward.get("required_units", 0)
            progress_units = campaign.get("progress_units", 0)
            
            progress_percent = int(progress * 100)
            progress_text = f"{progress_percent}% ({progress_units}/{required} units)"
            
            progress_label = ctk.CTkLabel(
                reward_frame,
                text=progress_text,
                font=ctk.CTkFont(size=10),
                text_color="gray",
            )
            progress_label.grid(row=0, column=1, sticky="e", padx=10, pady=5)
            
            # Progress bar
            progress_bar = ctk.CTkProgressBar(reward_frame)
            progress_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 5))
            progress_bar.set(progress)
            
            # Claimed status
            if reward.get("claimed"):
                claimed_label = ctk.CTkLabel(
                    reward_frame,
                    text="✓ Claimed",
                    font=ctk.CTkFont(size=10, weight="bold"),
                    text_color="#10b981",
                )
                claimed_label.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 5))

    def _is_channel_in_list(self, url):
        """Check if a URL is already in the list"""
        return any(item["url"] == url for item in self.config_data.items)
    
    def _find_channel_index(self, url):
        """Find the index of a URL in the list"""
        for idx, item in enumerate(self.config_data.items):
            if item["url"] == url:
                return idx
        return None

    def _show_campaign_channel_picker(self, campaign, status_label=None):
        """Open a small picker to choose the initial channel for a campaign."""
        channels = campaign.get("channels", [])
        if not channels:
            return

        picker = ctk.CTkToplevel(self)
        picker.title(f"Choose channel - {campaign.get('name', 'Drop')}")
        picker.geometry("460x520")
        picker.transient(self)
        picker.grab_set()
        picker.grid_columnconfigure(0, weight=1)
        picker.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(picker, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text=campaign.get("name", "Drop campaign"),
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        channel_list = ctk.CTkScrollableFrame(picker)
        channel_list.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 12))
        channel_list.grid_columnconfigure(0, weight=1)

        def choose_channel(channel):
            url = channel.get("url") if isinstance(channel, dict) else channel
            if not url:
                return
            if self._is_channel_in_list(url):
                self.status_var.set(f"Already selected: {url.split('/')[-1]}")
            else:
                self._add_drop_channel(url, 120, campaign)
                if status_label:
                    username = channel.get("username", url.split("/")[-1]) if isinstance(channel, dict) else url.split("/")[-1]
                    status_label.configure(text=f"Selected {username} for {campaign.get('name', 'drop')}")
            picker.destroy()

        for row, channel in enumerate(channels):
            url = channel.get("url") if isinstance(channel, dict) else channel
            username = channel.get("username", url.split("/")[-1]) if isinstance(channel, dict) and url else str(channel)
            is_added = bool(url and self._is_channel_in_list(url))

            row_frame = ctk.CTkFrame(channel_list, corner_radius=6)
            row_frame.grid(row=row, column=0, sticky="ew", pady=4)
            row_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                row_frame,
                text=username,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold" if is_added else "normal"),
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=8)

            btn = ctk.CTkButton(
                row_frame,
                text="Selected" if is_added else "Choose",
                width=90,
                height=28,
                state="disabled" if is_added else "normal",
                command=lambda ch=channel: choose_channel(ch),
            )
            btn.grid(row=0, column=1, padx=8, pady=6)

        footer = ctk.CTkFrame(picker, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            footer,
            text="Cancel",
            width=100,
            fg_color=("#6b7280", "#4b5563"),
            hover_color=("#4b5563", "#374151"),
            command=picker.destroy,
        ).grid(row=0, column=1, sticky="e")

    def _add_drop_channel(self, url, minutes=120, campaign=None):
        """Add a drop channel to the queue with campaign info"""
        try:
            campaign_id = campaign.get("id") if campaign else None
            campaign_channels = [
                {"url": ch["url"], "username": ch.get("username", "")} 
                for ch in campaign.get("channels", [])
            ] if campaign else []
            
            # Calculate max required time from rewards if campaign has rewards
            if campaign:
                rewards = campaign.get("rewards", [])
                if rewards:
                    max_required = 0
                    for reward in rewards:
                        required_units = reward.get("required_units", 0)
                        if required_units > max_required:
                            max_required = required_units
                    if max_required > 0:
                        minutes = max_required
            
            # Get category_id from campaign
            required_category_id = None
            if campaign:
                category = campaign.get("category", {})
                if isinstance(category, dict):
                    required_category_id = category.get("id")
                else:
                    # Try from progress_data
                    progress_data = campaign.get("progress_data", {})
                    if isinstance(progress_data, dict):
                        progress_category = progress_data.get("category", {})
                        if isinstance(progress_category, dict):
                            required_category_id = progress_category.get("id")
            
            self.config_data.add(
                url, 
                minutes, 
                campaign_id, 
                campaign_channels,
                required_category_id=required_category_id,
                is_global_drop=False  # Regular drop, not global
            )
            self.refresh_list()
            self.status_var.set(self.t("drops_added", channel=url.split("/")[-1]))
            # Auto-start if enabled and queue not running
            if self.config_data.auto_start and not self.queue_running:
                self.after(500, self._auto_start_queue)
        except Exception as e:
            print(f"Error adding channel: {e}")
    
    def _remove_drop_channel(self, url):
        """Remove a channel from the queue"""
        try:
            idx = self._find_channel_index(url)
            if idx is not None:
                if idx in self.workers:
                    self._stop_worker_at(idx)
                self.config_data.remove(idx)
                # Re-index workers
                reindexed_workers = {}
                for old_i, worker in self.workers.items():
                    if old_i < idx:
                        reindexed_workers[old_i] = worker
                    elif old_i > idx:
                        reindexed_workers[old_i - 1] = worker
                self.workers = reindexed_workers
                self.refresh_list()
                self.status_var.set(f"Removed: {url.split('/')[-1]}")
        except Exception as e:
            print(f"Error removing channel: {e}")

    def _remove_all_campaign_channels(self, campaign):
        """Remove all queued channels that belong to a campaign."""
        try:
            campaign_urls = {
                ch.get("url") if isinstance(ch, dict) else ch
                for ch in campaign.get("channels", [])
            }
            campaign_urls.discard(None)
            if not campaign_urls:
                return

            new_items = []
            old_to_new = {}
            removed_count = 0
            for old_idx, item in enumerate(self.config_data.items):
                if item.get("url") in campaign_urls:
                    removed_count += 1
                    if old_idx in self.workers:
                        self._stop_worker_at(old_idx)
                    continue
                old_to_new[old_idx] = len(new_items)
                new_items.append(item)

            if removed_count == 0:
                return

            self.config_data.items = new_items
            self.config_data.save()
            self.workers = {
                old_to_new[old_idx]: worker
                for old_idx, worker in self.workers.items()
                if old_idx in old_to_new
            }
            self.refresh_list()
            self.status_var.set(f"Removed {removed_count} channel(s) from {campaign.get('name', 'campaign')}")
        except Exception as e:
            print(f"Error removing campaign channels: {e}")

    def _add_all_campaign_channels(self, campaign):
        """Add all channels from a campaign with campaign grouping"""
        count = 0
        campaign_id = campaign.get("id")
        all_channels = campaign.get("channels", [])
        
        # Calculate max required time from rewards if campaign has rewards
        minutes = 120  # Default
        rewards = campaign.get("rewards", [])
        if rewards:
            max_required = 0
            for reward in rewards:
                required_units = reward.get("required_units", 0)
                if required_units > max_required:
                    max_required = required_units
            if max_required > 0:
                minutes = max_required
        
        # Get category_id from campaign
        required_category_id = None
        required_category_id = self._get_campaign_category_id(campaign)
        
        for channel in all_channels:
            try:
                url = channel.get("url") if isinstance(channel, dict) else channel
                if not url or self._is_channel_in_list(url):
                    continue
                # Store all channels as alternatives for each other
                campaign_channels = [
                    {"url": ch.get("url") if isinstance(ch, dict) else ch, 
                     "username": ch.get("username", "") if isinstance(ch, dict) else ""}
                    for ch in all_channels
                ]
                self.config_data.add(
                    url, 
                    minutes, 
                    campaign_id, 
                    campaign_channels,
                    required_category_id=required_category_id,
                    is_global_drop=False  # Regular drop, not global
                )
                count += 1
            except Exception as e:
                print(f"Error adding channel {channel.get('username', 'unknown')}: {e}")

        self.refresh_list()
        self.status_var.set(f"Added {count} channel(s) from {campaign['name']}")
        # Auto-start if enabled and queue not running
        if self.config_data.auto_start and not self.queue_running:
            self.after(500, self._auto_start_queue)

    def _create_tooltip(self, widget, text):
        """Create a tooltip that displays on widget hover"""
        tooltip = None

        def on_enter(event):
            nonlocal tooltip
            x = widget.winfo_rootx() + widget.winfo_width() // 2
            y = widget.winfo_rooty() - 10

            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_attributes("-topmost", True)
            
            # Frame with shadow (modern effect)
            frame = tk.Frame(
                tooltip,
                background="#1f2937" if self.config_data.dark_mode else "#ffffff",
                relief="flat",
                borderwidth=0
            )
            frame.pack(padx=2, pady=2)
            
            label = tk.Label(
                frame,
                text=text,
                justify="center",
                background="#1f2937" if self.config_data.dark_mode else "#ffffff",
                foreground="#f9fafb" if self.config_data.dark_mode else "#111827",
                font=("Segoe UI", 10, "bold"),
                padx=12,
                pady=8,
            )
            label.pack()
            
            # Center tooltip above widget
            tooltip.update_idletasks()
            tooltip_width = tooltip.winfo_width()
            tooltip.wm_geometry(f"+{x - tooltip_width // 2}+{y - tooltip.winfo_height() - 10}")

        def on_leave(event):
            nonlocal tooltip
            if tooltip:
                tooltip.destroy()
                tooltip = None

        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)

    # ----------- Toggles -----------
    def on_toggle_mute(self):
        if bool(self.mute_var.get()):
            allow_mute = messagebox.askyesno(
                "Aviso",
                "Kick puede dejar de contar el progreso si el reproductor está silenciado. "
                "Lo recomendado es dejarlo con audio activo. ¿Quieres silenciarlo igualmente?",
            )
            if not allow_mute:
                self.mute_var.set(False)
                self.config_data.mute = False
                self.config_data.save()
                return
        self.config_data.mute = bool(self.mute_var.get())
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.mute = self.config_data.mute
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_hide(self):
        if bool(self.hide_player_var.get()):
            allow_hide = messagebox.askyesno(
                "Aviso",
                "Ocultar el reproductor puede hacer que Kick no contabilice el progreso. "
                "Lo recomendado es dejarlo visible. ¿Quieres ocultarlo igualmente?",
            )
            if not allow_hide:
                self.hide_player_var.set(False)
                self.config_data.hide_player = False
                self.config_data.save()
                return
        self.config_data.hide_player = bool(self.hide_player_var.get())
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.hide_player = self.config_data.hide_player
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_mini(self):
        self.config_data.mini_player = bool(self.mini_player_var.get())
        if self.config_data.mini_player:
            self.config_data.background_browser = False
            self.background_browser_var.set(False)
        self.config_data.save()
        for w in list(self.workers.values()):
            try:
                w.mini_player = self.config_data.mini_player
                w.ensure_player_state()
            except Exception:
                pass

    def on_toggle_background_browser(self):
        if bool(self.background_browser_var.get()):
            allow_background = messagebox.askyesno(
                "Aviso",
                "El navegador en segundo plano usa Chrome headless y Kick puede no contar el drop. "
                "Para obtener progreso real, usa navegador visible. ¿Quieres activarlo igualmente?",
            )
            if not allow_background:
                self.background_browser_var.set(False)
                self.config_data.background_browser = False
                self.config_data.save()
                self.status_var.set("Navegador visible activado para que Kick contabilice el progreso")
                return
        self.config_data.background_browser = bool(self.background_browser_var.get())
        if self.config_data.background_browser:
            self.config_data.mini_player = False
            self.mini_player_var.set(False)
        self.config_data.save()
        self.status_var.set(
            "Navegador en segundo plano aplicado al próximo canal"
            if self.config_data.background_browser
            else "Navegador visible aplicado al próximo canal"
        )

    def on_toggle_force_160p(self):
        self.config_data.force_160p = bool(self.force_160p_var.get())
        self.config_data.save()
        # Note: force_160p only affects new streams (set during initialization)
        # Existing streams will need to be restarted to apply the change

    def on_toggle_auto_start(self):
        self.config_data.auto_start = bool(self.auto_start_var.get())
        self.config_data.save()
        if self.config_data.auto_start and not self.queue_running:
            # Auto-start if enabled and queue not running
            if self.config_data.items:
                self.start_all_in_order()
    

    def _auto_start_queue(self):
        """Auto-start queue on launch if enabled"""
        if not self.queue_running and self.config_data.items:
            # Check if there are any unfinished items
            unfinished = [i for i, item in enumerate(self.config_data.items) 
                         if not item.get("finished")]
            if unfinished:
                self.start_all_in_order()

    def _start_offline_retry_monitor(self):
        """Background thread that periodically checks offline streams and retries them"""
        def monitor():
            retry_backoff = ExponentialBackoff(base=1.6, shift=20, maximum=180)
            while True:
                time.sleep(next(retry_backoff))
                try:
                    if not self.queue_running:
                        retry_backoff.reset()
                        continue
                    
                    # Only check if we're not currently running a stream
                    # (Kick only allows 1 stream at a time)
                    if len(self.workers) > 0:
                        retry_backoff.reset()
                        continue
                    
                    # Find next unfinished item
                    started = False
                    for idx, item in enumerate(self.config_data.items):
                        if item.get("finished"):
                            continue
                        
                        if idx in self.workers:
                            continue  # Already running
                        
                        # Only auto-open when live is confirmed. Unknown status waits.
                        if self._cached_live_status(item["url"]) is True:
                            # Stream is back online, retry it
                            self.after(0, lambda i=idx: self._run_queue_from(i))
                            retry_backoff.reset()
                            started = True
                            break  # Only start one at a time
                    if started:
                        continue
                except Exception as e:
                    print(f"Monitor error: {e}")
        
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()

    # ----------- Callbacks Worker -----------
    def _format_countdown(self, seconds):
        try:
            seconds = max(0, int(seconds or 0))
        except (TypeError, ValueError):
            seconds = 0
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def on_worker_update(self, idx, seconds, live):
        def ui_update():
            if idx < 0 or idx >= len(self.config_data.items):
                return
            
            item = self.config_data.items[idx]
            is_global_drop = item.get("is_global_drop", False)
            self._set_status_progress(item, seconds)
            worker = self.workers.get(idx)
            offline_remaining = 0
            offline_elapsed = 0
            offline_grace = 60
            is_offline_wait = False
            if worker is not None:
                try:
                    offline_elapsed = int(getattr(worker, "offline_elapsed_seconds", 0) or 0)
                    offline_remaining = int(getattr(worker, "offline_grace_remaining_seconds", 0) or 0)
                    offline_grace = int(getattr(worker, "offline_grace_seconds", 60) or 60)
                    is_offline_wait = bool(getattr(worker, "playback_state", "") == "offline")
                except Exception:
                    pass
            countdown = self._format_countdown(offline_remaining)
            self._set_status_alert(is_offline_wait)
            
            if str(idx) in self.tree.get_children():
                values = list(self.tree.item(str(idx), "values"))
                if live:
                    tag = self.t("tag_live")
                elif is_offline_wait:
                    tag = f"{self.t('tag_offline')} cambia en {countdown}"
                else:
                    tag = self.t("tag_live")
                
                if is_global_drop:
                    # Show cumulative time for global drops
                    cumulative_seconds = item.get("cumulative_time", 0) + seconds
                    cumulative_minutes = cumulative_seconds // 60
                    values[3] = f"{cumulative_minutes}m ({tag})"
                else:
                    # Regular drop - show individual time
                    values[3] = f"{seconds}s ({tag})"
                
                current_tags = set(self.tree.item(str(idx), "tags") or [])
                if live:
                    current_tags.discard("paused")
                    current_tags.discard("redo")
                elif is_offline_wait:
                    current_tags.add("redo")
                    current_tags.discard("paused")
                else:
                    current_tags.discard("paused")
                    current_tags.discard("redo")
                self.tree.item(str(idx), values=values, tags=tuple(current_tags))
                self._update_overview()
            
            # Update status bar with elapsed time
            if is_global_drop:
                cumulative_seconds = item.get("cumulative_time", 0) + seconds
                cumulative_minutes = cumulative_seconds // 60
                secs = cumulative_seconds % 60
                time_str = f"{cumulative_minutes}m {secs}s" if cumulative_minutes > 0 else f"{secs}s"
                if live:
                    status = self.t("tag_live")
                elif is_offline_wait:
                    status = f"{self.t('tag_offline')} - cambio de canal en {countdown}"
                else:
                    status = self.t("tag_live")
                
                if self.queue_running and self.queue_current_idx == idx:
                    self.status_var.set(f"{self.t('queue_running_status', url=item['url'])} - {time_str} cumulative ({status})")
                else:
                    self.status_var.set(f"{self.t('status_playing', url=item['url'])} - {time_str} cumulative ({status})")
            else:
                minutes = seconds // 60
                secs = seconds % 60
                time_str = f"{minutes}m {secs}s" if minutes > 0 else f"{secs}s"
                if live:
                    status = self.t("tag_live")
                elif is_offline_wait:
                    status = f"{self.t('tag_offline')} - cambio de canal en {countdown}"
                else:
                    status = self.t("tag_live")
                
                if self.queue_running and self.queue_current_idx == idx:
                    self.status_var.set(f"{self.t('queue_running_status', url=item['url'])} - {time_str} ({status})")
                else:
                    self.status_var.set(f"{self.t('status_playing', url=item['url'])} - {time_str} ({status})")

        self.after(0, ui_update)

    def on_worker_finish(self, idx, elapsed, completed):
        def ui_finish():
            if idx < 0 or idx >= len(self.config_data.items):
                return

            worker = self.workers.get(idx)
            ended_offline = bool(worker and getattr(worker, "ended_because_offline", False))
            ended_wrong_category = bool(worker and getattr(worker, "ended_because_wrong_category", False))
            ended_navigation_error = bool(worker and getattr(worker, "ended_because_navigation_error", False))
            worker_error = getattr(worker, "error_message", "") if worker else ""
            if worker is not None and self.workers.get(idx) is worker:
                del self.workers[idx]
            
            item = self.config_data.items[idx]
            is_global_drop = item.get("is_global_drop", False)
            campaign_id = item.get("campaign_id")
            
            # Initialize completed variable
            # For regular drops, use the value passed from worker
            # For global drops, we'll recalculate based on cumulative time
            completed_value = completed  # Store original value from function parameter
            
            # Track cumulative time for global drops
            if is_global_drop and campaign_id:
                # Add elapsed time to cumulative time for all items in this campaign
                debug_print(f"DEBUG: Global drop - adding {elapsed} seconds to cumulative time")
                for other_item in self.config_data.items:
                    if other_item.get("campaign_id") == campaign_id:
                        current_cumulative = other_item.get("cumulative_time", 0)
                        other_item["cumulative_time"] = current_cumulative + elapsed
                        debug_print(f"DEBUG: Item {other_item['url']} cumulative time: {other_item['cumulative_time']}s")
                self.config_data.save()
                
                # Check if cumulative time reached target
                target_minutes = item.get("minutes", 0)
                cumulative_seconds = item.get("cumulative_time", 0)
                cumulative_minutes = cumulative_seconds // 60
                
                debug_print(f"DEBUG: Cumulative time: {cumulative_minutes} minutes / {target_minutes} minutes target")
                
                if target_minutes > 0 and cumulative_minutes >= target_minutes:
                    # Mark all items in campaign as finished
                    debug_print(f"DEBUG: Target reached! Marking all items in campaign as finished")
                    for other_item in self.config_data.items:
                        if other_item.get("campaign_id") == campaign_id:
                            other_item["finished"] = True
                    self.config_data.save()
                    completed_value = True
                else:
                    # Not finished yet, continue with other streamers
                    completed_value = False
                    debug_print(f"DEBUG: Still need {target_minutes - cumulative_minutes} more minutes")
            
            # Use completed_value (always defined - either from function parameter or recalculated for global drops)
            if completed_value:
                if not is_global_drop:
                    # Regular drop - mark individual item as finished
                    self.config_data.items[idx]["finished"] = True
                    self.config_data.save()
                # Reset tried_channels on successful completion
                self.config_data.items[idx]["tried_channels"] = []
                self.config_data.save()
                if str(idx) in self.tree.get_children():
                    values = list(self.tree.item(str(idx), "values"))
                    if is_global_drop:
                        cumulative_minutes = item.get("cumulative_time", 0) // 60
                        values[3] = f"{cumulative_minutes}m ({self.t('tag_finished')})"
                    else:
                        values[3] = f"{elapsed}s ({self.t('tag_finished')})"
                    current_tags = set(self.tree.item(str(idx), "tags") or [])
                    current_tags.add("finished")
                    current_tags.discard("paused")
                    current_tags.discard("redo")
                    self.tree.item(str(idx), values=values, tags=tuple(current_tags))
            elif ended_navigation_error:
                if str(idx) in self.tree.get_children():
                    values = list(self.tree.item(str(idx), "values"))
                    values[3] = self.t("retry")
                    current_tags = set(self.tree.item(str(idx), "tags") or [])
                    current_tags.add("redo")
                    current_tags.discard("paused")
                    current_tags.discard("finished")
                    self.tree.item(str(idx), values=values, tags=tuple(current_tags))
                self.status_var.set(f"Navigation failed: {worker_error or item['url']}")
            elif ended_offline or ended_wrong_category:
                # Try alternative channel from same campaign
                campaign_channels = item.get("campaign_channels", [])
                
                switched = False
                if campaign_id and campaign_channels:
                    current_url = item["url"]
                    tried_channels = item.get("tried_channels", [])
                    
                    # Add current URL to tried list if not already there
                    if current_url not in tried_channels:
                        tried_channels.append(current_url)
                    
                    # Get all channel URLs
                    all_channel_urls = []
                    for ch in campaign_channels:
                        ch_url = ch.get("url") if isinstance(ch, dict) else ch
                        if ch_url:
                            all_channel_urls.append(ch_url)
                    
                    # Also include current URL in the list
                    if current_url not in all_channel_urls:
                        all_channel_urls.append(current_url)
                    
                    # If we've tried all channels, reset the tried list
                    if len(tried_channels) >= len(all_channel_urls):
                        tried_channels.clear()
                        debug_print(f"DEBUG: Reset tried_channels for campaign {campaign_id} - all channels exhausted")
                    
                    # Find next available live channel from same campaign that hasn't been tried
                    for alt_channel in campaign_channels:
                        alt_url = alt_channel.get("url") if isinstance(alt_channel, dict) else alt_channel
                        if alt_url and alt_url != current_url and alt_url not in tried_channels:
                            # Check if this alternative is confirmed live
                            if self._cached_live_status(alt_url) is True:
                                category_match = self._category_matches_required(
                                    alt_url, item.get("required_category_id")
                                )
                                if category_match is not True:
                                    debug_print(
                                        f"DEBUG: Alternative {alt_url} skipped; wrong category "
                                        f"required={item.get('required_category_id')} match={category_match}"
                                    )
                                    continue
                                # Switch to this alternative channel
                                self.config_data.items[idx]["url"] = alt_url
                                tried_channels.append(alt_url)  # Mark as tried
                                item["tried_channels"] = tried_channels  # Update item
                                self.config_data.save()
                                self.refresh_list()
                                switched = True
                                debug_print(f"DEBUG: Switched to alternative: {alt_url} (tried: {len(tried_channels)}/{len(all_channel_urls)})")
                                self.status_var.set(f"Switched to alternative: {alt_url.split('/')[-1]}")
                                
                                if getattr(self, "queue_running", False):
                                    self.queue_current_idx = idx
                                    self._run_queue_from(idx)
                                    return
                                break
                    
                    # If no live alternative found, but we haven't tried all channels, mark current as tried and wait
                    if not switched and len(tried_channels) < len(all_channel_urls):
                        item["tried_channels"] = tried_channels  # Update tried list even if no switch
                        self.config_data.save()
                        debug_print(f"DEBUG: No live alternatives found, but {len(all_channel_urls) - len(tried_channels)} channels remain untried")
                
                if not switched:
                    # No alternative found, mark for retry
                    if str(idx) in self.tree.get_children():
                        values = list(self.tree.item(str(idx), "values"))
                        values[3] = f"{elapsed}s ({self.t('retry')})"
                        current_tags = set(self.tree.item(str(idx), "tags") or [])
                        current_tags.add("redo")
                        current_tags.discard("paused")
                        current_tags.discard("finished")
                        self.tree.item(str(idx), values=values, tags=tuple(current_tags))
                    try:
                        self.status_var.set(
                            self.t("offline_wait_retry", url=self.config_data.items[idx]["url"])
                        )
                    except Exception:
                        pass

            # Continue queue if applicable
            if getattr(self, "queue_running", False) and self.queue_current_idx == idx:
                self._run_queue_from(self._next_drop_start_index(idx), wrap=True)
            else:
                if not self.workers:
                    self._set_status_progress()
                self._update_overview()

        self.after(0, ui_finish)
