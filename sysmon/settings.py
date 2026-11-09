"""儲存介面語言與翻譯取樣狀態。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


LANGUAGES = ("en", "zh-TW")
DEFAULT_LANGUAGE = "zh-TW"


class SettingsError(ValueError):
    def __init__(self, kind: str, path: Path, detail: str) -> None:
        self.kind = kind
        self.path = path
        self.detail = detail
        message = {"read": "Cannot read", "format": "Invalid", "write": "Cannot save"}[kind]
        super().__init__(f"{message} language settings at {path}: {detail}")


def _path() -> Path:
    directory = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(directory) / "vital" / "config.json"


def load_language() -> str:
    path = _path()
    # Keep the language chosen before the sysmon → vital rename.
    if not path.exists():
        path = path.parent.parent / "sysmon" / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_LANGUAGE
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SettingsError("format", path, str(error)) from error
    except OSError as error:
        raise SettingsError("read", path, str(error)) from error
    if not isinstance(data, dict) or data.get("language") not in LANGUAGES:
        raise SettingsError("format", path, 'Expected {"language": "en"} or {"language": "zh-TW"}')
    return data["language"]


def save_language(language: str) -> None:
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language}")
    path = _path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".config-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump({"language": language}, stream)
            stream.write("\n")
        os.replace(temporary, path)
    except OSError as error:
        raise SettingsError("write", path, str(error)) from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


_STATUS_EN = {
    "系統未提供 CPU 功耗感測器": "CPU power sensor unavailable",
    "無權讀取 CPU 功耗": "Permission denied reading CPU power",
    "CPU 功耗感測器讀取失敗": "Cannot read CPU power sensor",
    "等待下一次取樣": "Waiting for next sample",
    "CPU 功耗計數器已重設": "CPU power counter reset",
    "未安裝 NVIDIA GPU 支援（nvidia-ml-py）": "NVIDIA GPU support not installed (nvidia-ml-py)",
    "無法讀取 NVIDIA GPU": "Cannot read NVIDIA GPU",
    "未偵測到 NVIDIA GPU": "No NVIDIA GPU detected",
    "無權讀取": "Permission denied",
    "無法讀取檔案系統": "Cannot read filesystem",
    "/proc/stat 缺少 CPU 計數器": "CPU counters missing from /proc/stat",
}


def translate_status(text: str, language: str) -> str:
    if language != "en" or not text:
        return text
    if "；" in text:
        return "; ".join(translate_status(part, language) for part in text.split("；"))
    if text in _STATUS_EN:
        return _STATUS_EN[text]
    if text.startswith("無法讀取 NVIDIA GPU："):
        return "Cannot read NVIDIA GPU: " + text.split("：", 1)[1]
    match = re.fullmatch(r"GPU (\d+) 無法讀取(?: (C|G|compute|graphics) 程序資料)?", text)
    if match:
        index, kind = match.groups()
        if kind is None:
            return f"Cannot read GPU {index}"
        kind = {"C": "compute", "G": "graphics"}.get(kind, kind)
        return f"Cannot read GPU {index} {kind} processes"
    return text
