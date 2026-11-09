"""Exercise the real collector and terminal interface together."""

import asyncio
from copy import deepcopy
import os
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

from rich.cells import cell_len

from textual.widgets import Checkbox, ContentSwitcher, DataTable, Input, Tab

from sysmon.app import SystemMonitorApp
from sysmon.settings import load_language
from scripts.capture_demo import DemoApp, demo_snapshot


def save_screenshot(app, name, directory):
    path = app.save_screenshot(name, directory)
    # Rich 15's SVG exporter uses len(text) for textLength. Correct CJK
    # glyph widths to the same terminal-cell widths used by the live UI.
    tree = ET.parse(path)
    for element in tree.iter():
        if element.tag.endswith("text") and "textLength" in element.attrib:
            text = "".join(element.itertext())
            if text:
                element.set("textLength", str(float(element.get("textLength")) * cell_len(text) / len(text)))
                element.set("lengthAdjust", "spacingAndGlyphs")
        if element.tag.endswith("style") and element.text:
            element.text = element.text.replace("font-family: Fira Code, monospace;", 'font-family: "DejaVu Sans Mono", "Noto Sans Mono CJK TC", monospace;')
    tree.write(path, encoding="unicode")


class AppTests(unittest.IsolatedAsyncioTestCase):
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
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#gpu-processes", DataTable)
            self.assertEqual(table.row_count, 4)
            self.assertGreater(table.columns[next(key for key in table.columns if key.value == "command")].width, 24)
            self.assertEqual(table.max_scroll_x, 0)
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
            self.assertEqual(table.frozen_columns, 1)
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
            self.assertEqual(str(app.query_one("#gpu-command").content), "No GPU processes")
            await pilot.press("ctrl+c")

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
                self.assertEqual(table.max_scroll_x, 0)
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
                app.query_one("#overview-search", Input).value = str(os.getpid())
                await pilot.pause()
                self.assertIn(str(os.getpid()), app.table_cache["overview-processes"])
                self.assertEqual(app.query_one("#search", Input).value, str(os.getpid()))
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
                    self.assertEqual(table.max_scroll_x, 0)
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
            await pilot.pause()
            capture = os.environ.get("SYSMON_SCREENSHOTS")
            if capture:
                Path(capture).mkdir(parents=True, exist_ok=True)
                save_screenshot(app, "overview.svg", capture)

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
            if capture:
                save_screenshot(app, "compact-cpu.svg", capture)
            await pilot.press("q")
        self.assertTrue(app.stop_sampling.is_set())


if __name__ == "__main__":
    unittest.main()
