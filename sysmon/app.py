"""Keyboard-first terminal dashboard for local system resources."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from functools import lru_cache
from ipaddress import ip_address
import math
from pathlib import Path
import platform
import shlex
import threading
import time

from rich.table import Table
from rich.text import Text
from rich.style import Style
from textual.renderables.digits import Digits
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, ContentSwitcher, DataTable, Footer, Input, Static, Tab, Tabs
from textual.worker import Worker, WorkerState

from .collector import Collector, vllm_launch_command
from .formatting import number, size
from .widgets import CommandDataTable, PlainDataTable
from .settings import SettingsError, save_language, translate_status


CYAN = "#52ded6"
MINT = "#aff5cf"
AMBER = "#ffcf70"
MUTED = "#a3b5c5"
RED = "#ff9393"
SYSTEM = platform.system()


def clean(value):
    """Keep terminal controls and multiline process arguments out of cell content."""
    return " ".join("".join(c for c in str(value) if c.isprintable() or c.isspace()).split())


def meter(percent, width=12, color=CYAN):
    if percent is None:
        return Text("—", style=MUTED)
    filled = round(max(0, min(100, percent)) * width / 100)
    return Text.assemble(("▮" * filled, color), ("▮" * (width - filled), "#344657"))


def text_table(headers, rows, widths):
    """Fixed-width, single-line cells for the frequently refreshed summary panels."""
    result = Text()
    for row_index, values in enumerate((headers, *rows)):
        if row_index:
            result.append("\n")
        for index, (value, width) in enumerate(zip(values, widths)):
            if index:
                result.append("  ")
            cell = value.copy() if isinstance(value, Text) else Text(clean(value))
            cell.truncate(max(1, width), overflow="ellipsis")
            cell.pad_right(max(0, width - cell.cell_len))
            if not row_index:
                cell.stylize(MUTED)
            result.append_text(cell)
    return result


def metric(value, values, width, color):
    """Fixed 0–100% history beside the current value; no plotting dependency."""
    value_text = number(value, "%")
    value_width = Digits.get_width("100.0%") + 2
    lines = [segment.text for segment in Digits(value_text).render(Style(color=color, bold=True))
             if segment.text != "\n"]
    if width < value_width + 12:
        lines = [value_text, "", ""]
        value_width = len("100.0%") + 1
    plot_width = max(1, min(90, width - value_width - 5))
    points = list(values)[-plot_width:]
    points = [None] * (plot_width - len(points)) + points
    levels = [None if point is None else round(max(0, min(100, point)) * 24 / 100) for point in points]
    rows = []
    chars = " ▁▂▃▄▅▆▇█"
    for row, label in enumerate(("100", " 50", "  0")):
        left = Text(lines[row], style=f"bold {color}")
        left.pad_right(max(0, value_width - left.cell_len))
        cells = []
        for level in levels:
            filled = 0 if level is None else max(0, min(8, level - (2 - row) * 8))
            cells.append(chars[filled] if filled else ("▁" if row == 2 and level == 0 else " "))
        rows.append(Text.assemble(left, (label + "┤", MUTED), ("".join(cells), color)))
    return Text("\n").join(rows)


def memory_segments(memory):
    """Disjoint htop-style portions; Linux cached already includes shared RAM."""
    if memory["cached"] is None:
        return memory["used"], None, None, memory["free"]
    shared = min(memory["cached"], memory["shared"] or 0)
    cached = memory["cached"] - shared + (memory["buffers"] or 0)
    used = max(0, memory["total"] - memory["free"] - cached - shared)
    return used, shared, cached, memory["free"]


def memory_meter(memory, width):
    result = Text()
    boundary = 0
    consumed = 0
    for value, color in zip(memory_segments(memory), (MINT, CYAN, AMBER, "#344657")):
        consumed += value or 0
        end = min(width, round(consumed / memory["total"] * width)) if memory["total"] else width
        result.append("▮" * max(0, end - boundary), style=color)
        boundary = end
    return result


def aligned(left, right, width):
    """Align two single-line fields without a Rich table layout pass."""
    right.truncate(width, overflow="ellipsis")
    left.truncate(max(0, width - right.cell_len - 1), overflow="ellipsis")
    left.pad_right(max(0, width - left.cell_len - right.cell_len))
    left.append_text(right)
    return left


@lru_cache(maxsize=4096)
def short_command(command, name):
    if SYSTEM == "Windows":
        return clean(command or name)
    try:
        parts = shlex.split(command)
    except ValueError:
        return clean(command or name)
    if not parts:
        return clean(name)
    parts[0] = Path(parts[0]).name
    return clean(" ".join(parts))


class ResizingPanel(VerticalScroll):
    def on_resize(self):
        # Wait for the content's actual width, including while paused and on
        # Jetson where the GPU process table is hidden.
        self.call_after_refresh(self.app.draw)


class CommandScreen(ModalScreen):
    DEFAULT_CSS = """
    CommandScreen { align: center middle; background: #10151c 85%; }
    #command-dialog { width: 90%; height: 85%; border: solid #52ded6;
                      background: #10151c; border-title-color: #52ded6; }
    #command-scroll { height: 1fr; padding: 0 1; overflow-x: hidden; }
    #command-text { height: auto; }
    """

    def __init__(self, title, command, language):
        super().__init__()
        self.command_title = title
        self.command = command
        self._bindings.key_to_bindings["escape"] = [
            Binding("escape", "dismiss", "Back" if language == "en" else "返回", key_display="Esc")
        ]
        self._bindings.key_to_bindings["ctrl+c"] = [
            Binding("ctrl+c", "copy_text", "Copy" if language == "en" else "複製", key_display="Ctrl+C")
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="command-dialog") as dialog:
            dialog.border_title = self.command_title
            with VerticalScroll(id="command-scroll"):
                yield Static(Text(self.command, overflow="fold", no_wrap=False), id="command-text")
            yield Footer()

    def on_mount(self):
        self.query_one("#command-scroll").focus()

    def on_click(self, event):
        if event.button == 3:
            widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
            if widget.id != "command-text" or "offset" not in event.style.meta:
                event.stop()
                self.dismiss()


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
    #summary { grid-size: 4; grid-gutter: 0; height: 6; }
    .summary { border: solid #455766; padding: 0 1; height: 6; border-title-color: #a3b5c5; }
    #content { height: 1fr; }
    #overview { overflow: hidden hidden; }
    #overview-top { height: 21; }
    #overview-bottom { height: 1fr; min-height: 7; }
    .panel { border: solid #455766; padding: 0 1; border-title-color: #52ded6; border-title-style: bold; }
    #cpu-panel, #process-panel { width: 2fr; }
    #gpu-panel, #filesystem-panel { width: 1fr; }
    #gpu-panel, #filesystem-panel { scrollbar-gutter: stable; }
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
    CommandDataTable { scrollbar-size-horizontal: 0; }
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
    .narrow #summary { grid-size: 2; height: 12; }
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
        Binding("b", "core_order", "核心排序", show=False),
        Binding("escape", "clear_search", "清除搜尋", show=False),
        *[Binding(str(i), f"page('{name}')", name, show=False) for i, name in enumerate(
            ("overview", "cpu", "processes", "gpu", "storage", "hardware"), 1)],
    ]

    def __init__(self, interval=1.0, language="zh-TW", demo_gpus=0):
        super().__init__()
        self.language = language
        self.interval = interval
        self.demo_gpus = demo_gpus
        self.snapshot = None
        self.latest = None
        self.sampling_error = None
        self.paused = False
        self.hot_first = False
        self.busy_only = False
        self.process_sort = "cpu"
        self._search_on_process_page = False
        self.stop_sampling = threading.Event()
        self.cpu_history = deque(maxlen=90)
        self.gpu_history = {}
        self.table_cache = {}
        self.table_signatures = {}
        self.table_selection = {}
        self.table_scroll = {}

    def update_text(self, identifier, content, *, layout=False):
        widget = self.query_one(f"#{identifier}", Static)
        if widget.content != content:
            widget.update(content, layout=layout)

    def update_block(self, identifier, content):
        """Reflow a variable-height panel only when its rendered height changes."""
        widget = self.query_one(f"#{identifier}", Static)
        if widget.content != content:
            widget.update(content, layout=False)
        if widget.content_size.width:
            height = widget.get_content_height(widget.parent.size, self.size, widget.content_size.width)
            widget.styles.height = height + widget.styles.gutter.height

    def t(self, zh, en):
        return en if self.language == "en" else zh

    def check_action(self, action, parameters):
        # Let the modal's copy binding handle Ctrl+C before the app's quit action.
        return not (action == "quit" and isinstance(self.screen, CommandScreen))

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
            with ResizingPanel(id="overview"):
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
                        yield CommandDataTable(id="overview-processes", zebra_stripes=True, cursor_type="row", fixed_columns=1)
                    with ResizingPanel(id="filesystem-panel", classes="panel"):
                        yield Static(id="filesystems")
            with Vertical(id="cpu"):
                yield Static(id="cpu-detail-summary")
                with Horizontal(id="cpu-controls"):
                    yield Button(id="detail-core-sort")
                    yield Checkbox(id="detail-core-busy")
                yield PlainDataTable(id="cpu-detail-table", zebra_stripes=True, cursor_type="row")
            with Vertical(id="processes"):
                with Horizontal(id="process-controls"):
                    yield Input(id="search")
                    yield Button("CPU% ↓", id="sort-cpu")
                    yield Button(id="sort-memory")
                    yield Static(id="process-count")
                yield CommandDataTable(id="process-table", zebra_stripes=True, cursor_type="row", fixed_columns=1)
                with VerticalScroll(id="process-command-scroll"):
                    yield Static(id="process-command")
            with Vertical(id="gpu", classes="panel"):
                with ResizingPanel(id="gpu-stats"):
                    yield Static(id="gpu-detail")
                yield CommandDataTable(id="gpu-processes", zebra_stripes=True, cursor_type="row", fixed_columns=1)
                with VerticalScroll(id="gpu-command-scroll"):
                    yield Static(id="gpu-command")
            with VerticalScroll(id="storage"):
                yield Static(id="storage-label", classes="section-label")
                yield PlainDataTable(id="storage-filesystems", zebra_stripes=True, cursor_type="row")
                yield Static(classes="section-label", id="io-label")
                yield PlainDataTable(id="storage-io", zebra_stripes=True, cursor_type="row")
                yield Static(id="network-label", classes="section-label")
                yield PlainDataTable(id="storage-network", zebra_stripes=True, cursor_type="row")
            with VerticalScroll(id="hardware"):
                yield Static(id="hardware-detail")
        yield Footer()

    def configure_table(self, identifier, columns):
        table = self.query_one(f"#{identifier}", DataTable)
        signature = tuple(columns)
        if self.table_signatures.get(identifier) == signature:
            return
        self.table_scroll[identifier] = table.scroll_offset
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
        for table in self.query(CommandDataTable):
            table._bindings.key_to_bindings["enter"] = [
                Binding("enter", "select_cursor", self.t("完整指令", "Full command"), key_display="Enter")
            ]
        for identifier, label in (("storage-label", self.t("磁碟容量", "Disk capacity")),
                                  ("io-label", self.t("磁碟 I/O", "Disk I/O")),
                                  ("network-label", self.t("網路介面", "Network interfaces"))):
            self.query_one(f"#{identifier}", Static).update(label)
        headings = {
            "cpu-detail-table": ("CPU", "Core", "Socket", "NUMA", self.t("使用率%", "Busy%"), "user%", "sys%", "iowait%", "irq%", "DPC%" if SYSTEM == "Windows" else "soft%", "steal%", "idle%"),
            "storage-filesystems": self.t(("掛載點", "裝置", "格式", "已用", "剩餘", "總計", "使用率%"), ("Mount", "Device", "Type", "Used", "Free", "Total", "Used%")),
            "storage-io": self.t(("裝置", "讀取/s", "寫入/s", "讀 IOPS", "寫 IOPS", "忙碌%"), ("Device", "Read/s", "Write/s", "Read IOPS", "Write IOPS", "Busy%")),
            "storage-network": self.t(("介面", "狀態", "接收/s", "傳送/s", "累計接收", "累計傳送", "位址"), ("Interface", "State", "Receive/s", "Send/s", "Received", "Sent", "Address")),
        }
        for identifier, labels in headings.items():
            self.configure_table(identifier, [(str(i), label, None) for i, label in enumerate(labels)])
        for key, action, label in (("q", "quit", self.t("離開", "Quit")), ("space", "pause", self.t("暫停/恢復", "Pause/Resume")),
                                   ("slash", "search", self.t("搜尋", "Search")), ("c", "sort_cpu", self.t("CPU 排序", "Sort CPU")),
                                   ("m", "sort_memory", self.t("記憶體排序", "Sort memory")), ("l", "language", self.t("English", "繁體中文"))):
            # bind() appends, leaving the original description first in the footer.
            self._bindings.key_to_bindings[key] = [Binding(key, action, label)]
        self.refresh_bindings()
        self.draw()

    def on_mount(self):
        self.apply_language()
        self.query_one("#overview-processes", DataTable).focus(scroll_visible=False)
        self.default_screen.set_class(self.size.width < 110, "narrow")
        self.default_screen.set_class(self.size.height < 32, "compact")
        self.collect()

    @work(thread=True, exit_on_error=False)
    def collect(self):
        collector = Collector(demo_gpus=self.demo_gpus)
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
        if self.is_running and event.state == WorkerState.ERROR:
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
        if not self.is_running:
            return
        self.sampling_error = None
        self.latest = data
        if self.paused:
            return
        new_sample = self.snapshot is None or data["timestamp"] != self.snapshot["timestamp"]
        self.snapshot = data
        histories = [(self.cpu_history, data["cpu"]["percent"])]
        current_gpus = set()
        for gpu in data["gpus"]:
            key = gpu.get("uuid") or gpu["index"]
            current_gpus.add(key)
            history = self.gpu_history.setdefault(key, deque(maxlen=90))
            histories.append((history, gpu["utilization"]))
        for history, value in histories:
            if not new_sample and history:
                history[-1] = value
            else:
                history.append(value)
        for key in self.gpu_history.keys() - current_gpus:
            del self.gpu_history[key]
        with self.batch_update():
            self.draw()

    def on_resize(self, event):
        self.default_screen.set_class(event.size.width < 110, "narrow")
        self.default_screen.set_class(event.size.height < 32, "compact")
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
        days, remainder = divmod(int(data["uptime"]), 86400)
        hours, remainder = divmod(remainder, 3600)
        state = self.t("已暫停", "Paused") if self.paused else self.t("即時", "Live")
        header = aligned(Text.assemble((self.TITLE, f"bold {CYAN}"), f"  │  {clean(data['hostname'])}  │  {SYSTEM}"),
                         Text.assemble((f"{state}  {self.interval:g}s", CYAN), "  │  ",
                                       (datetime.fromtimestamp(data["timestamp"]).strftime("%H:%M:%S"), MINT),
                                       (f"  │  {self.t('開機', 'Up')} {days}d {hours:02}:{remainder // 60:02}", MUTED)),
                         self.query_one("#top-line").content_size.width)
        self.update_text("top-line", header)
        if self.sampling_error:
            self.draw_error()
        if self.size.height >= 32:
            self.draw_summary()
        active = self.query_one("#content", ContentSwitcher).current
        if active == "overview":
            self.draw_cores()
            self.update_block("gpu-overview", self.gpu_view(compact=True))
            self.draw_processes("overview-processes", limit=100)
            disk_width = self.query_one("#filesystems").content_size.width
            if disk_width >= 64:
                headers = self.t(("掛載點", "已用", "剩餘", "總計", "使用率"), ("Mount", "Used", "Free", "Total", "Usage"))
                mount_width = min(16, disk_width // 5)
                usage_width = disk_width - mount_width - 38
                rows = []
                for disk in data["disks"]:
                    usage = Text.assemble(meter(disk["percent"], max(1, usage_width - 7), MINT), f" {number(disk['percent'], '%')}")
                    rows.append((disk["mountpoint"], size(disk["used"]), size(disk["free"]), size(disk["total"]),
                                 self.status(disk.get("error")) or usage))
                filesystems = text_table(headers, rows, (mount_width, 10, 10, 10, usage_width))
            else:
                lines = [Text(self.t("已用 / 剩餘 / 總計", "Used / Free / Total"), style=MUTED)]
                for disk in data["disks"]:
                    mount = Text(clean(disk["mountpoint"]))
                    percent = Text(number(disk["percent"], "%"), style=MINT)
                    sizes = [size(disk[key]) for key in ("used", "free", "total")]
                    capacity = " / ".join(sizes)
                    if mount.cell_len + len(capacity) + percent.cell_len + 2 > disk_width:
                        capacity = "/".join(value.replace(" ", "") for value in sizes)
                    details = Text(self.status(disk.get("error")) or capacity)
                    mount.truncate(max(3, disk_width - details.cell_len - percent.cell_len - 2), overflow="ellipsis")
                    lines.append(aligned(Text.assemble(mount, " ", details), percent, disk_width))
                filesystems = Text("\n", no_wrap=True, overflow="ellipsis").join(lines)
            self.update_block("filesystems", filesystems)
        elif active == "cpu":
            self.draw_cpu_details()
        elif active == "processes":
            self.draw_processes("process-table")
        elif active == "gpu":
            self.update_block("gpu-detail", self.gpu_view())
            self.draw_gpu_processes()
        elif active == "storage":
            self.draw_storage()
        elif active == "hardware":
            self.draw_hardware()

    def draw_summary(self):
        data = self.snapshot
        cpu, memory = data["cpu"], data["memory"]
        width = max(20, self.query_one("#summary-cpu").content_size.width)
        self.update_text("summary-cpu", Text.assemble(
            metric(cpu["percent"], self.cpu_history, width, CYAN),
            "\n", (self.t("溫度 ", "Temp "), MUTED), number(cpu["temperature"], "°C"),
            (self.t("  功率 ", "  Power "), MUTED), number(cpu["power_watts"], " W")))
        swap = memory["swap"]
        memory_width = max(1, self.query_one("#summary-memory").content_size.width - 7)
        used, shared, cached, _ = memory_segments(memory)
        used += shared or 0
        used_percent = used / memory["total"] * 100
        self.update_text("summary-memory", Text.assemble(
            (f"{self.t('已用', 'Used')} {size(used)} / {size(memory['total'])}\n", f"bold {MINT}"),
            memory_meter(memory, memory_width), f" {used_percent:5.1f}%\n",
            (f"Cache {size(cached)}", AMBER), (f"  Shared {size(shared)}\n", CYAN),
            f"Swap {size(swap['used'])} / {size(swap['total'])}  {number(swap['percent'], '%')}"))
        gpu = data["gpus"]
        self.query_one("#summary-gpu").border_title = f"GPU · {len(gpu)}" if len(gpu) > 1 else f"GPU {gpu[0]['index']}" if gpu else "GPU"
        gpu_width = max(8, self.query_one("#summary-gpu").content_size.width)
        if len(gpu) > 1:
            visible = 3 if len(gpu) > 4 else 4
            rows = [aligned(Text(f"GPU {item['index']}", style=CYAN),
                            Text(f"{number(item['utilization'], '%')}  {size(item['memory_used'])}", style=AMBER), gpu_width)
                    for item in gpu[:visible]]
            if len(gpu) > visible:
                rows.append(Text(f"… +{len(gpu) - visible} GPU", style=MUTED))
            summary = Text("\n").join(rows)
        elif gpu:
            summary = Text.assemble(
                metric(gpu[0]["utilization"], self.gpu_history.get(gpu[0].get("uuid") or gpu[0]["index"], ()), gpu_width, AMBER), "\n",
                Text(self.t("共用系統 RAM", "Shared system RAM") if gpu[0].get("memory_shared") else
                     f"VRAM {size(gpu[0]['memory_used'])} / {size(gpu[0]['memory_total'])}", style=MUTED))
        else:
            summary = Text(self.status(data["gpu_status"]) or self.t("未偵測到 NVIDIA GPU", "No NVIDIA GPU detected"), style=MUTED)
        self.update_text("summary-gpu", summary)
        interfaces = [
            n for n in data["network"] if n["up"] and n["name"] != "lo"
            and not (n["addresses"] and all(ip_address(address).is_loopback for address in n["addresses"]))
        ]
        receive = sum(n["recv_rate"] or 0 for n in interfaces) if any(n["recv_rate"] is not None for n in interfaces) else None
        send = sum(n["send_rate"] or 0 for n in interfaces) if any(n["send_rate"] is not None for n in interfaces) else None
        network = Text.assemble((f"↓ {size(receive)}/s", f"bold {CYAN}"),
                                "  " if width >= 40 else "\n", (f"↑ {size(send)}/s", f"bold {MINT}"))
        self.update_text("summary-network", network)

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
            panel.styles.height = "1fr"
        else:
            self.query_one("#overview-top").styles.height = "auto"
            panel.styles.height = rows * 2 + 5
        spacious = narrow or top_height >= rows * 2 + 4
        grid = Text()
        bar_width = max(3, width // columns - id_width - 11)
        for row in range(rows):
            for col in range(columns):
                position = row + col * rows
                if position >= len(indices):
                    grid.append(" " * (width // columns))
                    continue
                index = indices[position]
                value = cpu["per_core"][index]
                color = RED if value is not None and value >= 95 else AMBER if value is not None and value >= 80 else CYAN
                cpu_id = cpu["core_ids"][index]
                cell = Text.assemble((f"{cpu_id:0{id_width}} ", MUTED), meter(value, bar_width, color),
                                     (f" {number(value, '%'):>6}", color), (" │ " if col < columns - 1 else "", "#344657"))
                cell.pad_right(max(0, width // columns - cell.cell_len))
                grid.append_text(cell)
            if row < rows - 1:
                grid.append("\n")
            if spacious and row < rows - 1:
                grid.append(("─" * max(1, width // columns - 2) + "  ") * columns + "\n", style="#263440")
        if not indices:
            grid = Text(self.t("沒有符合條件的核心", "No matching cores"), style=MUTED)
        load = Text("Load " + "  ".join(f"{v:.2f}" for v in cpu["load"])) if cpu["load"] else Text()
        footer = aligned(load,
                         Text(f"user {number(cpu['times']['user'], '%')}  sys {number(cpu['times']['system'], '%')}  iowait {number(cpu['times']['iowait'], '%')}", style=MUTED),
                         self.query_one("#core-footer").content_size.width)
        self.query_one("#cores").styles.height = grid.plain.count("\n") + 1
        self.update_text("cores", grid)
        self.update_text("core-footer", footer)

    def draw_cpu_details(self):
        cpu = self.snapshot["cpu"]
        load = f"\nLoad {' / '.join(f'{v:.2f}' for v in cpu['load'])}" if cpu["load"] else ""
        power_status = self.status(cpu["power_status"]) if cpu["power_watts"] is None else ""
        if power_status:
            power_status = ("   " if load else "\n") + power_status
        self.update_block("cpu-detail-summary", Text.assemble(
            (clean(self.snapshot["hardware"]["cpu_model"] or "CPU"), f"bold {CYAN}"),
            f"\n{cpu['physical'] or '—'} {self.t('核心', 'cores')} / {cpu['logical']} {self.t('執行緒', 'threads')}   {self.t('頻率', 'Frequency')} {number(cpu['frequency_mhz'], ' MHz', 0)}",
            f"   {self.t('溫度', 'Temp')} {number(cpu['temperature'], '°C')}   {self.t('功率', 'Power')} {number(cpu['power_watts'], ' W')}",
            load, (power_status, MUTED)))
        topology = {item["cpu"]: item for item in self.snapshot["hardware"]["topology"]}
        rows = []
        for i in self.core_indices():
            cpu_id = cpu["core_ids"][i]
            top = topology.get(cpu_id, {})
            times = cpu["per_core_times"][i]
            rows.append((str(cpu_id), [cpu_id, *[top[k] if top.get(k) is not None else "—" for k in ("core", "socket", "node")], number(cpu["per_core"][i]),
                                 *[number(times[k]) for k in ("user", "system", "iowait", "irq", "softirq", "steal", "idle")]]))
        self.sync_table("cpu-detail-table", rows)

    def gpu_view(self, compact=False):
        if not self.snapshot["gpus"]:
            return Text(self.status(self.snapshot["gpu_status"]) or self.t("未偵測到 NVIDIA GPU", "No NVIDIA GPU detected"), style=MUTED)
        elements = []
        width = max(20, self.query_one("#gpu-overview" if compact else "#gpu-detail").content_size.width)
        for gpu in self.snapshot["gpus"]:
            title = Text.assemble((f"GPU {gpu['index']}  ", f"bold {CYAN}"), (clean(gpu["name"]), "bold"))
            history = self.gpu_history.get(gpu.get("uuid") or gpu["index"], ())
            usage = metric(gpu["utilization"], history, width, AMBER)
            memory_text = (self.t("共用系統 RAM", "Shared system RAM") if gpu.get("memory_shared") else
                           f"{size(gpu['memory_used'])} / {size(gpu['memory_total'])}")
            if not compact:
                memory_label = self.t("記憶體", "Memory") if gpu.get("memory_shared") else self.t("顯存", "VRAM")
                headers = ("GPU%", memory_label, self.t("溫度", "Temp"),
                           self.t("功率", "Power"), self.t("風扇", "Fan"), self.t("功率上限", "Power limit"))
                values = (number(gpu["utilization"], "%", 0), memory_text,
                          number(gpu["temperature"], "°C", 0), number(gpu["power_watts"], " W", 0),
                          number(gpu["fan_percent"], "%", 0), number(gpu["power_limit_watts"], " W", 0))
                widths = (6, 23, 7, 10, 7, 13)
                extra = max(0, width - sum(widths) - 10) // len(widths)
                metrics = text_table(headers, [values], [size + extra for size in widths])
                elements.extend([title, usage, metrics, Text("")])
                continue
            memory_percent = (gpu["memory_used"] / gpu["memory_total"] * 100) if gpu["memory_total"] and gpu["memory_used"] is not None else None
            memory = Text.assemble((self.t("顯存  ", "VRAM  "), MUTED), f"{size(gpu['memory_used'])} / {size(gpu['memory_total'])}\n", meter(memory_percent, width, AMBER))
            if gpu.get("memory_shared"):
                memory = Text(memory_text, style=MUTED)
            metrics = text_table((self.t("溫度", "Temp"), self.t("功率", "Power"), self.t("風扇", "Fan")),
                                 [[Text(number(gpu[key], unit, 0), style="bold") for key, unit in
                                   (("temperature", "°C"), ("power_watts", " W"), ("fan_percent", "%"))]],
                                 [(width - 4) // 3] * 3)
            elements.extend([title, usage, memory, Text(""), metrics])
            if len(self.snapshot["gpus"]) > 1 or not gpu.get("processes_supported", True):
                elements.append(Text(""))
                continue
            items = sorted(gpu["processes"], key=lambda p: (-(p.get("gpu_utilization") or 0), -(p["memory"] or 0), p["pid"]))[:8]
            headers = ["PID", self.t("程序", "Process"), "GPU%", "CPU%", self.t("顯存", "VRAM")]
            rows = []
            for p in items:
                link = Style(underline=True, meta={"@click": f"app.gpu_command({gpu['index']},{p['pid']})"})
                rows.append([Text(str(p["pid"]), style=link), Text(clean(p["name"]), style=link),
                             number(p.get("gpu_utilization"), digits=0), number(p.get("cpu")), size(p["memory"])])
            widths = (7, max(4, width - 37), 5, 7, 10)
            if width < 41:
                # Keep PID and usage visible when the side panel is narrow.
                headers.pop(1)
                for row in rows:
                    row.pop(1)
                widths = (7, 4, 6, max(7, width - 23))
            processes = text_table(headers, rows, widths) if items else Text(self.t("未取得 GPU 程序", "No GPU processes reported"), style=MUTED)
            elements.extend([Text("─" * width, style="#344657"), processes])
            elements.append(Text(""))
        if self.snapshot["gpu_status"]:
            elements.append(Text(self.status(self.snapshot["gpu_status"]), style=AMBER))
        return Text("\n").join(elements)

    def draw_gpu_processes(self):
        table = self.query_one("#gpu-processes", DataTable)
        table.display = any(gpu.get("processes_supported", True) for gpu in self.snapshot["gpus"])
        self.query_one("#gpu-command-scroll").display = table.display
        self.query_one("#gpu-stats").styles.max_height = "50%" if table.display else "100%"
        widths = [8, 10, 4, 9, 6, 18, 7, 11]
        labels = ["PID", self.t("使用者", "User"), self.t("卡號", "Dev"), self.t("類型", "Type"), "GPU%",
                  self.t("顯存", "GPU memory"), "CPU%", self.t("主記憶體", "Host memory"), self.t("指令", "Command")]
        keys = ("pid", "user", "device", "type", "gpu", "memory", "cpu", "host_memory", "command")
        self.configure_table("gpu-processes", list(zip(keys, labels, [*widths, None])))
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
        search_id = "#overview-search" if identifier == "overview-processes" else "#search"
        search = self.query_one(search_id, Input).value.casefold().strip()
        widths = [8, 10, 8, 10, 10]
        labels = ["PID", self.t("使用者", "User"), "CPU%" + (" ↓" if self.process_sort == "cpu" else ""),
                  self.t("記憶體", "Memory") + (" ↓" if self.process_sort == "memory" else ""), self.t("狀態", "State"), self.t("程序", "Command")]
        self.configure_table(identifier, list(zip(("pid", "user", "cpu", "memory", "status", "command"), labels, [*widths, None])))
        processes = [p for p in self.snapshot["processes"] if not search or search in f"{p['pid']} {p['user']} {p['name']} {p['command']}".casefold()]
        processes.sort(key=lambda p: (-(p[self.process_sort] if p[self.process_sort] is not None else -1), p["pid"]))
        states = {"running": "執行中", "sleeping": "睡眠", "disk-sleep": "I/O 等待", "stopped": "已停止", "zombie": "殭屍", "idle": "閒置"}
        rows = []
        for index, p in enumerate(processes[:limit]):
            # The overview follows rank; reuse its slots when a different PID
            # enters the top 100. The full process page keeps PID selection.
            key = str(index) if identifier == "overview-processes" else str(p["pid"])
            rows.append((key, [str(p["pid"]), p["user"], number(p["cpu"]), size(p["memory"]),
                                         states.get(p["status"], p["status"]) if self.language != "en" else p["status"],
                                         short_command(p["command"], p["name"])]))
        self.sync_table(identifier, rows)
        if identifier == "overview-processes":
            self.update_text("overview-process-count", f"{min(len(processes), 100)} / {len(processes)}", layout=True)
        else:
            self.update_text("process-count", f"{len(processes)} " + self.t("個程序", "processes"), layout=True)
            self.update_command()

    def sync_table(self, identifier, rows):
        table = self.query_one(f"#{identifier}", DataTable)
        previous = self.table_cache.get(identifier, {})
        current = dict(rows)
        removed = previous.keys() - current.keys()
        selected = self.table_selection.pop(identifier, None)
        if identifier != "overview-processes" and table.row_count and table.cursor_row < table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        with self.batch_update():
            for key in removed:
                table.remove_row(key)
            columns = list(table.columns)
            for key, values in rows:
                if key not in previous:
                    table.add_row(*(Text(clean(value)) if isinstance(value, str) else value for value in values), key=key)
                else:
                    for column, old, new in zip(columns, previous[key], values):
                        if old != new:
                            table.update_cell(key, column, Text(clean(new)) if isinstance(new, str) else new,
                                              update_width=column.value == "command")
            if removed and "command" in table.columns:
                # Textual retains auto widths after removing their widest row.
                if rows:
                    key = rows[0][0]
                    table.update_cell(key, "command", table.get_cell(key, "command"), update_width=True)
                else:
                    column = table.columns["command"]
                    column.content_width = column.label.cell_len
            # A process may appear on multiple GPUs, so PID alone is not a row identity.
            if list(previous) != list(current):
                if identifier == "gpu-processes":
                    order = {(str(values[2]), str(values[0])): i for i, (_, values) in enumerate(rows)}
                    table.sort("device", "pid", key=lambda values: order[tuple(map(str, values))])
                else:
                    order = {str(values[0]): i for i, (_, values) in enumerate(rows)}
                    table.sort(columns[0], key=lambda value: order[str(value)])
            if selected in current:
                # Textual 6's move_cursor(scroll=False) still scrolls from its
                # reactive watcher. Keep the viewport steady during live sorting.
                table.set_reactive(DataTable.cursor_coordinate, Coordinate(table.get_row_index(selected), table.cursor_column))
        self.table_cache[identifier] = current
        if identifier in self.table_scroll:
            offset = self.table_scroll.pop(identifier)
            self.call_after_refresh(table.scroll_to, x=offset.x, y=offset.y, animate=False, immediate=True)

    def draw_storage(self):
        data = self.snapshot
        self.sync_table("storage-filesystems", [(str(i), [clean(d["mountpoint"]), clean(d["device"]), d["fstype"], size(d["used"]), size(d["free"]), size(d["total"]), self.status(d.get("error")) or number(d["percent"])]) for i, d in enumerate(data["disks"])])
        self.sync_table("storage-io", [(d["name"], [d["name"], size(d["read_rate"]), size(d["write_rate"]), number(d["read_iops"]), number(d["write_iops"]), number(d["busy_percent"])]) for d in data["disk_io"]])
        self.sync_table("storage-network", [(n["name"], [n["name"], self.t("連線", "Up") if n["up"] else self.t("未連線", "Down"), size(n["recv_rate"]), size(n["send_rate"]), size(n["received"]), size(n["sent"]), ", ".join(n["addresses"])]) for n in data["network"]])

    def draw_hardware(self):
        data = self.snapshot
        table = Table(self.t("項目", "Item"), self.t("資訊", "Details"),
                      expand=True, box=None, padding=(0, 1), header_style=MUTED)
        for key, label in (("os", self.t("作業系統", "Operating system")), ("kernel", "Kernel"), ("architecture", self.t("架構", "Architecture")), ("cpu_model", "CPU"),
                           ("sockets", self.t("Socket 數", "Sockets")), ("cores_per_socket", self.t("每 Socket 核心", "Cores per socket")), ("threads_per_core", self.t("每核心執行緒", "Threads per core")),
                           ("virtualization", self.t("虛擬化", "Virtualization")), ("cache_l1d", "L1d Cache"), ("cache_l1i", "L1i Cache"),
                           ("cache_l2", "L2 Cache"), ("cache_l3", "L3 Cache"), ("driver_version", self.t("NVIDIA 驅動", "NVIDIA driver"))):
            table.add_row(label, Text(clean(data["hardware"].get(key) or "—")))
        table.add_row(self.t("實體記憶體", "Total memory"), size(data["memory"]["total"]))
        table.add_row(self.t("已用記憶體", "Used memory"), f"{size(data['memory']['used'])} ({data['memory']['percent']:.1f}%)")
        table.add_row(self.t("空閒記憶體", "Free memory"), size(data["memory"]["free"]))
        table.add_row(self.t("可用記憶體", "Available memory"), size(data["memory"]["available"]))
        table.add_row(self.t("記憶體 Cache", "Memory cache"), size(data["memory"]["cached"]))
        swap = data["memory"]["swap"]
        table.add_row(self.t("Swap 已用 / 總計", "Swap used / total"), f"{size(swap['used'])} / {size(swap['total'])} ({number(swap['percent'], '%')})")
        table.add_row("Buffers", size(data["memory"]["buffers"]))
        table.add_row("Shared", size(data["memory"]["shared"]))
        self.update_block("hardware-detail", table)

    @on(Tabs.TabActivated, "#navigation")
    def tab_changed(self, event):
        self.query_one("#content", ContentSwitcher).current = event.tab.id.removeprefix("tab-")
        self.draw()
        identifier = {"tab-overview": "overview-processes", "tab-processes": "process-table", "tab-gpu": "gpu-processes"}.get(event.tab.id)
        if event.tab.id == "tab-processes" and self._search_on_process_page:
            identifier = "search"
        self._search_on_process_page = False
        if identifier:
            if identifier == "gpu-processes" and not self.query_one("#gpu-processes").display:
                identifier = "gpu-stats"
            self.query_one(f"#{identifier}").focus(scroll_visible=False)

    def action_page(self, name):
        self.query_one("#navigation", Tabs).active = f"tab-{name}"

    def action_pause(self):
        self.paused = not self.paused
        if not self.paused and self.latest:
            self.accept_snapshot(self.latest)
        else:
            self.draw()

    def action_search(self):
        page = self.query_one("#content", ContentSwitcher).current
        if page == "overview":
            self.query_one("#overview-search", Input).focus(scroll_visible=False)
        elif page == "processes":
            self.query_one("#search", Input).focus(scroll_visible=False)
        else:
            self._search_on_process_page = True
            self.action_page("processes")

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

    def show_command(self, process, device=None):
        title = f"PID {process['pid']}"
        command = clean(process.get("command") or process["name"])
        if device is not None:
            title = f"GPU {device}  ·  {title}"
            if command.casefold().startswith("vllm::"):
                launch = vllm_launch_command(process["pid"])
                if launch is not None:
                    pid, args = launch
                    label = self.t("上層程序", "Ancestor process")
                    command += f"\n\n{label} PID {pid}\n{clean(args)}"
        self.push_screen(CommandScreen(title, command, self.language))

    @on(DataTable.RowSelected, "#overview-processes")
    @on(DataTable.RowSelected, "#process-table")
    @on(DataTable.RowSelected, "#gpu-processes")
    def command_selected(self, event):
        if not self.snapshot or event.row_key not in event.data_table.rows:
            return
        if event.data_table.id == "gpu-processes":
            device, pid = map(int, event.row_key.value.split(":"))
            self.action_gpu_command(device, pid)
        else:
            pid = int(str(event.data_table.get_cell(event.row_key, "pid")))
            process = next((p for p in self.snapshot["processes"] if p["pid"] == pid), None)
            if process:
                self.show_command(process)

    def action_gpu_command(self, device, pid):
        if not self.snapshot:
            return
        for gpu in self.snapshot["gpus"]:
            if gpu["index"] == device:
                process = next((p for p in gpu["processes"] if p["pid"] == pid), None)
                if process:
                    self.show_command(process, device)
                return

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
        self.update_text("process-command", Text(command), layout=True)

    @on(DataTable.RowHighlighted, "#gpu-processes")
    def update_gpu_command(self):
        table = self.query_one("#gpu-processes", DataTable)
        command = self.t("未取得 GPU 程序", "No GPU processes reported")
        if table.row_count:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            command = f"GPU {table.get_cell(key, 'device')}  ·  PID {table.get_cell(key, 'pid')}\n{table.get_cell(key, 'command')}"
        self.update_text("gpu-command", Text(command), layout=True)
