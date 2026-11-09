"""命令列入口與定時取樣。"""

from __future__ import annotations

import argparse
from datetime import datetime
from functools import partial
import json
import math
import os
import sys
import time
from typing import Any

from rich.console import Console
from rich.table import Table

from . import __version__
from .settings import DEFAULT_LANGUAGE, LANGUAGES, SettingsError, load_language, save_language, translate_status


def _text(language: str, zh: str, en: str) -> str:
    return en if language == "en" else zh


class _ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *, language: str, **kwargs: Any) -> None:
        self.language = language
        super().__init__(**kwargs)

    def format_help(self) -> str:
        text = super().format_help()
        return text if self.language == "en" else text.replace("usage: ", "用法：", 1).replace("options:\n", "選項：\n")

    def format_usage(self) -> str:
        text = super().format_usage()
        return text if self.language == "en" else text.replace("usage: ", "用法：", 1)

    def error(self, message: str) -> None:
        if self.language != "en":
            for original, translated in (
                ("unrecognized arguments:", "無法辨識的參數："),
                ("argument ", "參數 "),
                ("expected one argument", "需要一個值"),
                ("invalid choice:", "無效選項："),
                ("choose from", "可選"),
                ("not allowed with", "無法同時使用"),
                ("ignored explicit argument", "不接受額外的值"),
            ):
                message = message.replace(original, translated)
            if message.startswith("參數 "):
                message = message.replace(": ", "：", 1)
        self.print_usage(sys.stderr)
        label = _text(self.language, "錯誤", "error")
        separator = _text(self.language, "：", ": ")
        self.exit(2, f"{self.prog}{separator}{label}{separator}{message}\n")


def _interval(value: str, language: str = DEFAULT_LANGUAGE) -> float:
    message = _text(language, "間隔必須是至少 0.2 秒的有限數字", "interval must be a finite number of at least 0.2 seconds")
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if not math.isfinite(number) or number < 0.2:
        raise argparse.ArgumentTypeError(message)
    return number


def _sample_count(value: str, language: str = DEFAULT_LANGUAGE) -> int:
    message = _text(language, "取樣次數必須是正整數", "sample count must be a positive integer")
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(message) from None
    if number < 1:
        raise argparse.ArgumentTypeError(message)
    return number


def _parser(language: str = DEFAULT_LANGUAGE) -> argparse.ArgumentParser:
    t = partial(_text, language)
    parser = _ArgumentParser(
        language=language, prog="vital",
        description=t("CPU、GPU、記憶體、磁碟與程序監視器", "CPU, GPU, memory, disk and process monitor"),
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help=t("顯示說明", "show help"))
    parser.add_argument(
        "--version", action="version", version=f"vital {__version__}", help=t("顯示版本", "show version"),
    )
    parser.add_argument(
        "-i", "--interval", type=partial(_interval, language=language), default=1.0,
        metavar=t("秒", "SECONDS"),
        help=t("更新／取樣間隔（預設：1，最短：0.2）", "refresh/sample interval (default: 1, minimum: 0.2)"),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help=t("輸出一次文字摘要", "print one summary"))
    mode.add_argument(
        "-n", "--samples", type=partial(_sample_count, language=language), metavar=t("次數", "COUNT"),
        help=t("連續取樣，包含每顆 CPU 核心的使用率", "take consecutive samples with per-core CPU usage"),
    )
    parser.add_argument(
        "--json", action="store_true",
        help=t("輸出 JSON；搭配 --samples 時每行一筆", "print JSON; one line per sample with --samples"),
    )
    parser.add_argument(
        "--lang", choices=LANGUAGES,
        help=t("介面語言，啟動時記住選擇", "interface language; remember this choice on launch"),
    )
    return parser


def _bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(amount) < 1024 or unit == "PiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("unreachable")


def _number(value: int | float | None, unit: str = "") -> str:
    return "—" if value is None else f"{value:.1f}{unit}"


def print_snapshot(
    snapshot: dict[str, Any], *, per_core: bool, console: Console, language: str = DEFAULT_LANGUAGE,
) -> None:
    """輸出一次量測；連續取樣另列每顆核心。"""
    cpu, memory = snapshot["cpu"], snapshot["memory"]
    t = partial(_text, language)
    swap = memory["swap"]
    stamp = datetime.fromtimestamp(snapshot["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    console.print(f"{snapshot['hostname']}  {stamp}", style="bold cyan")
    console.print(
        f"CPU {_number(cpu['percent'], '%')}  "
        f"{cpu['physical'] or '—'} {t('核心', 'cores')} / {cpu['logical']} {t('執行緒', 'threads')}  "
        f"{_number(cpu['frequency_mhz'], ' MHz')}  "
        f"{t('溫度', 'Temp')} {_number(cpu['temperature'], ' °C')}  "
        f"{t('功率', 'Power')} {_number(cpu['power_watts'], ' W')}"
    )
    if cpu["power_watts"] is None and cpu["power_status"]:
        console.print(translate_status(cpu["power_status"], language), style="dim")
    console.print(
        f"Load {' / '.join(f'{value:.2f}' for value in cpu['load'])}  "
        f"{t('程序', 'Processes')} {len(snapshot['processes'])}"
    )
    console.print(
        f"RAM {_bytes(memory['used'])} / {_bytes(memory['total'])} "
        f"({_number(memory['percent'], '%')})  {t('可用', 'Available')} {_bytes(memory['available'])}"
    )
    console.print(
        f"Cache {_bytes(memory['cached'])}  Buffers {_bytes(memory['buffers'])}  "
        f"Swap {_bytes(swap['used'])} / {_bytes(swap['total'])} "
        f"({_number(swap['percent'], '%')})"
    )

    if per_core:
        table = Table("CPU", "%usr", "%sys", "%iowait", "%idle", "%busy", box=None)
        for column in table.columns[1:]:
            column.justify = "right"
        rows = [("all", cpu["times"], cpu["percent"])]
        rows.extend(
            (str(cpu_id), times, busy)
            for cpu_id, times, busy in zip(cpu["core_ids"], cpu["per_core_times"], cpu["per_core"])
        )
        for name, times, busy in rows:
            table.add_row(
                name, *(_number(times[key]) for key in ("user", "system", "iowait", "idle")),
                _number(busy),
            )
        console.print(table)

    for gpu in snapshot["gpus"]:
        console.print(
            f"GPU {gpu['index']} · {gpu['name']}  {_number(gpu['utilization'], '%')}"
        )
        console.print(
            f"  VRAM {_bytes(gpu['memory_used'])} / {_bytes(gpu['memory_total'])}  "
            f"{_number(gpu['temperature'], ' °C')}  {_number(gpu['power_watts'], ' W')}"
        )
    if snapshot["gpu_status"]:
        console.print(translate_status(snapshot["gpu_status"], language), style="dim")

    if snapshot["disks"]:
        table = Table(
            t("磁碟", "Disk"), t("掛載點", "Mount"), t("已用", "Used"), t("總量", "Total"),
            t("可用", "Free"), t("使用率", "Usage"), box=None,
        )
        for row in snapshot["disks"]:
            table.add_row(
                row["device"], row["mountpoint"], _bytes(row["used"]),
                _bytes(row["total"]), _bytes(row["free"]),
                translate_status(row["error"], language) if row.get("error") else _number(row["percent"], "%"),
            )
        console.print(table)
    if snapshot["disk_io"]:
        table = Table(t("磁碟 I/O", "Disk I/O"), t("讀取 /s", "Read /s"), t("寫入 /s", "Write /s"), t("忙碌", "Busy"), box=None)
        for row in snapshot["disk_io"]:
            table.add_row(
                row["name"], _bytes(row["read_rate"]), _bytes(row["write_rate"]),
                _number(row["busy_percent"], "%"),
            )
        console.print(table)
    interfaces = [row for row in snapshot["network"] if row["up"]]
    if interfaces:
        table = Table(t("網路", "Network"), t("接收 /s", "Receive /s"), t("傳送 /s", "Send /s"), box=None)
        for row in interfaces:
            table.add_row(row["name"], _bytes(row["recv_rate"]), _bytes(row["send_rate"]))
        console.print(table)


def _sample(args: argparse.Namespace, language: str) -> int:
    from .collector import Collector

    collector = Collector()
    console = Console(highlight=False, markup=False)
    try:
        deadline = time.monotonic() + args.interval
        collector.sample()
        for index in range(args.samples or 1):
            time.sleep(max(0, deadline - time.monotonic()))
            snapshot = collector.sample()
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, allow_nan=False), flush=True)
            else:
                if index:
                    console.print()
                print_snapshot(snapshot, per_core=args.samples is not None, console=console, language=language)
            deadline += args.interval
    finally:
        collector.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    language_parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    language_parser.add_argument("--lang", choices=LANGUAGES)
    try:
        explicit_language = language_parser.parse_known_args(argv)[0].lang
    except argparse.ArgumentError:
        # The full parser reports invalid language arguments in the saved language.
        explicit_language = None
    language = explicit_language or DEFAULT_LANGUAGE
    try:
        if explicit_language is None:
            language = load_language()
        parser = _parser(language)
        args = parser.parse_args(argv)
        if args.lang is not None:
            save_language(language)
    except SettingsError as error:
        label = {
            "read": _text(language, "無法讀取語言設定", "Cannot read language settings"),
            "format": _text(language, "語言設定格式錯誤", "Invalid language settings"),
            "write": _text(language, "無法儲存語言設定", "Cannot save language settings"),
        }[error.kind]
        separator = _text(language, "：", ": ")
        print(f"{label}{separator}{error.path}{separator}{error.detail}", file=sys.stderr)
        if error.kind == "format":
            print(_text(language, "請用 --lang en 或 --lang zh-TW 重新設定。", "Use --lang en or --lang zh-TW to reset."), file=sys.stderr)
        return 2
    try:
        if args.once or args.json or args.samples is not None:
            return _sample(args, language)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            parser.exit(2, _text(
                language, "即時介面需要終端機，請改用 --once、--samples 或 --json。\n",
                "Live view requires a terminal. Use --once, --samples or --json.\n",
            ))
        os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")
        from .app import SystemMonitorApp

        SystemMonitorApp(interval=args.interval, language=language).run()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # A consumer such as `head` may finish before a sampling stream does.
        with open(os.devnull, "w") as null:
            os.dup2(null.fileno(), sys.stdout.fileno())
        return 0
    return 0
