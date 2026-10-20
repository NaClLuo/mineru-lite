"""配置读写：API Key、默认输出目录与上次使用的参数。"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "MinerU Lite"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
ERROR_LOG = CONFIG_DIR / "error.log"

DEFAULTS = {
    "api_key": "",
    "output_dir": str(Path.home() / "Documents" / "MinerU"),
    "out_mode": "source",   # source | subfolder | custom
    "fmt": "md",            # md | json | both
    "model": "vlm",         # vlm | pipeline
    "language": "ch",
    "ocr": "auto",          # auto | on | off
    "pages": "",
    "formula": True,
    "table": True,
    "workers": 4,
}

# 首次启动时可以从这些位置尝试导入已有的 API Key
_TOKEN_SOURCES = (
    Path.home() / ".workbuddy" / "mcp.json",
    Path.home() / ".cursor" / "mcp.json",
    Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
)


def load() -> dict:
    """读取配置，缺失字段用默认值补齐。"""
    data = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, json.JSONDecodeError):
        stored = {}

    if isinstance(stored, dict):
        if "out_mode" not in stored and "to_source_dir" in stored:
            stored["out_mode"] = "source" if stored.get("to_source_dir") else "custom"
        data.update({k: v for k, v in stored.items() if k in DEFAULTS})

    if not data.get("api_key"):
        imported = discover_token()
        if imported:
            data["api_key"] = imported
            try:
                save(data)
            except OSError:
                pass
    return data


def save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {key: data.get(key, DEFAULTS[key]) for key in DEFAULTS}
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def discover_token() -> str:
    """从本机已有的 MCP 配置中找一个 MinerU Token，省去手动填写。"""
    for path in _TOKEN_SOURCES:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        servers = (data or {}).get("mcpServers") or (data or {}).get("mcp_servers") or {}
        if not isinstance(servers, dict):
            continue
        fallback = ""
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            env = entry.get("env") or {}
            token = (env.get("MINERU_API_TOKEN") or env.get("MINERU_TOKEN") or "").strip()
            if not token:
                continue
            if "mineru" in str(name).lower():
                return token
            fallback = fallback or token
        if fallback:
            return fallback
    env_token = os.environ.get("MINERU_API_TOKEN") or os.environ.get("MINERU_TOKEN")
    return (env_token or "").strip()


def write_error_log(text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass
