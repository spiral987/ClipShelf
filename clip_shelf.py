"""CLIP Shelf: a local Windows gallery for illustration files."""
from __future__ import annotations

import argparse
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import traceback
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from clip_core import Cancelled, Catalog

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

BG = "#14191d"
PANEL = "#1b2228"
CARD = "#222c33"
WELL = "#11171b"
FG = "#edf2ed"
MUTED = "#9aa9b0"
ACCENT = "#c2dfa5"
LINE = "#303c44"
WARN = "#edbd87"
STATUS = {"present": "", "missing": "保存先が見つかりません", "offline": "ドライブ未接続",
          "unreadable": "読み取りを保留"}
FONT = "Yu Gothic UI" if sys.platform == "win32" else "sans-serif"


def size_text(size):
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.0f} KB"


def date_text(ns):
    return datetime.fromtimestamp(ns / 1e9).strftime("%Y/%m/%d %H:%M")


class App(tk.Tk):
    def __init__(self, catalog):
        super().__init__()
        self.catalog = catalog
        self.title("CLIP Shelf · イラストの作品棚")
        self.geometry("1280x820")
        self.minsize(1040, 660)
        self.configure(bg=BG)
        self.option_add("*Font", (FONT, 10))
        self.option_add("*Background", PANEL)
        self.option_add("*Foreground", FG)
        self.option_add("*selectBackground", "#47653e")
        self.option_add("*selectForeground", FG)
        self.option_add("*insertBackground", ACCENT)
        self.works = []
        self.filtered = []
        self.selected = None
        self.scope = ("all", "")
        self.photos = OrderedDict()
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.closing = False
        self.loading = False
        self.dirty = False
        self.render_job = None
        self.save_job = None
        self.favorite = False
        self.last_errors = []
        self.search = tk.StringVar()
        self.sort = tk.StringVar(value="更新が新しい順")
        self.status = tk.StringVar(value="フォルダを登録すると、サブフォルダ内の .clip も一覧に表示します。")
        self._styles()
        self._layout()
        self.search.trace_add("write", lambda *_: self._filter())
        self.bind("<Control-f>", self._focus_search)
        self.bind("<Control-s>", lambda _: self._save_metadata())
        self.bind("<F5>", lambda _: self.scan())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.after(100, self._poll)
        if self.catalog.roots():
            self.after(450, self.scan)

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                        foreground=FG, arrowcolor=ACCENT, padding=6, borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", FG)],
                  selectbackground=[("readonly", CARD)], selectforeground=[("readonly", FG)])
        style.configure("Vertical.TScrollbar", background=LINE, troughcolor=BG,
                        borderwidth=0, arrowcolor=MUTED, width=9)
        style.configure("TProgressbar", troughcolor=BG, background=ACCENT, borderwidth=0)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=FG, borderwidth=0, rowheight=32, font=(FONT, 10))
        style.map("Treeview", background=[("selected", "#344635")],
                  foreground=[("selected", ACCENT)])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    def label(self, parent, text, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", PANEL), fg=kw.pop("fg", FG),
                        font=kw.pop("font", (FONT, 10)), **kw)

    def button(self, parent, text, command, primary=False, **kw):
        return tk.Button(parent, text=text, command=command,
                         bg=ACCENT if primary else CARD, fg=BG if primary else FG,
                         activebackground="#d6ebc2" if primary else LINE,
                         activeforeground=BG if primary else FG,
                         relief="flat", bd=0, padx=kw.pop("padx", 14), pady=kw.pop("pady", 8),
                         cursor="hand2", disabledforeground="#607078", **kw)

    def entry(self, parent, **kw):
        return tk.Entry(parent, bg=WELL, fg=FG, relief="flat", highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=ACCENT, insertbackground=ACCENT,
                        **kw)

    def _layout(self):
        sidebar = tk.Frame(self, bg=PANEL, width=220)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self.label(sidebar, "CLIP Shelf", font=(FONT, 23, "bold"), fg=ACCENT).pack(anchor="w", padx=22, pady=(24, 1))
        self.label(sidebar, "イラストの作品棚", fg=MUTED).pack(anchor="w", padx=24, pady=(0, 27))
        self.label(sidebar, "ライブラリ", fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=24, pady=(0, 7))
        self.nav = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=10)
        self.nav.pack(fill="both", expand=True, padx=12)
        self.nav.bind("<<TreeviewSelect>>", self._navigate)
        self.nav.bind("<Button-3>", self._nav_menu)
        self.button(sidebar, "＋ フォルダを登録", self.add_folder, primary=True).pack(fill="x", padx=18, pady=(18, 8))
        self.button(sidebar, "管理情報をバックアップ", self.backup, padx=8).pack(fill="x", padx=18, pady=(0, 12))
        self.label(sidebar, "PC内だけで管理します\n作品は元のフォルダに保存", fg=MUTED,
                   justify="left", font=(FONT, 9)).pack(anchor="w", padx=22, pady=(0, 21))

        body = tk.Frame(self, bg=BG)
        body.pack(side="left", fill="both", expand=True)
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x", padx=26, pady=(24, 16))
        self.heading = self.label(top, "すべての作品", font=(FONT, 21, "bold"), bg=BG)
        self.heading.pack(side="left")
        self.cancel_button = self.button(top, "中止", self.cancel.set)
        self.scan_button = self.button(top, "↻ 再スキャン", self.scan)
        self.scan_button.pack(side="right")
        self.count_label = self.label(body, "", fg=MUTED, bg=BG)
        self.count_label.pack(anchor="w", padx=28, pady=(0, 14))
        tools = tk.Frame(body, bg=BG)
        tools.pack(fill="x", padx=26, pady=(0, 16))
        self.search_box = self.entry(tools, textvariable=self.search, font=(FONT, 11))
        self.search_box.pack(side="left", fill="x", expand=True, ipady=8)
        self.search_box.bind("<Escape>", lambda _: self.search.set(""))
        self.label(tools, "検索", bg=BG, fg=MUTED).pack(side="left", padx=(8, 14))
        sorting = ttk.Combobox(tools, textvariable=self.sort, state="readonly", width=17,
                              values=("更新が新しい順", "更新が古い順", "名前順"))
        sorting.pack(side="right")
        sorting.bind("<<ComboboxSelected>>", lambda _: self._filter())

        middle = tk.Frame(body, bg=BG)
        middle.pack(fill="both", expand=True)
        detail_shell = tk.Frame(middle, bg=PANEL, width=306)
        detail_shell.pack(side="right", fill="y", padx=(8, 20), pady=(0, 16))
        detail_shell.pack_propagate(False)
        self.detail_canvas = tk.Canvas(detail_shell, bg=PANEL, highlightthickness=0, bd=0)
        detail_scrollbar = ttk.Scrollbar(detail_shell, orient="vertical", command=self.detail_canvas.yview)
        detail_scrollbar.pack(side="right", fill="y")
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        self.detail_canvas.configure(yscrollcommand=detail_scrollbar.set)
        self.detail = tk.Frame(self.detail_canvas, bg=PANEL)
        detail_item = self.detail_canvas.create_window(0, 0, window=self.detail, anchor="nw")
        self.detail.bind("<Configure>", lambda _: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all")))
        self.detail_canvas.bind("<Configure>", lambda e: self.detail_canvas.itemconfigure(detail_item, width=e.width))
        gallery = tk.Frame(middle, bg=BG)
        gallery.pack(side="left", fill="both", expand=True, padx=(18, 0), pady=(0, 14))
        self.canvas = tk.Canvas(gallery, bg=BG, highlightthickness=0, bd=0, takefocus=True)
        scrollbar = ttk.Scrollbar(gallery, orient="vertical", command=self._scroll)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind("<Configure>", lambda _: self.schedule_render())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-1>", self._card_click)
        self.canvas.bind("<Double-1>", lambda _: self.open_work())
        self.canvas.bind("<Button-3>", self._card_menu)
        self.canvas.bind("<Return>", lambda _: self.open_work())
        self._detail_layout()

        footer = tk.Frame(body, bg=BG)
        footer.pack(fill="x", padx=26, pady=(0, 12))
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=90)
        self.status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED,
                                     anchor="w", font=(FONT, 9))
        self.status_label.pack(side="left", fill="x", expand=True)
        self.error_button = self.button(footer, "詳細", self.show_errors, padx=8, pady=2)

    def _detail_layout(self):
        self.detail_hint = self.label(self.detail, "作品を選ぶと\nプレビュー・タグ・メモを表示します。",
                                      fg=MUTED, justify="left")
        self.detail_hint.pack(anchor="w", padx=20, pady=26)
        self.detail_content = tk.Frame(self.detail, bg=PANEL)
        self.preview = tk.Label(self.detail_content, bg=WELL, height=1)
        self.preview.pack(fill="x", padx=14, pady=(14, 12))
        self.detail_name = self.label(self.detail_content, "", font=(FONT, 13, "bold"),
                                      anchor="w", justify="left", wraplength=258)
        self.detail_name.pack(fill="x", padx=18)
        self.detail_info = self.label(self.detail_content, "", fg=MUTED, font=(FONT, 9),
                                      anchor="w", justify="left", wraplength=258)
        self.detail_info.pack(fill="x", padx=18, pady=(4, 9))
        actions = tk.Frame(self.detail_content, bg=PANEL)
        actions.pack(fill="x", padx=16)
        self.open_button = self.button(actions, "クリスタで開く", self.open_work, primary=True, padx=12)
        self.open_button.pack(side="left", fill="x", expand=True)
        self.favorite_button = self.button(actions, "☆", self.toggle_favorite, padx=12)
        self.favorite_button.pack(side="right", padx=(7, 0))
        self.path_label = self.label(self.detail_content, "", fg=MUTED, justify="left", anchor="w",
                                     wraplength=254, font=(FONT, 9))
        self.path_label.pack(fill="x", padx=18, pady=(12, 6))
        path_actions = tk.Frame(self.detail_content, bg=PANEL)
        self.path_actions = path_actions
        path_actions.pack(fill="x", padx=16)
        self.button(path_actions, "場所を開く", self.reveal, padx=12, pady=5).pack(side="left")
        self.button(path_actions, "パスをコピー", self.copy_path, padx=12, pady=5).pack(side="left", padx=(6, 0))
        self.relink_button = self.button(self.detail_content, "移動先の .clip を指定", self.relink, pady=6)
        self.label(self.detail_content, "タグ", fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=18, pady=(16, 5))
        self.tags_box = self.entry(self.detail_content)
        self.tags_box.pack(fill="x", padx=18, ipady=6)
        self.tags_box.bind("<KeyRelease>", self._changed)
        self.label(self.detail_content, "カンマ区切り  例：創作, 完成, 2026", fg=MUTED,
                   font=(FONT, 8)).pack(anchor="w", padx=18, pady=(3, 8))
        self.label(self.detail_content, "メモ", fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=18, pady=(3, 5))
        self.notes_box = tk.Text(self.detail_content, bg=WELL, fg=FG, height=3, wrap="word",
                                 relief="flat", padx=8, pady=6, undo=True, insertbackground=ACCENT)
        self.notes_box.pack(fill="x", padx=18)
        self.notes_box.bind("<KeyRelease>", self._changed)
        self.save_label = self.label(self.detail_content, "変更は自動保存されます", fg=MUTED, font=(FONT, 8))
        self.save_label.pack(anchor="w", padx=18, pady=(6, 12))

    def _focus_search(self, _=None):
        self.search_box.focus_set()
        self.search_box.select_range(0, "end")
        return "break"

    def refresh(self):
        if not self._save_metadata():
            return
        self.works = self.catalog.works()
        self._build_nav()
        self._filter()
        if self.selected:
            self._show_detail(self.selected)

    def _build_nav(self):
        self.nav.delete(*self.nav.get_children())
        self.nav_scopes = {}
        def add(key, title, scope, parent="", opened=False):
            self.nav.insert(parent, "end", iid=key, text=title, open=opened)
            self.nav_scopes[key] = scope
        add("all", f"すべての作品     {len(self.works):,}", ("all", ""))
        add("favorite", f"☆ お気に入り     {sum(bool(w['favorite']) for w in self.works):,}", ("favorite", ""))
        add("missing", f"保存先の確認     {sum(w['status'] != 'present' for w in self.works):,}", ("missing", ""))
        add("folders", "登録フォルダ", ("all", ""), opened=True)
        for i, root in enumerate(self.catalog.roots()):
            add(f"root-{i}", Path(root).name or root, ("root", root), "folders")
        tags = sorted({tag for w in self.works for tag in w["tags"]}, key=str.casefold)
        add("tags", "タグ", ("all", ""), opened=True)
        for i, tag in enumerate(tags):
            add(f"tag-{i}", "# " + tag, ("tag", tag), "tags")
        selection = next((key for key, scope in self.nav_scopes.items()
                          if scope == self.scope and key not in ("folders", "tags")), "all")
        if selection == "all" and self.scope != ("all", ""):
            self.scope = ("all", "")
        self.nav.selection_set(selection)

    def _navigate(self, _=None):
        selection = self.nav.selection()
        if selection:
            new_scope = self.nav_scopes[selection[0]]
            if self.scope != new_scope:
                self.scope = new_scope
                self._filter()

    def _nav_menu(self, event):
        item = self.nav.identify_row(event.y)
        if not item or self.nav_scopes[item][0] != "root":
            return
        root = self.nav_scopes[item][1]
        menu = tk.Menu(self, tearoff=False, bg=CARD, fg=FG)
        menu.add_command(label="このフォルダの登録を外す", command=lambda: self.remove_folder(root))
        menu.tk_popup(event.x_root, event.y_root)

    def _filter(self):
        scope, value = self.scope
        query = self.search.get().casefold().split()
        result = []
        for work in self.works:
            if scope == "favorite" and not work["favorite"]:
                continue
            if scope == "missing" and work["status"] == "present":
                continue
            if scope == "root" and work["root"] != value:
                continue
            if scope == "tag" and value not in work["tags"]:
                continue
            haystack = " ".join([work["name"], work["path"], work["notes"], *work["tags"]]).casefold()
            if all(word in haystack for word in query):
                result.append(work)
        if self.sort.get() == "名前順":
            result.sort(key=lambda w: w["name"].casefold())
        else:
            result.sort(key=lambda w: w["mtime_ns"], reverse=self.sort.get() != "更新が古い順")
        self.filtered = result
        heading = {"all": "すべての作品", "favorite": "お気に入り", "missing": "保存先の確認",
                   "root": Path(value).name if value else "", "tag": "# " + value}[scope]
        self.heading.configure(text=heading)
        self.count_label.configure(text=f"{len(result):,} 作品  ·  ファイル名・タグ・メモで検索")
        self.canvas.yview_moveto(0)
        self.schedule_render()

    def schedule_render(self):
        if self.render_job:
            self.after_cancel(self.render_job)
        self.render_job = self.after(30, self._render)

    def _scroll(self, *args):
        self.canvas.yview(*args)
        self.schedule_render()

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        self.schedule_render()
        return "break"

    def _photo(self, work, width, height):
        key = (work["thumb"], width, height)
        if key in self.photos:
            self.photos.move_to_end(key)
            return self.photos[key]
        photo = None
        if work["thumb"]:
            try:
                if Image:
                    with Image.open(work["thumb"]) as image:
                        image = image.convert("RGBA")
                        image.thumbnail((width, height), Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS)
                        photo = ImageTk.PhotoImage(image)
                else:
                    photo = tk.PhotoImage(file=work["thumb"])
                    scale = max(1, math.ceil(photo.width() / width), math.ceil(photo.height() / height))
                    if scale > 1:
                        photo = photo.subsample(scale, scale)
            except Exception:
                photo = None
        self.photos[key] = photo
        while len(self.photos) > 160:
            self.photos.popitem(last=False)
        return photo

    def _short(self, text, width, font):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def _render(self):
        self.render_job = None
        canvas = self.canvas
        canvas.delete("all")
        # Retain visible PhotoImages even if the LRU cache evicts them.
        self.visible_photos = []
        width = max(100, canvas.winfo_width())
        height = max(100, canvas.winfo_height())
        if not self.filtered:
            canvas.configure(scrollregion=(0, 0, width, height))
            title = "最初の作品フォルダを登録" if not self.works else "該当する作品がありません"
            subtitle = "左下の「＋ フォルダを登録」から始めましょう。\n.clip を読み取り、プレビューを一覧にします。" if not self.works else "検索条件やタグを変えてみてください。"
            canvas.create_text(width / 2, height / 2 - 45, text="▧", fill=ACCENT, font=(FONT, 42))
            canvas.create_text(width / 2, height / 2 + 16, text=title, fill=FG, font=(FONT, 16, "bold"))
            canvas.create_text(width / 2, height / 2 + 65, text=subtitle, fill=MUTED,
                               font=(FONT, 10), justify="center", width=max(200, width - 40))
            return
        cols = max(1, int((width - 16) / 204))
        tile_width = min(286, (width - 16 - (cols - 1) * 14) / cols)
        image_height = 176
        tile_height = 264
        rows = math.ceil(len(self.filtered) / cols)
        canvas.configure(scrollregion=(0, 0, width, rows * (tile_height + 16)))
        top = canvas.canvasy(0)
        first = max(0, int(top // (tile_height + 16)) - 1)
        last = min(rows, int((top + height) // (tile_height + 16)) + 2)
        title_font = tkfont.Font(family=FONT, size=11, weight="bold")
        meta_font = tkfont.Font(family=FONT, size=9)
        for index in range(first * cols, min(len(self.filtered), last * cols)):
            work = self.filtered[index]
            col, row = index % cols, index // cols
            x, y = 3 + col * (tile_width + 14), row * (tile_height + 16) + 2
            tag = "work:" + work["id"]
            outline = ACCENT if work["id"] == self.selected else LINE
            canvas.create_rectangle(x, y, x + tile_width, y + tile_height, fill=CARD,
                                    outline=outline, width=2 if work["id"] == self.selected else 1, tags=tag)
            canvas.create_rectangle(x + 9, y + 9, x + tile_width - 9, y + image_height + 9,
                                    fill=WELL, outline="", tags=tag)
            photo = self._photo(work, int(tile_width - 22), image_height - 8)
            if photo:
                self.visible_photos.append(photo)
                canvas.create_image(x + tile_width / 2, y + 9 + image_height / 2, image=photo, tags=tag)
            else:
                canvas.create_text(x + tile_width / 2, y + 68, text=".clip", fill=MUTED,
                                   font=(FONT, 23), tags=tag)
                canvas.create_text(x + tile_width / 2, y + 104, text="プレビュー未取得", fill=MUTED,
                                   font=(FONT, 9), tags=tag)
            if work["favorite"]:
                canvas.create_text(x + tile_width - 23, y + 24, text="★", fill=ACCENT,
                                   font=(FONT, 16), tags=tag)
            canvas.create_text(x + 13, y + 204, text=self._short(work["name"], tile_width - 26, title_font),
                               fill=FG, font=title_font, anchor="w", tags=tag)
            if work["status"] != "present":
                meta = STATUS.get(work["status"], "保存先を確認")
                color = WARN
            elif work["tags"]:
                meta = "  ".join("#" + t for t in work["tags"])
                color = ACCENT
            else:
                meta = size_text(work["size"])
                color = MUTED
            canvas.create_text(x + 13, y + 227, text=self._short(meta, tile_width - 26, meta_font),
                               fill=color, font=meta_font, anchor="w", tags=tag)
            canvas.create_text(x + 13, y + 248, text=date_text(work["mtime_ns"]),
                               fill=MUTED, font=(FONT, 8), anchor="w", tags=tag)

    def _hit(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("work:"):
                    return tag[5:]
        return None

    def _card_click(self, event):
        work_id = self._hit(event)
        if work_id:
            if not self._save_metadata():
                return
            self._show_detail(work_id)
            self.canvas.focus_set()
            self.schedule_render()

    def _card_menu(self, event):
        self._card_click(event)
        if not self._hit(event):
            return
        menu = tk.Menu(self, tearoff=False, bg=CARD, fg=FG)
        menu.add_command(label="クリスタで開く", command=self.open_work)
        menu.add_command(label="保存場所を開く", command=self.reveal)
        menu.add_command(label="お気に入りを切り替え", command=self.toggle_favorite)
        menu.add_separator()
        menu.add_command(label="移動先の .clip を指定", command=self.relink)
        menu.tk_popup(event.x_root, event.y_root)

    def current(self):
        return next((w for w in self.works if w["id"] == self.selected), None)

    def _show_detail(self, work_id):
        work = next((w for w in self.works if w["id"] == work_id), None)
        previous = self.selected
        self.selected = work_id if work else None
        if not work:
            self.detail_content.pack_forget()
            self.detail_hint.pack(anchor="w", padx=20, pady=26)
            return
        self.loading = True
        if previous != work_id:
            self.detail_canvas.yview_moveto(0)
        self.detail_hint.pack_forget()
        self.detail_content.pack(fill="both", expand=True)
        photo = self._photo(work, 262, 168)
        self.detail_photo = photo
        self.preview.configure(image=photo or "", text="" if photo else "プレビュー未取得",
                               height=photo.height() if photo else 5, fg=MUTED,
                               compound="center", padx=0, pady=0)
        self.detail_name.configure(text=work["name"])
        detail = f"{size_text(work['size'])}  ·  {date_text(work['mtime_ns'])}"
        if work["status"] != "present":
            detail += "\n" + STATUS.get(work["status"], "保存先を確認")
        elif work["preview_error"] or (work["thumb"] and not photo):
            detail += "\nプレビューを表示できません（作品は開けます）"
        self.detail_info.configure(text=detail, fg=WARN if work["status"] != "present" else MUTED)
        display_path = work["path"]
        if len(display_path) > 180:
            display_path = display_path[:50] + " … " + display_path[-124:]
        self.path_label.configure(text=display_path)
        self.favorite = bool(work["favorite"])
        self.favorite_button.configure(text="★" if self.favorite else "☆", fg=ACCENT if self.favorite else FG)
        self.open_button.configure(state="normal" if work["status"] == "present" else "disabled")
        self.relink_button.pack_forget()
        if work["status"] != "present":
            self.relink_button.pack(fill="x", padx=16, pady=(10, 0), after=self.path_actions)
        self.tags_box.delete(0, "end")
        self.tags_box.insert(0, ", ".join(work["tags"]))
        self.notes_box.delete("1.0", "end")
        self.notes_box.insert("1.0", work["notes"])
        self.dirty = False
        self.save_label.configure(text="変更は自動保存されます")
        self.loading = False

    def _changed(self, _=None):
        if self.loading or not self.selected:
            return
        self.dirty = True
        self.save_label.configure(text="保存中…")
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(650, self._save_metadata)

    def _save_metadata(self):
        if self.save_job:
            self.after_cancel(self.save_job)
            self.save_job = None
        work = self.current()
        if not work or not self.dirty:
            return True
        tags = [s.strip() for s in re.split("[,、，\n]", self.tags_box.get()) if s.strip()]
        tags = list(dict.fromkeys(tags))
        notes = self.notes_box.get("1.0", "end-1c")
        try:
            self.catalog.metadata(work["id"], tags, notes, self.favorite)
        except Exception as exc:
            self.save_label.configure(text="保存に失敗しました。Ctrl+Sで再試行")
            messagebox.showerror("管理情報の保存", str(exc), parent=self)
            return False
        work.update(tags=tags, notes=notes, favorite=int(self.favorite))
        self.dirty = False
        self.save_label.configure(text="保存済み")
        self._build_nav()
        self.schedule_render()
        return True

    def toggle_favorite(self):
        if not self.current():
            return
        self.favorite = not self.favorite
        self.dirty = True
        self._save_metadata()
        self.favorite_button.configure(text="★" if self.favorite else "☆", fg=ACCENT if self.favorite else FG)
        self._filter()

    def add_folder(self):
        if self.worker and self.worker.is_alive():
            self.status.set("スキャン終了後にフォルダを追加できます。")
            return
        folder = filedialog.askdirectory(title="作品フォルダを登録（サブフォルダも対象）", parent=self)
        if not folder:
            return
        try:
            self.catalog.add_root(folder)
            self.scope = ("all", "")
            self.refresh()
            self.scan()
        except Exception as exc:
            messagebox.showerror("フォルダ登録", str(exc), parent=self)

    def remove_folder(self, root):
        if self.worker and self.worker.is_alive():
            self.status.set("スキャン終了後に登録を変更できます。")
            return
        if messagebox.askyesno("登録フォルダ", f"スキャン対象から外しますか？\n\n{root}\n\n作品ファイルと登録済みのタグ・メモは保持します。", parent=self):
            self.catalog.remove_root(root)
            self.scope = ("all", "")
            self.refresh()

    def scan(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.catalog.roots():
            self.add_folder()
            return
        if not self._save_metadata():
            return
        self.cancel.clear()
        self.last_errors = []
        self.error_button.pack_forget()
        self.scan_button.configure(state="disabled")
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.progress.pack(side="right", padx=(10, 0))
        self.progress.start(14)
        self.status.set("スキャンを開始しています…")
        def run():
            try:
                result = self.catalog.scan(lambda kind, text: self.events.put((kind, text)), self.cancel)
                self.events.put(("done", result))
            except Cancelled:
                self.events.put(("cancelled", None))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
        self.worker = threading.Thread(target=run, daemon=True, name="clip-shelf-scan")
        self.worker.start()

    def _poll(self):
        if self.closing:
            return
        latest = None
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest = value
                continue
            latest = None
            self.progress.stop()
            self.progress.pack_forget()
            self.cancel_button.pack_forget()
            self.scan_button.configure(state="normal")
            if kind == "done":
                self.refresh()
                text = f"確認 {value['found']:,} 件  ·  新規 {value['added']:,}  ·  再接続 {value['moved']:,}  ·  更新 {value['updated']:,}"
                if value["ambiguous"]:
                    text += f"  ·  同一コピーの候補 {value['ambiguous']}"
                if value["errors"]:
                    text += f"  ·  読み取り保留 {len(value['errors'])}"
                self.last_errors = value["errors"]
                self.status.set(text)
                if self.last_errors:
                    self.error_button.pack(side="right", padx=8)
            elif kind == "cancelled":
                self.status.set("スキャンを中止しました。前回の管理情報を保持しています。")
            elif kind == "error":
                self.status.set("スキャンに失敗しました。詳細をご確認ください。")
                self.last_errors = [value]
                self.error_button.pack(side="right", padx=8)
        if latest:
            self.status.set(latest[:130])
        self.after(100, self._poll)

    def show_errors(self):
        if self.last_errors:
            window = tk.Toplevel(self)
            window.title("読み取りを保留したファイル")
            window.geometry("760x430")
            text = tk.Text(window, bg=WELL, fg=FG, wrap="word", padx=15, pady=15)
            text.pack(fill="both", expand=True)
            text.insert("1.0", "\n\n".join(self.last_errors))
            text.configure(state="disabled")

    def open_work(self):
        work = self.current()
        if not work:
            return
        try:
            if not Path(work["path"]).is_file():
                raise OSError("ファイルが見つかりません。移動先のフォルダを登録し、再スキャンしてください。")
            os.startfile(work["path"])
        except (OSError, AttributeError) as exc:
            messagebox.showerror("作品を開く", f"{exc}\n\nWindowsで .clip の既定アプリをCLIP STUDIO PAINTに設定してください。", parent=self)

    def reveal(self):
        work = self.current()
        if not work:
            return
        try:
            path = Path(work["path"])
            if not path.exists():
                raise OSError("保存先が見つかりません。")
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        except OSError as exc:
            messagebox.showerror("保存場所", str(exc), parent=self)

    def copy_path(self):
        work = self.current()
        if work:
            self.clipboard_clear()
            self.clipboard_append(work["path"])
            self.status.set("作品のパスをコピーしました。")

    def relink(self):
        work = self.current()
        if not work:
            return
        if self.worker and self.worker.is_alive():
            self.status.set("スキャン終了後に再接続できます。")
            return
        filename = filedialog.askopenfilename(title="この作品の移動先を選択", filetypes=[("CLIP STUDIO作品", "*.clip")], parent=self)
        if not filename:
            return
        if not messagebox.askyesno("管理情報を再接続", f"「{work['name']}」のタグ・メモを、次のファイルに再接続しますか？\n\n{filename}\n\n指定先が注釈のない登録済み作品の場合は、一覧の重複もまとめます。", parent=self):
            return
        if not self._save_metadata():
            return
        self.status.set("再接続するファイルを確認しています…")
        self.update_idletasks()
        def run():
            try:
                self.catalog.relink(work["id"], filename)
                self.events.put(("done", dict(found=1, added=0, moved=1, updated=0, ambiguous=0, errors=[])))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
        self.worker = threading.Thread(target=run, daemon=True, name="clip-shelf-relink")
        self.worker.start()
        self.scan_button.configure(state="disabled")
        self.progress.pack(side="right", padx=(10, 0))
        self.progress.start(14)

    def backup(self):
        if not self._save_metadata():
            return
        filename = filedialog.asksaveasfilename(title="タグ・メモ等の管理情報をバックアップ", parent=self,
                    initialfile="ClipShelf-" + datetime.now().strftime("%Y%m%d") + ".sqlite",
                    defaultextension=".sqlite", filetypes=[("管理データ", "*.sqlite")])
        if filename:
            try:
                self.catalog.backup(filename)
                self.status.set("管理情報をバックアップしました。作品ファイルのバックアップは別途行ってください。")
            except Exception as exc:
                messagebox.showerror("バックアップ", str(exc), parent=self)

    def close(self):
        self._save_metadata()
        if self.dirty:
            return
        self.closing = True
        self.cancel.set()
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description="CLIP Shelf · local .clip gallery")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    args = parser.parse_args()
    try:
        if sys.platform == "win32":
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        App(Catalog(args.data_dir)).mainloop()
    except Exception:
        error = traceback.format_exc()
        try:
            args.data_dir.mkdir(parents=True, exist_ok=True)
            (args.data_dir / "last-error.txt").write_text(error, encoding="utf-8")
            messagebox.showerror("CLIP Shelf", "起動できませんでした。data/last-error.txt を確認してください。\n\n" + error[-900:])
        except Exception:
            print(error, file=sys.stderr)


if __name__ == "__main__":
    main()
