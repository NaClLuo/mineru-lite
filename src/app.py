"""MinerU Lite —— 轻量化的 MinerU 文档转换工具。

选文件 -> 选输出目录 -> 导出 Markdown / JSON。
图形界面用 tkinter，网络层用标准库，打包后为单文件 exe，无运行时依赖。
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import config as cfg
import mineru_client as api

APP_TITLE = "MinerU Lite"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------- 配色
BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E3E6EA"
TEXT = "#1D2129"
MUTED = "#6B7280"
FAINT = "#9AA1AC"
ACCENT = "#1F6FEB"
ACCENT_HOVER = "#1858C4"
ACCENT_SOFT = "#EEF4FF"
OK = "#15803D"
ERR = "#C0392B"
WARN = "#B45309"

LANGUAGES = [
    ("中文 / 英文（ch）", "ch"),
    ("英文（en）", "en"),
    ("日文（japan）", "japan"),
    ("韩文（korean）", "korean"),
    ("繁体中文（chinese_cht）", "chinese_cht"),
    ("拉丁语系（latin）", "latin"),
    ("西里尔语系（cyrillic）", "cyrillic"),
    ("东斯拉夫语系（east_slavic）", "east_slavic"),
    ("阿拉伯语系（arabic）", "arabic"),
    ("天城文（devanagari）", "devanagari"),
]

MODELS = [("vlm · 精度更高", "vlm"), ("pipeline · 速度更快", "pipeline")]
OCR_MODES = [("自动判断", "auto"), ("强制开启", "on"), ("关闭", "off")]


def resource_path(relative: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def enable_dpi_awareness() -> None:
    """必须在创建 Tk 窗口之前调用，否则设置无效。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


class Job:
    __slots__ = ("index", "path", "size", "status", "progress", "outputs")

    def __init__(self, index: int, path: Path):
        self.index = index
        self.path = path
        self.size = path.stat().st_size if path.exists() else 0
        self.status = "等待中"
        self.progress = "—"
        self.outputs: list[str] = []


def round_rect_points(x1: float, y1: float, x2: float, y2: float, radius: float) -> list[float]:
    """给 Canvas 的 smooth polygon 用的圆角矩形顶点。"""
    r = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class ToggleRow(tk.Frame):
    """自绘的单选框 / 复选框。

    ttk 的 indicator 在高 DPI 下会退化成极小的点，这里改用 Canvas 直接绘制，
    保证任何缩放比例下都清晰可辨。
    """

    def __init__(self, parent, text: str, variable, value=True, kind: str = "check",
                 scale: float = 1.0, font=None, bg: str = CARD, command=None):
        super().__init__(parent, bg=bg, cursor="hand2")
        self.var = variable
        self.value = value
        self.kind = kind
        self.command = command
        self.bg = bg
        self._hover = False
        self._enabled = True
        self._size = int(round(18 * scale))
        self._radius = int(round(4 * scale))
        self._stroke = max(1, int(round(1 * scale)))

        self.canvas = tk.Canvas(self, width=self._size, height=self._size, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack(side="left")
        self.label = tk.Label(self, text=text, bg=bg, fg=TEXT, font=font, cursor="hand2")
        self.label.pack(side="left", padx=(int(round(7 * scale)), 0))

        for widget in (self, self.canvas, self.label):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

        variable.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _selected(self) -> bool:
        if self.kind == "radio":
            return self.var.get() == self.value
        return bool(self.var.get())

    def _redraw(self):
        if not self.winfo_exists():
            return
        canvas = self.canvas
        canvas.delete("all")
        size = self._size
        inset = self._stroke / 2
        selected = self._selected()

        if not self._enabled:
            border = "#E4E7EC"
        else:
            border = ACCENT if (selected or self._hover) else "#C9CED6"
        fill_accent = ACCENT if self._enabled else "#B9C3D4"

        if self.kind == "radio":
            canvas.create_oval(inset, inset, size - inset, size - inset,
                               fill="#FFFFFF" if self._enabled else "#FAFBFC",
                               outline=border,
                               width=max(1, int(round(self._stroke * 1.6))))
            if selected:
                pad = size * 0.30
                canvas.create_oval(pad, pad, size - pad, size - pad, fill=fill_accent, outline="")
        else:
            points = round_rect_points(inset, inset, size - inset, size - inset, self._radius)
            if selected:
                canvas.create_polygon(points, smooth=True, splinesteps=18,
                                      fill=fill_accent, outline=fill_accent, width=self._stroke)
                canvas.create_line(
                    size * 0.28, size * 0.52, size * 0.43, size * 0.68, size * 0.74, size * 0.33,
                    fill="#FFFFFF", width=max(1, int(round(size * 0.10))),
                    capstyle="round", joinstyle="round",
                )
            else:
                canvas.create_polygon(points, smooth=True, splinesteps=18,
                                      fill="#FFFFFF" if self._enabled else "#FAFBFC",
                                      outline=border, width=self._stroke)

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self.label.configure(fg=TEXT if enabled else FAINT,
                             cursor="hand2" if enabled else "arrow")
        self.canvas.configure(cursor="hand2" if enabled else "arrow")
        self._redraw()

    def _on_click(self, _event=None):
        if not self._enabled:
            return
        if self.kind == "radio":
            if self.var.get() != self.value:
                self.var.set(self.value)
        else:
            self.var.set(not bool(self.var.get()))
        if self.command:
            self.command()

    def _on_enter(self, _event=None):
        if not self._enabled:
            return
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None):
        self._hover = False
        self._redraw()


# ---------------------------------------------------------------- 主窗口
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = cfg.load()
        self.jobs: list[Job] = []
        self.running = False
        self.cancel_event: threading.Event | None = None
        self.events: queue.Queue = queue.Queue()
        self.last_out_dir: Path | None = None
        self._drop_target = None
        self._advanced_open = True
        self._dir_chosen = False
        self._pending_drops: list[str] = []

        self.scale = self._setup_dpi()
        self._setup_fonts()
        self._setup_styles()
        self._build_ui()
        self._bind_events()
        self._install_drop_target()
        self.root.after(80, self._drain_events)
        self._update_key_badge()
        self._refresh_summary()

    # ------------------------------------------------------------ 基础设置
    def _setup_dpi(self) -> float:
        if sys.platform != "win32":
            return 1.0
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except Exception:
            dpi = 96
        dpi = dpi or 96
        self.root.tk.call("tk", "scaling", dpi / 72.0)
        return dpi / 96.0

    def _px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _setup_fonts(self):
        available = set(tkfont.families(self.root))
        family = "Microsoft YaHei UI"
        if family not in available:
            family = "Microsoft YaHei" if "Microsoft YaHei" in available else "Segoe UI"
        self.family = family

        for name, size, weight in (
            ("TkDefaultFont", 10, "normal"),
            ("TkTextFont", 10, "normal"),
            ("TkMenuFont", 10, "normal"),
            ("TkHeadingFont", 10, "bold"),
        ):
            tkfont.nametofont(name).configure(family=family, size=size, weight=weight)

        self.f_base = tkfont.Font(family=family, size=10)
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_bold = tkfont.Font(family=family, size=10, weight="bold")
        self.f_title = tkfont.Font(family=family, size=14, weight="bold")
        self.f_section = tkfont.Font(family=family, size=10, weight="bold")

    def _setup_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        bg = BG
        card = CARD

        style.configure(".", background=bg, foreground=TEXT, font=self.f_base,
                        bordercolor=BORDER, focuscolor=ACCENT)
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("TLabel", background=bg, foreground=TEXT)
        style.configure("Card.TLabel", background=card, foreground=TEXT)
        style.configure("Muted.TLabel", background=bg, foreground=MUTED, font=self.f_small)
        style.configure("CardMuted.TLabel", background=card, foreground=MUTED, font=self.f_small)
        style.configure("Section.TLabel", background=card, foreground=TEXT, font=self.f_section)
        style.configure("Title.TLabel", background=card, foreground=TEXT, font=self.f_title)
        style.configure("Status.TLabel", background=bg, foreground=MUTED, font=self.f_small)

        style.configure(
            "Accent.TButton",
            background=ACCENT, foreground="#FFFFFF", bordercolor=ACCENT,
            lightcolor=ACCENT, darkcolor=ACCENT, relief="flat",
            padding=(self._px(18), self._px(9)), font=self.f_bold,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("pressed", ACCENT_HOVER), ("disabled", "#AFC3E4")],
            bordercolor=[("disabled", "#AFC3E4")],
            foreground=[("disabled", "#F4F7FC")],
        )

        style.configure(
            "Ghost.TButton",
            background=card, foreground=TEXT, bordercolor=BORDER,
            lightcolor=card, darkcolor=card, relief="solid", borderwidth=1,
            padding=(self._px(12), self._px(6)),
        )
        style.map(
            "Ghost.TButton",
            background=[("active", "#F2F4F7"), ("pressed", "#EDEFF3"), ("disabled", card)],
            foreground=[("disabled", FAINT)],
        )

        style.configure(
            "Link.TButton",
            background=card, foreground=ACCENT, borderwidth=0,
            lightcolor=card, darkcolor=card, relief="flat",
            padding=(self._px(8), self._px(4)), font=self.f_small,
        )
        style.map("Link.TButton", background=[("active", card)], foreground=[("active", ACCENT_HOVER)])

        style.configure("TSeparator", background=BORDER)

        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF", background="#FFFFFF", foreground=TEXT,
            bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
            insertcolor=TEXT, padding=(self._px(6), self._px(4)),
        )
        style.map("TEntry", bordercolor=[("focus", ACCENT)])

        style.configure(
            "TCombobox",
            fieldbackground="#FFFFFF", background="#FFFFFF", foreground=TEXT,
            bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
            arrowcolor=MUTED, padding=(self._px(6), self._px(3)),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#FFFFFF"), ("disabled", "#F5F6F8")],
            background=[("readonly", "#FFFFFF")],
            foreground=[("readonly", TEXT), ("disabled", FAINT)],
            selectbackground=[("readonly", "#FFFFFF")],
            selectforeground=[("readonly", TEXT)],
            bordercolor=[("focus", ACCENT)],
            arrowcolor=[("disabled", FAINT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", "#FFFFFF")
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)

        style.configure("Card.TRadiobutton", background=card, foreground=TEXT, focuscolor=card)
        style.map("Card.TRadiobutton", background=[("active", card)])
        style.configure("Card.TCheckbutton", background=card, foreground=TEXT, focuscolor=card)
        style.map("Card.TCheckbutton", background=[("active", card)])

        style.configure(
            "Files.Treeview",
            background=card, fieldbackground=card, foreground=TEXT,
            rowheight=self._px(30), borderwidth=0, relief="flat", font=self.f_base,
        )
        style.map("Files.Treeview",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", TEXT)])
        style.configure(
            "Files.Treeview.Heading",
            background="#F7F8FA", foreground=MUTED, relief="flat",
            font=self.f_small, padding=(self._px(8), self._px(6)),
        )
        style.map("Files.Treeview.Heading", background=[("active", "#F0F2F5")])

        style.configure(
            "Thin.Horizontal.TProgressbar",
            troughcolor="#E6E9EE", background=ACCENT, bordercolor="#E6E9EE",
            lightcolor=ACCENT, darkcolor=ACCENT, thickness=self._px(4),
        )

        style.configure(
            "Vertical.TScrollbar",
            background="#D9DDE3", troughcolor=card, bordercolor=card,
            arrowcolor=MUTED, relief="flat", width=self._px(10),
        )
        style.map("Vertical.TScrollbar", background=[("active", "#C6CBD3")])

    # ------------------------------------------------------------ 界面
    def _build_ui(self):
        root = self.root
        root.title(APP_TITLE)
        root.configure(bg=BG)
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = min(self._px(920), int(screen_w * 0.92))
        height = min(self._px(770), int(screen_h * 0.90))
        root.geometry(f"{width}x{height}+{max(0, (screen_w - width) // 2)}+{max(0, (screen_h - height) // 2 - self._px(16))}")
        root.minsize(min(self._px(820), width), min(self._px(680), height))

        try:
            icon = resource_path(os.path.join("assets", "icon.ico"))
            if os.path.exists(icon):
                root.iconbitmap(icon)
        except Exception:
            pass

        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._build_header()          # row 0
        self._build_progress_strip()  # row 1（贴在 header 下方的细进度条）
        self._build_body()            # row 2
        self._build_footer()          # row 3

    def _build_header(self):
        header = tk.Frame(self.root, bg=CARD, highlightthickness=1,
                          highlightbackground=BORDER, highlightcolor=BORDER)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        mark = tk.Canvas(header, width=self._px(34), height=self._px(34), bg=CARD,
                         highlightthickness=0, bd=0)
        mark.grid(row=0, column=0, padx=(self._px(18), self._px(10)), pady=self._px(12))
        self._draw_logo(mark)

        text_box = tk.Frame(header, bg=CARD)
        text_box.grid(row=0, column=1, sticky="w")
        tk.Label(text_box, text=APP_TITLE, bg=CARD, fg=TEXT, font=self.f_title).pack(anchor="w")
        tk.Label(text_box, text="文档转 Markdown / JSON · 本地轻量客户端", bg=CARD, fg=MUTED,
                 font=self.f_small).pack(anchor="w")

        right = tk.Frame(header, bg=CARD)
        right.grid(row=0, column=2, sticky="e", padx=(0, self._px(16)))

        self.key_badge = tk.Label(right, text="API Key", bg=ACCENT_SOFT, fg=ACCENT,
                                  font=self.f_small, padx=self._px(8), pady=self._px(3))
        self.key_badge.pack(side="left", padx=(0, self._px(10)))

        ttk.Button(right, text="设置", style="Ghost.TButton",
                   command=self._open_settings).pack(side="left")

    def _draw_logo(self, canvas: tk.Canvas):
        size = self._px(34)
        canvas.create_polygon(
            round_rect_points(0, 0, size, size, self._px(9)),
            smooth=True, splinesteps=24, fill=ACCENT, outline=ACCENT,
        )
        left, top = self._px(9), self._px(7)
        right, bottom = self._px(25), self._px(28)
        canvas.create_polygon(
            round_rect_points(left, top, right, bottom, self._px(2)),
            smooth=True, splinesteps=12, fill=CARD, outline="",
        )
        fold = self._px(6)
        canvas.create_polygon(
            right - fold, top, right, top + fold, right - fold, top + fold,
            fill="#C9D9F3", outline="",
        )
        line_w = max(1, self._px(1.5))
        for offset in (self._px(11), self._px(15), self._px(19)):
            shorten = self._px(4) if offset != self._px(19) else self._px(8)
            canvas.create_line(
                left + self._px(4), top + offset, right - shorten, top + offset,
                fill="#6E9BEA", width=line_w, capstyle="round",
            )

    def _build_progress_strip(self):
        self.strip = ttk.Progressbar(self.root, style="Thin.Horizontal.TProgressbar",
                                     mode="determinate", maximum=100, value=0)
        self.strip.grid(row=1, column=0, sticky="sew")
        self.strip.grid_remove()

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.grid(row=2, column=0, sticky="nsew", padx=self._px(16), pady=(self._px(12), 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_files_card(body)
        self._build_output_card(body)
        self._build_advanced_card(body)

    def _card(self, parent, row: int, pady=(0, 0)) -> tk.Frame:
        card = tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.grid(row=row, column=0, sticky="nsew", pady=pady)
        card.columnconfigure(0, weight=1)
        return card

    def _build_files_card(self, body):
        card = self._card(body, 0)
        card.rowconfigure(1, weight=1)

        head = tk.Frame(card, bg=CARD)
        head.grid(row=0, column=0, sticky="ew", padx=self._px(14), pady=(self._px(12), self._px(8)))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="文件", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="拖拽文件到窗口，或点击下方添加", style="CardMuted.TLabel").grid(
            row=0, column=1, sticky="e")

        table = tk.Frame(card, bg=CARD)
        table.grid(row=1, column=0, sticky="nsew", padx=self._px(14))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        columns = ("name", "size", "status", "progress")
        self.tree = ttk.Treeview(table, columns=columns, show="headings",
                                 style="Files.Treeview", selectmode="extended", height=5)
        self.tree.heading("name", text="文件名", anchor="w")
        self.tree.heading("size", text="大小", anchor="e")
        self.tree.heading("status", text="状态", anchor="w")
        self.tree.heading("progress", text="进度", anchor="w")
        self.tree.column("name", width=self._px(400), minwidth=self._px(200), anchor="w", stretch=True)
        self.tree.column("size", width=self._px(78), minwidth=self._px(64), anchor="e", stretch=False)
        self.tree.column("status", width=self._px(96), minwidth=self._px(80), anchor="w", stretch=False)
        self.tree.column("progress", width=self._px(118), minwidth=self._px(90), anchor="w", stretch=False)
        self.tree.grid(row=0, column=0, sticky="nsew")

        self.tree.tag_configure("pending", foreground=MUTED)
        self.tree.tag_configure("active", foreground=ACCENT)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)
        self.tree.tag_configure("muted", foreground=FAINT)

        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview,
                               style="Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.scrollbar = scroll

        # 空状态提示
        self.empty_hint = tk.Label(card, text="还没有文件", bg=CARD, fg=FAINT, font=self.f_small)
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

        bar = tk.Frame(card, bg=CARD)
        bar.grid(row=2, column=0, sticky="ew", padx=self._px(14), pady=(self._px(8), self._px(12)))
        bar.columnconfigure(2, weight=1)

        self.btn_add = ttk.Button(bar, text="添加文件", style="Ghost.TButton", command=self._pick_files)
        self.btn_add.grid(row=0, column=0, sticky="w")
        self.btn_remove = ttk.Button(bar, text="移除选中", style="Ghost.TButton", command=self._remove_selected)
        self.btn_remove.grid(row=0, column=1, sticky="w", padx=(self._px(8), 0))

        self.summary = ttk.Label(bar, text="共 0 个文件", style="CardMuted.TLabel")
        self.summary.grid(row=0, column=3, sticky="e")

    def _build_output_card(self, body):
        card = self._card(body, 1, pady=(self._px(12), 0))
        card.columnconfigure(1, weight=1)

        ttk.Label(card, text="输出格式", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(self._px(14), self._px(12)), pady=(self._px(12), self._px(6)))

        self.fmt_var = tk.StringVar(value=self.cfg.get("fmt", "md"))
        fmt_box = tk.Frame(card, bg=CARD)
        fmt_box.grid(row=0, column=1, columnspan=3, sticky="w", pady=(self._px(12), self._px(6)))
        self.fmt_rows: dict[str, ToggleRow] = {}
        for text, value in (("Markdown", "md"), ("JSON", "json"), ("两者都要", "both")):
            row = ToggleRow(fmt_box, text, self.fmt_var, value=value, kind="radio",
                            scale=self.scale, font=self.f_base)
            row.pack(side="left", padx=(0, self._px(22)))
            self.fmt_rows[value] = row

        ttk.Label(card, text="输出位置", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(self._px(14), self._px(12)), pady=(0, self._px(6)))

        self.out_mode_var = tk.StringVar(value=self.cfg.get("out_mode", "source"))
        mode_box = tk.Frame(card, bg=CARD)
        mode_box.grid(row=1, column=1, columnspan=3, sticky="w", pady=(0, self._px(6)))
        for text, value in (
            ("原文件目录", "source"),
            ("原目录下新建 md / json 文件夹", "subfolder"),
            ("指定目录", "custom"),
        ):
            ToggleRow(mode_box, text, self.out_mode_var, value=value, kind="radio",
                      scale=self.scale, font=self.f_base,
                      command=self._update_dir_entry_state).pack(side="left", padx=(0, self._px(22)))

        ttk.Label(card, text="目录", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", padx=(self._px(14), self._px(12)), pady=(0, self._px(12)))

        self.dir_var = tk.StringVar(value=self.cfg.get("output_dir", ""))

        self.dir_entry = ttk.Entry(card, textvariable=self.dir_var)
        self.dir_entry.grid(row=2, column=1, sticky="ew", pady=(0, self._px(12)))
        self.dir_entry.bind("<KeyRelease>", lambda _e: setattr(self, "_dir_chosen", True))

        self.btn_browse = ttk.Button(card, text="浏览", style="Ghost.TButton", command=self._pick_dir)
        self.btn_browse.grid(row=2, column=2, padx=(self._px(8), self._px(12)), pady=(0, self._px(12)))

        self.btn_open_folder = ttk.Button(card, text="打开目录", style="Ghost.TButton",
                                          command=self._open_current_dir)
        self.btn_open_folder.grid(row=2, column=3, sticky="e", padx=(0, self._px(14)),
                                  pady=(0, self._px(12)))

        self.out_mode_var.trace_add("write", lambda *_: self._update_dir_entry_state())
        self._update_dir_entry_state()

    def _build_advanced_card(self, body):
        card = self._card(body, 2, pady=(self._px(12), self._px(4)))

        head = tk.Frame(card, bg=CARD, cursor="hand2")
        head.grid(row=0, column=0, sticky="ew", padx=self._px(14), pady=(self._px(10), self._px(10)))
        self.adv_label = tk.Label(head, text="▾  高级选项", bg=CARD, fg=TEXT, font=self.f_section,
                                  cursor="hand2")
        self.adv_label.pack(side="left")
        self.adv_hint = tk.Label(head, text="模型、语言、页码范围等", bg=CARD, fg=FAINT,
                                 font=self.f_small)
        self.adv_hint.pack(side="left", padx=(self._px(10), 0))
        head.bind("<Button-1>", lambda _e: self._toggle_advanced())
        self.adv_label.bind("<Button-1>", lambda _e: self._toggle_advanced())

        self.adv_body = tk.Frame(card, bg=CARD)
        self.adv_body.grid(row=1, column=0, sticky="ew", padx=self._px(14), pady=(0, self._px(12)))
        for col in range(4):
            self.adv_body.columnconfigure(col, weight=1)

        self.model_var = tk.StringVar(value=self._label_for(MODELS, self.cfg.get("model", "vlm")))
        self.lang_var = tk.StringVar(value=self._label_for(LANGUAGES, self.cfg.get("language", "ch")))
        self.ocr_var = tk.StringVar(value=self._label_for(OCR_MODES, self.cfg.get("ocr", "auto")))
        self.workers_var = tk.StringVar(value=str(self.cfg.get("workers", 4)))

        self._combo_field(0, 0, "模型", self.model_var, [label for label, _ in MODELS])
        self.model_combo = self._last_combo
        self._combo_field(0, 1, "语言", self.lang_var, [label for label, _ in LANGUAGES])
        self._combo_field(0, 2, "OCR", self.ocr_var, [label for label, _ in OCR_MODES])
        self._combo_field(0, 3, "并发上传", self.workers_var, ["1", "2", "3", "4", "6", "8"])
        self.workers_combo = self._last_combo

        self.pages_var = tk.StringVar(value=self.cfg.get("pages", ""))
        pages_box = tk.Frame(self.adv_body, bg=CARD)
        pages_box.grid(row=1, column=0, columnspan=2, sticky="w", pady=(self._px(8), 0))
        ttk.Label(pages_box, text="页码范围", style="Card.TLabel").pack(side="left", padx=(0, self._px(8)))
        ttk.Entry(pages_box, textvariable=self.pages_var, width=18).pack(side="left")
        ttk.Label(pages_box, text="仅 PDF，例 1-20 或 2,4-6",
                  style="CardMuted.TLabel").pack(side="left", padx=(self._px(8), 0))

        switch_box = tk.Frame(self.adv_body, bg=CARD)
        switch_box.grid(row=1, column=2, columnspan=2, sticky="e", pady=(self._px(8), 0))
        self.formula_var = tk.BooleanVar(value=bool(self.cfg.get("formula", True)))
        self.table_var = tk.BooleanVar(value=bool(self.cfg.get("table", True)))
        ToggleRow(switch_box, "识别公式", self.formula_var, kind="check",
                  scale=self.scale, font=self.f_base).pack(side="left", padx=(0, self._px(18)))
        ToggleRow(switch_box, "识别表格", self.table_var, kind="check",
                  scale=self.scale, font=self.f_base).pack(side="left")

    def _combo_field(self, row: int, col: int, label: str, var, values) -> None:
        """下拉框按像素定宽：ttk 的 width 以字符为单位，中文会被算窄。"""
        box = tk.Frame(self.adv_body, bg=CARD)
        box.grid(row=row, column=col, sticky="w", padx=(0, self._px(14)))

        ttk.Label(box, text=label, style="Card.TLabel").grid(row=0, column=0, padx=(0, self._px(6)))
        combo = ttk.Combobox(box, textvariable=var, state="readonly", values=values, width=1)
        combo.grid(row=0, column=1, sticky="ew")

        text_width = max(self.f_base.measure(str(value)) for value in values)
        box.columnconfigure(1, minsize=text_width + self._px(38))
        self._last_combo = combo

    def _build_footer(self):
        footer = tk.Frame(self.root, bg=BG)
        footer.grid(row=3, column=0, sticky="ew", padx=self._px(16), pady=(self._px(10), self._px(14)))
        footer.columnconfigure(3, weight=1)

        self.btn_start = ttk.Button(footer, text="开始转换", style="Accent.TButton", command=self._start)
        self.btn_start.grid(row=0, column=0, sticky="w")

        self.btn_cancel = ttk.Button(footer, text="取消", style="Ghost.TButton", command=self._cancel)
        self.btn_cancel.grid(row=0, column=1, sticky="w", padx=(self._px(8), 0))
        self.btn_cancel.grid_remove()

        footer.columnconfigure(2, weight=1)

        self.btn_open = ttk.Button(footer, text="打开输出目录", style="Link.TButton",
                                   command=self._open_out_dir)
        self.btn_open.grid(row=0, column=3, sticky="e", padx=(0, self._px(12)))
        self.btn_open.grid_remove()

        self.status_label = ttk.Label(footer, text="就绪", style="Status.TLabel", anchor="e")
        self.status_label.grid(row=0, column=4, sticky="e")

    # ------------------------------------------------------------ 交互状态
    def _label_for(self, pairs, value: str) -> str:
        for label, key in pairs:
            if key == value:
                return label
        return pairs[0][0]

    def _value_for(self, pairs, label: str) -> str:
        for text, key in pairs:
            if text == label:
                return key
        return pairs[0][1]

    def _update_dir_entry_state(self):
        custom = self.out_mode_var.get() == "custom"
        state = "normal" if custom else "disabled"
        self.dir_entry.configure(state=state)
        self.btn_browse.configure(state=state)
        self.dir_entry.configure(foreground=TEXT if custom else FAINT)
        if custom and not self.dir_var.get().strip() and self.jobs:
            self.dir_var.set(str(self.jobs[0].path.parent))

    def _toggle_advanced(self):
        self._advanced_open = not self._advanced_open
        if self._advanced_open:
            self.adv_body.grid()
            self.adv_label.configure(text="▾  高级选项")
        else:
            self.adv_body.grid_remove()
            self.adv_label.configure(text="▸  高级选项")

    def _install_drop_target(self):
        if sys.platform != "win32" or os.environ.get("MINERU_LITE_NO_DND"):
            return
        try:
            import dnd
            self._drop_target = dnd.DropTarget(
                self.root.winfo_id(), self._queue_drop_paths, log=cfg.write_error_log
            )
        except Exception as exc:  # 拖拽不可用不影响主流程
            cfg.write_error_log(f"[dnd] {exc!r}")
            self._drop_target = None

    def _queue_drop_paths(self, paths):
        """由 OLE 回调调用，只入队，不碰 Tk。"""
        self._pending_drops.extend(paths)

    def _bind_events(self):
        self.root.bind("<Control-o>", lambda _e: self._pick_files())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.tree.bind("<Double-1>", self._show_row_detail)
        self.tree.bind("<Delete>", lambda _e: self._remove_selected())

    # ------------------------------------------------------------ 文件操作
    def _pick_files(self):
        paths = filedialog.askopenfilenames(title="选择要转换的文件", filetypes=[
            ("支持的文件", "*.pdf *.doc *.docx *.ppt *.pptx *.xls *.xlsx *.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp"),
            ("PDF", "*.pdf"),
            ("Office 文档", "*.doc *.docx *.ppt *.pptx *.xls *.xlsx"),
            ("图片", "*.png *.jpg *.jpeg *.jp2 *.webp *.gif *.bmp"),
            ("全部文件", "*.*"),
        ])
        if paths:
            self._add_files(list(paths))

    def _pick_dir(self):
        initial = self.dir_var.get() if os.path.isdir(self.dir_var.get()) else str(Path.home())
        chosen = filedialog.askdirectory(title="选择输出目录", initialdir=initial)
        if chosen:
            self.dir_var.set(chosen)
            self._dir_chosen = True
            self.out_mode_var.set("custom")

    def _add_files(self, paths):
        if self.running:
            return
        existing = {str(job.path) for job in self.jobs}
        added, rejected = 0, []

        for raw in paths:
            path = Path(raw).expanduser()
            try:
                path = path.resolve()
            except OSError:
                pass
            if not path.is_file():
                continue
            if str(path) in existing:
                continue
            if path.suffix.lower() not in api.SUPPORTED_EXTENSIONS:
                rejected.append(path.name)
                continue
            job = Job(len(self.jobs), path)
            self.jobs.append(job)
            existing.add(str(path))
            iid = str(job.index)
            self.tree.insert("", "end", iid=iid, values=(
                job.path.name, human_size(job.size), job.status, job.progress), tags=("pending",))
            if job.size > api.MAX_FILE_BYTES:
                job.status = "超过 200 MB"
                self.tree.set(iid, "status", job.status)
                self.tree.item(iid, tags=("err",))
            added += 1

        self._refresh_summary()
        if added and not self._dir_chosen:
            # 输出目录默认跟随文件来源目录，用户手动改过就不再覆盖
            self.dir_var.set(str(self.jobs[0].path.parent))
        if rejected:
            messagebox.showwarning(
                "部分文件已跳过",
                "以下格式不受支持：\n" + "\n".join(rejected[:10]) +
                ("\n…" if len(rejected) > 10 else "") +
                "\n\n支持：PDF、Word、PPT、Excel、常见图片格式。",
                parent=self.root,
            )

    def _remove_selected(self):
        if self.running:
            return
        selected = self.tree.selection()
        if not selected:
            return
        keep = [job for job in self.jobs if str(job.index) not in selected]
        for iid in selected:
            self.tree.delete(iid)
        self.jobs = keep
        self._reindex_rows()
        self._refresh_summary()

    def _reindex_rows(self):
        """删除后重排索引，保证 iid 与 Job.index 一致。"""
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for new_index, job in enumerate(self.jobs):
            job.index = new_index
            if job.status == "完成":
                tags = ("ok",)
            elif job.status.startswith("失败") or job.status == "超过 200 MB":
                tags = ("err",)
            else:
                tags = ("pending",)
            self.tree.insert("", "end", iid=str(new_index), values=(
                job.path.name, human_size(job.size), job.status, job.progress), tags=tags)

    def _refresh_summary(self):
        count = len(self.jobs)
        total = sum(job.size for job in self.jobs)
        self.summary.configure(text=f"共 {count} 个文件 · {human_size(total)}" if count else "共 0 个文件")
        if count:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        if count > 6:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()

    def _show_row_detail(self, _event):
        selected = self.tree.selection()
        if not selected:
            return
        job = self.jobs[int(selected[0])]
        detail = f"{job.path}\n\n状态：{job.status}\n进度：{job.progress}"
        if job.outputs:
            detail += "\n\n输出：\n" + "\n".join(job.outputs)
        messagebox.showinfo("文件详情", detail, parent=self.root)

    # ------------------------------------------------------------ 设置
    def _update_key_badge(self):
        has_key = bool(self.cfg.get("api_key", "").strip())
        if has_key:
            self.key_badge.configure(text="API Key 已设置", bg=ACCENT_SOFT, fg=ACCENT)
        else:
            self.key_badge.configure(text="免费模式 · 仅 Markdown", bg="#FFF3E0", fg=WARN)
        self._apply_mode_state()

    def _apply_mode_state(self):
        """没有 API Key 时锁掉免费模式不支持的能力，避免用户以为设置生效了。"""
        has_key = bool(self.cfg.get("api_key", "").strip())

        for value, row in self.fmt_rows.items():
            row.set_enabled(has_key or value == "md")
        if not has_key and self.fmt_var.get() != "md":
            self.fmt_var.set("md")

        self.model_combo.configure(state="readonly" if has_key else "disabled")
        self.workers_combo.configure(state="readonly" if has_key else "disabled")
        self.adv_hint.configure(
            text="模型、语言、页码范围等" if has_key else "免费模式：模型与并发设置不生效"
        )

    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, self._on_settings_saved, self.family, self.scale)

    def _on_settings_saved(self, data: dict):
        self.cfg.update(data)
        try:
            cfg.save(self.cfg)
        except OSError as exc:
            messagebox.showerror("保存失败", f"配置写入失败：{exc}", parent=self.root)
        self._update_key_badge()

    # ------------------------------------------------------------ 转换
    def _collect_settings(self) -> dict:
        return {
            "api_key": self.cfg.get("api_key", "").strip(),
            "fmt": self.fmt_var.get(),
            "out_mode": self.out_mode_var.get(),
            "out_dir": self.dir_var.get().strip(),
            "model": self._value_for(MODELS, self.model_var.get()),
            "language": self._value_for(LANGUAGES, self.lang_var.get()),
            "ocr": self._value_for(OCR_MODES, self.ocr_var.get()),
            "pages": self.pages_var.get().strip(),
            "formula": self.formula_var.get(),
            "table": self.table_var.get(),
            "workers": int(self.workers_var.get() or 4),
        }

    def _persist_settings(self, settings: dict):
        self.cfg.update({
            "fmt": settings["fmt"],
            "out_mode": settings["out_mode"],
            "output_dir": settings["out_dir"],
            "model": settings["model"],
            "language": settings["language"],
            "ocr": settings["ocr"],
            "pages": settings["pages"],
            "formula": settings["formula"],
            "table": settings["table"],
            "workers": settings["workers"],
        })
        try:
            cfg.save(self.cfg)
        except OSError:
            pass

    def _start(self):
        if self.running:
            return
        settings = self._collect_settings()
        if not self.jobs:
            messagebox.showinfo("还没有文件", "请先添加要转换的文件。", parent=self.root)
            return

        settings["flash"] = not settings["api_key"]
        if settings["flash"]:
            if settings["fmt"] != "md":
                messagebox.showinfo(
                    "免费模式仅支持 Markdown",
                    "未设置 API Key 时使用 MinerU 的免费接口，只能导出 Markdown，格式已自动切换。\n\n"
                    "免费模式限制：单文件 ≤ 10 MB、≤ 20 页、不支持批量排队。",
                    parent=self.root,
                )
                self.fmt_var.set("md")
                settings["fmt"] = "md"
            too_big = [job for job in self.jobs if job.size > api.FLASH_MAX_BYTES]
            if too_big:
                names = "、".join(job.path.name for job in too_big[:3])
                messagebox.showwarning(
                    "超出免费模式限制",
                    f"免费模式单文件上限 10 MB，以下文件需要先在设置里填写 API Key：\n{names}",
                    parent=self.root,
                )
                return

        if settings["out_mode"] == "custom" and not settings["out_dir"]:
            messagebox.showinfo("还没有输出目录", "请选择输出目录，或改为输出到原文件目录。", parent=self.root)
            return

        oversized = [job for job in self.jobs if job.size > api.MAX_FILE_BYTES]
        if oversized:
            names = "、".join(job.path.name for job in oversized[:3])
            messagebox.showwarning("文件过大", f"以下文件超过 200 MB 上限，请先拆分：\n{names}", parent=self.root)
            return

        # 重置状态
        for job in self.jobs:
            if job.size > api.MAX_FILE_BYTES:
                continue
            job.status = "等待中"
            job.progress = "—"
            job.outputs = []
            iid = str(job.index)
            self.tree.set(iid, "status", job.status)
            self.tree.set(iid, "progress", job.progress)
            self.tree.item(iid, tags=("pending",))

        self._persist_settings(settings)
        self._set_busy(True)
        self.cancel_event = threading.Event()
        BatchRunner(self, list(self.jobs), settings, self.cancel_event).start()

    def _cancel(self):
        if self.cancel_event and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.status_label.configure(text="正在取消…")

    def _set_busy(self, busy: bool):
        self.running = busy
        state = "disabled" if busy else "normal"
        for widget in (self.btn_add, self.btn_start, self.btn_remove):
            widget.configure(state=state)
        if busy:
            self.btn_cancel.grid()
            self.strip.grid()
            self.strip.configure(value=0)
        else:
            self.btn_cancel.grid_remove()
            self.strip.grid_remove()

    # ------------------------------------------------------------ 事件循环
    def emit(self, kind: str, **payload):
        self.events.put((kind, payload))

    def _drain_events(self):
        if self._pending_drops:
            paths = self._pending_drops[:]
            self._pending_drops.clear()
            try:
                self._add_files(paths)
            except Exception as exc:  # noqa: BLE001
                cfg.write_error_log(f"[drop] {exc!r}")
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "row":
                    self._apply_row(payload)
                elif kind == "status":
                    self.status_label.configure(text=payload["text"])
                elif kind == "progress":
                    self.strip.configure(value=payload["value"])
                elif kind == "done":
                    self._on_batch_done(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _apply_row(self, payload: dict):
        index = payload["index"]
        iid = str(index)
        if not self.tree.exists(iid):
            return
        if "status" in payload:
            self.tree.set(iid, "status", payload["status"])
        if "progress" in payload:
            self.tree.set(iid, "progress", payload["progress"])
        if "tag" in payload:
            self.tree.item(iid, tags=(payload["tag"],))

    def _on_batch_done(self, payload: dict):
        self._set_busy(False)
        ok = payload.get("ok", 0)
        failed = payload.get("failed", 0)
        cancelled = payload.get("cancelled", 0)
        elapsed = payload.get("elapsed", 0)
        out_dir = payload.get("out_dir")

        if out_dir:
            self.last_out_dir = Path(out_dir)
            self.btn_open.grid()

        parts = []
        if ok:
            parts.append(f"完成 {ok}")
        if failed:
            parts.append(f"失败 {failed}")
        if cancelled:
            parts.append(f"取消 {cancelled}")
        head = "已取消" if cancelled and not ok else "转换结束"
        self.status_label.configure(
            text=f"{head} · {' · '.join(parts) or '无结果'} · 用时 {elapsed:.0f} 秒")
        self.strip.configure(value=0)

    def _open_out_dir(self):
        target = self.last_out_dir
        if not target:
            return
        try:
            os.startfile(str(target))  # noqa: S606
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _open_current_dir(self):
        """一键打开当前生效的输出目录。"""
        if self.out_mode_var.get() == "custom" and self.dir_var.get().strip():
            target: Path | None = Path(self.dir_var.get())
        elif self.jobs:
            target = self.jobs[0].path.parent
        elif self.dir_var.get().strip():
            target = Path(self.dir_var.get())
        else:
            target = None

        if target is None or not target.is_dir():
            messagebox.showinfo("目录还不存在", "先添加文件，或者选择一个已存在的输出目录。", parent=self.root)
            return
        try:
            os.startfile(str(target))  # noqa: S606
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _on_close(self):
        if self.running and self.cancel_event:
            if not messagebox.askyesno("正在转换", "还有任务在进行，确定要退出吗？", parent=self.root):
                return
            self.cancel_event.set()
        if self._drop_target:
            self._drop_target.close()
        self.root.destroy()


# ---------------------------------------------------------------- 批处理
class BatchRunner(threading.Thread):
    CHUNK = 20

    def __init__(self, app: App, jobs: list[Job], settings: dict, cancel_event: threading.Event):
        super().__init__(daemon=True)
        self.app = app
        self.jobs = jobs
        self.s = settings
        self.cancel = cancel_event
        self.started_at = time.time()
        self.total = 0
        self.finished_before = 0

    # -------------------------------------------------- 工具
    def _row(self, job: Job, status=None, progress=None, tag=None):
        payload = {"index": job.index}
        if status is not None:
            job.status = status
            payload["status"] = status
        if progress is not None:
            job.progress = progress
            payload["progress"] = progress
        if tag:
            payload["tag"] = tag
        self.app.emit("row", **payload)

    def _status(self, text: str):
        self.app.emit("status", text=text)

    def _overall(self, finished: int, total: int):
        self.app.emit("progress", value=(finished / total * 100) if total else 0)

    # -------------------------------------------------- 主流程
    def run(self):
        if self.s.get("flash"):
            self._run_flash()
            return

        jobs = [job for job in self.jobs if job.size <= api.MAX_FILE_BYTES]
        total = len(jobs)
        self.total = total
        ok = failed = cancelled = 0
        finished = 0
        last_out_dir: Path | None = None

        try:
            for start in range(0, total, self.CHUNK):
                chunk = jobs[start:start + self.CHUNK]
                if self.cancel.is_set():
                    for job in chunk:
                        self._row(job, "已取消", "—", "muted")
                    cancelled += len(chunk)
                    finished += len(chunk)
                    continue

                self.finished_before = finished
                try:
                    outputs, chunk_ok, chunk_fail = self._run_chunk(chunk)
                    ok += chunk_ok
                    failed += chunk_fail
                    if outputs:
                        last_out_dir = outputs
                    finished += len(chunk)
                    self._overall(finished, total)
                except api.Cancelled:
                    for job in chunk:
                        if not job.status.startswith(("完成", "失败")):
                            self._row(job, "已取消", "—", "muted")
                            cancelled += 1
                    finished += len(chunk)
                    self._overall(finished, total)
                    break
                except api.MineruError as exc:
                    for job in chunk:
                        if not job.status.startswith(("完成", "失败")):
                            self._row(job, "失败", "—", "err")
                            failed += 1
                    self._status(f"批次失败：{exc.message}")
                    finished += len(chunk)
                    self._overall(finished, total)
                except Exception as exc:  # noqa: BLE001
                    cfg.write_error_log(f"[batch] {exc!r}")
                    for job in chunk:
                        if not job.status.startswith(("完成", "失败")):
                            self._row(job, "失败", "—", "err")
                            failed += 1
                    self._status(f"出现异常：{exc}")
                    finished += len(chunk)
                    self._overall(finished, total)
        finally:
            self.app.emit(
                "done",
                ok=ok,
                failed=failed,
                cancelled=cancelled,
                elapsed=time.time() - self.started_at,
                out_dir=str(last_out_dir) if last_out_dir else None,
            )

    def _run_flash(self):
        """无 API Key 时的免费模式：逐文件解析，只输出 Markdown。"""
        jobs = [job for job in self.jobs if job.size <= api.MAX_FILE_BYTES]
        total = len(jobs)
        self.total = total
        ok = failed = cancelled = 0
        last_dir: Path | None = None
        out_root = Path(self.s["out_dir"]) if self.s["out_dir"] else Path.home()
        ocr = {"auto": None, "on": True, "off": False}[self.s["ocr"]]

        for index, job in enumerate(jobs, start=1):
            self.finished_before = index - 1
            if self.cancel.is_set():
                self._row(job, "已取消", "—", "muted")
                cancelled += 1
                self._overall(index, total)
                continue
            if job.size > api.FLASH_MAX_BYTES:
                self._row(job, "失败：超过 10 MB", "—", "err")
                failed += 1
                self._overall(index, total)
                continue

            self._row(job, "上传中", "免费模式", "active")
            self._status(f"免费模式 · {index}/{total}")
            try:
                def on_state(state: str, target=job):
                    self._row(target, api.STATE_TEXT.get(state, "解析中"), "—")

                text = api.flash_extract(
                    job.path, language=self.s["language"], ocr=ocr,
                    on_state=on_state, cancel_event=self.cancel,
                )
                base_dir = out_root if self.s["out_mode"] == "custom" else job.path.parent
                folder = base_dir / "md" if self.s["out_mode"] == "subfolder" else base_dir
                folder.mkdir(parents=True, exist_ok=True)
                path = unique_path(folder / f"{job.path.stem}.md")
                path.write_text(text, encoding="utf-8", newline="\n")
                job.outputs = [path.name]
                self._row(job, "完成", path.name, "ok")
                last_dir = base_dir
                ok += 1
            except api.Cancelled:
                self._row(job, "已取消", "—", "muted")
                cancelled += 1
            except Exception as exc:  # noqa: BLE001
                cfg.write_error_log(f"[flash] {job.path}: {exc!r}")
                self._row(job, f"失败：{str(exc)[:40]}", "—", "err")
                failed += 1
            self._overall(index, total)

        self.app.emit(
            "done", ok=ok, failed=failed, cancelled=cancelled,
            elapsed=time.time() - self.started_at,
            out_dir=str(last_dir) if last_dir else None,
        )

    def _run_chunk(self, chunk: list[Job]):
        names = [job.path.name for job in chunk]
        data_ids = [f"mlite-{job.index}" for job in chunk]
        pages = []
        for job in chunk:
            if self.s["pages"] and job.path.suffix.lower() == ".pdf":
                pages.append(self.s["pages"])
            else:
                pages.append(None)

        ocr = {"auto": None, "on": True, "off": False}[self.s["ocr"]]

        self._status(f"正在提交 {len(chunk)} 个文件…")
        batch_id, urls = api.create_batch(
            token=self.s["api_key"],
            names=names,
            data_ids=data_ids,
            model=self.s["model"],
            language=self.s["language"],
            ocr=ocr,
            page_ranges=pages,
            enable_formula=self.s["formula"],
            enable_table=self.s["table"],
        )

        self._status(f"正在上传 {len(chunk)} 个文件…")
        self._upload_all(chunk, urls)

        results = self._poll(batch_id, chunk)

        # 收结果
        out_root = Path(self.s["out_dir"]) if self.s["out_dir"] else Path.home()
        chunk_ok = chunk_fail = 0
        latest_dir: Path | None = None
        for job in chunk:
            result = results.get(job.index)
            if not result or result.get("state") != "done":
                if job.status not in ("失败", "已取消"):
                    message = (result or {}).get("error") or "解析未完成"
                    self._row(job, "失败", "—", "err")
                    job.status = f"失败：{message[:40]}"
                    self.app.emit("row", index=job.index, status=job.status)
                    chunk_fail += 1
                else:
                    chunk_fail += 1
                continue

            self._row(job, "保存中", "写入文件", "active")
            try:
                written, target_dir = self._save(job, result, out_root)
                job.outputs = written
                latest_dir = target_dir
                label = written[0] if len(written) == 1 else f"{len(written)} 个文件"
                self._row(job, "完成", label, "ok")
                chunk_ok += 1
            except api.Cancelled:
                self._row(job, "已取消", "—", "muted")
                raise
            except Exception as exc:  # noqa: BLE001
                cfg.write_error_log(f"[save] {job.path}: {exc!r}")
                self._row(job, f"失败：{str(exc)[:40]}", "—", "err")
                chunk_fail += 1

        return latest_dir, chunk_ok, chunk_fail

    def _upload_all(self, chunk: list[Job], urls: list[str]):
        workers = max(1, min(self.s["workers"], len(chunk)))

        def upload(job: Job, url: str):
            self._row(job, "上传中", "0%", "active")

            def on_progress(sent: int, total: int):
                self._row(job, "上传中", f"{int(sent / total * 100)}%" if total else "—")

            api.upload_file(url, job.path, on_progress=on_progress, cancel_event=self.cancel)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(upload, job, url): job for job, url in zip(chunk, urls)}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    future.result()
                except api.Cancelled:
                    raise
                except api.MineruError as exc:
                    self._row(job, f"上传失败", "—", "err")
                    job.status = f"失败：{exc.message[:40]}"
                    self.app.emit("row", index=job.index, status=job.status)

    def _poll(self, batch_id: str, chunk: list[Job]) -> dict[int, dict]:
        by_data_id = {f"mlite-{job.index}": job for job in chunk}
        results: dict[int, dict] = {}
        deadline = time.time() + 60 * 60
        interval = 3.0
        last_count = -1
        stale_since = time.time()

        while True:
            if self.cancel.is_set():
                raise api.Cancelled()
            if time.time() > deadline:
                return results

            items = api.get_batch(self.s["api_key"], batch_id)
            running = 0
            for item in items:
                data_id = item.get("data_id") or ""
                job = by_data_id.get(data_id)
                if job is None:
                    job = next(
                        (j for j in chunk
                         if j.path.name == item.get("file_name") and j.index not in results),
                        None,
                    )
                if job is None:
                    continue

                state = item.get("state", "")
                if state in ("done", "failed"):
                    if job.index in results:
                        continue
                    results[job.index] = item
                    if state == "failed":
                        message = item.get("error") or "解析失败"
                        job.status = f"失败：{message[:40]}"
                        self.app.emit("row", index=job.index, status=job.status, tag="err")
                    else:
                        self._row(job, "下载结果", "—", "active")
                else:
                    running += 1
                    if job.status.startswith("失败"):
                        continue
                    if state == "running":
                        extracted = item.get("extracted_pages") or 0
                        total_pages = item.get("total_pages") or 0
                        text = f"{extracted}/{total_pages} 页" if total_pages else "解析中"
                        self._row(job, "解析中", text, "active")
                    else:
                        self._row(job, api.STATE_TEXT.get(state, "排队中"), "—", "active")

            if self.total:
                done_total = self.finished_before + len(results)
                self.app.emit("progress", value=done_total / self.total * 100)

            if len(results) >= len(chunk):
                return results

            if len(results) != last_count:
                last_count = len(results)
                stale_since = time.time()
            elif running == 0 and time.time() - stale_since > 180:
                # 服务端始终不返回部分文件，避免无限等待
                return results

            self._status(f"解析中 · 已完成 {len(results)}/{len(chunk)}")
            time.sleep(interval)
            interval = min(interval + 1.0, 6.0) if running else 3.0

    def _save(self, job: Job, result: dict, out_root: Path):
        zip_url = result.get("zip_url") or ""
        if not zip_url:
            raise api.MineruError("服务未返回结果压缩包")

        mode = self.s["out_mode"]
        base_dir = out_root if mode == "custom" else job.path.parent
        fmt = self.s["fmt"]

        self._row(job, "下载结果", "定位文件", "active")

        def progress(done: int, total: int):
            if total:
                self._row(job, "下载结果", f"{done / total * 100:.0f}%")
            else:
                self._row(job, "下载结果", f"{done / 1024 / 1024:.1f} MB")

        contents = api.fetch_outputs(
            zip_url,
            need_md=fmt in ("md", "both"),
            need_json=fmt in ("json", "both"),
            on_progress=progress,
            cancel_event=self.cancel,
        )

        self._row(job, "写入文件", "—", "active")
        items: list[tuple[str, str, str]] = []

        if fmt in ("md", "both"):
            if contents.markdown is None:
                raise api.MineruError("结果包中没有 Markdown 文件")
            items.append(("md", contents.markdown, f"{job.path.stem}.md"))

        if fmt in ("json", "both"):
            if contents.content_list is None:
                raise api.MineruError("结果包中没有 content_list.json")
            items.append(("json", contents.content_list, f"{job.path.stem}_content_list.json"))

        written: list[str] = []
        for kind, content, filename in items:
            # 子文件夹模式下，md 与 json 各自归到 md/ 和 json/
            folder = base_dir / kind if mode == "subfolder" else base_dir
            folder.mkdir(parents=True, exist_ok=True)
            path = unique_path(folder / filename)
            path.write_text(content, encoding="utf-8", newline="\n")
            written.append(path.name)

        return written, base_dir


# ---------------------------------------------------------------- 设置窗口
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, current: dict, on_save, family: str, scale: float):
        super().__init__(master)
        self.on_save = on_save
        self.current = current
        self._saved = False
        self.scale = scale

        self.title("设置")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = int(20 * scale)
        wrap = tk.Frame(self, bg=CARD, padx=pad, pady=pad)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="MinerU API Key", bg=CARD, fg=TEXT,
                 font=(family, 11, "bold")).pack(anchor="w")
        tk.Label(wrap, text="在 mineru.net 控制台的「API 管理」中创建，本地仅保存在自己的电脑上。",
                 bg=CARD, fg=MUTED, font=(family, 9)).pack(anchor="w", pady=(4, 12))

        row = tk.Frame(wrap, bg=CARD)
        row.pack(fill="x")

        self.var = tk.StringVar(value=current.get("api_key", ""))
        self.entry = ttk.Entry(row, textvariable=self.var, width=44, show="•")
        self.entry.pack(side="left", fill="x", expand=True)

        self.show_var = tk.BooleanVar(value=False)
        ToggleRow(row, "显示", self.show_var, kind="check", scale=scale,
                  font=(family, 9), command=self._toggle_show).pack(
            side="left", padx=(int(8 * scale), 0))

        hint = tk.Frame(wrap, bg=CARD)
        hint.pack(fill="x", pady=(int(10 * scale), int(4 * scale)))
        self.result_label = tk.Label(hint, text="", bg=CARD, fg=MUTED, font=(family, 9), anchor="w")
        self.result_label.pack(side="left")
        ttk.Button(hint, text="测试连接", style="Link.TButton",
                   command=self._test).pack(side="right")

        buttons = tk.Frame(wrap, bg=CARD)
        buttons.pack(fill="x", pady=(int(16 * scale), 0))
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(buttons, text="取消", style="Ghost.TButton", command=self.destroy).pack(
            side="right", padx=(0, int(8 * scale)))

        self.entry.focus_set()
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())

        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _toggle_show(self):
        self.entry.configure(show="" if self.show_var.get() else "•")

    def _test(self):
        token = self.var.get().strip()
        if not token:
            self.result_label.configure(text="请先填写 API Key", fg=ERR)
            return
        self.result_label.configure(text="正在验证…", fg=MUTED)
        self.update_idletasks()

        def worker():
            try:
                api.verify_token(token)
                self.after(0, lambda: self.result_label.configure(text="验证通过，可以正常使用", fg=OK))
            except api.MineruError as exc:
                message = exc.message
                self.after(0, lambda: self.result_label.configure(text=message, fg=ERR))

        threading.Thread(target=worker, daemon=True).start()

    def _save(self):
        token = self.var.get().strip()
        if not token:
            self.result_label.configure(text="API Key 不能为空", fg=ERR)
            return
        self.on_save({"api_key": token})
        self.destroy()


# ---------------------------------------------------------------- 入口
def run_selftest(argv: list[str]) -> int:
    """命令行自检：转换一个文件后退出，用于验证打包产物是否可用。"""
    args = [a for a in argv[1:] if not a.startswith("--")]
    lines: list[str] = []
    code = 1
    try:
        source = Path(args[0]).expanduser().resolve()
        out_dir = Path(args[1]).expanduser().resolve() if len(args) > 1 else source.parent / "md"
        out_dir.mkdir(parents=True, exist_ok=True)

        conf = cfg.load()
        lines.append(f"token={'set' if conf.get('api_key') else 'missing'}")
        lines.append(f"src={source}")

        batch_id, urls = api.create_batch(
            conf["api_key"], [source.name], ["selftest"], model="vlm", language="ch",
        )
        api.upload_file(urls[0], source)

        result = None
        deadline = time.time() + 600
        while time.time() < deadline:
            items = api.get_batch(conf["api_key"], batch_id)
            if items and items[0]["state"] in ("done", "failed"):
                result = items[0]
                break
            time.sleep(3)

        if not result or result["state"] != "done":
            raise RuntimeError(f"state={result and result['state']} err={result and result['error']}")

        with tempfile.TemporaryDirectory(prefix="mineru-selftest-") as tmp:
            zip_path = Path(tmp) / "result.zip"
            api.download_zip(result["zip_url"], zip_path)
            contents = api.read_zip(zip_path)

        target = out_dir / f"{source.stem}.md"
        target.write_text(contents.markdown or "", encoding="utf-8")
        lines.append(f"ok {target} {target.stat().st_size} bytes")
        code = 0
    except Exception as exc:  # noqa: BLE001
        lines.append(f"FAIL {exc!r}")

    try:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (cfg.CONFIG_DIR / "selftest.log").write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return code


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest(sys.argv))

    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MinerU.Lite.1")
        except Exception:
            pass

    enable_dpi_awareness()
    root = tk.Tk()
    app = App(root)

    def excepthook(exc_type, exc_value, exc_tb):
        import traceback
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        cfg.write_error_log(f"[fatal] {datetime.now():%Y-%m-%d %H:%M:%S}\n{text}")
        try:
            messagebox.showerror("程序出错", f"{exc_value}\n\n详细信息已写入：\n{cfg.ERROR_LOG}")
        except Exception:
            pass

    root.report_callback_exception = lambda *args: excepthook(*args)
    sys.excepthook = excepthook

    extra_files = [p for p in sys.argv[1:] if not p.startswith("-") and os.path.isfile(p)]
    if extra_files:
        app._add_files(extra_files)

    root.mainloop()


if __name__ == "__main__":
    main()
