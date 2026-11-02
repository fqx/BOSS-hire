"""macOS accessibility transport for the iOS BOSS application."""
import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

from log_utils import logger
from timing_utils import jitter


class NativeDriverError(RuntimeError):
    """A native application operation failed; callers must stop processing."""


class NativeStaleElementError(NativeDriverError):
    """The bridge rejected an outdated target before performing its action."""


class NativeSnapshotInvalidatedError(NativeDriverError):
    """An element disappeared while the bridge was reading a snapshot."""


class NativeNetworkError(NativeDriverError):
    """The client shows a network-failure banner; read-only polling may retry."""


class NativeDailyLimitError(NativeDriverError):
    """The client reached its daily view or communication limit."""


class NativeMarkLimitError(NativeDriverError):
    """The client explicitly rejected an unsuitable mark because of its quota."""


class NativeMarkUnconfirmedError(NativeDriverError):
    """A mark was attempted but its result could not be confirmed."""


@dataclass(frozen=True)
class AXNode:
    path: tuple[int, ...]
    role: str
    title: str
    description: str
    value: str
    rect: dict[str, float]
    actions: tuple[str, ...]


def mark_limit_notice(nodes):
    for node in nodes:
        text = node.description or node.title or node.value
        if node.role in ("AXStaticText", "AXButton") and re.fullmatch(
            r"(?:您)?(?:今日|今天|当日|当天)?(?:的)?标记(?:不合适|不符合|不合格)"
            r"(?:人数|次数|数量|数|的牛人数)?已(?:达(?:到)?|超过)(?:今日|每日)?上限(?:[，,。！!].*)?",
            text,
        ):
            return text
    return None


class NativeDriver:
    ACTIONS = {
        "press": "AXPress",
        "scrollDown": "AXScrollDownByPage",
        "scrollUp": "AXScrollUpByPage",
        "scrollToVisible": "AXScrollToVisible",
    }

    def __init__(self, bundle_id="com.hpbr.bosszhipin", timeout=15):
        self.bundle_id = bundle_id
        self.timeout = timeout
        self._binary = None
        self._build_lock = asyncio.Lock()

    async def _run(self, *args, timeout=None):
        process = await asyncio.create_subprocess_exec(
            *(str(arg) for arg in args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self.timeout if timeout is None else timeout
            )
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            message = stderr.decode("utf-8", errors="replace").strip()
            try:
                details = json.loads(message)
            except json.JSONDecodeError:
                details = None
            if isinstance(details, dict) and details.get("error") in (
                "Stale accessibility path",
                "Stale accessibility element: role or description changed",
            ):
                raise NativeStaleElementError(message)
            if (
                len(args) > 1 and args[1] == "snapshot"
                and isinstance(details, dict) and isinstance(details.get("error"), str)
                and re.fullmatch(r"Cannot read (?:AX[A-Za-z]+|actions): AX error -25202", details["error"])
            ):
                raise NativeSnapshotInvalidatedError(message)
            raise NativeDriverError(message)
        return stdout

    async def _prepare(self):
        if sys.platform != "darwin":
            raise NativeDriverError("Native BOSS automation requires macOS")
        async with self._build_lock:
            if self._binary is not None:
                return self._binary
            source = Path(__file__).with_name("native") / "AccessibilityBridge.swift"
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
            # A private per-process build avoids sharing executable cache files with
            # other users or racing another automation process during compilation.
            self._build_dir = tempfile.TemporaryDirectory(prefix=f"boss-ax-{digest}-")
            binary = Path(self._build_dir.name) / "AccessibilityBridge"
            await self._run(
                "/usr/bin/xcrun", "swiftc", "-O", "-module-cache-path",
                Path(self._build_dir.name) / "module-cache", source, "-o", binary,
                timeout=120,
            )
            self._binary = binary
            return binary

    async def snapshot(self) -> list[AXNode]:
        binary = await self._prepare()
        for attempt in range(3):
            try:
                data = json.loads(await self._run(binary, "snapshot", self.bundle_id))
                break
            except NativeSnapshotInvalidatedError:
                if attempt == 2:
                    raise
                # Discard the entire tree; paths from a partial read are unsafe.
                logger.info("读取期间辅助功能元素失效，重新获取完整快照（第 %s 次重试）", attempt + 1)
                await asyncio.sleep(jitter(0.2))
        if not isinstance(data, list):
            raise NativeDriverError("Accessibility bridge returned an invalid snapshot")
        nodes = []
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), list)
                or any(type(index) is not int or index < 0 for index in item["path"])
                or any(not isinstance(item.get(key), str) for key in ("role", "title", "description", "value"))
                or not isinstance(item.get("actions"), list)
                or any(not isinstance(action, str) for action in item["actions"])
                or not isinstance(item.get("rect"), dict)
                or any(type(item["rect"].get(key)) not in (int, float) for key in ("x", "y", "width", "height"))
                or any(not math.isfinite(item["rect"][key]) for key in ("x", "y", "width", "height"))
            ):
                raise NativeDriverError("Accessibility bridge returned an invalid node")
            nodes.append(AXNode(**{**item, "path": tuple(item["path"]), "actions": tuple(item["actions"])}))
        return nodes

    async def window_info(self) -> dict:
        binary = await self._prepare()
        data = json.loads(await self._run(binary, "window-info", self.bundle_id))
        if (
            not isinstance(data, dict)
            or type(data.get("window_id")) is not int or data["window_id"] <= 0
            or not isinstance(data.get("bounds"), dict)
            or any(type(data["bounds"].get(key)) not in (int, float)
                   or not math.isfinite(data["bounds"][key]) for key in ("x", "y", "width", "height"))
            or data["bounds"]["width"] <= 0 or data["bounds"]["height"] <= 0
        ):
            raise NativeDriverError("Accessibility bridge returned invalid window metadata")
        return data

    async def capture_window(self) -> tuple[bytes, dict[str, float]]:
        """Capture only when explicitly requested for optional visual review."""
        before = await self.window_info()
        with tempfile.TemporaryDirectory(prefix="boss-capture-") as directory:
            output = Path(directory) / "window.png"
            try:
                await self._run("/usr/sbin/screencapture", "-x", "-o", "-l", before["window_id"], output)
            except NativeDriverError as error:
                raise NativeDriverError(
                    f"BOSS 窗口截图失败，请检查窗口可用性及执行程序的屏幕录制权限：{error}"
                ) from error
            after = await self.window_info()
            if after != before:
                raise NativeDriverError("截图期间 BOSS 窗口或位置尺寸发生变化，请重试人工复核")
            png = output.read_bytes()
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise NativeDriverError("BOSS 窗口截图没有返回 PNG 图像")
        return png, before["bounds"]

    async def perform(self, node: AXNode, action: str) -> None:
        action = self.ACTIONS.get(action, action)
        if action not in self.ACTIONS.values() or action not in node.actions:
            raise NativeDriverError(f"Element does not support {action}")
        binary = await self._prepare()
        request = {
            "path": node.path,
            "role": node.role,
            "description": node.description,
            "action": action,
        }
        data = json.loads(await self._run(binary, "action", self.bundle_id, json.dumps(request, ensure_ascii=False)))
        if data != {"ok": True}:
            raise NativeDriverError("Accessibility bridge did not confirm the action")

    async def set_value(self, node: AXNode, text: str) -> None:
        if node.role not in ("AXTextArea", "AXTextField"):
            raise NativeDriverError("set_value requires a text field")
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        binary = await self._prepare()
        request = {
            "path": node.path, "role": node.role, "description": node.description,
            "action": "setValue", "text": text,
        }
        data = json.loads(await self._run(binary, "action", self.bundle_id, json.dumps(request, ensure_ascii=False)))
        if data != {"ok": True}:
            raise NativeDriverError("Accessibility bridge did not confirm the text update")

    async def type_text(self, editor: AXNode, text: str, expected_card_description: str, *, replace_draft: str | None = None) -> str:
        """Replace text once through AX and return verified complete editor text."""
        known_editor = editor.role in ("AXTextArea", "AXTextField", "AXStaticText") or (
            editor.role == "AXGenericElement" and editor.description == "和Ta聊一聊~"
        )
        if not known_editor or "AXPress" not in editor.actions:
            raise NativeDriverError("type_text requires a known pressable chat editor")
        if replace_draft is not None and not isinstance(replace_draft, str):
            raise ValueError("Approved replacement draft must be an exact string")
        # UIKit's snapshot value can omit marked text. An explicit replacement
        # is checked against complete AXStringForRange content in the bridge.
        if editor.value and replace_draft is None:
            raise NativeDriverError("Chat editor already contains a draft; it was not overwritten")
        if not isinstance(text, str) or not text or any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text):
            raise ValueError("type_text requires nonempty plain text without line breaks or control characters")
        if not isinstance(expected_card_description, str) or not expected_card_description:
            raise ValueError("type_text requires the expected chat candidate")
        binary = await self._prepare()
        request = {
            "path": editor.path, "role": editor.role, "description": editor.description,
            "action": "typeText", "text": text, "expected_card_description": expected_card_description,
        }
        if replace_draft is not None:
            request["expected_draft"] = replace_draft
        data = json.loads(await self._run(binary, "action", self.bundle_id, json.dumps(request, ensure_ascii=False)))
        if data != {"ok": True, "text": text}:
            raise NativeDriverError("Accessibility bridge did not verify the complete typed text")
        return data["text"]
