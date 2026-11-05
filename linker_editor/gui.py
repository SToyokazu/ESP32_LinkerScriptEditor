"""Tkinter によるメモリ配置編集画面。外部パッケージは使用しない。"""

from __future__ import annotations

import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .core import (add_region, add_section, move_section,
                   parse_script, resolve_regions, update_region,
                   update_section_body, validate_scripts)
from .mapfile import parse_map
from .profiles import CHIP_NAMES, create_template, detect_chip, profile_notes
from .storage import (export_scripts, read_project, read_text, safe_filename,
                      save_project)

BG = "#eef2f6"
INK = "#172b43"
MUTED = "#64758b"
BLUE = "#2463db"
COLORS = ("#2365cc", "#8b52bd", "#00897e", "#b77917", "#b84b70", "#43768e")


def hex_address(value):
    return "未解決" if value is None else f"0x{value:08X}"


def byte_size(value):
    if value is None:
        return "未解決"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):,.2f} MiB"
    if value >= 1024:
        return f"{value / 1024:,.2f} KiB"
    return f"{value:,} B"


class Editor:
    """ソースを正として、可視化・編集・履歴を同期する。"""

    def __init__(self, root, chip="ESP32"):
        self.root = root
        self.chip = chip
        self.scripts = [parse_script(create_template(chip), f"{chip.lower()}_custom.ld")]
        self.paths = {}
        self.map_text = ""
        self.map_name = ""
        self.map_stale = False
        self.map_data = parse_map("")
        self.history = []
        self.future = []
        self.selected = None
        self.active_file = 0
        self.source_dirty = False
        self._source_setting = False
        self.drag = None
        self.region_refs = []
        self.section_refs = []
        self.saved_state = self.capture()
        self.status = tk.StringVar(value="セクションを選択して配置を編集できます。")
        self.root.title("ESP32 Linker Script Editor")
        width = max(1000, min(1420, self.root.winfo_screenwidth() - 80))
        height = max(660, min(940, self.root.winfo_screenheight() - 100))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1080, width), min(760, height))
        self.root.configure(bg=BG)
        self._build_style()
        self._build_menu()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-o>", lambda e: self.open_scripts())
        self.root.bind("<Control-s>", lambda e: self.save_project_dialog())
        self.root.bind("<Control-Shift-S>", lambda e: self.export_dialog())
        self.root.bind("<Control-z>", self._undo_key)
        self.root.bind("<Control-y>", self._redo_key)
        self.refresh()

    def _build_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Yu Gothic UI", 10), background=BG, foreground=INK)
        style.configure("TButton", padding=(12, 7))
        style.configure("Accent.TButton", background=BLUE, foreground="white")
        style.map("Accent.TButton", background=[("active", "#1d4eae")])
        style.configure("TNotebook.Tab", padding=(16, 8))
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=27)
        style.configure("Treeview.Heading", padding=(8, 6), font=("Yu Gothic UI", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Yu Gothic UI", 10, "bold"))

    def _build_menu(self):
        menu = tk.Menu(self.root)
        files = tk.Menu(menu, tearoff=False)
        for label, command in [("新規作成…", self.new_dialog), ("スクリプトを開く / 追加…", self.open_scripts),
                               ("ESP-IDF build フォルダー…", self.open_build), ("MAP を読み込む…", self.open_map),
                               ("INCLUDE 参照を追加…", self.open_includes)]:
            files.add_command(label=label, command=command)
        files.add_separator()
        files.add_command(label="プロジェクトを開く…", command=self.open_project_dialog)
        files.add_command(label="プロジェクトを保存…    Ctrl+S", command=self.save_project_dialog)
        files.add_command(label=".ld を出力…    Ctrl+Shift+S", command=self.export_dialog)
        files.add_separator()
        files.add_command(label="終了", command=self.close)
        menu.add_cascade(label="ファイル", menu=files)
        edits = tk.Menu(menu, tearoff=False)
        edits.add_command(label="元に戻す    Ctrl+Z", command=self.undo)
        edits.add_command(label="やり直す    Ctrl+Y", command=self.redo)
        edits.add_separator()
        edits.add_command(label="領域を追加…", command=self.add_region_dialog)
        edits.add_command(label="セクションを追加…", command=self.add_section_dialog)
        edits.add_command(label="選択ファイルを閉じる", command=self.remove_script)
        menu.add_cascade(label="編集", menu=edits)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="使い方と制限", command=self.show_help)
        menu.add_cascade(label="ヘルプ", menu=help_menu)
        self.root.config(menu=menu)

    def _build_ui(self):
        header = tk.Frame(self.root, bg=INK, padx=22, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="ESP32  /  LINKER SCRIPT EDITOR", bg=INK, fg="white",
                 font=("Segoe UI", 18, "bold")).pack(side="left")
        self.chip_label = tk.Label(header, bg="#284561", fg="#cbe4ff", padx=15, pady=5,
                                   font=("Segoe UI", 11, "bold"))
        self.chip_label.pack(side="right")
        toolbar = ttk.Frame(self.root, padding=(18, 10))
        toolbar.pack(fill="x")
        for label, cmd in [("新規", self.new_dialog), (".ld を開く", self.open_scripts),
                           ("build を開く", self.open_build), ("MAP を読込", self.open_map),
                           ("戻す", self.undo), ("やり直す", self.redo)]:
            ttk.Button(toolbar, text=label, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text=".ld を出力", command=self.export_dialog,
                   style="Accent.TButton").pack(side="right")
        ttk.Button(toolbar, text="編集を保存", command=self.save_project_dialog).pack(side="right", padx=8)
        self.summary_label = ttk.Label(self.root, padding=(22, 0, 22, 6), foreground=MUTED)
        self.summary_label.pack(fill="x")
        self.map_label = tk.Label(self.root, anchor="w", bg="#dfeafa", fg="#254e86",
                                 padx=20, pady=7, font=("Yu Gothic UI", 10))
        self.map_label.pack(fill="x", padx=18, pady=(0, 8))
        vertical = ttk.Panedwindow(self.root, orient="vertical")
        vertical.pack(fill="both", expand=True, padx=18)
        top = ttk.Panedwindow(vertical, orient="horizontal")
        vertical.add(top, weight=3)
        map_frame = ttk.Frame(top)
        top.add(map_frame, weight=3)
        ttk.Label(map_frame, text="メモリ領域  /  セクションをドラッグして移動",
                  font=("Yu Gothic UI", 11, "bold"), padding=(5, 5)).pack(anchor="w")
        holder = ttk.Frame(map_frame)
        holder.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, height=385)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.draw_map())
        self.canvas.bind("<ButtonPress-1>", self.canvas_press)
        self.canvas.bind("<B1-Motion>", self.canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self.canvas_release)
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120), "units"))
        self.inspector = ttk.Frame(top, padding=(14, 5, 8, 8), width=375)
        top.add(self.inspector, weight=1)
        self.notebook = ttk.Notebook(vertical)
        vertical.add(self.notebook, weight=2)
        section_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(section_tab, text="配置ルール")
        search_bar = ttk.Frame(section_tab)
        search_bar.pack(fill="x", pady=(0, 6))
        ttk.Label(search_bar, text="検索").pack(side="left")
        self.filter_var = tk.StringVar()
        ttk.Entry(search_bar, textvariable=self.filter_var, width=28).pack(side="left", padx=8)
        self.filter_var.trace_add("write", lambda *args: self.fill_sections())
        ttk.Button(search_bar, text="＋ セクション", command=self.add_section_dialog).pack(side="right")
        ttk.Button(search_bar, text="＋ 領域", command=self.add_region_dialog).pack(side="right", padx=6)
        self.section_tree = self._tree(section_tab, ("name", "region", "lma", "address", "size", "file"),
                                       ("出力セクション", "配置先 (VMA)", "ロード先 (AT>)", "MAP アドレス", "MAP サイズ", "ファイル"),
                                       (200, 180, 130, 115, 110, 170))
        self.section_tree.bind("<<TreeviewSelect>>", self.select_tree_section)
        map_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(map_tab, text="コード / オブジェクト (MAP)")
        map_search = ttk.Frame(map_tab)
        map_search.pack(fill="x", pady=(0, 6))
        ttk.Label(map_search, text="名前 / オブジェクト検索").pack(side="left")
        self.map_filter = tk.StringVar()
        ttk.Entry(map_search, textvariable=self.map_filter, width=30).pack(side="left", padx=8)
        self.map_all = tk.BooleanVar(value=False)
        ttk.Checkbutton(map_search, text="全セクションから検索", variable=self.map_all,
                        command=self.fill_entries).pack(side="left")
        self.map_filter.trace_add("write", lambda *args: self.fill_entries())
        self.entry_title = ttk.Label(map_tab, text="配置ルールからセクションを選択してください。")
        self.entry_title.pack(anchor="w", pady=(0, 6))
        self.entry_tree = self._tree(map_tab, ("name", "address", "size", "object", "section"),
                                     ("入力セクション / シンボル", "アドレス", "サイズ", "オブジェクト / アーカイブ", "出力セクション"),
                                     (270, 130, 100, 420, 180))
        source_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(source_tab, text="ソース")
        source_toolbar = ttk.Frame(source_tab)
        source_toolbar.pack(fill="x", pady=(0, 6))
        self.source_file_var = tk.StringVar()
        self.source_combo = ttk.Combobox(source_toolbar, textvariable=self.source_file_var, state="readonly", width=42)
        self.source_combo.pack(side="left")
        self.source_combo.bind("<<ComboboxSelected>>", self.change_source_file)
        ttk.Button(source_toolbar, text="ソースの変更を反映", command=self.apply_source).pack(side="right")
        ttk.Label(source_toolbar, text="直接編集後は反映してください", foreground=MUTED).pack(side="right", padx=12)
        self.source_text = self._text(source_tab, height=11, undo=True)
        self.source_text.bind("<<Modified>>", self._source_modified)
        diagnostic_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(diagnostic_tab, text="診断 / チップ情報")
        self.diagnostic_text = self._text(diagnostic_tab, height=10)
        ttk.Label(self.root, textvariable=self.status, padding=(20, 9), foreground=MUTED).pack(fill="x")

    def _tree(self, parent, columns, headings, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=7)
        for key, title, width in zip(columns, headings, widths):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=70)
        y = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _text(self, parent, **kwargs):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(frame, wrap="none", bg="white", fg=INK, insertbackground=BLUE,
                       font=("Consolas", 10), relief="flat", padx=10, pady=8, **kwargs)
        y = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        x = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        text.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return text

    def capture(self):
        return {"chip": self.chip, "scripts": [{"name": s.name, "source": s.source} for s in self.scripts],
                "map_text": self.map_text, "map_name": self.map_name, "map_stale": self.map_stale,
                "paths": {name: str(path) for name, path in self.paths.items()}}

    def checkpoint(self):
        self.history.append(self.capture())
        self.history = self.history[-40:]
        self.future.clear()

    def restore(self, data):
        self.chip = data["chip"]
        self.scripts = [parse_script(s["source"], s["name"]) for s in data["scripts"]]
        self.map_text = data.get("map_text", "")
        self.map_name = data.get("map_name", "")
        self.map_stale = data.get("map_stale", False)
        self.paths = {name: Path(path) for name, path in data.get("paths", {}).items()}
        self.map_data = parse_map(self.map_text)
        self.selected = None
        self.refresh()

    def refresh(self):
        resolve_regions(self.scripts)
        self.region_refs = [(di, ri, r) for di, d in enumerate(self.scripts) for ri, r in enumerate(d.regions)]
        self.section_refs = [(di, si, s) for di, d in enumerate(self.scripts) for si, s in enumerate(d.sections)]
        self.aliases = {k: v for d in self.scripts for k, v in d.aliases.items()}
        self.map_by_name = {s.name: s for s in self.map_data.sections}
        dirty = self.capture() != self.saved_state
        self.root.title(f"{'* ' if dirty else ''}ESP32 Linker Script Editor — {self.chip}")
        self.chip_label.config(text=self.chip)
        self.summary_label.config(text=f"{len(self.scripts)} ファイル   /   {len(self.region_refs)} 領域   /   "
                                  f"{len(self.section_refs)} 出力セクション   •   CPU の仮想アドレスで表示")
        if not self.map_text:
            msg = "配置ルールを表示中  ·  実アドレス・使用量・関数は、リンク後の .map を読み込むと表示されます。"
        elif self.map_stale:
            msg = f"MAP は編集前の参考値です  ·  {self.map_name}  ·  再ビルド後の .map を読み込んで更新してください。"
        else:
            msg = f"MAP スナップショット  ·  {self.map_name}  ·  バーは各領域の実アドレス比率を表示します。"
        self.map_label.config(text=msg, bg="#fff0ce" if self.map_stale else "#dfeafa")
        self.active_file = min(self.active_file, len(self.scripts) - 1)
        self.source_combo.config(values=[s.name for s in self.scripts])
        self.set_source()
        self.fill_sections()
        self.draw_map()
        self.show_inspector()
        diagnostics = validate_scripts(self.scripts)
        for script in self.scripts:
            for inc in script.includes:
                diagnostics.append(f"INCLUDE 参照: {script.name} → {inc}。必要な参照先を追加して解析してください。")
        diagnostics += list(self.map_data.diagnostics) if self.map_text else []
        diagnostics += self.map_diagnostics()
        self.diagnostic_text.config(state="normal")
        self.diagnostic_text.delete("1.0", "end")
        self.diagnostic_text.insert("1.0", profile_notes(self.chip) + "\n\n" +
                                    ("\n\n".join(diagnostics) if diagnostics else "解析できた範囲では問題は検出されませんでした。") +
                                    "\n\n本ツールは GNU ld を実行しません。最終的な配置・ASSERT・容量は再リンクして確認してください。")
        self.diagnostic_text.config(state="disabled")
        self.notebook.tab(3, text=f"診断 / チップ情報 ({len(diagnostics)})")

    def canonical_region(self, name):
        seen = set()
        while name in self.aliases and name not in seen:
            seen.add(name)
            name = self.aliases[name]
        return name

    def map_diagnostics(self):
        if not self.map_text or self.map_stale:
            return []
        regions = {r.name: r for _, _, r in self.region_refs}
        messages = []
        for _, _, section in self.section_refs:
            linked = self.map_by_name.get(section.name)
            region = regions.get(self.canonical_region(section.region))
            if linked and linked.size and region and region.origin is not None and region.length is not None:
                if linked.address < region.origin or linked.address + linked.size > region.origin + region.length:
                    messages.append(f"MAP と領域の不一致: {section.name} が {region.name} の範囲外です。同じビルドのファイルか確認してください。")
        return messages

    def fill_sections(self):
        if not hasattr(self, "section_tree"):
            return
        self.section_tree.delete(*self.section_tree.get_children())
        query = self.filter_var.get().lower()
        for i, (di, _, section) in enumerate(self.section_refs):
            if query not in f"{section.name} {section.region} {section.body}".lower():
                continue
            linked = self.map_by_name.get(section.name)
            self.section_tree.insert("", "end", iid=str(i), values=(section.name, section.region or "自動 / 明示なし",
                section.load_region or "—", hex_address(linked.address) if linked else "—",
                byte_size(linked.size) if linked else "—", self.scripts[di].name))

    def draw_map(self):
        if not hasattr(self, "canvas"):
            return
        c = self.canvas
        c.delete("all")
        width = max(c.winfo_width(), 400)
        columns = 2 if width >= 650 else 1
        gap, height = 12, 228
        card_width = (width - gap * (columns + 1)) / columns
        self.region_boxes = []
        if not self.region_refs:
            c.create_text(28, 35, anchor="nw", text="MEMORY 領域がありません。memory.ld を追加してください。", fill=MUTED)
        for i, (_, _, region) in enumerate(self.region_refs):
            x = gap + (i % columns) * (card_width + gap)
            y = 8 + (i // columns) * (height + gap)
            right = x + card_width
            color = COLORS[i % len(COLORS)]
            tags = (f"r:{i}",)
            selected = self.selected == ("region", i)
            c.create_rectangle(x, y, right, y + height, fill="white", outline=color if selected else "#d4dce6", width=2 if selected else 1, tags=tags)
            c.create_rectangle(x, y, x + 4, y + height, fill=color, outline="", tags=tags)
            c.create_text(x + 15, y + 18, anchor="w", text=region.name, fill=INK, font=("Consolas", 11, "bold"), tags=tags)
            c.create_text(right - 12, y + 42, anchor="e", text=f"{region.attributes}   {byte_size(region.length)}", fill=color, font=("Consolas", 10), tags=tags)
            c.create_text(x + 15, y + 64, anchor="w", text=f"{hex_address(region.origin)} → " +
                          (hex_address(region.origin + region.length) if region.origin is not None and region.length is not None else "未解決") + " (終端除外)", fill=MUTED, font=("Consolas", 9), tags=tags)
            bx, by, bw = x + 15, y + 85, card_width - 30
            c.create_rectangle(bx, by, bx + bw, by + 17, fill="#eaf0f6", outline="", tags=tags)
            assigned = [(si, s) for si, (_, _, s) in enumerate(self.section_refs) if self.canonical_region(s.region) == region.name]
            intervals = []
            if self.map_text and not self.map_stale and region.origin is not None and region.length and region.length > 0:
                for si, section in assigned:
                    linked = self.map_by_name.get(section.name)
                    if linked and linked.size:
                        lo = max(0, linked.address - region.origin)
                        hi = min(region.length, linked.address + linked.size - region.origin)
                        if hi > lo:
                            intervals.append((lo, hi))
                            c.create_rectangle(bx + bw * lo / region.length, by, bx + bw * hi / region.length, by + 17,
                                               fill=color, outline="white", tags=(f"s:{si}",))
            covered, end = 0, 0
            for lo, hi in sorted(intervals):
                covered += max(0, hi - max(lo, end))
                end = max(end, hi)
            bar_note = (f"MAP 配置済み {byte_size(covered)} / {byte_size(region.length)}" if self.map_text and not self.map_stale
                        else "サイズ未確定  ·  以下は配置ルール（幅はサイズを表しません）")
            c.create_text(bx, by + 28, anchor="w", text=bar_note, fill=MUTED, font=("Yu Gothic UI", 8), tags=tags)
            for n, (si, section) in enumerate(assigned[:3]):
                sy = y + 130 + n * 23
                fill = "#dbe9ff" if self.selected == ("section", si) else "#f0f4f9"
                c.create_rectangle(bx, sy, bx + bw, sy + 20, fill=fill, outline="", tags=(f"s:{si}",))
                c.create_text(bx + 8, sy + 10, text="≡  " + section.name, anchor="w", fill=INK,
                              font=("Consolas", 9), tags=(f"s:{si}",))
            if len(assigned) > 3:
                c.create_text(bx, y + 211, anchor="w", text=f"＋ {len(assigned) - 3} 件  ·  下の配置ルール一覧で選択", fill=MUTED,
                              font=("Yu Gothic UI", 8), tags=tags)
            if region.length is not None and region.length > 0:
                c.create_text(right - 13, y + 211, anchor="e", text="↔ 容量", fill=color,
                              font=("Yu Gothic UI", 9, "bold"), tags=(f"resize:{i}",))
            self.region_boxes.append((x, y, right, y + height))
        c.config(scrollregion=(0, 0, width, 20 + ((len(self.region_refs) + columns - 1) // columns) * (height + gap)))

    def canvas_press(self, event):
        tags = self.canvas.gettags("current")
        if not tags:
            return
        tag = next((t for t in tags if t.startswith(("s:", "r:", "resize:"))), "")
        if not tag:
            return
        kind, index = tag.split(":")
        index = int(index)
        self.selected = ("section" if kind == "s" else "region", index)
        self.drag = (kind, index, event.x, event.y)
        self.draw_map()
        self.show_inspector()

    def canvas_motion(self, event):
        if not self.drag:
            return
        kind, index, start_x, _ = self.drag
        self.canvas.delete("ghost")
        if kind == "resize":
            length = self.region_refs[index][2].length
            new_length = max(16, round(length * max(.05, 1 + (event.x - start_x) / 240) / 16) * 16)
            label = f"容量: {byte_size(new_length)} ({hex(new_length)})"
        elif kind == "s":
            label = self.section_refs[index][2].name + " → 配置先領域へ"
        else:
            return
        self.canvas.create_text(event.x + 10, self.canvas.canvasy(event.y) - 12, text=label,
                                fill=BLUE, anchor="w", font=("Yu Gothic UI", 10, "bold"), tags="ghost")

    def canvas_release(self, event):
        drag, self.drag = self.drag, None
        self.canvas.delete("ghost")
        if not drag:
            return
        kind, index, start_x, start_y = drag
        if abs(event.x - start_x) + abs(event.y - start_y) < 5:
            return
        if kind == "resize":
            di, ri, region = self.region_refs[index]
            length = max(16, round(region.length * max(.05, 1 + (event.x - start_x) / 240) / 16) * 16)
            self.perform_edit(di, lambda: update_region(self.scripts[di], ri, region.origin_expr, hex(length)), "領域の容量を変更しました。")
        elif kind == "s":
            y = self.canvas.canvasy(event.y)
            target = next((i for i, (x0, y0, x1, y1) in enumerate(self.region_boxes) if x0 <= event.x <= x1 and y0 <= y <= y1), None)
            if target is not None:
                self.move_to_region(index, self.region_refs[target][2].name)

    def select_tree_section(self, event=None):
        values = self.section_tree.selection()
        if values:
            self.selected = ("section", int(values[0]))
            self.draw_map()
            self.show_inspector()

    def _field(self, title, value):
        ttk.Label(self.inspector, text=title, foreground=MUTED).pack(anchor="w", pady=(7, 2))
        entry = ttk.Entry(self.inspector)
        entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    def show_inspector(self):
        for child in self.inspector.winfo_children():
            child.destroy()
        self.fill_entries()
        if not self.selected:
            ttk.Label(self.inspector, text="配置を編集", font=("Yu Gothic UI", 14, "bold")).pack(anchor="w")
            ttk.Label(self.inspector, text="領域やセクションを選択してください。\n\n・カード間のドラッグで配置先を変更\n・「↔ 容量」を左右にドラッグして調整\n・開始アドレスとサイズは式で指定可能\n・入力セクションのパターンも編集可能\n\n新しい領域・セクションは下の一覧から追加できます。",
                      wraplength=330, foreground=MUTED).pack(anchor="w", pady=16)
            return
        kind, index = self.selected
        refs = self.region_refs if kind == "region" else self.section_refs
        if index >= len(refs):
            self.selected = None
            return self.show_inspector()
        di, item_index, item = refs[index]
        ttk.Label(self.inspector, text=item.name, font=("Consolas", 13, "bold"), wraplength=350).pack(anchor="w")
        ttk.Label(self.inspector, text=self.scripts[di].name, foreground=MUTED).pack(anchor="w", pady=(3, 6))
        if kind == "region":
            origin = self._field("開始アドレス / ORIGIN", item.origin_expr)
            length = self._field("容量 / LENGTH （16進・K・M・算術式）", item.length_expr)
            ttk.Label(self.inspector, text=f"属性: {item.attributes}  /  {byte_size(item.length)}", foreground=MUTED).pack(anchor="w", pady=8)
            ttk.Button(self.inspector, text="領域の変更を反映", style="Accent.TButton", command=lambda:
                       self.perform_edit(di, lambda: update_region(self.scripts[di], item_index, origin.get(), length.get()),
                                         "領域を更新しました。")).pack(fill="x", pady=6)
            ttk.Label(self.inspector, text="ドラッグでの容量変更は 16 B 単位です。共有 SRAM・予約領域はチップ情報と実際の構成を確認してください。",
                      foreground=MUTED, wraplength=330).pack(anchor="w", pady=8)
        else:
            ttk.Label(self.inspector, text="配置先領域 / VMA", foreground=MUTED).pack(anchor="w")
            destination = ttk.Combobox(self.inspector, state="readonly", values=[r.name for _, _, r in self.region_refs] + list(self.aliases))
            destination.set(item.region or "")
            destination.pack(fill="x", pady=5)
            ttk.Button(self.inspector, text="選択領域へ移動", command=lambda: self.move_to_region(index, destination.get())).pack(fill="x")
            ttk.Label(self.inspector, text=f"アドレス式: {item.address_expr or '自動配置'}\nロード先: {item.load_region or '明示なし'}",
                      foreground=MUTED, wraplength=330).pack(anchor="w", pady=7)
            ttk.Label(self.inspector, text="入力セクション / シンボル定義", foreground=MUTED).pack(anchor="w")
            body = self._text(self.inspector, height=6, width=30, undo=True)
            body.insert("1.0", item.body)
            ttk.Button(self.inspector, text="本文の変更を反映", command=lambda:
                       self.perform_edit(di, lambda: update_section_body(self.scripts[di], item_index, body.get("1.0", "end-1c")),
                                         "セクション本文を更新しました。")).pack(fill="x")

    def fill_entries(self):
        if not hasattr(self, "entry_tree"):
            return
        self.entry_tree.delete(*self.entry_tree.get_children())
        sections = self.map_data.sections if self.map_all.get() else []
        label = "全セクション" if self.map_all.get() else "セクションを選択してください"
        if not self.map_all.get() and self.selected and self.selected[0] == "section":
            index = self.selected[1]
            if index < len(self.section_refs):
                label = self.section_refs[index][2].name
                linked = self.map_by_name.get(label)
                sections = [linked] if linked else []
        query = self.map_filter.get().casefold()
        count = 0
        for section in sections:
            for entry in section.entries:
                if query not in f"{entry.name} {entry.object_file} {section.name}".casefold():
                    continue
                count += 1
                self.entry_tree.insert("", "end", values=(entry.name, hex_address(entry.address),
                                        "—" if entry.kind == "symbol" else byte_size(entry.size), entry.object_file, section.name))
        self.entry_title.config(text=f"{label}  /  {self.map_name or 'MAP 未読込'}  /  {count:,} 件" +
                                ("  [編集前の参考値]" if self.map_stale else ""))

    def perform_edit(self, document_index, operation, message):
        if not self.guard_source():
            return
        try:
            updated = operation()
            if updated == self.scripts[document_index].source:
                return
            parsed = parse_script(updated, self.scripts[document_index].name)
            if not parsed.editable:
                raise ValueError("変更により構文を解析できなくなりました。ソースタブで確認してください。")
            self.checkpoint()
            self.scripts[document_index] = parsed
            self.map_stale = bool(self.map_text)
            self.refresh()
            self.status.set(message)
        except (ValueError, IndexError) as exc:
            messagebox.showerror("変更できません", str(exc), parent=self.root)

    def move_to_region(self, index, destination):
        if not destination:
            return
        di, si, section = self.section_refs[index]
        if section.region == destination:
            return
        self.perform_edit(di, lambda: move_section(self.scripts[di], si, destination),
                          f"{section.name} → {destination} に変更しました。再リンクして配置を確認してください。")

    def set_source(self):
        self._source_setting = True
        self.source_file_var.set(self.scripts[self.active_file].name)
        self.source_text.delete("1.0", "end")
        self.source_text.insert("1.0", self.scripts[self.active_file].source)
        self.source_text.edit_reset()
        self.source_text.edit_modified(False)
        self.source_dirty = False
        self._source_setting = False

    def _source_modified(self, event=None):
        if self.source_text.edit_modified():
            if not self._source_setting:
                self.source_dirty = True
                self.status.set("ソースに未反映の変更があります。「ソースの変更を反映」で適用してください。")
            self.source_text.edit_modified(False)

    def guard_source(self):
        if not self.source_dirty:
            return True
        choice = messagebox.askyesnocancel("ソースの変更", "未反映のソース編集を反映しますか？\n「いいえ」で入力を破棄します。", parent=self.root)
        if choice is None:
            return False
        if choice:
            # A source edit can change the indices held by another GUI action.
            if self.apply_source():
                self.status.set("ソースを反映しました。続ける操作をもう一度選択してください。")
            else:
                self.status.set("ソースを反映できませんでした。入力内容を修正してください。")
            return False
        self.set_source()
        return True

    def change_source_file(self, event=None):
        new = self.source_combo.current()
        if not self.guard_source():
            self.source_combo.current(self.active_file)
            return
        self.active_file = new
        self.set_source()

    def apply_source(self):
        source = self.source_text.get("1.0", "end-1c")
        original = self.scripts[self.active_file]
        # Tk normalizes line endings while editing. Restore the source's convention.
        if "\r\n" in original.source:
            source = source.replace("\r\n", "\n").replace("\n", "\r\n")
        if source == original.source:
            self.source_dirty = False
            return True
        parsed = parse_script(source, original.name)
        if not parsed.editable:
            messagebox.showerror("解析できません", "\n".join(parsed.diagnostics) or "構文を確認してください。", parent=self.root)
            return False
        self.checkpoint()
        self.scripts[self.active_file] = parsed
        self.map_stale = bool(self.map_text)
        self.selected = None
        self.refresh()
        self.status.set("ソースを反映しました。")
        return True

    def _undo_key(self, event):
        if isinstance(event.widget, (tk.Text, ttk.Entry)):
            return
        self.undo()
        return "break"

    def _redo_key(self, event):
        if isinstance(event.widget, (tk.Text, ttk.Entry)):
            return
        self.redo()
        return "break"

    def undo(self):
        if self.guard_source() and self.history:
            self.future.append(self.capture())
            self.restore(self.history.pop())
            self.status.set("直前の操作を元に戻しました。")

    def redo(self):
        if self.guard_source() and self.future:
            self.history.append(self.capture())
            self.restore(self.future.pop())
            self.status.set("操作をやり直しました。")

    def confirm_replace(self):
        """新規作成・読込・終了の前に、保存するかを共通の意味で確認する。"""
        if not self.source_dirty and self.capture() == self.saved_state:
            return True
        choice = messagebox.askyesnocancel(
            "未保存の変更",
            "未保存の変更があります。変更を保存しますか？\n\n"
            "はい：保存して続行\nいいえ：保存せずに続行\nキャンセル：操作を中止",
            parent=self.root,
        )
        if choice is None:
            return False
        if not choice:
            # 次のファイル選択などがキャンセルされた場合は編集内容を残す。
            # 実際の置換・終了が行われるまで、未反映の入力も破棄しない。
            return True
        if self.source_dirty and not self.apply_source():
            return False
        return self.save_project_dialog()

    def new_dialog(self):
        if not self.confirm_replace():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("新規リンカスクリプト")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text="対象チップ", padding=14).pack(anchor="w")
        chip = ttk.Combobox(dialog, values=CHIP_NAMES, state="readonly", width=32)
        chip.set(self.chip)
        chip.pack(padx=14)
        ttk.Label(dialog, text="編集用テンプレートを作成します。\nESP-IDF の起動に必要な完全なスクリプトではありません。", padding=14).pack()
        def create():
            self.checkpoint()
            self.chip = chip.get()
            self.scripts = [parse_script(create_template(self.chip), f"{self.chip.lower()}_custom.ld")]
            self.paths = {}
            self.map_text = self.map_name = ""
            self.map_stale = False
            self.map_data = parse_map("")
            self.active_file = 0
            self.selected = None
            dialog.destroy()
            self.refresh()
        ttk.Button(dialog, text="作成", command=create, style="Accent.TButton").pack(pady=(0, 14))

    def load_paths(self, paths, replace=False):
        parsed = []
        path_lookup = {}
        for path in paths:
            path = Path(path)
            if path.suffix.lower() in (".in", ".lf"):
                raise ValueError(".ld.in / .lf は直接編集の対象外です。ビルド後の memory.ld / sections.ld を指定してください。")
            safe_filename(path.name)
            parsed.append(parse_script(read_text(path), path.name))
            path_lookup[path.name] = path.resolve()
        combined = parsed if replace else self.scripts + parsed
        if len({s.name.casefold() for s in combined}) != len(combined):
            raise ValueError("同名ファイルが既にあります。先に編集メニューで該当ファイルを閉じてください。")
        self.checkpoint()
        self.scripts = combined
        self.paths = path_lookup if replace else {**self.paths, **path_lookup}
        detected = detect_chip("\n".join(s.source for s in self.scripts))
        if detected:
            self.chip = detected
        if replace:
            self.map_text = self.map_name = ""
            self.map_data = parse_map("")
            self.map_stale = False
        else:
            self.map_stale = bool(self.map_text)
        self.selected = None
        self.active_file = 0
        self.refresh()
        self.status.set(f"{len(parsed)} ファイルを読み込みました。")

    def open_scripts(self):
        if not self.guard_source():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="memory.ld と sections.ld を同時に選択できます",
                                            filetypes=[("Linker scripts", "*.ld *.lds"), ("すべて", "*.*")])
        if not paths:
            return
        # Replace the untouched starter template, otherwise add documents.
        replace = len(self.scripts) == 1 and not self.paths and self.capture() == self.saved_state
        self._catch(lambda: self.load_paths(paths, replace=replace))

    def open_build(self):
        if not self.confirm_replace():
            return
        folder = filedialog.askdirectory(parent=self.root, title="ESP-IDF の build フォルダー")
        if not folder:
            return
        def load():
            base = Path(folder)
            files = [base / "esp-idf" / "esp_system" / "ld" / n for n in ("memory.ld", "sections.ld")]
            if not all(p.is_file() for p in files):
                raise ValueError("build/esp-idf/esp_system/ld に memory.ld と sections.ld が必要です。先にプロジェクトをビルドしてください。")
            self.load_paths(files, replace=True)
            maps = list(base.glob("*.map"))
            description = base / "project_description.json"
            if description.is_file():
                data = json.loads(read_text(description))
                wanted = base / (str(data.get("project_name", "")) + ".map")
                if wanted.is_file():
                    maps = [wanted]
            if len(maps) == 1:
                self.load_map(maps[0])
        self._catch(load)

    def load_map(self, path):
        text = read_text(Path(path))
        parsed = parse_map(text)
        if not parsed.sections:
            raise ValueError("リンク結果の出力セクションを検出できません。GNU ld の .map を指定してください。")
        self.checkpoint()
        self.map_text, self.map_name, self.map_data = text, Path(path).name, parsed
        self.map_stale = False
        self.refresh()
        self.status.set("MAP を読み込みました。スクリプトと同じビルドの出力を使用してください。")

    def open_map(self):
        if not self.guard_source():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("GNU link map", "*.map"), ("すべて", "*.*")])
        if path:
            self._catch(lambda: self.load_map(path))

    def open_includes(self):
        if not self.guard_source():
            return
        candidates = []
        missing = []
        for script in self.scripts:
            for include in script.includes:
                if Path(include).name in [s.name for s in self.scripts]:
                    continue
                origin = self.paths.get(script.name)
                path = origin.parent / include if origin else Path(include)
                if origin and path.is_file():
                    candidates.append(path.resolve())
                else:
                    missing.append(include)
        if candidates:
            self._catch(lambda: self.load_paths(list(dict.fromkeys(candidates))))
        if missing or not candidates:
            messagebox.showinfo("INCLUDE 参照", "見つからない参照先は「.ld を開く」で指定してください。\n" + "\n".join(missing), parent=self.root)

    def remove_script(self):
        if not self.guard_source():
            return
        if len(self.scripts) <= 1:
            messagebox.showinfo("ファイルを閉じる", "最後のファイルは閉じられません。新規作成を使用してください。", parent=self.root)
            return
        self.checkpoint()
        self.scripts.pop(self.active_file)
        self.map_stale = bool(self.map_text)
        self.selected = None
        self.refresh()

    def _add_dialog(self, section=False):
        if not self.guard_source():
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("セクションを追加" if section else "領域を追加")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        fields = {}
        def field(key, label, default, values=None):
            ttk.Label(frame, text=label).pack(anchor="w", pady=(6, 2))
            widget = ttk.Combobox(frame, values=values, state="readonly", width=47) if values is not None else ttk.Entry(frame, width=50)
            widget.set(default) if values is not None else widget.insert(0, default)
            widget.pack(fill="x")
            fields[key] = widget
        field("file", "追加先ファイル", self.scripts[self.active_file].name, [s.name for s in self.scripts])
        field("name", "名前", ".custom" if section else "custom_ram")
        if section:
            names = [r.name for _, _, r in self.region_refs] + list(self.aliases)
            field("region", "配置先", names[0] if names else "", names)
            ttk.Label(frame, text="本文（入力セクションのパターン）").pack(anchor="w", pady=(6, 2))
            body = tk.Text(frame, height=6, width=50, font=("Consolas", 10))
            body.insert("1.0", "    . = ALIGN(4);\n    *(.custom .custom.*)\n")
            body.pack(fill="both", expand=True)
            noload = tk.BooleanVar()
            ttk.Checkbutton(frame, text="NOLOAD（初期化データをロードしない）", variable=noload).pack(anchor="w", pady=8)
        else:
            field("attrs", "属性", "RW")
            field("origin", "ORIGIN", "0x3FFB0000" if self.chip == "ESP32" else "0x3FC88000")
            field("length", "LENGTH", "4K")
        def add():
            di = [s.name for s in self.scripts].index(fields["file"].get())
            try:
                if section:
                    source = add_section(self.scripts[di], fields["name"].get(), fields["region"].get(), body.get("1.0", "end-1c"), noload.get())
                else:
                    source = add_region(self.scripts[di], fields["name"].get(), fields["attrs"].get(), fields["origin"].get(), fields["length"].get())
                parsed = parse_script(source, self.scripts[di].name)
                if not parsed.editable:
                    raise ValueError("追加内容の構文を確認してください。")
                self.checkpoint()
                self.scripts[di] = parsed
                self.map_stale = bool(self.map_text)
                dialog.destroy()
                self.refresh()
            except ValueError as exc:
                messagebox.showerror("追加できません", str(exc), parent=dialog)
        ttk.Button(frame, text="追加", style="Accent.TButton", command=add).pack(fill="x", pady=(12, 0))

    def add_region_dialog(self):
        self._add_dialog()

    def add_section_dialog(self):
        self._add_dialog(section=True)

    def save_project_dialog(self):
        if not self.guard_source():
            return False
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json", initialfile="linker-project.json", filetypes=[("編集プロジェクト", "*.json")])
        if path:
            def save():
                save_project(Path(path), self.capture())
                self.saved_state = self.capture()
                self.refresh()
                self.status.set(f"編集プロジェクトを保存しました: {path}")
                return True
            return bool(self._catch(save))
        return False

    def open_project_dialog(self):
        if not self.confirm_replace():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("編集プロジェクト", "*.json")])
        if path:
            def load():
                data = read_project(Path(path))
                self.checkpoint()
                self.restore(data)
                self.saved_state = self.capture()
                self.refresh()
                self.status.set(f"編集プロジェクトを開きました: {path}")
            self._catch(load)

    def export_dialog(self):
        if not self.guard_source():
            return
        folder = filedialog.askdirectory(parent=self.root, title="編集した .ld の出力先フォルダー")
        if not folder:
            return
        existing = [s.name for s in self.scripts if (Path(folder) / s.name).exists()]
        if existing and not messagebox.askyesno("既存ファイルの置換", "次のファイルを置き換えますか？\n" + "\n".join(existing), parent=self.root):
            return
        def save():
            paths = export_scripts(Path(folder), self.scripts)
            self.saved_state = self.capture()
            self.refresh()
            self.status.set(f"{len(paths)} 件の .ld を出力しました: {folder}")
        self._catch(save)

    def _catch(self, operation):
        try:
            return operation()
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("処理できません", str(exc), parent=self.root)

    def show_help(self):
        messagebox.showinfo("使い方", "1. build フォルダー、または memory.ld と sections.ld を開きます。\n"
                            "2. .map を読み込むと実配置と入力コードを確認できます。\n"
                            "3. カードや一覧で選択し、配置先・容量・本文を変更します。\n"
                            "4. .ld を出力し、実際のリンカで再リンクして確認します。\n\n"
                            "新規作成は ESP32 / ESP32-S3 の編集用テンプレートです。\n"
                            "完全な ESP-IDF 起動スクリプトではありません。\n"
                            "生成済み .ld は ESP-IDF の再ビルドで上書きされます。\n"
                            "恒久的な配置変更には .lf / ビルド設定への反映が必要です。\n\n"
                            "仕様・対応構文・制限は同梱 README.md を参照してください。", parent=self.root)

    def close(self):
        """破棄は解析や保存を経由せず終了し、保存時は成功を確認する。"""
        if self.confirm_replace():
            self.root.destroy()
