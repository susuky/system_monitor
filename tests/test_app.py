"""Exercise the real collector and terminal interface together."""

import asyncio
from copy import deepcopy
import os
import time
import tempfile
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from rich.cells import cell_len

from textual import events
from textual.widgets import Checkbox, ContentSwitcher, DataTable, Input, Tab

from sysmon.app import CYAN, CommandScreen, SystemMonitorApp, memory_meter, memory_segments, metric
from sysmon.gpu_demo import sample_gpus
from sysmon.settings import load_language
from scripts.capture_demo import DemoApp, demo_snapshot


def click_action_position(widget, action):
    for y, line in enumerate(widget.render_lines(widget.size.region)):
        x = 0
        for segment in line:
            if segment.style and segment.style.meta.get("@click") == action:
                return x, y
            x += cell_len(segment.text)
    return None


class ResourceRenderingTests(unittest.TestCase):
    def test_fixed_percent_scale_width_without_time_labels(self):
        for width in (20, 26, 36, 64, 120):
            axes = []
            for value in (None, 0, 9.9, 50, 100):
                rendered = metric(value, [0, None, 50, 100], width, CYAN)
                lines = rendered.split("\n")
                self.assertEqual(len(lines), 3)
                self.assertTrue(all(line.cell_len <= width for line in lines))
                self.assertIn("100┤", lines[0].plain)
                self.assertIn(" 50┤", lines[1].plain)
                self.assertIn("  0┤", lines[2].plain)
                self.assertNotIn("0s", rendered.plain)
                axes.append(lines[0].plain.index("100┤"))
            self.assertEqual(len(set(axes)), 1)
        trace = metric(40, [0, 40, 95, 30, 40], 40, CYAN).split("\n")[2].plain.rsplit("┤", 1)[1]
        self.assertNotIn(" ", trace[-5:])
        near_zero = metric(0, [0, 1, 0, 2, 0], 40, CYAN).split("\n")[2].plain.rsplit("┤", 1)[1]
        self.assertEqual(near_zero[-5:], "▁" * 5)

    def test_shared_and_cache_are_counted_once(self):
        memory = {"total": 1000, "used": 600, "available": 400, "free": 100,
                  "cached": 700, "buffers": 50, "shared": 200}
        self.assertEqual(memory_segments(memory), (150, 200, 550, 100))
        self.assertEqual(sum(memory_segments(memory)), memory["total"])
        for width in (8, 33, 50):
            self.assertEqual(memory_meter(memory, width).cell_len, width)
        windows = {**memory, "used": 900, "available": 100, "cached": None, "buffers": None, "shared": None}
        self.assertEqual(memory_segments(windows), (900, None, None, 100))
        self.assertEqual(memory_meter(windows, 50).cell_len, 50)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_network_processes_fit_summary_and_compact_storage(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            download = '/usr/bin/python3 "download 台灣.py" --output "[literal]"'
            upload = '/usr/bin/python3 "upload 台灣.py" ' + '--input "[literal]" ' * 30 + "UPLOAD_END"
            snapshot["processes"][0].update(pid=456, name="downloader", command=download)
            snapshot["network_processes"] = [
                {"pid": 456, "name": "downloader", "recv_rate": 8 * 1024**2, "send_rate": 1024},
                {"pid": 123, "name": "同一個很長的程序名稱" * 4, "command": upload,
                 "recv_rate": 1024, "send_rate": 2 * 1024**2},
            ]
            app.accept_snapshot(snapshot)
            await pilot.pause()
            summary = app.query_one("#summary-network")
            lines = summary.content.split("\n")
            self.assertLessEqual(len(lines), summary.content_size.height)
            self.assertTrue(all(line.cell_len <= summary.content_size.width for line in lines))
            self.assertTrue(any("downloader" in line.plain and "8.0 MiB/s" in line.plain for line in lines))
            position = click_action_position(summary, "app.network_command(456)")
            self.assertIsNotNone(position)
            self.assertTrue(await pilot.click("#summary-network", offset=position))
            self.assertIsInstance(app.screen, CommandScreen)
            self.assertEqual(app.screen.query_one("#command-text").content.plain, download)
            await pilot.press("escape")

            await pilot.resize_terminal(80, 24)
            await pilot.press("5")
            table = app.query_one("#storage-network-processes", DataTable)
            preview = app.query_one("#network-command")
            self.assertEqual(table.row_count, 2)
            self.assertEqual(table.get_row_at(0)[0].plain, "456")
            self.assertEqual(table.get_row_at(1)[3].plain, "2.0 MiB")
            rate_columns = sum(table.columns[key].get_render_width(table) for key in ("pid", "name", "receive", "send"))
            self.assertLessEqual(rate_columns, table.size.width)
            self.assertEqual(app.focused, table)
            self.assertIn(download, preview.content.plain)
            await pilot.press("down")
            self.assertEqual(table.cursor_row, 1)
            self.assertIn(upload, preview.content.plain)

            snapshot = deepcopy(snapshot)
            snapshot["network_processes"].reverse()
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(table.get_row_at(table.cursor_row)[0].plain, "123")
            self.assertIn(upload, preview.content.plain)
            await pilot.press("tab", "end")
            preview_scroll = app.query_one("#network-command-scroll")
            self.assertIs(app.focused, preview_scroll)
            self.assertGreater(preview_scroll.max_scroll_y, 0)
            self.assertEqual(preview_scroll.scroll_y, preview_scroll.max_scroll_y)
            await pilot.press("shift+tab")
            self.assertIs(app.focused, table)
            self.assertTrue(await pilot.click(table, offset=(2, table.header_height)))
            self.assertIsInstance(app.screen, CommandScreen)
            command_text = app.screen.query_one("#command-text")
            self.assertEqual(command_text.content.plain, upload)
            last_line = command_text.size.height - 1
            end_x = cell_len(command_text.render_line(last_line).text.rstrip())
            await pilot.mouse_down(command_text, offset=(0, 0))
            await pilot.mouse_up(command_text, offset=(end_x, last_line))
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, upload)
            await pilot.press("escape")
            self.assertIs(app.focused, table)

            snapshot["network_processes"] = []
            snapshot["network_process_status"] = "以系統管理員身分開啟 Vital"
            app.accept_snapshot(snapshot)
            self.assertFalse(table.display)
            self.assertFalse(app.query_one("#network-command-scroll").display)
            self.assertEqual(app.query_one("#network-process-status").content, "Run Vital as administrator")

    async def test_consecutive_history_without_delay_gaps_or_pause_duplicates(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            started = snapshot["timestamp"]
            app.accept_snapshot(snapshot)
            await pilot.press("space", "space")
            self.assertEqual(len(app.cpu_history), 1)
            for index in range(1, 6):
                snapshot = deepcopy(snapshot)
                snapshot["timestamp"] = started + index * 1.4
                snapshot["cpu"]["percent"] = index * 10
                app.accept_snapshot(snapshot)
            self.assertEqual(list(app.cpu_history)[1:], [10, 20, 30, 40, 50])
            self.assertEqual(len(app.cpu_history), 6)
            self.assertNotIn(None, app.cpu_history)
            self.assertAlmostEqual(app.snapshot["timestamp"], started + 7)
            self.assertEqual(len(app.gpu_history[snapshot["gpus"][0]["uuid"]]), 6)

    async def test_clocks_and_disk_trends_fit_overview_and_storage(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            app.accept_snapshot(snapshot)
            end = deepcopy(snapshot)
            end["timestamp"] += app.interval
            end["disk_throughput"] = {"read_rate": 0, "write_rate": 64 * 1024**2}
            app.accept_snapshot(end)
            await pilot.pause()
            visible = "".join(ET.fromstring(app.export_screenshot()).itertext())
            for value in ("3600", "5600", "2625", "10501"):
                self.assertIn(value, visible)
            overview = app.query_one("#overview-disk-throughput")
            self.assertRegex(overview.content.plain, r"Read\s+0\.0 B/s\s+█·")
            self.assertRegex(overview.content.plain, r"Write\s+64\.0 MiB/s\s+▁▃")
            self.assertTrue(all(line.cell_len <= overview.content_size.width for line in overview.content.split("\n")))
            await pilot.press("space", "5")
            app.accept_snapshot({**end, "timestamp": end["timestamp"] + app.interval,
                                 "disk_throughput": {"read_rate": None, "write_rate": None}})
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            detail = app.query_one("#storage-disk-throughput")
            self.assertIn("0–176.0 MiB/s", detail.content.plain)
            self.assertIn("64.0 MiB/s", detail.content.plain)
            self.assertTrue(all(line.cell_len <= detail.content_size.width for line in detail.content.split("\n")))
            self.assertEqual(len(app.disk_history), 2)
            await pilot.press("space")
            self.assertRegex(detail.content.plain, r"Read\s+—")
            self.assertRegex(detail.content.plain, r"Write\s+—")
            self.assertEqual(len(app.disk_history), 3)
            await pilot.press("4")
            self.assertIn("Core 2625 MHz  VRAM 10501 MHz", app.query_one("#gpu-detail").content.plain)

    async def test_network_summary_shows_selected_interfaces_and_two_processes(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            iface = snapshot["network"][0]
            snapshot["network"].append({**iface, "name": "bridge-port", "recv_rate": 128 * 1024**2})
            snapshot["network_processes"] = [
                {"pid": pid, "name": name, "recv_rate": rate * 1024**2, "send_rate": 0}
                for pid, name, rate in ((10, "download", 6), (20, "sync", 4), (30, "hidden", 1))
            ]
            snapshot["network_capture_interfaces"] = [iface["name"]]
            snapshot["network_process_scope"] = "packet"
            app.accept_snapshot(snapshot)
            await pilot.pause()
            summary = app.query_one("#summary-network")
            self.assertEqual(summary.border_title, "Network")
            self.assertEqual([line.split() for line in summary.content.plain.splitlines()],
                             [["↓", "12.4", "MiB/s", "↑", "2.1", "MiB/s"],
                              ["download", "6.0", "MiB/s"], ["sync", "4.0", "MiB/s"]])
            await pilot.press("5")
            label = app.query_one("#network-process-label")
            self.assertEqual(label.content, "TCP+UDP process traffic")
            self.assertFalse(app.query_one("#network-process-status").display)
            self.assertEqual(app.query_one("#storage-network-processes", DataTable).row_count, 3)
            snapshot["network_process_scope"] = "tcp"
            app.accept_snapshot(snapshot)
            self.assertEqual(label.content, "TCP process traffic")

    async def test_multiple_gpu_histories_scrolling_pause_and_removal(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            snapshot["gpus"] = sample_gpus(8, 0)
            snapshot["gpu_status"] = "Simulated GPUs"
            app.accept_snapshot(snapshot)
            first = {gpu["uuid"]: gpu["utilization"] for gpu in snapshot["gpus"]}
            updated = deepcopy(snapshot)
            updated["timestamp"] += app.interval
            updated["gpus"].reverse()
            for gpu in updated["gpus"]:
                gpu["utilization"] = gpu["index"] * 10
            app.accept_snapshot(updated)
            for gpu in updated["gpus"]:
                self.assertEqual(list(app.gpu_history[gpu["uuid"]]), [first[gpu["uuid"]], gpu["utilization"]])
                self.assertEqual(app.gpu_history[gpu["uuid"]].maxlen, 90)
            self.assertIn("… +5 GPU", app.query_one("#summary-gpu").content.plain)
            await pilot.press("4")
            await pilot.pause()
            stats = app.query_one("#gpu-stats")
            self.assertIs(app.focused, stats)
            self.assertGreater(stats.max_scroll_y, 0)
            detail = app.query_one("#gpu-detail").content.plain
            self.assertEqual(detail.count("100┤"), 8)
            for gpu in updated["gpus"]:
                self.assertIn(gpu["name"], detail)
            await pilot.press("end")
            await pilot.wait_for_scheduled_animations()
            self.assertEqual(stats.scroll_y, stats.max_scroll_y)
            await pilot.press("space")
            changed = deepcopy(updated)
            changed["timestamp"] += 10 * app.interval
            changed["gpus"] = changed["gpus"][:2]
            changed["gpus"][0]["utilization"] = None
            app.accept_snapshot(changed)
            self.assertEqual(len(app.gpu_history), 8)
            await pilot.resize_terminal(80, 24)
            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(len(app.gpu_history), 2)
            history = app.gpu_history[changed["gpus"][0]["uuid"]]
            self.assertEqual(len(history), 3)
            self.assertIsNone(history[-1])
            self.assertTrue(all(line.cell_len <= app.query_one("#gpu-detail").content_size.width
                                for line in app.query_one("#gpu-detail").content.split("\n")))

    async def test_memory_and_filesystem_summary_survive_scrollbar_resize(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(200, 44)) as pilot:
            snapshot = demo_snapshot()
            for i in range(10):
                disk = deepcopy(snapshot["disks"][0])
                disk["mountpoint"] = f"/run/test-{i}"
                snapshot["disks"].append(disk)
            app.accept_snapshot(snapshot)
            await pilot.pause(.3)
            memory = app.query_one("#summary-memory")
            for label in ("Used", "Cache", "Shared", "Swap"):
                self.assertIn(label, memory.content.plain)
            used, shared, _, _ = memory_segments(snapshot["memory"])
            expected_percent = (used + (shared or 0)) / snapshot["memory"]["total"] * 100
            self.assertIn(f"{expected_percent:.1f}%", memory.content.split("\n")[1].plain)
            self.assertEqual(len(memory.content.split("\n")), 4)
            self.assertNotIn("Avail", memory.content.plain)
            with tempfile.TemporaryDirectory() as config, patch.dict(os.environ, {"XDG_CONFIG_HOME": config}):
                await pilot.press("l")
                self.assertIn("Shared", memory.content.plain)
                self.assertNotIn("共用", memory.content.plain)
                await pilot.press("l")
            filesystems = app.query_one("#filesystems")
            heading, root, *_ = filesystems.content.plain.splitlines()
            self.assertEqual(heading.split(), ["Mount", "Used", "Free", "Total", "Usage"])
            self.assertRegex(root, r"^/ {2,}384\.0 GiB {2,}640\.0 GiB {2,}1\.0 TiB {2,}37\.5%$")
            self.assertTrue(all(line.cell_len <= filesystems.content_size.width for line in filesystems.content.split("\n")))
            panel = app.query_one("#filesystem-panel")
            panel.focus()
            await pilot.press("end")
            await pilot.pause(.3)
            self.assertEqual(panel.scroll_y, panel.max_scroll_y)
            self.assertIn("/run/test-9", app.export_screenshot())
            await pilot.resize_terminal(250, 50)
            await pilot.pause(.3)
            self.assertIn("Free", filesystems.content.plain)
            self.assertTrue(all(line.cell_len <= filesystems.content_size.width for line in filesystems.content.split("\n")))
            await pilot.resize_terminal(80, 44)
            await pilot.pause(.3)
            self.assertTrue(all(line.cell_len <= memory.content_size.width for line in memory.content.split("\n")))
            await pilot.resize_terminal(80, 24)
            await pilot.press("6")
            app.query_one("#hardware").focus()
            await pilot.press("end")
            await pilot.pause(.3)
            visible = "".join(ET.fromstring(app.export_screenshot()).itertext()).replace("\u00a0", " ")
            self.assertIn("Free memory", visible)

    async def test_compact_disk_summary_aligns_free_space_and_usage_when_resized(self):
        app = DemoApp(language="zh-TW")
        async with app.run_test(size=(124, 30)) as pilot:
            snapshot = demo_snapshot()
            snapshot["disks"] = [
                {"mountpoint": "C:\\", "used": 1.6 * 1024**4, "free": 181.3 * 1024**3,
                 "total": 1.8 * 1024**4, "percent": 90.3},
                {"mountpoint": "D:\\", "used": 1.8 * 1024**4, "free": 1.9 * 1024**4,
                 "total": 3.6 * 1024**4, "percent": 48.7},
                {"mountpoint": "/media/外接硬碟/備份資料", "used": None, "free": None,
                 "total": None, "percent": None, "error": "無法讀取檔案系統"},
            ]
            app.accept_snapshot(snapshot)
            await pilot.press("space")
            filesystems = app.query_one("#filesystems")
            for width, height in ((124, 30), (198, 44), (110, 30)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                lines = filesystems.content.split("\n")
                self.assertTrue(all(line.cell_len <= filesystems.content_size.width for line in lines))
                self.assertIn("無法讀取檔案系統", lines[3].plain)
                if width == 198:
                    self.assertEqual(lines[0].plain.split(), ["掛載點", "已用", "剩餘", "總計", "使用率"])
                    self.assertRegex(lines[1].plain, r"^C:\\ {2,}1\.6 TiB {2,}181\.3 GiB {2,}1\.8 TiB {2,}90\.3%$")
                else:
                    self.assertEqual(lines[0].plain.split(), ["掛載點", "剩餘", "使用率"])
                    self.assertRegex(lines[1].plain, r"^C:\\ {2,}181\.3 GiB {2,}90\.3%$")
                    self.assertRegex(lines[2].plain, r"^D:\\ {2,}1\.9 TiB {2,}48\.7%$")
                    self.assertEqual(lines[1].plain.index("GiB"), lines[2].plain.index("TiB"))

    async def test_paused_jetson_gpu_resizes_without_a_process_table(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            snapshot["gpus"][0].update(memory_shared=True, memory_used=None,
                                       memory_total=None, processes=[], processes_supported=False)
            snapshot["gpu_status"] = "Jetson GPU 程序資料不可用"
            app.accept_snapshot(snapshot)
            await pilot.press("4", "space")
            self.assertTrue(app.paused)
            self.assertFalse(app.query_one("#gpu-processes").display)
            for width, height in ((80, 24), (160, 44), (80, 24)):
                await pilot.resize_terminal(width, height)
                await pilot.pause(.3)
                header = app.query_one("#top-line")
                details = app.query_one("#gpu-detail")
                self.assertLessEqual(header.content.cell_len, header.content_size.width)
                self.assertTrue(all(line.cell_len <= details.content_size.width
                                    for line in details.content.split("\n")))
                self.assertIn("Shared system RAM", details.content.plain)
                self.assertEqual(app.snapshot["timestamp"], snapshot["timestamp"])
            await pilot.press("q")

    async def test_plain_table_matches_rich_cells_and_scrolled_viewport(self):
        from rich.segment import Segment
        from rich.style import Style
        from rich.text import Text
        from textual.app import App
        from sysmon.widgets import PlainDataTable

        class TableApp(App):
            CSS = "PlainDataTable.tinted > .datatable--cursor {color: #11ee22; background: #ab12cd;}"

            def compose(self):
                yield PlainDataTable(id="table", fixed_columns=1, zebra_stripes=True, cursor_type="row")

        def segments(lines):
            return [list(Segment.simplify(line)) for line in lines]

        app = TableApp()
        async with app.run_test(size=(40, 12)) as pilot:
            table = app.query_one(PlainDataTable)
            table.add_column(Text("PID"), key="pid", width=6)
            table.add_column(Text("使用者"), key="user", width=8)
            table.add_column(Text("指令"), key="command", width=40)
            for i in range(20):
                table.add_row(Text(str(i)), Text("測試用戶"), Text("python 長指令.py " + "x" * 50), key=str(i))
            table._show_hover_cursor = True
            style = Style(color="white", bgcolor="#10151c")
            for text in (Text("你e\u0301好🧑‍💻longtext"), Text("styled", style="bold red"),
                         Text("longtext", overflow="ellipsis"), Text("first\nsecond")):
                table.update_cell("0", "user", text)
                for width in (3, 4, 7, 16):
                    for cursor, hover in ((False, False), (True, False), (False, True), (True, True)):
                        table._cell_render_cache.clear()
                        expected = segments(DataTable._render_cell(table, 0, 1, style, width, cursor, hover))
                        table._cell_render_cache.clear()
                        self.assertEqual(segments(table._render_cell(0, 1, style, width, cursor, hover)), expected)
            table.update_cell("0", "user", Text("測試用戶"))
            await pilot.pause()
            await pilot.click("#table", offset=(12, 3))
            self.assertEqual(table.cursor_row, 2)
            table.move_cursor(row=15)
            table.scroll_to(x=15, animate=False, immediate=True)
            await pilot.pause()
            self.assertGreater(table.scroll_x, 0)
            self.assertGreater(table.scroll_y, 0)
            await pilot.hover("#table", offset=(12, 4))
            await pilot.pause()
            table.update_cell("15", "command", Text("changed 指令"))
            table.sort("pid", key=lambda cell: -int(str(cell)))
            await pilot.pause()
            clicked_row = int(table.scroll_y) + 2
            await pilot.click("#table", offset=(12, 3))
            self.assertEqual(table.cursor_row, clicked_row)
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value, str(19 - clicked_row))
            for state in ("focus", "blur", "css", "theme"):
                if state == "blur":
                    app.set_focus(None)
                elif state == "css":
                    table.focus()
                    table.add_class("tinted")
                elif state == "theme":
                    app.theme = "textual-light"
                await pilot.pause()
                actual = segments(table.render_lines(table.size.region))
                if state == "css":
                    self.assertEqual(table.get_component_rich_style("datatable--cursor").bgcolor.name, "#ab12cd")
                table._clear_caches()
                with patch.object(table, "_render_cell", lambda *args, **kwargs: DataTable._render_cell(table, *args, **kwargs)):
                    expected = segments(table.render_lines(table.size.region))
                self.assertEqual(actual, expected, state)

    async def test_gpu_process_columns_selection_and_narrow_scrolling(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(160, 44)) as pilot:
            snapshot = demo_snapshot()
            second = deepcopy(snapshot["gpus"][0])
            second["index"] = 1
            second["processes"] = [second["processes"][0]]
            long_command = "python other-gpu.py " + "--parameter value " * 20 + "END_MARKER"
            second["processes"][0].update(device=1, gpu_utilization=99, command=long_command)
            snapshot["gpus"].append(second)
            app.accept_snapshot(snapshot)
            overview = app.query_one("#overview-processes", DataTable)
            row_keys = list(overview.rows)
            snapshot["processes"][1]["cpu"] = 9999
            app.accept_snapshot(snapshot)
            self.assertEqual(list(overview.rows), row_keys)
            expected = sorted(snapshot["processes"], key=lambda process: (-process["cpu"], process["pid"]))
            self.assertEqual([str(value) for value in overview.get_column("pid")], [str(process["pid"]) for process in expected])
            self.assertEqual(str(overview.get_row_at(0)[2]), "9999.0")
            self.assertIn("ffmpeg", str(overview.get_row_at(0)[5]))
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#gpu-processes", DataTable)
            self.assertEqual(table.row_count, 4)
            self.assertGreater(table.columns[next(key for key in table.columns if key.value == "command")].get_render_width(table), 24)
            self.assertGreater(table.max_scroll_x, 0)
            self.assertEqual([str(c.label) for c in table.columns.values()],
                             ["PID", "User", "Dev", "Type", "GPU%", "GPU memory", "CPU%", "Host memory", "Command"])
            self.assertEqual(table.get_row_index("1:4102"), 0)
            row = [str(value) for value in table.get_row("0:4102")]
            self.assertEqual(row[1:6], ["demo", "0", "Compute", "72", "16.0 GiB 67%"])
            self.assertEqual(row[7], "12.0 GiB")
            self.assertEqual(str(table.get_row("0:4407")[3]), "Graphics")
            table.move_cursor(row=table.get_row_index("1:4102"))
            await pilot.pause()
            self.assertIn("python other-gpu.py", str(app.query_one("#gpu-command").content))
            second["processes"][0]["gpu_utilization"] = None
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(str(table.get_row("1:4102")[4]), "—")
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value, "1:4102")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertGreater(table.size.height, 3)
            self.assertGreater(table.max_scroll_x, 0)
            self.assertEqual(table.fixed_columns, 1)
            command_scroll = app.query_one("#gpu-command-scroll")
            self.assertLessEqual(command_scroll.region.bottom, app.query_one("#gpu").region.bottom)
            await pilot.press("right", "right", "right")
            await pilot.pause()
            self.assertGreater(table.scroll_x, 0)
            self.assertIn("python other-gpu.py", str(app.query_one("#gpu-command").content))
            await pilot.press("tab")
            self.assertEqual(app.focused.id, "gpu-command-scroll")
            await pilot.press("end")
            await pilot.pause(1.1)
            self.assertGreater(command_scroll.scroll_y, 0)
            self.assertEqual(command_scroll.scroll_y, command_scroll.max_scroll_y)
            self.assertIn("END_MARKER", app.export_screenshot())
            snapshot["gpus"].clear()
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(table.row_count, 0)
            self.assertFalse(table.display)
            self.assertEqual(str(app.query_one("#gpu-command").content), "No GPU processes reported")
            await pilot.press("ctrl+c")

    async def test_full_command_keyboard_access_on_every_process_table(self):
        commands = {
            "Linux": "/usr/bin/python3 /home/demo/實驗/train.py " + "--dataset 台灣資料集 " * 180 + "LINUX_COMMAND_END",
            "Windows": r'"C:\Program Files\Python\python.exe" "C:\Users\測試\工作\訓練.py" '
                       + r'--資料 "C:\訓練資料\dataset.bin" ' * 100 + "WINDOWS_COMMAND_END",
        }
        for system, command in commands.items():
            with self.subTest(system=system), patch("sysmon.app.SYSTEM", system):
                app = DemoApp(language="zh-TW")
                async with app.run_test(size=(120, 34)) as pilot:
                    snapshot = demo_snapshot()
                    snapshot["processes"][0]["command"] = command
                    snapshot["gpus"][0]["processes"][0]["command"] = command
                    snapshot["network_processes"] = [
                        {"pid": 4102, "name": "python", "command": command, "recv_rate": 1024, "send_rate": 0}
                    ]
                    app.accept_snapshot(snapshot)
                    for key, identifier in (("1", "overview-processes"), ("3", "process-table"),
                                            ("4", "gpu-processes"), ("5", "storage-network-processes")):
                        with self.subTest(table=identifier):
                            await pilot.press(key)
                            await pilot.pause()
                            table = app.query_one(f"#{identifier}", DataTable)
                            self.assertIs(app.focused, table)
                            self.assertEqual(table.fixed_columns, 1)
                            self.assertEqual(table.horizontal_scrollbar.region.height, 0)
                            self.assertEqual(str(table.get_row_at(table.cursor_row)[0]), "4102")
                            await pilot.press("right")
                            await pilot.pause(.3)
                            self.assertGreater(table.scroll_x, 0)
                            await pilot.press("end")
                            await pilot.pause()
                            self.assertEqual(table.scroll_x, table.max_scroll_x)
                            self.assertIn(f"{system.upper()}_COMMAND_END", app.export_screenshot())
                            visible_rows = table.render_lines(table.size.region)
                            self.assertTrue(any("4102" in line.text for line in visible_rows))
                            scroll_x = table.scroll_x

                            await pilot.press("enter")
                            self.assertIsInstance(app.screen, CommandScreen)
                            self.assertEqual(app.screen.query_one("#command-text").content.plain, command)
                            scroll = app.screen.query_one("#command-scroll")
                            self.assertIs(app.focused, scroll)
                            self.assertEqual(scroll.max_scroll_x, 0)
                            self.assertGreater(scroll.max_scroll_y, 0)
                            await pilot.press("pagedown")
                            await pilot.pause(.3)
                            self.assertGreater(scroll.scroll_y, 0)
                            await pilot.press("end")
                            await pilot.pause(.3)
                            self.assertEqual(scroll.scroll_y, scroll.max_scroll_y)
                            self.assertIn(f"{system.upper()}_COMMAND_END", app.export_screenshot())
                            await pilot.press("escape")
                            self.assertNotIsInstance(app.screen, CommandScreen)
                            self.assertIs(app.focused, table)
                            self.assertEqual(table.scroll_x, scroll_x)
                            await pilot.press("home")
                            await pilot.pause()
                            self.assertEqual(table.scroll_x, 0)

    async def test_command_mouse_selection_copies_original_text_and_escape_returns(self):
        commands = {
            "Linux": "/usr/bin/python3 /home/demo/實驗/train.py " + "--dataset '台灣資料集 [literal]' " * 5 + "COPY_END",
            "Windows": r'"C:\Program Files\Python\python.exe" "C:\Users\測試\訓練.py" '
                       + r'--資料 "C:\訓練資料\dataset.bin" ' * 5 + "COPY_END",
        }
        for system, command in commands.items():
            with self.subTest(system=system), patch("sysmon.app.SYSTEM", system):
                app = DemoApp(language="en")
                async with app.run_test(size=(100, 30)) as pilot:
                    snapshot = demo_snapshot()
                    snapshot["processes"][0]["command"] = command
                    app.accept_snapshot(snapshot)
                    await pilot.press("3", "end")
                    table = app.query_one("#process-table", DataTable)
                    self.assertGreater(table.scroll_x, 0)
                    scroll_x = table.scroll_x
                    cursor = table.cursor_coordinate
                    await pilot.click(table, offset=(2, table.header_height))
                    screen = app.screen
                    self.assertIsInstance(screen, CommandScreen)
                    await pilot.press("ctrl+c")
                    self.assertIs(app.screen, screen)
                    self.assertTrue(app.is_running)

                    command_text = screen.query_one("#command-text")
                    last_line = command_text.size.height - 1
                    self.assertGreater(last_line, 0)
                    end_x = cell_len(command_text.render_line(last_line).text.rstrip())
                    self.assertLess(end_x, command_text.size.width)
                    await pilot.mouse_down(command_text, offset=(0, 0))
                    await pilot.mouse_up(command_text, offset=(end_x, last_line))
                    self.assertEqual(screen.get_selected_text(), command)
                    await pilot.press("ctrl+c")
                    self.assertEqual(app.clipboard, command)
                    self.assertIs(app.screen, screen)

                    await pilot.press("escape")
                    self.assertNotIsInstance(app.screen, CommandScreen)
                    self.assertIs(app.focused, table)
                    self.assertEqual(table.cursor_coordinate, cursor)
                    self.assertEqual(table.scroll_x, scroll_x)
                    await pilot.press("ctrl+c")
                    self.assertFalse(app.is_running)

    async def test_command_right_click_only_leaves_outside_text(self):
        app = DemoApp(language="zh-TW")
        async with app.run_test(size=(100, 30)) as pilot:
            snapshot = demo_snapshot()
            snapshot["processes"][0]["command"] = "python 台灣.py " + "--dataset [literal] " * 9 + "RIGHT_CLICK_END"
            app.accept_snapshot(snapshot)
            await pilot.press("3", "end")
            table = app.query_one("#process-table", DataTable)
            self.assertGreater(table.scroll_x, 0)
            scroll_x = table.scroll_x
            cursor = table.cursor_coordinate
            for target in ("wrapped-line-end", "command-end", "below-text", "border", "outside"):
                with self.subTest(target=target):
                    await pilot.press("enter")
                    screen = app.screen
                    self.assertIsInstance(screen, CommandScreen)
                    command_text = screen.query_one("#command-text")
                    await pilot._post_mouse_events(
                        [events.MouseDown, events.MouseUp, events.Click],
                        widget=command_text, offset=(8, 0), button=3,
                    )
                    self.assertIs(app.screen, screen)

                    if target in ("wrapped-line-end", "command-end"):
                        row = 0 if target == "wrapped-line-end" else command_text.size.height - 1
                        text_width = cell_len(command_text.render_line(row).text.rstrip())
                        self.assertLess(text_width + 1, command_text.size.width)
                        widget, offset = command_text, (text_width + 1, row)
                    elif target == "below-text":
                        widget = screen.query_one("#command-scroll")
                        offset = (2, command_text.region.bottom - widget.region.y + 1)
                    elif target == "border":
                        widget, offset = screen.query_one("#command-dialog"), (0, 1)
                    else:
                        widget, offset = screen, (0, 0)
                    await pilot._post_mouse_events(
                        [events.MouseDown, events.MouseUp, events.Click],
                        widget=widget, offset=offset, button=3,
                    )
                    self.assertNotIsInstance(app.screen, CommandScreen)
                    self.assertIs(app.focused, table)
                    self.assertEqual(table.cursor_coordinate, cursor)
                    self.assertEqual(table.scroll_x, scroll_x)

    async def test_command_growth_refresh_and_resize_keep_the_table_view(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(120, 34)) as pilot:
            for key, identifier in (("1", "overview-processes"), ("3", "process-table"), ("4", "gpu-processes")):
                with self.subTest(table=identifier):
                    snapshot = demo_snapshot()
                    command = "python train.py " + "--dataset sample " * 30 + "OLD_END"
                    snapshot["processes"][0]["command"] = command
                    snapshot["gpus"][0]["processes"][0]["command"] = command
                    app.accept_snapshot(snapshot)
                    await pilot.press(key)
                    await pilot.pause()
                    table = app.query_one(f"#{identifier}", DataTable)
                    table.move_cursor(row=1)
                    await pilot.press("right", "right", "right")
                    await pilot.pause(.3)
                    self.assertGreater(table.scroll_x, 0)
                    scroll_x = table.scroll_x
                    selected_pid = str(table.get_row_at(table.cursor_row)[0])
                    command_column = next(column for column_key, column in table.columns.items() if column_key.value == "command")
                    previous_width = command_column.get_render_width(table)

                    command += " --new-parameter value" * 30 + " UPDATED_COMMAND_END"
                    snapshot["processes"][0]["command"] = command
                    snapshot["gpus"][0]["processes"][0]["command"] = command
                    app.accept_snapshot(snapshot)
                    await pilot.pause()
                    self.assertGreater(command_column.get_render_width(table), previous_width)
                    self.assertEqual(table.scroll_x, scroll_x)
                    self.assertEqual(str(table.get_row_at(table.cursor_row)[0]), selected_pid)
                    for width, height in ((100, 28), (160, 44)):
                        await pilot.resize_terminal(width, height)
                        await pilot.pause(.3)
                        self.assertEqual(table.scroll_x, scroll_x)
                        self.assertEqual(str(table.get_row_at(table.cursor_row)[0]), selected_pid)
                    await pilot.press("end")
                    await pilot.pause()
                    self.assertIn("UPDATED_COMMAND_END", app.export_screenshot())
                    await pilot.press("home")
                    await pilot.resize_terminal(120, 34)

    async def test_command_width_tracks_simultaneous_changes_in_terminal_cells(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(80, 24)) as pilot:
            snapshot = demo_snapshot()
            processes = snapshot["processes"]
            processes[2]["command"] = "node " + "x" * 90
            app.accept_snapshot(snapshot)
            await pilot.pause()
            table = app.query_one("#overview-processes", DataTable)
            column = table.columns["command"]
            processes[0]["command"] = "python " + "寬" * 60
            processes[1]["command"] = "ffmpeg " + "a" * 100
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(column.content_width, cell_len(processes[0]["command"]))
            processes[0]["command"] = "python"
            processes[1]["command"] = "ffmpeg"
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(column.content_width, cell_len(processes[2]["command"]))
            processes[0]["command"] = "python " + "寬" * 75
            processes[1]["command"] = "ffmpeg " + "a" * 120
            app.accept_snapshot(snapshot)
            processes[0]["command"] = "python " + "a" * 100
            app.accept_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(column.content_width, cell_len(processes[1]["command"]))

    async def test_removed_long_commands_shrink_the_scrollable_columns(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(120, 34)) as pilot:
            snapshot = demo_snapshot()
            longest = "python longest.py " + "--dataset sample " * 80 + "LONGEST_END"
            shorter = "python shorter.py " + "--dataset sample " * 20 + "SHORTER_END"
            for processes in (snapshot["processes"], snapshot["gpus"][0]["processes"]):
                processes[0]["command"] = longest
                processes[1]["command"] = shorter
            app.accept_snapshot(snapshot)
            for page, identifier, search in (("1", "overview-processes", "overview-search"), ("3", "process-table", "search")):
                with self.subTest(table=identifier):
                    await pilot.press(page)
                    field = app.query_one(f"#{search}", Input)
                    field.value = ""
                    await pilot.pause()
                    table = app.query_one(f"#{identifier}", DataTable)
                    longest_scroll = table.max_scroll_x
                    field.value = "4120"
                    await pilot.pause()
                    table.focus(scroll_visible=False)
                    await pilot.press("end")
                    self.assertEqual(table.row_count, 1)
                    self.assertGreater(table.max_scroll_x, 0)
                    self.assertLess(table.max_scroll_x, longest_scroll)
                    self.assertIn("SHORTER_END", "\n".join(line.text for line in table.render_lines(table.size.region)))
                    field.value = ""
                    await pilot.pause()
                    field.value = "no-matching-process"
                    deadline = time.monotonic() + 3
                    while table.row_count and time.monotonic() < deadline:
                        await pilot.pause()
                    self.assertEqual(table.row_count, 0)
                    field.value = "4120"
                    await pilot.pause()
                    await pilot.press("end")
                    self.assertEqual(table.row_count, 1)
                    self.assertIn("SHORTER_END", "\n".join(line.text for line in table.render_lines(table.size.region)))

            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#gpu-processes", DataTable)
            longest_scroll = table.max_scroll_x
            snapshot["gpus"][0]["processes"].pop(0)
            app.accept_snapshot(snapshot)
            await pilot.pause()
            await pilot.press("end")
            self.assertEqual(table.row_count, 2)
            self.assertGreater(table.max_scroll_x, 0)
            self.assertLess(table.max_scroll_x, longest_scroll)
            self.assertIn("SHORTER_END", "\n".join(line.text for line in table.render_lines(table.size.region)))

    async def test_command_dialog_snapshot_search_and_compact_gpu_click(self):
        app = DemoApp(language="en")
        async with app.run_test(size=(120, 34)) as pilot:
            snapshot = demo_snapshot()
            command = '/usr/bin/python3 "train [literal].py" --name 台灣\t資料\x00\n--last ORIGINAL_END'
            snapshot["processes"][0]["command"] = command
            snapshot["gpus"][0]["processes"][0]["command"] = command
            expected = '/usr/bin/python3 "train [literal].py" --name 台灣 資料 --last ORIGINAL_END'
            app.accept_snapshot(snapshot)
            for key, identifier in (("1", "overview-search"), ("3", "search")):
                await pilot.press(key, "/")
                field = app.query_one(f"#{identifier}", Input)
                field.value = "4102"
                await pilot.press("enter")
                self.assertNotIsInstance(app.screen, CommandScreen)
                self.assertIs(app.focused, field)
                await pilot.press("escape")

            await pilot.press("1")
            await pilot.pause()
            compact = app.query_one("#gpu-overview")
            position = click_action_position(compact, "app.gpu_command(0,4102)")
            self.assertIsNotNone(position, "The compact GPU PID must open its full command")
            self.assertTrue(await pilot.click("#gpu-overview", offset=position))
            self.assertIsInstance(app.screen, CommandScreen)
            self.assertEqual(app.screen.query_one("#command-text").content.plain, expected)
            updated = deepcopy(snapshot)
            updated["processes"][0]["command"] = "python new-command.py"
            updated["gpus"][0]["processes"][0]["command"] = "python new-command.py"
            app.accept_snapshot(updated)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#command-text").content.plain, expected)
            await pilot.press("escape", "4", "enter")
            self.assertEqual(app.screen.query_one("#command-text").content.plain, "python new-command.py")
            await pilot.press("escape")

    async def test_language_layout_and_live_viewport(self):
        with tempfile.TemporaryDirectory(prefix="sysmon-language-") as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            app = SystemMonitorApp(interval=0.3, language="en")
            async with app.run_test(size=(200, 44)) as pilot:
                await pilot.pause(1.2)
                self.assertIsNotNone(app.snapshot)
                self.assertEqual(app._bindings.key_to_bindings["q"][0].description, "Quit")
                self.assertEqual(app._bindings.key_to_bindings["slash"][0].description, "Search")
                self.assertEqual(str(app.query_one("#tab-overview", Tab).label), "1 Overview")
                table = app.query_one("#overview-processes", DataTable)
                self.assertEqual(table.scroll_y, 0)
                self.assertEqual(table.scroll_x, 0)
                self.assertEqual(table.fixed_columns, 1)
                await pilot.press("/")
                self.assertEqual(app.focused.id, "overview-search")
                field = app.query_one("#overview-search", Input)
                field.value = "sys"
                field.value = "sysmon"
                await asyncio.wait_for(pilot.pause(), timeout=3)
                self.assertEqual(app.query_one("#search", Input).value, "sysmon")
                checkbox = app.query_one("#core-busy", Checkbox)
                checkbox.value = True
                checkbox.value = False
                await asyncio.wait_for(pilot.pause(), timeout=3)
                self.assertFalse(app.busy_only)
                self.assertFalse(app.query_one("#detail-core-busy", Checkbox).value)
                pid = str(os.getpid())
                field.value = pid
                deadline = time.monotonic() + 3
                while app.query_one("#search", Input).value != pid and time.monotonic() < deadline:
                    await pilot.pause()
                self.assertIn(pid, [str(value) for value in table.get_column("pid")])
                self.assertEqual(app.query_one("#search", Input).value, pid)
                await pilot.press("escape", "space", "l")
                self.assertTrue(app.paused)
                self.assertEqual(app.language, "zh-TW")
                self.assertEqual(load_language(), "zh-TW")
                self.assertEqual(app._bindings.key_to_bindings["q"][0].description, "離開")
                frozen = app.snapshot["timestamp"]
                for width, height in ((120, 24), (160, 50), (200, 44)):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause(.4)
                    overview = app.query_one("#overview")
                    top = app.query_one("#overview-top")
                    bottom = app.query_one("#overview-bottom")
                    self.assertEqual(overview.scroll_y, 0)
                    self.assertEqual(bottom.region.bottom, overview.region.bottom)
                    self.assertLess(top.region.bottom, overview.region.bottom)
                    self.assertEqual(table.scroll_x, 0)
                    self.assertEqual(app.snapshot["timestamp"], frozen)
                await pilot.press("l")
                self.assertEqual(load_language(), "en")
                for page in "23456":
                    await pilot.press(page)
                    await pilot.pause()
                    svg = app.export_screenshot()
                    for untranslated in ("離開", "暫停/恢復", "搜尋", "CPU 排序", "記憶體排序", "無權讀取 CPU 功耗"):
                        self.assertNotIn(untranslated, svg)
                # A stopped sampler must not return to a misleading Live state
                # when a user changes pages or language.
                app.stop_sampling.set()
                await pilot.pause(.4)
                app.sampling_error = "test read failure"
                await pilot.press("1")
                await pilot.pause()
                self.assertIn("Could not read system data", str(app.query_one("#top-line").content))
                await pilot.press("q")

    async def test_live_dashboard_and_keyboard(self):
        app = SystemMonitorApp(interval=0.3)
        async with app.run_test(size=(160, 50)) as pilot:
            deadline = time.monotonic() + 10
            while not app.snapshot or app.snapshot["cpu"]["percent"] is None:
                self.assertLess(time.monotonic(), deadline, "No measured CPU sample arrived")
                await pilot.pause(0.1)
            self.assertEqual(len(app.snapshot["cpu"]["per_core"]), app.snapshot["cpu"]["logical"])
            self.assertGreater(app.query_one("#overview-processes", DataTable).row_count, 0)

            for key, page in zip("23456", ("cpu", "processes", "gpu", "storage", "hardware")):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(app.query_one("#content", ContentSwitcher).current, page)
            self.assertEqual(app.query_one("#cpu-detail-table", DataTable).row_count, len(app.snapshot["cpu"]["core_ids"]))

            await pilot.press("/")
            self.assertIsInstance(app.focused, Input)
            query = "no-match-" + uuid.uuid4().hex
            app.query_one("#search", Input).value = query
            await pilot.pause()
            self.assertEqual(app.query_one("#process-table", DataTable).row_count, 0)
            app.query_one("#search", Input).value = str(os.getpid())
            await pilot.pause()
            self.assertIn(str(os.getpid()), app.table_cache["process-table"])
            await pilot.press("escape", "m")
            self.assertEqual(app.process_sort, "memory")
            values = [p[3] for p in app.table_cache["process-table"].values()]
            self.assertGreater(len(values), 0)
            await pilot.press("c")
            self.assertEqual(app.process_sort, "cpu")

            await pilot.press("space")
            frozen = app.snapshot["timestamp"]
            await pilot.pause(0.7)
            self.assertTrue(app.paused)
            self.assertEqual(app.snapshot["timestamp"], frozen)
            self.assertGreater(app.latest["timestamp"], frozen)
            await pilot.press("space")
            self.assertFalse(app.paused)
            self.assertGreater(app.snapshot["timestamp"], frozen)

            await pilot.press("1", "b")
            self.assertTrue(app.hot_first)
            indices = app.core_indices()
            loads = [app.snapshot["cpu"]["per_core"][i] or 0 for i in indices]
            self.assertEqual(loads, sorted(loads, reverse=True))
            await pilot.press("b")

            await pilot.resize_terminal(80, 24)
            await pilot.press("6")
            await pilot.pause()
            self.assertIn("narrow", app.screen.classes)
            self.assertFalse(app.query_one("#summary").display)
            # The memory detail remains available when the summary is hidden.
            hardware = app.query_one("#hardware-detail").content
            labels = [str(cell) for cell in hardware.columns[0]._cells]
            self.assertIn("記憶體 Cache", labels)
            self.assertIn("Swap 已用 / 總計", labels)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.query_one("#cpu-detail-table", DataTable).row_count, len(app.snapshot["cpu"]["core_ids"]))
            await pilot.press("q")
        self.assertTrue(app.stop_sampling.is_set())


if __name__ == "__main__":
    unittest.main()
