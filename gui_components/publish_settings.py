"""Publish settings UI and timezone-safe scheduling."""
from .runtime import *  # noqa: F401,F403

class PublishSettingsMixin:
    def _build_publish_tab_ui(self, parent):
        frame = ttk.LabelFrame(parent, text="YouTube 影片公開發布排程（台灣時間 UTC+8）", padding=15)
        frame.pack(fill=tk.BOTH, expand=True)

        mode_frame = ttk.Frame(frame)
        mode_frame.pack(fill=tk.X, pady=(0, 15))

        ttk.Label(mode_frame, text="發布模式：", style="Header.TLabel").pack(side=tk.LEFT, padx=(0, 15))
        r1 = ttk.Radiobutton(
            mode_frame, text="🔘 立即公開（上傳後直接為公開狀態，預設）",
            variable=self.publish_mode_var, value="immediate",
            command=self._on_publish_mode_changed,
        )
        r1.pack(side=tk.LEFT, padx=(0, 20))
        r2 = ttk.Radiobutton(
            mode_frame, text="⏰ 排定時間公開（整本小說所有 Part 同步排程）",
            variable=self.publish_mode_var, value="scheduled",
            command=self._on_publish_mode_changed,
        )
        r2.pack(side=tk.LEFT)

        self.publish_schedule_frame = ttk.LabelFrame(frame, text="排程詳細設定", padding=12)
        self.publish_schedule_frame.pack(fill=tk.X, pady=(0, 15))

        quick_frame = ttk.Frame(self.publish_schedule_frame)
        quick_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(quick_frame, text="常用快捷設定：", style="Header.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(quick_frame, text="今日 18:00", command=lambda: self._set_publish_preset(0, "18:00")).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_frame, text="今日 20:00", command=lambda: self._set_publish_preset(0, "20:00")).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_frame, text="明日 18:00", command=lambda: self._set_publish_preset(1, "18:00")).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_frame, text="明日 20:00", command=lambda: self._set_publish_preset(1, "20:00")).pack(side=tk.LEFT, padx=3)
        ttk.Button(quick_frame, text="後日 18:00", command=lambda: self._set_publish_preset(2, "18:00")).pack(side=tk.LEFT, padx=3)

        input_frame = ttk.Frame(self.publish_schedule_frame)
        input_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(input_frame, text="發布日期：").pack(side=tk.LEFT, padx=(0, 5))
        taipei_now = datetime.now(timezone(timedelta(hours=8)))
        date_options = [
            (taipei_now + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(15)
        ]
        self.combo_publish_date = ttk.Combobox(
            input_frame, textvariable=self.publish_date_var, values=date_options, width=14,
        )
        self.combo_publish_date.pack(side=tk.LEFT, padx=(0, 20))
        self.combo_publish_date.bind("<<ComboboxSelected>>", lambda _: self._update_publish_preview())
        self.combo_publish_date.bind("<KeyRelease>", lambda _: self._update_publish_preview())

        ttk.Label(input_frame, text="發布時間：").pack(side=tk.LEFT, padx=(0, 5))
        time_options = [
            "00:00", "06:00", "08:00", "10:00", "12:00", "14:00", "16:00",
            "17:30", "18:00", "18:30", "19:00", "19:30", "20:00", "20:30",
            "21:00", "21:30", "22:00", "23:00",
        ]
        self.combo_publish_time = ttk.Combobox(
            input_frame, textvariable=self.publish_time_var, values=time_options, width=10,
        )
        self.combo_publish_time.pack(side=tk.LEFT, padx=(0, 10))
        self.combo_publish_time.bind("<<ComboboxSelected>>", lambda _: self._update_publish_preview())
        self.combo_publish_time.bind("<KeyRelease>", lambda _: self._update_publish_preview())

        ttk.Label(input_frame, text="（時:分，可自行輸入如 18:45）").pack(side=tk.LEFT)

        status_frame = ttk.LabelFrame(frame, text="設定狀態與運作規則", padding=12)
        status_frame.pack(fill=tk.X)

        self.lbl_publish_preview = ttk.Label(
            status_frame, textvariable=self.publish_preview_var, style="Header.TLabel", foreground="#0097e6",
        )
        self.lbl_publish_preview.pack(anchor=tk.W, pady=(0, 8))

        rule_box = ttk.Frame(status_frame)
        rule_box.pack(fill=tk.X)
        ttk.Label(
            rule_box,
            text="💡 策略 A 保障：若因雲端排隊或 GitHub Actions 失敗重試，導致影片上傳成功時已超過排定時間，YouTube 將自動「立即公開」，絕不延遲至隔天。",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=2)
        ttk.Label(
            rule_box,
            text="💡 全集同步發布：無論整本小說最終切分為幾部 Part 影片，所有影片均會統一套用此公開時間，確保下班觀眾能一次收聽完整播放清單。",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=2)
        ttk.Label(
            rule_box,
            text="💡 時區對齊：介面已自動鎖定台灣時間 (UTC+8)，系統在送出上傳請求時會帶入完整時區，精準無誤。",
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=2)

        self._on_publish_mode_changed()

    def _on_publish_mode_changed(self):
        is_scheduled = (self.publish_mode_var.get() == "scheduled")
        state = tk.NORMAL if is_scheduled else tk.DISABLED
        if hasattr(self, "publish_schedule_frame"):
            for child in self.publish_schedule_frame.winfo_children():
                for sub in child.winfo_children():
                    if isinstance(sub, (ttk.Button, ttk.Combobox, ttk.Entry)):
                        try:
                            sub.configure(state=state)
                        except Exception:
                            pass
        self._update_publish_preview()

    def _set_publish_preset(self, days_offset, time_str="18:00"):
        taipei_tz = timezone(timedelta(hours=8))
        target_date = (datetime.now(taipei_tz) + timedelta(days=days_offset)).strftime("%Y-%m-%d")
        self.publish_mode_var.set("scheduled")
        self.publish_date_var.set(target_date)
        self.publish_time_var.set(time_str)
        self._on_publish_mode_changed()

    def _update_publish_preview(self):
        if self.publish_mode_var.get() != "scheduled":
            self.publish_preview_var.set("📢 目前設定：【立即公開】（影片上傳完成後直接公開顯示，所有觀眾可見）")
            return
        iso_str = self._get_configured_publish_at()
        if not iso_str:
            self.publish_preview_var.set("⚠️ 日期或時間格式有誤，請輸入有效之 YYYY-MM-DD 與 HH:MM")
            return
        date_str = self.publish_date_var.get().strip()
        time_str = self.publish_time_var.get().strip()
        self.publish_preview_var.set(
            f"📅 目前設定：【排定公開】將於 {date_str} {time_str}:00 (台灣時間 UTC+8) 自動向觀眾公開"
        )

    def _get_configured_publish_at(self):
        if self.publish_mode_var.get() != "scheduled":
            return ""
        date_str = self.publish_date_var.get().strip()
        time_str = self.publish_time_var.get().strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return ""
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            return ""
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return ""
        norm_time = f"{hour:02d}:{minute:02d}:00"
        return f"{date_str}T{norm_time}+08:00"

    def _set_configured_publish_at(self, iso_str):
        if not iso_str or not str(iso_str).strip():
            self.publish_mode_var.set("immediate")
            self._on_publish_mode_changed()
            return
        iso_str = str(iso_str).strip()
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            taipei_tz = timezone(timedelta(hours=8))
            dt_taipei = dt.astimezone(taipei_tz)
            self.publish_date_var.set(dt_taipei.strftime("%Y-%m-%d"))
            self.publish_time_var.set(dt_taipei.strftime("%H:%M"))
            self.publish_mode_var.set("scheduled")
        except Exception:
            self.publish_mode_var.set("immediate")
        self._on_publish_mode_changed()

