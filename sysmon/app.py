"""Keyboard-first terminal dashboard for local system resources."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import math
from pathlib import Path
import shlex
import threading
import time

from rich.table import Table
from rich.text import Text
from rich.console import Group
from textual.renderables.digits import Digits
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Footer, Input, Static, Tab, Tabs
from textual.worker import Worker, WorkerState

from .collector import Collector
from .settings import SettingsError, save_language, translate_status


CYAN = "#52ded6"
MINT = "#aff5cf"
AMBER = "#ffcf70"
MUTED = "#a3b5c5"
RED = "#ff9393"


def number(value, suffix="", digits=1):
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


def size(value):
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024 or unit == "PiB":
            return f"{value:.1f} {unit}"
        value /= 1024


def clean(value):
    """Keep terminal controls and multiline process arguments out of cell content."""
    return " ".join("".join(c for c in str(value) if c.isprintable() or c.isspace()).split())


def meter(percent, width=12, color=CYAN):
    if percent is None:
        return Text("—", style=MUTED)
    filled = round(max(0, min(100, percent)) * width / 100)
    return Text.assemble(("▮" * filled, color), ("▮" * (width - filled), "#344657"))


def spark(values, width=24, color=CYAN):
    chars = "▁▂▃▄▅▆▇█"
    return Text("".join(chars[min(7, int(max(0, v) * 7 / 100))] for v in list(values)[-width:]), style=color)


def panel_table(*headers):
    table = Table(expand=True, box=None, padding=(0, 1), header_style=MUTED)
    for header in headers:
        table.add_column(header)
    return table


def metric(value, values, width, color):
    """A three-row value and history, using only terminal cells."""
    value_text = number(value, "%")
    table = Table.grid(expand=True, padding=0)
    value_width = Digits.get_width(value_text) + 2
    table.add_column(width=value_width)
    table.add_column(ratio=1, vertical="bottom")
    table.add_row(Digits(value_text, style=f"bold {color}"), spark(values, max(1, width - value_width), color))
    return table


def short_command(command, name):
    try:
        parts = shlex.split(command)
    except ValueError:
        return clean(command or name)
    if not parts:
        return clean(name)
    parts[0] = Path(parts[0]).name
    return clean(" ".join(parts))


class Overview(VerticalScroll):
    def on_resize(self):
        # The summary changes height at breakpoints; wait for this container's
        # actual resize, including when sampling is paused.
        self.call_after_refresh(self.app.draw)


class GPUProcessTable(DataTable):
    def on_resize(self):
        # A newly revealed tab receives its table width after TabActivated.
        self.call_after_refresh(self.app.draw)


class SystemMonitorApp(App):
    TITLE = "vital"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: #10151c; color: #e8eff6; }
    #top-line { height: 1; padding: 0 1; background: #171f29; }
    Tabs { height: 2; background: #10151c; }
    Tab { color: #a3b5c5; padding: 0 2; }
    Tab.-active { color: #10151c; background: #52ded6; text-style: bold; }
    Underline > .underline--bar { color: #52ded6; }
    #summary { grid-size: 4; grid-gutter: 0; height: 7; }
    .summary { border: solid #455766; padding: 0 1; height: 7; border-title-color: #a3b5c5; }
    #content { height: 1fr; }
    #overview { overflow: hidden hidden; }
    #overview-top { height: 21; }
    #overview-bottom { height: 1fr; min-height: 7; }
    .panel { border: solid #455766; padding: 0 1; border-title-color: #52ded6; border-title-style: bold; }
    #cpu-panel, #process-panel { width: 2fr; }
    #gpu-panel, #filesystem-panel { width: 1fr; }
    #cores, #gpu-overview, #filesystems, #cpu-detail-summary, #gpu-detail, #hardware-detail { height: auto; }
    #core-scroll { height: 1fr; overflow-x: hidden; }
    #core-footer { height: 2; border-top: solid #344657; color: #a3b5c5; }
    #core-controls { height: 1; align-horizontal: right; margin-bottom: 1; }
    #core-controls Button, #core-controls Checkbox { height: 1; min-width: 14; padding: 0 1; }
    #cpu-controls, #process-controls { height: 3; }
    Button { min-width: 14; height: 3; margin: 0 1 0 0; background: #20303e; color: #e8eff6; border: none; }
    Button:focus { background: #335564; text-style: bold; }
    Checkbox { background: #10151c; border: none; height: 3; padding: 0; }
    Input { height: 3; border: solid #344657; background: #171f29; }
    Input:focus { border: solid #52ded6; }
    #search { width: 1fr; }
    #overview-search { height: 1; border: none; padding: 0 1; margin-bottom: 1; width: 1fr; }
    #overview-process-controls { height: 2; }
    #overview-process-count { width: auto; color: #a3b5c5; padding: 0 1; }
    #process-count { width: auto; padding: 1; }
    #process-command-scroll, #gpu-command-scroll { height: 4; border-top: solid #344657; padding: 0 1; }
    #process-command, #gpu-command { height: auto; }
    #gpu-stats { height: auto; max-height: 50%; }
    #gpu-processes { min-height: 4; }
    DataTable { height: 1fr; background: #10151c; scrollbar-size: 1 1; }
    #overview-processes, #process-table { overflow-x: hidden; }
    DataTable > .datatable--header { background: #171f29; color: #a3b5c5; text-style: bold; }
    DataTable > .datatable--cursor { background: #24414d; color: #f1ffff; }
    DataTable > .datatable--odd-row { background: #141c25; }
    #cpu-detail-summary { padding: 1; }
    #cpu-detail-table { height: 1fr; }
    #storage { padding: 0 1; }
    #storage-filesystems { height: 12; }
    #storage-io { height: 12; }
    #storage-network { height: 14; }
    .section-label { height: 1; color: #52ded6; text-style: bold; margin-top: 1; }
    #hardware { padding: 1 2; }
    Footer { background: #171f29; }
    FooterKey > .footer-key--key { background: #20303e; color: #52ded6; }
    .narrow #summary { grid-size: 2; height: 14; }
    .narrow #overview { overflow-y: auto; }
    .narrow #overview-top, .narrow #overview-bottom { layout: vertical; height: auto; }
    .narrow #cpu-panel, .narrow #process-panel, .narrow #gpu-panel, .narrow #filesystem-panel { width: 1fr; }
    .narrow #cpu-panel { height: 22; }
    .narrow #gpu-panel { height: 21; }
    .narrow #process-panel { height: 16; }
    .narrow #filesystem-panel { height: 10; }
    .narrow #process-count { display: none; }
    .compact #summary { display: none; }
    """
    BINDINGS = [
        Binding("ctrl+c", "quit", "離開", show=False, priority=True),
        Binding("q", "quit", "離開"),
        Binding("space", "pause", "暫停/恢復"),
        Binding("/", "search", "搜尋"),
        Binding("c", "sort_cpu", "CPU 排序"),
        Binding("m", "sort_memory", "記憶體排序"),
        Binding("b", "core_order", "核心排序", show=False),
        Binding("l", "language", "English"),
        Binding("escape", "clear_search", "清除搜尋", show=False),
        *[Binding(str(i), f"page('{name}')", name, show=False) for i, name in enumerate(
            ("overview", "cpu", "processes", "gpu", "storage", "hardware"), 1)],
    ]

    def __init__(self, interval=1.0, language="zh-TW"):
        super().__init__()
        self.language = language
        self.interval = interval
        self.snapshot = None
        self.latest = None
        self.sampling_error = None
        self.paused = False
        self.hot_first = False
        self.busy_only = False
        self.process_sort = "cpu"
        self.stop_sampling = threading.Event()
        self.history = {key: deque(maxlen=90) for key in ("cpu", "memory", "gpu")}
        self.table_cache = {}
        self.table_signatures = {}
        self.table_selection = {}
        self.table_scroll = {}

    def t(self, zh, en):
        return en if self.language == "en" else zh

    def status(self, text):
        return translate_status(text or "", self.language)

    def pages(self):
        return (("overview", self.t("概覽", "Overview")), ("cpu", "CPU"),
                ("processes", self.t("程序", "Processes")), ("gpu", "GPU"),
                ("storage", self.t("磁碟 / 網路", "Disk / Network")), ("hardware", self.t("硬體", "Hardware")))

    def compose(self) -> ComposeResult:
        yield Static(self.t("讀取系統資料…", "Reading system data…"), id="top-line")
        yield Tabs(*[Tab(f"{i} {label}", id=f"tab-{name}") for i, (name, label) in enumerate(self.pages(), 1)], id="navigation")
        with Grid(id="summary"):
            for name in ("cpu", "memory", "gpu", "network"):
                yield Static(id=f"summary-{name}", classes="summary")
        with ContentSwitcher(initial="overview", id="content"):
            with Overview(id="overview"):
                with Horizontal(id="overview-top"):
                    with Vertical(id="cpu-panel", classes="panel"):
                        with Horizontal(id="core-controls"):
                            yield Button(id="core-sort")
                            yield Checkbox(id="core-busy")
                        with VerticalScroll(id="core-scroll"):
                            yield Static(id="cores")
                        yield Static(id="core-footer")
                    with VerticalScroll(id="gpu-panel", classes="panel"):
                        yield Static(id="gpu-overview")
                with Horizontal(id="overview-bottom"):
                    with Vertical(id="process-panel", classes="panel"):
                        with Horizontal(id="overview-process-controls"):
                            yield Input(id="overview-search")
                            yield Static(id="overview-process-count")
                        yield DataTable(id="overview-processes", zebra_stripes=True, cursor_type="row")
                    with VerticalScroll(id="filesystem-panel", classes="panel"):
                        yield Static(id="filesystems")
            with Vertical(id="cpu"):
                yield Static(id="cpu-detail-summary")
                with Horizontal(id="cpu-controls"):
                    yield Button(id="detail-core-sort")
                    yield Checkbox(id="detail-core-busy")
                yield DataTable(id="cpu-detail-table", zebra_stripes=True, cursor_type="row")
            with Vertical(id="processes"):
                with Horizontal(id="process-controls"):
                    yield Input(id="search")
                    yield Button("CPU% ↓", id="sort-cpu")
                    yield Button(id="sort-memory")
                    yield Static(id="process-count")
                yield DataTable(id="process-table", zebra_stripes=True, cursor_type="row")
                with VerticalScroll(id="process-command-scroll"):
                    yield Static(id="process-command")
            with Vertical(id="gpu", classes="panel"):
                with VerticalScroll(id="gpu-stats"):
                    yield Static(id="gpu-detail")
                yield GPUProcessTable(id="gpu-processes", zebra_stripes=True, cursor_type="row")
                with VerticalScroll(id="gpu-command-scroll"):
                    yield Static(id="gpu-command")
            with VerticalScroll(id="storage"):
                yield Static(id="storage-label", classes="section-label")
                yield DataTable(id="storage-filesystems", zebra_stripes=True, cursor_type="row")
                yield Static(self.t("磁碟 I/O", "Disk I/O"), classes="section-label", id="io-label")
                yield DataTable(id="storage-io", zebra_stripes=True, cursor_type="row")
                yield Static(id="network-label", classes="section-label")
                yield DataTable(id="storage-network", zebra_stripes=True, cursor_type="row")
            with VerticalScroll(id="hardware"):
                yield Static(id="hardware-detail")
        yield Footer()

    def configure_table(self, identifier, columns):
        table = self.query_one(f"#{identifier}", DataTable)
        signature = tuple(columns)
        if self.table_signatures.get(identifier) == signature:
            return
        self.table_scroll[identifier] = table.scroll_y
        if table.row_count and identifier != "overview-processes":
            self.table_selection[identifier] = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear(columns=True)
        self.table_cache.pop(identifier, None)
        for key, label, width in columns:
            table.add_column(label, key=key, width=width)
        self.table_signatures[identifier] = signature

    def apply_language(self):
        for i, (name, label) in enumerate(self.pages(), 1):
            self.query_one(f"#tab-{name}", Tab).label = f"{i} {label}"
        for identifier, title in (("cpu-panel", self.t("CPU 核心", "CPU cores")), ("gpu-panel", "GPU"),
                                  ("process-panel", self.t("程序", "Processes")), ("filesystem-panel", self.t("磁碟", "Disks")),
                                  ("summary-cpu", "CPU"), ("summary-memory", self.t("記憶體", "Memory")),
                                  ("summary-gpu", "GPU"), ("summary-network", self.t("網路", "Network"))):
            self.query_one(f"#{identifier}").border_title = title
        for name in ("core-sort", "detail-core-sort"):
            self.query_one(f"#{name}", Button).label = self.t("依使用率 ↓", "Usage ↓") if self.hot_first else self.t("依核心編號", "Core ID")
        for name in ("core-busy", "detail-core-busy"):
            self.query_one(f"#{name}", Checkbox).label = self.t("僅顯示 >1%", "Busy >1%")
        for name in ("search", "overview-search"):
            field = self.query_one(f"#{name}", Input)
            field.placeholder = self.t("搜尋程序 / PID / 使用者", "Search process / PID / user")
            field.tooltip = self.t("搜尋名稱、指令、PID 或使用者", "Search name, command, PID or user")
        self.query_one("#sort-memory", Button).label = self.t("記憶體 ↓", "Memory ↓")
        for identifier, label in (("storage-label", self.t("磁碟容量", "Disk capacity")),
                                  ("io-label", self.t("磁碟 I/O", "Disk I/O")),
                                  ("network-label", self.t("網路介面", "Network interfaces"))):
            self.query_one(f"#{identifier}", Static).update(label)
        headings = {
            "cpu-detail-table": ("CPU", "Core", "Socket", "NUMA", self.t("使用率%", "Busy%"), "user%", "sys%", "iowait%", "irq%", "soft%", "steal%", "idle%"),
            "storage-filesystems": self.t(("掛載點", "裝置", "格式", "已用", "可用", "總計", "使用率%"), ("Mount", "Device", "Type", "Used", "Free", "Total", "Used%")),
            "storage-io": self.t(("裝置", "讀取/s", "寫入/s", "讀 IOPS", "寫 IOPS", "忙碌%"), ("Device", "Read/s", "Write/s", "Read IOPS", "Write IOPS", "Busy%")),
            "storage-network": self.t(("介面", "狀態", "接收/s", "傳送/s", "累計接收", "累計傳送", "位址"), ("Interface", "State", "Receive/s", "Send/s", "Received", "Sent", "Address")),
        }
        for identifier, labels in headings.items():
            self.configure_table(identifier, [(str(i), label, None) for i, label in enumerate(labels)])
        for key, action, label in (("q", "quit", self.t("離開", "Quit")), ("space", "pause", self.t("暫停/恢復", "Pause/Resume")),
                                   ("/", "search", self.t("搜尋", "Search")), ("c", "sort_cpu", self.t("CPU 排序", "Sort CPU")),
                                   ("m", "sort_memory", self.t("記憶體排序", "Sort memory")), ("l", "language", self.t("English", "繁體中文"))):
            # bind() appends, leaving the original description first in the footer.
            binding = next(iter(Binding.make_bindings([Binding(key, action, label)])))
            self._bindings.key_to_bindings[binding.key] = [binding]
        self.refresh_bindings()
        self.draw()

    def on_mount(self):
        self.apply_language()
        self.query_one("#overview-processes", DataTable).focus(scroll_visible=False)
        self.screen.set_class(self.size.width < 110, "narrow")
        self.screen.set_class(self.size.height < 32, "compact")
        self.collect()

    @work(thread=True, exit_on_error=False)
    def collect(self):
        collector = Collector()
        try:
            while not self.stop_sampling.is_set():
                started = time.monotonic()
                data = collector.sample()
                if self.stop_sampling.is_set():
                    break
                self.call_from_thread(self.accept_snapshot, data)
                self.stop_sampling.wait(max(0.05, self.interval - (time.monotonic() - started)))
        finally:
            collector.close()

    def on_worker_state_changed(self, event: Worker.StateChanged):
        if event.state == WorkerState.ERROR:
            self.sampling_error = clean(event.worker.error)
            self.draw_error()

    def draw_error(self):
        self.query_one("#top-line", Static).update(Text(
            self.t("資料讀取失敗：", "Could not read system data: ") + self.status(self.sampling_error), style=RED))

    def on_unmount(self):
        self.stop_sampling.set()

    def action_quit(self):
        self.stop_sampling.set()
        self.exit()

    def accept_snapshot(self, data):
        self.sampling_error = None
        self.latest = data
        if self.paused:
            return
        self.snapshot = data
        for key, value in (("cpu", data["cpu"]["percent"]), ("memory", data["memory"]["percent"]),
                           ("gpu", data["gpus"][0]["utilization"] if data["gpus"] else None)):
            if value is not None:
                self.history[key].append(value)
        self.draw()

    def on_resize(self, event):
        self.screen.set_class(event.size.width < 110, "narrow")
        self.screen.set_class(event.size.height < 32, "compact")
        if self.snapshot:
            self.call_after_refresh(self.draw)

    def draw(self):
        if not self.snapshot:
            if self.sampling_error:
                self.draw_error()
            else:
                self.query_one("#top-line", Static).update(self.t("讀取系統資料…", "Reading system data…"))
            return
        data = self.snapshot
        cpu, memory = data["cpu"], data["memory"]
        days, remainder = divmod(int(data["uptime"]), 86400)
        hours, remainder = divmod(remainder, 3600)
        state = self.t("已暫停", "Paused") if self.paused else self.t("即時", "Live")
        header = Table.grid(expand=True)
        header.add_column(ratio=1)
        header.add_column(justify="right")
        header.add_row(Text.assemble((self.TITLE, f"bold {CYAN}"), f"  │  {clean(data['hostname'])}  │  Linux"),
                       Text.assemble((f"{state}  {self.interval:g}s", CYAN), "  │  ",
                                     (datetime.fromtimestamp(data["timestamp"]).strftime("%H:%M:%S"), MINT),
                                     (f"  │  {self.t('開機', 'Up')} {days}d {hours:02}:{remainder // 60:02}", MUTED)))
        self.query_one("#top-line", Static).update(header)
        if self.sampling_error:
            self.draw_error()
        width = max(20, self.query_one("#summary-cpu").content_size.width)
        self.query_one("#summary-cpu", Static).update(Group(
            metric(cpu["percent"], self.history["cpu"], width, CYAN),
            Text(""), Text.assemble((self.t("溫度 ", "Temp "), MUTED), number(cpu["temperature"], "°C"),
                                    (self.t("  功率 ", "  Power "), MUTED), number(cpu["power_watts"], " W"))))
        swap = memory["swap"]
        memory_width = max(8, self.query_one("#summary-memory").content_size.width - 8)
        self.query_one("#summary-memory", Static).update(Text.assemble(
            (f"{size(memory['used'])} / {size(memory['total'])}\n", f"bold {MINT}"),
            meter(memory["percent"], memory_width, MINT), f" {memory['percent']:.1f}%\n",
            (f"Cache {size(memory['cached'])}\n", MUTED),
            f"Swap  {size(swap['used'])} / {size(swap['total'])}\n",
            meter(swap["percent"], memory_width, MINT), f" {swap['percent']:.1f}%"))
        gpu = data["gpus"]
        self.query_one("#summary-gpu").border_title = (
            f"GPU {gpu[0]['index']}" + (f" · {len(gpu)} {self.t('張', 'GPUs')}" if len(gpu) > 1 else "")
            if gpu else "GPU")
        self.query_one("#summary-gpu", Static).update(Group(
            metric(gpu[0]["utilization"], self.history["gpu"], width, AMBER), Text(""),
            Text(f"VRAM {size(gpu[0]['memory_used'])} / {size(gpu[0]['memory_total'])}", style=MUTED))
            if gpu else Text(self.status(data["gpu_status"]) or self.t("未偵測到 NVIDIA GPU", "No NVIDIA GPU detected"), style=MUTED))
        interfaces = [n for n in data["network"] if n["name"] != "lo" and n["up"]]
        receive = sum(n["recv_rate"] or 0 for n in interfaces) if any(n["recv_rate"] is not None for n in interfaces) else None
        send = sum(n["send_rate"] or 0 for n in interfaces) if any(n["send_rate"] is not None for n in interfaces) else None
        network = Text.assemble((f"↓ {size(receive)}/s", f"bold {CYAN}"),
                                "  " if width >= 40 else "\n", (f"↑ {size(send)}/s", f"bold {MINT}"))
        self.query_one("#summary-network", Static).update(network)
        active = self.query_one("#content", ContentSwitcher).current
        if active == "overview":
            self.draw_cores()
            self.query_one("#gpu-overview", Static).update(self.gpu_view(compact=True))
            self.draw_processes("overview-processes", limit=100)
            disk_width = self.query_one("#filesystem-panel").content_size.width
            table = panel_table(self.t("掛載點", "Mount"), self.t("已用 / 總計", "Used / Total"), self.t("使用率", "Usage"))
            table.columns[0].no_wrap = True
            table.columns[0].overflow = "ellipsis"
            for disk in data["disks"]:
                capacity = self.status(disk.get("error")) or f"{size(disk['used'])} / {size(disk['total'])}"
                usage = Text.assemble(meter(disk["percent"], max(4, disk_width - 45), MINT), f" {number(disk['percent'], '%')}")
                table.add_row(Text(clean(disk["mountpoint"])), capacity, usage)
            self.query_one("#filesystems", Static).update(table)
        elif active == "cpu":
            self.draw_cpu_details()
        elif active == "processes":
            self.draw_processes("process-table")
        elif active == "gpu":
            self.query_one("#gpu-detail", Static).update(self.gpu_view())
            self.draw_gpu_processes()
        elif active == "storage":
            self.draw_storage()
        elif active == "hardware":
            self.draw_hardware()

    def core_indices(self):
        values = self.snapshot["cpu"]["per_core"]
        indices = [i for i, value in enumerate(values) if not self.busy_only or (value is not None and value > 1)]
        return sorted(indices, key=lambda i: (-(values[i] or 0), i)) if self.hot_first else indices

    def draw_cores(self):
        cpu = self.snapshot["cpu"]
        panel = self.query_one("#cpu-panel")
        panel.border_title = self.t("CPU 核心", "CPU cores") + f"  ·  {cpu['physical'] or '—'} {self.t('核心', 'cores')} / {cpu['logical']} {self.t('執行緒', 'threads')}"
        id_width = max(2, len(str(max(cpu["core_ids"], default=0))))
        width = max(16, panel.content_size.width)
        columns = max(1, min(6, width // (id_width + 14)))
        indices = self.core_indices()
        rows = max(1, math.ceil(len(indices) / columns))
        narrow = self.size.width < 110
        available = self.query_one("#content").size.height
        top_height = max(10, min(rows * 2 + 5, available - 8))
        if not narrow:
            self.query_one("#overview-top").styles.height = top_height
        else:
            self.query_one("#overview-top").styles.height = "auto"
            panel.styles.height = rows * 2 + 5
        if not narrow:
            panel.styles.height = "1fr"
        spacious = narrow or top_height >= rows * 2 + 4
        grid = Table.grid(expand=True, padding=0)
        for _ in range(columns):
            grid.add_column(ratio=1)
        bar_width = max(3, width // columns - id_width - 11)
        for row in range(rows):
            cells = []
            for col in range(columns):
                position = row + col * rows
                if position >= len(indices):
                    cells.append("")
                    continue
                index = indices[position]
                value = cpu["per_core"][index]
                color = RED if value is not None and value >= 95 else AMBER if value is not None and value >= 80 else CYAN
                cpu_id = cpu["core_ids"][index]
                cells.append(Text.assemble((f"{cpu_id:0{id_width}} ", MUTED), meter(value, bar_width, color),
                                          (f" {number(value, '%'):>6}", color), (" │ " if col < columns - 1 else "", "#344657")))
            grid.add_row(*cells)
            if spacious and row < rows - 1:
                grid.add_row(*[Text("─" * max(1, width // columns - 2), style="#263440") for _ in range(columns)])
        if not indices:
            grid = Text(self.t("沒有符合條件的核心", "No matching cores"), style=MUTED)
        footer = Table.grid(expand=True)
        footer.add_column(ratio=1)
        footer.add_column(justify="right")
        footer.add_row(Text("Load " + "  ".join(f"{v:.2f}" for v in cpu["load"])),
                       Text(f"user {number(cpu['times']['user'], '%')}  sys {number(cpu['times']['system'], '%')}  iowait {number(cpu['times']['iowait'], '%')}", style=MUTED))
        self.query_one("#cores", Static).update(grid)
        self.query_one("#core-footer", Static).update(footer)

    def draw_cpu_details(self):
        cpu = self.snapshot["cpu"]
        self.query_one("#cpu-detail-summary", Static).update(Text.assemble(
            (clean(self.snapshot["hardware"]["cpu_model"] or "CPU"), f"bold {CYAN}"),
            f"\n{cpu['physical'] or '—'} {self.t('核心', 'cores')} / {cpu['logical']} {self.t('執行緒', 'threads')}   {self.t('頻率', 'Frequency')} {number(cpu['frequency_mhz'], ' MHz', 0)}",
            f"   {self.t('溫度', 'Temp')} {number(cpu['temperature'], '°C')}   {self.t('功率', 'Power')} {number(cpu['power_watts'], ' W')}",
            f"\nLoad {' / '.join(f'{v:.2f}' for v in cpu['load'])}",
            (f"   {self.status(cpu['power_status'])}" if cpu["power_watts"] is None else "", MUTED)))
        topology = {item["cpu"]: item for item in self.snapshot["hardware"]["topology"]}
        rows = []
        for i in self.core_indices():
            cpu_id = cpu["core_ids"][i]
            top = topology.get(cpu_id, {})
            times = cpu["per_core_times"][i]
            rows.append((str(cpu_id), [cpu_id, *[top.get(k, "—") for k in ("core", "socket", "node")], number(cpu["per_core"][i]),
                                 *[number(times[k]) for k in ("user", "system", "iowait", "irq", "softirq", "steal", "idle")]]))
        self.sync_table("cpu-detail-table", rows)

    def gpu_view(self, compact=False):
        if not self.snapshot["gpus"]:
            return Text(self.status(self.snapshot["gpu_status"]) or self.t("未偵測到 NVIDIA GPU", "No NVIDIA GPU detected"), style=MUTED)
        elements = []
        width = max(20, self.query_one("#gpu-overview" if compact else "#gpu-detail").content_size.width)
        for gpu in self.snapshot["gpus"]:
            title = Text.assemble((f"GPU {gpu['index']}  ", f"bold {CYAN}"), (clean(gpu["name"]), "bold"))
            if not compact:
                metrics = panel_table("GPU%", self.t("顯存", "VRAM"), self.t("溫度", "Temp"),
                                      self.t("功率", "Power"), self.t("風扇", "Fan"), self.t("功率上限", "Power limit"))
                metrics.add_row(number(gpu["utilization"], "%", 0), f"{size(gpu['memory_used'])} / {size(gpu['memory_total'])}",
                                number(gpu["temperature"], "°C", 0), number(gpu["power_watts"], " W", 0),
                                number(gpu["fan_percent"], "%", 0), number(gpu["power_limit_watts"], " W", 0))
                elements.extend([title, metrics, Text("")])
                continue
            memory_percent = (gpu["memory_used"] / gpu["memory_total"] * 100) if gpu["memory_total"] and gpu["memory_used"] is not None else None
            history = self.history["gpu"] if gpu is self.snapshot["gpus"][0] else []
            usage = metric(gpu["utilization"], history, width, AMBER)
            memory = Text.assemble((self.t("顯存  ", "VRAM  "), MUTED), f"{size(gpu['memory_used'])} / {size(gpu['memory_total'])}\n", meter(memory_percent, width, AMBER))
            metrics = panel_table(self.t("溫度", "Temp"), self.t("功率", "Power"), self.t("風扇", "Fan"))
            metrics.add_row(Text(number(gpu["temperature"], "°C", 0), style="bold"),
                            Text(number(gpu["power_watts"], " W", 0), style="bold"),
                            Text(number(gpu["fan_percent"], "%", 0), style="bold"))
            processes = panel_table("PID", self.t("程序", "Process"), "GPU%", self.t("顯存", "VRAM"))
            processes.columns[0].no_wrap = True
            processes.columns[0].width = processes.columns[0].min_width = 7
            processes.columns[1].overflow = "ellipsis"
            processes.columns[1].no_wrap = True
            processes.columns[1].max_width = max(4, width - 31)
            processes.columns[2].no_wrap = True
            processes.columns[2].width = processes.columns[2].min_width = 5
            processes.columns[2].justify = "right"
            processes.columns[3].no_wrap = True
            processes.columns[3].width = processes.columns[3].min_width = 10
            processes.columns[3].justify = "right"
            items = sorted(gpu["processes"], key=lambda p: (-(p.get("gpu_utilization") or 0), -(p["memory"] or 0), p["pid"]))[:8]
            for process in items:
                processes.add_row(str(process["pid"]), Text(clean(process["name"])),
                                  number(process.get("gpu_utilization"), digits=0), size(process["memory"]))
            if not items:
                processes.add_row("—", self.t("沒有 GPU 程序", "No GPU processes"), "—", "—")
            elements.extend([title, usage, Text(""), memory, Text(""), metrics,
                             Text("─" * width, style="#344657"), processes])
            elements.append(Text(""))
        if self.snapshot["gpu_status"]:
            elements.append(Text(self.status(self.snapshot["gpu_status"]), style=AMBER))
        return Group(*elements)

    def draw_gpu_processes(self):
        table = self.query_one("#gpu-processes", DataTable)
        table.display = bool(self.snapshot["gpus"])
        self.query_one("#gpu-command-scroll").display = table.display
        widths = [8, 10, 4, 9, 6, 18, 7, 11]
        labels = ["PID", self.t("使用者", "User"), self.t("卡號", "Dev"), self.t("類型", "Type"), "GPU%",
                  self.t("顯存", "GPU memory"), "CPU%", self.t("主記憶體", "Host memory"), self.t("指令", "Command")]
        keys = ("pid", "user", "device", "type", "gpu", "memory", "cpu", "host_memory", "command")
        command_width = max(24, table.size.width - 1 - sum(widths) - 18)
        self.configure_table("gpu-processes", list(zip(keys, labels, [*widths, command_width])))
        table.frozen_columns = 1
        processes = [(gpu["index"], p) for gpu in self.snapshot["gpus"] for p in gpu["processes"]]
        processes.sort(key=lambda row: (-(row[1].get("gpu_utilization") or 0), -(row[1]["memory"] or 0), row[0], row[1]["pid"]))
        kinds = self.t({"C": "運算", "G": "圖形", "C+G": "運算/圖形"}, {"C": "Compute", "G": "Graphics", "C+G": "C+G"})
        rows = []
        for device, p in processes:
            memory = size(p["memory"])
            if p.get("memory_percent") is not None:
                memory += f" {p['memory_percent']:.0f}%"
            rows.append((f"{device}:{p['pid']}", [str(p["pid"]), Text(clean(p.get("user") or "—")), str(device),
                         kinds.get(p["type"], p["type"]), number(p.get("gpu_utilization"), digits=0), memory,
                         number(p.get("cpu")), size(p.get("host_memory")), Text(clean(p.get("command") or p["name"]))]))
        self.sync_table("gpu-processes", rows)
        self.update_gpu_command()

    def draw_processes(self, identifier, limit=None):
        table = self.query_one(f"#{identifier}", DataTable)
        search = self.query_one("#search", Input).value.casefold().strip()
        widths = [8, 10, 8, 10, 10]
        command_width = max(8, table.size.width - 1 - sum(widths) - 12)
        labels = ["PID", self.t("使用者", "User"), "CPU%" + (" ↓" if self.process_sort == "cpu" else ""),
                  self.t("記憶體", "Memory") + (" ↓" if self.process_sort == "memory" else ""), self.t("狀態", "State"), self.t("程序", "Command")]
        self.configure_table(identifier, list(zip(("pid", "user", "cpu", "memory", "status", "command"), labels, [*widths, command_width])))
        processes = [p for p in self.snapshot["processes"] if not search or search in f"{p['pid']} {p['user']} {p['name']} {p['command']}".casefold()]
        processes.sort(key=lambda p: (-(p[self.process_sort] if p[self.process_sort] is not None else -1), p["pid"]))
        states = {"running": "執行中", "sleeping": "睡眠", "disk-sleep": "I/O 等待", "stopped": "已停止", "zombie": "殭屍", "idle": "閒置"}
        rows = []
        for p in processes[:limit]:
            rows.append((str(p["pid"]), [str(p["pid"]), Text(clean(p["user"])), number(p["cpu"]), size(p["memory"]),
                                         states.get(p["status"], p["status"]) if self.language != "en" else p["status"],
                                         Text(short_command(p["command"], p["name"]))]))
        self.sync_table(identifier, rows)
        self.query_one("#process-count", Static).update(f"{len(processes)} " + self.t("個程序", "processes"))
        self.query_one("#overview-process-count", Static).update(f"{min(len(processes), 100)} / {len(processes)}")
        if identifier == "process-table":
            self.update_command()

    def sync_table(self, identifier, rows):
        table = self.query_one(f"#{identifier}", DataTable)
        rows = [(key, [Text(clean(value)) if isinstance(value, str) else value for value in values]) for key, values in rows]
        previous = self.table_cache.get(identifier, {})
        current = dict(rows)
        selected = self.table_selection.pop(identifier, None)
        if identifier != "overview-processes" and table.row_count and table.cursor_row < table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        with self.batch_update():
            for key in previous.keys() - current.keys():
                table.remove_row(key)
            columns = list(table.columns)
            for key, values in rows:
                if key not in previous:
                    table.add_row(*values, key=key)
                else:
                    for column, old, new in zip(columns, previous[key], values):
                        if old != new:
                            table.update_cell(key, column, new)
            # A process may appear on multiple GPUs, so PID alone is not a row identity.
            order = {tuple(str(value) for value in values): i for i, (_, values) in enumerate(rows)}
            table.sort(key=lambda values: order[tuple(str(value) for value in values)])
            if selected in current:
                # Textual 6's move_cursor(scroll=False) still scrolls from its
                # reactive watcher. Keep the viewport steady during live sorting.
                table.set_reactive(DataTable.cursor_coordinate, Coordinate(table.get_row_index(selected), table.cursor_column))
        self.table_cache[identifier] = current
        if identifier in self.table_scroll:
            self.call_after_refresh(table.scroll_to, y=self.table_scroll.pop(identifier), animate=False, immediate=True)

    def draw_storage(self):
        data = self.snapshot
        self.sync_table("storage-filesystems", [(str(i), [clean(d["mountpoint"]), clean(d["device"]), d["fstype"], size(d["used"]), size(d["free"]), size(d["total"]), self.status(d.get("error")) or number(d["percent"])]) for i, d in enumerate(data["disks"])])
        self.sync_table("storage-io", [(d["name"], [d["name"], size(d["read_rate"]), size(d["write_rate"]), number(d["read_iops"]), number(d["write_iops"]), number(d["busy_percent"])]) for d in data["disk_io"]])
        self.sync_table("storage-network", [(n["name"], [n["name"], self.t("連線", "Up") if n["up"] else self.t("未連線", "Down"), size(n["recv_rate"]), size(n["send_rate"]), size(n["received"]), size(n["sent"]), ", ".join(n["addresses"])]) for n in data["network"]])

    def draw_hardware(self):
        data = self.snapshot
        table = panel_table(self.t("項目", "Item"), self.t("資訊", "Details"))
        for key, label in (("os", self.t("作業系統", "Operating system")), ("kernel", "Kernel"), ("architecture", self.t("架構", "Architecture")), ("cpu_model", "CPU"),
                           ("sockets", self.t("Socket 數", "Sockets")), ("cores_per_socket", self.t("每 Socket 核心", "Cores per socket")), ("threads_per_core", self.t("每核心執行緒", "Threads per core")),
                           ("virtualization", self.t("虛擬化", "Virtualization")), ("cache_l1d", "L1d Cache"), ("cache_l1i", "L1i Cache"),
                           ("cache_l2", "L2 Cache"), ("cache_l3", "L3 Cache"), ("driver_version", self.t("NVIDIA 驅動", "NVIDIA driver"))):
            table.add_row(label, Text(clean(data["hardware"].get(key) or "—")))
        table.add_row(self.t("實體記憶體", "Total memory"), size(data["memory"]["total"]))
        table.add_row(self.t("已用記憶體", "Used memory"), f"{size(data['memory']['used'])} ({data['memory']['percent']:.1f}%)")
        table.add_row(self.t("可用記憶體", "Available memory"), size(data["memory"]["available"]))
        table.add_row(self.t("記憶體 Cache", "Memory cache"), size(data["memory"]["cached"]))
        swap = data["memory"]["swap"]
        table.add_row(self.t("Swap 已用 / 總計", "Swap used / total"), f"{size(swap['used'])} / {size(swap['total'])} ({swap['percent']:.1f}%)")
        table.add_row("Buffers", size(data["memory"]["buffers"]))
        table.add_row("Shared", size(data["memory"]["shared"]))
        self.query_one("#hardware-detail", Static).update(table)

    @on(Tabs.TabActivated, "#navigation")
    def tab_changed(self, event):
        self.query_one("#content", ContentSwitcher).current = event.tab.id.removeprefix("tab-")
        self.draw()
        if event.tab.id == "tab-gpu":
            self.query_one("#gpu-processes", DataTable).focus(scroll_visible=False)

    def action_page(self, name):
        self.query_one("#navigation", Tabs).active = f"tab-{name}"

    def action_pause(self):
        self.paused = not self.paused
        if not self.paused and self.latest:
            self.accept_snapshot(self.latest)
        else:
            self.draw()

    def action_search(self):
        if self.query_one("#content", ContentSwitcher).current == "overview":
            self.query_one("#overview-search", Input).focus(scroll_visible=False)
        else:
            self.action_page("processes")
            self.query_one("#search", Input).focus(scroll_visible=False)

    def action_clear_search(self):
        self.query_one("#search", Input).value = ""
        self.query_one("#overview-search", Input).value = ""
        identifier = "overview-processes" if self.query_one("#content", ContentSwitcher).current == "overview" else "process-table"
        self.query_one(f"#{identifier}", DataTable).focus(scroll_visible=False)

    @on(Input.Changed, "#search")
    @on(Input.Changed, "#overview-search")
    def search_changed(self, event):
        if event.value != event.input.value:
            return
        other = self.query_one("#search" if event.input.id == "overview-search" else "#overview-search", Input)
        if other.value != event.value:
            with other.prevent(Input.Changed):
                other.value = event.value
        self.draw()

    def action_language(self):
        self.language = "en" if self.language == "zh-TW" else "zh-TW"
        try:
            save_language(self.language)
        except SettingsError as error:
            self.notify(self.t("無法儲存語言設定：", "Could not save language: ") + str(error), severity="error")
        self.apply_language()

    @on(Button.Pressed, "#sort-cpu")
    def action_sort_cpu(self):
        self.process_sort = "cpu"
        self.draw()

    @on(Button.Pressed, "#sort-memory")
    def action_sort_memory(self):
        self.process_sort = "memory"
        self.draw()

    @on(Button.Pressed, "#core-sort")
    @on(Button.Pressed, "#detail-core-sort")
    def action_core_order(self):
        self.hot_first = not self.hot_first
        for name in ("core-sort", "detail-core-sort"):
            self.query_one(f"#{name}", Button).label = self.t("依使用率 ↓", "Usage ↓") if self.hot_first else self.t("依核心編號", "Core ID")
        self.draw()

    @on(Checkbox.Changed)
    def busy_changed(self, event):
        if event.value != event.checkbox.value:
            return
        self.busy_only = event.value
        for name in ("core-busy", "detail-core-busy"):
            other = self.query_one(f"#{name}", Checkbox)
            if other.value != event.value:
                with other.prevent(Checkbox.Changed):
                    other.value = event.value
        self.draw()

    @on(DataTable.RowHighlighted, "#process-table")
    def update_command(self):
        if not self.snapshot:
            return
        table = self.query_one("#process-table", DataTable)
        command = ""
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            process = next((p for p in self.snapshot["processes"] if str(p["pid"]) == key), None)
            if process:
                command = f"PID {process['pid']}  ·  {process['threads'] if process['threads'] is not None else '—'} {self.t('執行緒', 'threads')}\n{clean(process['command'] or process['name'])}"
        self.query_one("#process-command", Static).update(Text(command))

    @on(DataTable.RowHighlighted, "#gpu-processes")
    def update_gpu_command(self):
        if not self.snapshot:
            return
        table = self.query_one("#gpu-processes", DataTable)
        command = self.t("沒有 GPU 程序", "No GPU processes")
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            for gpu in self.snapshot["gpus"]:
                process = next((p for p in gpu["processes"] if f"{gpu['index']}:{p['pid']}" == key), None)
                if process:
                    command = f"GPU {gpu['index']}  ·  PID {process['pid']}\n{clean(process.get('command') or process['name'])}"
                    break
        self.query_one("#gpu-command", Static).update(Text(command))
