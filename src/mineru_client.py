"""MinerU Open API 客户端。

仅依赖 Python 标准库：不引入 httpx / requests，便于打包成体积最小的单文件 exe。

调用链与官方 mineru-open-sdk 一致：
    POST /file-urls/batch  ->  batch_id + 预签名上传地址
    PUT  <预签名地址>       ->  直传文件字节（不设 Content-Type）
    GET  /extract-results/batch/{batch_id}  ->  轮询状态
    GET  <full_zip_url>    ->  下载结果压缩包，取 full.md 与 *_content_list.json
"""

from __future__ import annotations

import http.client
import io
import json
import os
import ssl
import struct
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Sequence

API_BASE = "https://mineru.net/api/v4"
FLASH_BASE = "https://mineru.net/api/v1/agent"
USER_AGENT = "MinerU-Lite/1.1"

SUPPORTED_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp",
)

# MinerU 官方限制
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_PAGES = 200
MAX_FILES_PER_BATCH = 50

# 免费模式（无 API Key）的限制
FLASH_MAX_BYTES = 10 * 1024 * 1024
FLASH_MAX_PAGES = 20

FILE_DIALOG_FILTER = "支持的文件|*.pdf;*.doc;*.docx;*.ppt;*.pptx;*.xls;*.xlsx;*.png;*.jpg;*.jpeg;*.jp2;*.webp;*.gif;*.bmp"

# 官方错误码 -> 人话
ERROR_HINTS: Dict[int, str] = {
    -60001: "请求参数有误",
    -60002: "文件格式不支持，或文件名缺少正确的后缀",
    -60003: "文件读取失败，请重新上传",
    -60004: "文件内容为空",
    -60005: "文件超过 200 MB 上限",
    -60006: "页数超过 200 页上限，请先用页码范围拆分",
    -60007: "PDF 文件已加密，无法解析",
    -60008: "文件解析失败，可稍后重试",
    -60009: "文件页数超出限制",
    -60010: "解析超时",
    -60011: "文件未成功上传，请重试",
    -60012: "任务不存在或已过期",
    -60013: "文件大小超过限制",
    -60014: "页码格式错误",
    -60015: "文件未找到",
    -60016: "不支持的模型版本",
    -60017: "任务已取消",
    -60018: "今日解析额度已用完，请明日再试",
    -60019: "并发任务数超限，请稍后重试",
    -60020: "账号权限不足，请确认套餐状态",
    -60021: "每日上传文件数已达上限",
}

STATE_TEXT = {
    "waiting-file": "等待上传",
    "pending": "排队中",
    "running": "解析中",
    "converting": "转换格式",
    "done": "完成",
    "failed": "失败",
}


class MineruError(Exception):
    """带错误码的接口异常。"""

    def __init__(self, message: str, code: Optional[int] = None, trace_id: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.trace_id = trace_id


class Cancelled(Exception):
    """用户主动取消。"""


def describe_code(code: Optional[int], fallback: str = "") -> str:
    if code is None:
        return fallback or "未知错误"
    hint = ERROR_HINTS.get(int(code))
    if hint:
        return f"{hint}（错误码 {code}）" if fallback in ("", "unknown error") else f"{hint}（{fallback}，错误码 {code}）"
    return f"{fallback or '接口返回错误'}（错误码 {code}）"


# --------------------------------------------------------------------------- #
# 基础请求
# --------------------------------------------------------------------------- #

def _api_request(
    method: str,
    path: str,
    token: str,
    payload: Optional[dict] = None,
    timeout: float = 60.0,
    retries: int = 2,
) -> dict:
    """向 MinerU API 发一次请求，返回 data 字段。自动重试网络类错误。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    url = API_BASE + path
    last_exc: Optional[Exception] = None

    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if exc.code in (401, 403):
                raise MineruError("API Key 无效或已过期，请在设置中重新填写", code=exc.code) from None
            if exc.code >= 500 and attempt < retries:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                parsed = json.loads(raw)
                raise MineruError(
                    describe_code(parsed.get("code"), parsed.get("msg", "")),
                    code=parsed.get("code"),
                    trace_id=parsed.get("trace_id", ""),
                ) from None
            except json.JSONDecodeError:
                raise MineruError(f"服务返回 HTTP {exc.code}，请稍后重试") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt < retries:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            raise MineruError(f"网络连接失败：{getattr(exc, 'reason', exc)}") from None

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise MineruError("服务返回了非 JSON 内容，请稍后重试") from None

        code = parsed.get("code", 0)
        if code != 0:
            raise MineruError(
                describe_code(code, parsed.get("msg", "")),
                code=code,
                trace_id=parsed.get("trace_id", ""),
            )
        return parsed.get("data") or {}

    raise MineruError(f"网络连接失败：{last_exc}")


def verify_token(token: str, timeout: float = 20.0) -> None:
    """用一次必然失败的任务查询来判断鉴权是否通过。"""
    if not token.strip():
        raise MineruError("请先填写 API Key")
    try:
        _api_request(
            "GET",
            "/extract-results/batch/00000000-0000-0000-0000-000000000000",
            token,
            timeout=timeout,
            retries=0,
        )
    except MineruError as exc:
        # -60012 说明鉴权已通过，只是任务不存在
        if exc.code == -60012:
            return
        raise


# --------------------------------------------------------------------------- #
# 上传与轮询
# --------------------------------------------------------------------------- #

def create_batch(
    token: str,
    names: Sequence[str],
    data_ids: Sequence[str],
    model: str = "vlm",
    language: Optional[str] = "ch",
    ocr: Optional[bool] = None,
    page_ranges: Optional[Sequence[Optional[str]]] = None,
    enable_formula: bool = True,
    enable_table: bool = True,
) -> tuple[str, List[str]]:
    """申请上传链接，返回 (batch_id, 预签名地址列表)。"""
    files = []
    for index, name in enumerate(names):
        entry: dict = {"name": name, "data_id": data_ids[index]}
        if ocr is not None:
            entry["is_ocr"] = bool(ocr)
        if page_ranges and index < len(page_ranges) and page_ranges[index]:
            entry["page_ranges"] = page_ranges[index]
        files.append(entry)

    payload: dict = {"files": files, "model_version": model}
    if model != "MinerU-HTML":
        payload["language"] = language or "ch"
        payload["enable_formula"] = bool(enable_formula)
        payload["enable_table"] = bool(enable_table)

    data = _api_request("POST", "/file-urls/batch", token, payload=payload, timeout=90)
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    if not batch_id or not urls:
        raise MineruError("服务未返回上传地址，请稍后重试")
    return batch_id, list(urls)


def upload_file(
    presigned_url: str,
    path: Path,
    on_progress: Optional[Callable[[int, int], None]] = None,
    cancel_event=None,
) -> None:
    """把文件流式 PUT 到预签名地址。按官方要求不设置 Content-Type。"""
    parts = urllib.parse.urlsplit(presigned_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise MineruError("上传地址无效")

    target = parts.path + (("?" + parts.query) if parts.query else "")
    total = os.path.getsize(path)
    connection_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = connection_cls(parts.netloc, timeout=300)
    if parts.scheme == "https":
        conn.context = ssl.create_default_context()

    try:
        conn.putrequest("PUT", target, skip_accept_encoding=True)
        conn.putheader("Content-Length", str(total))
        conn.putheader("User-Agent", USER_AGENT)
        conn.endheaders()

        sent = 0
        with open(path, "rb") as handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if on_progress:
                    on_progress(sent, total)

        response = conn.getresponse()
        payload = response.read()
        if response.status // 100 != 2:
            detail = payload[:200].decode("utf-8", "replace")
            raise MineruError(f"上传失败（HTTP {response.status}）{detail}")
    except (http.client.HTTPException, OSError) as exc:
        raise MineruError(f"上传中断：{exc}") from None
    finally:
        conn.close()


def get_batch(token: str, batch_id: str) -> List[dict]:
    """查询批任务，返回规整后的结果列表。"""
    data = _api_request("GET", f"/extract-results/batch/{batch_id}", token, timeout=60)
    raw_items = data.get("extract_result") or []
    results: List[dict] = []
    for item in raw_items:
        progress = item.get("extract_progress") or {}
        results.append(
            {
                "file_name": item.get("file_name", ""),
                "data_id": item.get("data_id", ""),
                "state": item.get("state", ""),
                "error": item.get("err_msg") or "",
                "zip_url": item.get("full_zip_url") or "",
                "extracted_pages": int(progress.get("extracted_pages") or 0),
                "total_pages": int(progress.get("total_pages") or 0),
            }
        )
    return results


# --------------------------------------------------------------------------- #
# 免费模式（Flash）：无需 API Key，单文件、≤10 MB、≤20 页、只有 Markdown
# --------------------------------------------------------------------------- #

def _flash_request(method: str, path: str, payload: Optional[dict] = None, timeout: float = 60) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        FLASH_BASE + path, data=body, method=method,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise MineruError(f"免费模式请求失败（HTTP {exc.code}）") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MineruError(f"网络连接失败：{getattr(exc, 'reason', exc)}") from None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise MineruError("免费模式返回了非 JSON 内容") from None

    code = parsed.get("code", 0)
    if code != 0:
        raise MineruError(describe_code(code, parsed.get("msg", "")), code=code)
    return parsed.get("data") or {}


def flash_extract(
    path: Path,
    language: str = "ch",
    ocr: Optional[bool] = None,
    on_state: Optional[Callable[[str], None]] = None,
    cancel_event=None,
    timeout: int = 900,
) -> str:
    """走免费接口解析单个文件，返回 Markdown 文本。"""
    payload: dict = {"file_name": path.name, "language": language or "ch"}
    if ocr is not None:
        payload["is_ocr"] = bool(ocr)

    data = _flash_request("POST", "/parse/file", payload)
    task_id = data.get("task_id")
    file_url = data.get("file_url")
    if not task_id or not file_url:
        raise MineruError("免费模式未返回上传地址")

    upload_file(file_url, path, cancel_event=cancel_event)

    deadline = time.time() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        if time.time() > deadline:
            raise MineruError("免费模式解析超时")

        info = _flash_request("GET", f"/parse/{task_id}")
        state = info.get("state", "")
        if on_state:
            on_state(state)
        if state == "done":
            markdown_url = info.get("markdown_url")
            if not markdown_url:
                raise MineruError("免费模式没有返回 Markdown")
            request = urllib.request.Request(markdown_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read().decode("utf-8", "replace")
        if state == "failed":
            raise MineruError(info.get("err_msg") or "解析失败")
        time.sleep(3)


def download_zip(url: str, dest: Path, cancel_event=None, on_progress=None) -> Path:
    """下载结果压缩包到本地临时文件。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=300) as response, open(dest, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise Cancelled()
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MineruError(f"结果下载失败：{exc}") from None
    return dest


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #

class ZipContents:
    """从结果压缩包里取出的内容。"""

    __slots__ = ("markdown", "content_list", "md_member", "json_member")

    def __init__(self, markdown: Optional[str], content_list: Optional[str],
                 md_member: str = "", json_member: str = ""):
        self.markdown = markdown
        self.content_list = content_list
        self.md_member = md_member
        self.json_member = json_member


def read_zip(zip_path: Path) -> ZipContents:
    """读取 full.md 与 *_content_list.json（只取这两个成员，不落盘解压）。"""
    md_text: Optional[str] = None
    json_text: Optional[str] = None
    md_member = ""
    json_member = ""

    with zipfile.ZipFile(zip_path) as archive:
        members = [info.filename for info in archive.infolist() if not info.is_dir()]

        md_candidates = [m for m in members if PurePosixPath(m).name.lower() == "full.md"]
        if not md_candidates:
            md_candidates = [m for m in members if m.lower().endswith(".md")]

        json_candidates = [m for m in members if PurePosixPath(m).name.lower().endswith("_content_list.json")]
        if not json_candidates:
            json_candidates = [m for m in members if PurePosixPath(m).name.lower() == "content_list.json"]

        if md_candidates:
            md_member = md_candidates[0]
            md_text = archive.read(md_member).decode("utf-8", "replace")

        if json_candidates:
            json_member = json_candidates[0]
            raw = archive.read(json_member).decode("utf-8", "replace")
            # 重新缩进，便于人工阅读与 diff
            try:
                json_text = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                json_text = raw

    return ZipContents(md_text, json_text, md_member, json_member)


class _RangeUnsupported(Exception):
    """服务端不支持按字节范围读取，需要回退为整包下载。"""


def _range_get(url: str, start: int, end: int, timeout: float = 120) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 206:
            raise _RangeUnsupported()
        return response.read()


def _tail_get(url: str, length: int = 70_000) -> tuple[bytes, int]:
    """取文件末尾若干字节，并顺带拿到总大小（省掉一次 HEAD 请求）。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Range": f"bytes=-{length}"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 206:
            raise _RangeUnsupported()
        data = response.read()
        content_range = response.headers.get("Content-Range") or ""
    total = 0
    if "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            total = 0
    return data, total


def _zip_members(url: str) -> list[dict]:
    """只用两次范围请求读出 zip 的中央目录。"""
    tail, size = _tail_get(url)
    if size <= 0:
        raise _RangeUnsupported()

    index = tail.rfind(b"PK\x05\x06")
    if index < 0:
        raise _RangeUnsupported()

    entries = struct.unpack_from("<H", tail, index + 10)[0]
    cd_size, cd_offset = struct.unpack_from("<II", tail, index + 12)
    if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        raise _RangeUnsupported()

    directory = _range_get(url, cd_offset, cd_offset + cd_size - 1)
    members: list[dict] = []
    cursor = 0
    while True:
        index = directory.find(b"PK\x01\x02", cursor)
        if index < 0:
            break
        method = struct.unpack_from("<H", directory, index + 10)[0]
        comp_size = struct.unpack_from("<I", directory, index + 20)[0]
        name_len = struct.unpack_from("<H", directory, index + 28)[0]
        extra_len = struct.unpack_from("<H", directory, index + 30)[0]
        comment_len = struct.unpack_from("<H", directory, index + 32)[0]
        local_offset = struct.unpack_from("<I", directory, index + 42)[0]
        name = directory[index + 46:index + 46 + name_len].decode("utf-8", "replace")
        if comp_size == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
            raise _RangeUnsupported()
        members.append(
            {"name": name, "method": method, "comp_size": comp_size, "offset": local_offset}
        )
        cursor = index + 46 + name_len + extra_len + comment_len
    return members


def _read_member(url: str, member: dict) -> bytes:
    header = _range_get(url, member["offset"], member["offset"] + 29)
    if header[:4] != b"PK\x03\x04":
        raise _RangeUnsupported()
    name_len, extra_len = struct.unpack_from("<HH", header, 26)
    data_start = member["offset"] + 30 + name_len + extra_len

    if member["comp_size"] == 0:
        return b""
    raw = _range_get(url, data_start, data_start + member["comp_size"] - 1)

    if member["method"] == 0:
        return raw
    if member["method"] == 8:
        return zlib.decompress(raw, -15)
    raise _RangeUnsupported()


def _pick_members(members: list[dict]) -> tuple[dict | None, dict | None]:
    names = [m["name"] for m in members if not m["name"].endswith("/")]

    md_name = next((n for n in names if PurePosixPath(n).name.lower() == "full.md"), "")
    if not md_name:
        md_name = next((n for n in names if n.lower().endswith(".md")), "")

    json_name = next(
        (n for n in names if PurePosixPath(n).name.lower().endswith("_content_list.json")), ""
    )
    if not json_name:
        json_name = next(
            (n for n in names if PurePosixPath(n).name.lower() == "content_list.json"), ""
        )

    by_name = {m["name"]: m for m in members}
    return (by_name.get(md_name) if md_name else None, by_name.get(json_name) if json_name else None)


def fetch_outputs(
    url: str,
    need_md: bool = True,
    need_json: bool = False,
    on_progress=None,
    cancel_event=None,
) -> ZipContents:
    """只取 full.md 与 content_list.json。

    结果包里通常还塞着 origin.pdf、layout.pdf 和整页图片，整包动辄几十上百 MB。
    这里按 zip 中央目录定位成员，用 HTTP Range 只抓需要的两段字节；
    服务端不支持范围请求时，回退为整包下载。
    """
    try:
        members = _zip_members(url)
        md_member, json_member = _pick_members(members)
        if need_md and md_member is None:
            raise _RangeUnsupported()
        if need_json and json_member is None:
            raise _RangeUnsupported()

        md_text = None
        if need_md and md_member is not None:
            md_text = _read_member(url, md_member).decode("utf-8", "replace")

        json_text = None
        if need_json and json_member is not None:
            raw = _read_member(url, json_member).decode("utf-8", "replace")
            try:
                json_text = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                json_text = raw

        return ZipContents(
            md_text,
            json_text,
            md_member["name"] if md_member else "",
            json_member["name"] if json_member else "",
        )
    except (_RangeUnsupported, zipfile.BadZipFile, KeyError, ValueError, struct.error, zlib.error):
        pass
    except MineruError:
        # 网络问题导致的失败同样回退，整包下载往往更稳
        pass

    with tempfile.TemporaryDirectory(prefix="mineru-lite-") as tmp:
        zip_path = Path(tmp) / "result.zip"
        download_zip(url, zip_path, cancel_event=cancel_event, on_progress=on_progress)
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        return read_zip(zip_path)
