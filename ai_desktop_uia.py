"""Windows UI Automation (UIA) behind the accessibility commands: `ui`, `click --ref/--name`,
the accessibility step of `click --target`, `set-text` and `read`.

It answers the same two requests as the AT-SPI helper on Linux (ai_desktop_helper.py),
`a11y-tree` and `a11y-do`, in the same shapes, so references (e12), the snapshot file, the
stale-reference check and `via=accessibility` are all shared code in ai_desktop_ui.py. UIA
control types are reported with the AT-SPI role names (`push button`, `entry`, `link`...)
so `--role` means the same on both systems.

It talks to UIAutomationCore through comtypes (the `windows` extra) rather than the
`uiautomation` package: comtypes exposes IUIAutomation itself, including cache requests,
so one cross-process call returns a whole level of children with every property needed.
`uiautomation` is a large wrapper over comtypes that reads properties one call at a time.

The COM part is the small `ComApi` class; the walking, filtering, roles and actions work on
any object with the same methods, which is how the tests run on Linux and macOS.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import threading
import time

import ai_desktop as _ai_desktop

AD = _ai_desktop
MAX_NODES = 4000
MAX_DEPTH = 40
MAX_CHILDREN = 500
MIN_SIZE = 3  # px; anything thinner can't be hit reliably by a real click
CACHE_FILE = "uia-cache.json"
CACHE_MAX_AGE = 60.0  # s; the tree is also re-read whenever the screen changes
CHROMIUM_WINDOW = "Chrome_WidgetWin_"  # Chrome, Edge, Electron apps
WEB_WINDOWS = (CHROMIUM_WINDOW, "MozillaWindowClass")
CHROMIUM_RETRIES = 2  # Chromium builds its page tree after the first UIA request
CHROMIUM_RETRY_WAIT = 0.5

# UIA control type (programmatic name without UIA_..ControlTypeId) -> AT-SPI role name.
ROLES = {
    "AppBar": "tool bar", "Button": "push button", "Calendar": "calendar", "CheckBox": "check box",
    "ComboBox": "combo box", "Custom": "unknown", "DataGrid": "table", "DataItem": "table row",
    "Edit": "entry", "Group": "grouping", "Header": "header", "HeaderItem": "column header",
    "Hyperlink": "link", "Image": "image", "List": "list", "ListItem": "list item", "Menu": "menu",
    "MenuBar": "menu bar", "MenuItem": "menu item", "Pane": "panel", "ProgressBar": "progress bar",
    "RadioButton": "radio button", "ScrollBar": "scroll bar", "SemanticZoom": "panel",
    "Separator": "separator", "Slider": "slider", "Spinner": "spin button", "SplitButton": "push button",
    "StatusBar": "status bar", "Tab": "page tab list", "TabItem": "page tab", "Table": "table",
    "Text": "label", "Thumb": "thumb", "TitleBar": "title bar", "ToolBar": "tool bar",
    "ToolTip": "tool tip", "Tree": "tree", "TreeItem": "tree item", "Window": "frame",
}
# The same list as the AT-SPI helper: roles worth showing an agent even without a name.
ACTIONABLE_ROLES = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "link", "combo box", "entry", "password text", "text", "slider", "spin button",
    "page tab", "list item", "tree item", "table cell", "icon", "menu", "tool bar", "button",
}
TEXT_ROLES = ("entry", "text", "spin button", "combo box")  # their value is listed (never a password)
ACTIONS = ("invoke", "toggle", "select", "expand")  # patterns reported as an element's actions
EXPANDED, PARTIALLY_EXPANDED = 1, 2
TOGGLE_ON = 1

# Property ids, looked up by name in the generated UIAutomationClient module; the numbers
# are the documented values, used if a name is missing there.
PROPERTY_IDS = {
    "type": ("UIA_ControlTypePropertyId", 30003),
    "name": ("UIA_NamePropertyId", 30005),
    "rect": ("UIA_BoundingRectanglePropertyId", 30001),
    "offscreen": ("UIA_IsOffscreenPropertyId", 30022),
    "enabled": ("UIA_IsEnabledPropertyId", 30010),
    "focused": ("UIA_HasKeyboardFocusPropertyId", 30008),
    "password": ("UIA_IsPasswordPropertyId", 30019),
    "framework": ("UIA_FrameworkIdPropertyId", 30024),
    "class_name": ("UIA_ClassNamePropertyId", 30012),
    "pid": ("UIA_ProcessIdPropertyId", 30002),
    "hwnd": ("UIA_NativeWindowHandlePropertyId", 30020),
    "value": ("UIA_ValueValuePropertyId", 30045),
    "readonly": ("UIA_ValueIsReadOnlyPropertyId", 30046),
    "toggle": ("UIA_ToggleToggleStatePropertyId", 30086),
    "selected": ("UIA_SelectionItemIsSelectedPropertyId", 30079),
    "expand": ("UIA_ExpandCollapseExpandCollapseStatePropertyId", 30070),
}
PATTERN_AVAILABLE = {
    "invoke": ("UIA_IsInvokePatternAvailablePropertyId", 30031),
    "toggle": ("UIA_IsTogglePatternAvailablePropertyId", 30041),
    "select": ("UIA_IsSelectionItemPatternAvailablePropertyId", 30036),
    "expand": ("UIA_IsExpandCollapsePatternAvailablePropertyId", 30028),
    "value": ("UIA_IsValuePatternAvailablePropertyId", 30043),
    "text": ("UIA_IsTextPatternAvailablePropertyId", 30040),
    "range": ("UIA_IsRangeValuePatternAvailablePropertyId", 30033),
    "legacy": ("UIA_IsLegacyIAccessiblePatternAvailablePropertyId", 30090),
}
PATTERNS = {  # pattern -> (pattern id name, number, interface name)
    "invoke": ("UIA_InvokePatternId", 10000, "IUIAutomationInvokePattern"),
    "value": ("UIA_ValuePatternId", 10002, "IUIAutomationValuePattern"),
    "range": ("UIA_RangeValuePatternId", 10003, "IUIAutomationRangeValuePattern"),
    "expand": ("UIA_ExpandCollapsePatternId", 10005, "IUIAutomationExpandCollapsePattern"),
    "select": ("UIA_SelectionItemPatternId", 10010, "IUIAutomationSelectionItemPattern"),
    "text": ("UIA_TextPatternId", 10014, "IUIAutomationTextPattern"),
    "toggle": ("UIA_TogglePatternId", 10015, "IUIAutomationTogglePattern"),
    "legacy": ("UIA_LegacyIAccessiblePatternId", 10018, "IUIAutomationLegacyIAccessiblePattern"),
}

_LOCAL = threading.local()  # one ComApi per thread: COM objects belong to their apartment


def bind(module) -> None:
    global AD
    AD = module


def unavailable() -> str | None:
    """Why UI Automation can't be used here, or None. Cheap: nothing is loaded."""
    import importlib.util

    if importlib.util.find_spec("comtypes") is None:
        return 'Windows UI Automation needs comtypes: pip install "ai-desktop[windows]"'
    return None


# --- the COM layer ------------------------------------------------------------------------


class ComApi:
    """UIAutomationCore through comtypes. Nodes are IUIAutomationElement pointers carrying
    the cached properties of PROPERTY_IDS and PATTERN_AVAILABLE."""

    def __init__(self):
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDPIAware()  # physical pixels, like clicks and screenshots
        except (AttributeError, OSError):
            pass
        import comtypes
        import comtypes.client

        if threading.current_thread() is not threading.main_thread():
            try:  # comtypes initialises COM on the main thread only (MCP runs commands on workers)
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except (AttributeError, OSError):
                pass
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U

        self.U = U
        self.Error = (comtypes.COMError, OSError, ValueError)
        try:  # IUIAutomation2 can time out on a hung app instead of waiting 20 s
            self.uia = comtypes.client.CreateObject(U.CUIAutomation8, interface=U.IUIAutomation2)
            self.uia.ConnectionTimeout = 2000
            self.uia.TransactionTimeout = 5000
        except (AttributeError, comtypes.COMError, OSError):
            self.uia = comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation)
        self.types = {getattr(U, n): n[4:-13] for n in dir(U) if n.startswith("UIA_") and n.endswith("ControlTypeId")}
        self.ids = {key: getattr(U, name, number) for key, (name, number) in {**PROPERTY_IDS, **PATTERN_AVAILABLE}.items()}
        self.cache = self.uia.CreateCacheRequest()
        for pid in self.ids.values():
            self.cache.AddProperty(pid)
        self.view = self.uia.ControlViewCondition
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32

    # finding elements

    def foreground(self) -> int:
        hwnd = self.user32.GetForegroundWindow()
        return int(self.user32.GetAncestor(hwnd, 2) or hwnd) if hwnd else 0  # GA_ROOT

    def window(self, hwnd: int):
        try:
            node = self.uia.ElementFromHandleBuildCache(hwnd, self.cache)
        except self.Error:
            return None
        return node if node else None

    def top_windows(self) -> list:
        return self.children(self.uia.GetRootElement())

    def children(self, node) -> list:
        found = node.FindAllBuildCache(self.U.TreeScope_Children, self.view, self.cache)
        if not found:
            return []
        return [found.GetElement(i) for i in range(found.Length)]

    def props(self, node) -> dict:
        get = node.GetCachedPropertyValue
        raw = {key: _plain(get(pid)) for key, pid in self.ids.items()}
        out = {key: raw[key] for key in PROPERTY_IDS}
        out["type"] = self.types.get(raw["type"], str(raw["type"]))
        out["rect"] = _rect(raw["rect"])
        out["patterns"] = [p for p in PATTERN_AVAILABLE if raw[p]]
        out["name"] = out["name"] if isinstance(out["name"], str) else ""
        for key in ("framework", "class_name"):
            out[key] = out[key] if isinstance(out[key], str) else ""
        return out

    def process_name(self, pid) -> str:
        import ctypes
        import os

        handle = self.kernel32.OpenProcess(0x1000, False, int(pid or 0))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buf))
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.splitext(os.path.basename(buf.value))[0]
        finally:
            self.kernel32.CloseHandle(handle)

    def virtual_screen(self) -> tuple[int, int, int, int]:
        m = self.user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)  # SM_X/YVIRTUALSCREEN, SM_CX/CYVIRTUALSCREEN

    # acting

    def _pattern(self, node, which: str):
        name, number, iface = PATTERNS[which]
        unknown = node.GetCurrentPattern(getattr(self.U, name, number))
        if not unknown:
            raise ValueError(f"no {which} pattern")
        return unknown.QueryInterface(getattr(self.U, iface))

    def invoke(self, node):
        self._pattern(node, "invoke").Invoke()

    def toggle(self, node):
        self._pattern(node, "toggle").Toggle()

    def select(self, node):
        self._pattern(node, "select").Select()

    def expand(self, node) -> str:
        pattern = self._pattern(node, "expand")
        if pattern.CurrentExpandCollapseState in (EXPANDED, PARTIALLY_EXPANDED):
            pattern.Collapse()
            return "collapse"
        pattern.Expand()
        return "expand"

    def default_action(self, node):
        self._pattern(node, "legacy").DoDefaultAction()

    def focus(self, node):
        node.SetFocus()

    def has_focus(self, node) -> bool:
        return bool(node.CurrentHasKeyboardFocus)

    def set_value(self, node, text: str):
        self._pattern(node, "value").SetValue(text)

    def value(self, node) -> str | None:
        return _plain(self._pattern(node, "value").CurrentValue)

    def text(self, node, limit: int) -> str | None:
        return _plain(self._pattern(node, "text").DocumentRange.GetText(limit))

    def range_value(self, node):
        return _plain(self._pattern(node, "range").CurrentValue)


def _plain(value):
    """A property value as plain Python; UIA's "not supported" marker (a COM object) is None."""
    value = getattr(value, "value", value) if not isinstance(value, (str, bytes, tuple, list)) else value
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (tuple, list)) and all(isinstance(v, (int, float)) for v in value):
        return tuple(value)
    return None


def _rect(value) -> list[int] | None:
    """UIA's (left, top, width, height) as integers, or None when empty or unusable."""
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value):
        return None
    x, y, w, h = (round(v) for v in value)
    return [x, y, w, h] if w > 0 and h > 0 else None


def api():
    """This thread's ComApi (UIAutomationCore is loaded on first use)."""
    current = getattr(_LOCAL, "api", None)
    if current is None:
        current = _LOCAL.api = ComApi()
    return current


# --- what the tree looks like to an agent ---------------------------------------------------


def role_of(p: dict, web: bool = False) -> str:
    kind = p["type"]
    if kind == "Button" and "toggle" in p["patterns"]:
        return "toggle button"
    if kind == "MenuItem" and "toggle" in p["patterns"]:
        return "check menu item"
    if kind == "Edit" and p.get("password"):
        return "password text"
    if kind == "Document":
        return "document web" if web else "document frame"
    return ROLES.get(kind, kind.lower())


def states_of(p: dict) -> list[str]:
    """The AT-SPI state names the Linux helper reports, in the same order."""
    names = []
    if p.get("focused"):
        names.append("focused")
    if p.get("toggle") == TOGGLE_ON:
        names.append("checked")
    if p.get("selected"):
        names.append("selected")
    if p.get("expand") in (EXPANDED, PARTIALLY_EXPANDED):
        names.append("expanded")
    if p.get("enabled"):
        names.append("enabled")
    if "value" in p["patterns"] and p.get("readonly") is False:
        names.append("editable")
    if not p.get("offscreen"):
        names += ["showing", "visible"]
    if p.get("enabled"):
        names.append("sensitive")
    return names


def actions_of(p: dict) -> list[str]:
    return [a for a in ACTIONS if a in p["patterns"]]


def usable(rect, bounds) -> bool:
    """A real click can land on it: big enough, with its middle inside `bounds`
    (its window, clipped to the screen)."""
    if not rect or rect[2] < MIN_SIZE or rect[3] < MIN_SIZE or bounds is None:
        return False
    cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    bx, by, bw, bh = bounds
    return bx <= cx < bx + bw and by <= cy < by + bh


def _clip(a, b):
    if not a or not b:
        return None
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return [x1, y1, x2 - x1, y2 - y1] if x2 > x1 and y2 > y1 else None


def is_web(class_name: str) -> bool:
    return class_name.startswith(WEB_WINDOWS)


# --- the two requests -----------------------------------------------------------------------


def _walk(uia, root, props: dict, index: int, bounds, out: list, budget: list) -> dict:
    """Append the useful, visible elements under one window; returns counts for the
    Chromium check."""
    hwnd = int(props.get("hwnd") or 0)
    web = is_web(props["class_name"])
    seen = {"documents": 0, "in_documents": 0}
    stack = [(root, [hwnd], 0, props, False)]
    while stack and budget[0] < MAX_NODES:
        node, path, depth, p, in_doc = stack.pop()
        budget[0] += 1
        try:
            if p is None:
                p = uia.props(node)
            is_root = node is root
            if not is_root and p.get("offscreen") and p["rect"]:
                continue  # offscreen, and so is everything inside it
            role = role_of(p, web)
            if role == "document web":
                seen["documents"] += 1
            elif in_doc:
                seen["in_documents"] += 1
            name = (p["name"] or "").strip()
            rect = p["rect"]
            actions = actions_of(p)
            states = states_of(p)
            editable = "editable" in states
            listed = name or role in ACTIONABLE_ROLES or actions or editable
            if not is_root and not p.get("offscreen") and listed and (rect or actions or editable):
                item = {"path": path, "role": role, "name": name[:200], "rect": rect or [0, 0, 0, 0],
                        "states": states, "actions": actions[:6], "window": index}
                if not usable(rect, bounds):
                    item["where"] = "none"  # no point to click: pressed through UIA instead
                if role in TEXT_ROLES and not p.get("password") and isinstance(p.get("value"), str) and p["value"]:
                    item["text"] = p["value"][:200]
                out.append(item)
            if depth < MAX_DEPTH:
                kids = uia.children(node)[:MAX_CHILDREN]
                inside = in_doc or role == "document web"
                for k in range(len(kids) - 1, -1, -1):
                    stack.append((kids[k], path + [k], depth + 1, None, inside))
        except uia.Error:
            continue
    return seen


def a11y_tree(req: dict, uia=None) -> dict:
    """Visible, useful elements of the active window (or of every top-level window),
    flattened, in the AT-SPI helper's format. Paths are [window handle, child index, ...]."""
    uia = uia or api()
    active = uia.foreground()
    roots = []
    if req.get("scope") == "all":
        for node in uia.top_windows():
            p = uia.props(node)
            if p["rect"] and not p.get("offscreen") and p.get("hwnd"):
                roots.append((node, p))
    elif active:
        node = uia.window(active)
        if node is not None:
            roots.append((node, uia.props(node)))
    screen = list(uia.virtual_screen())
    windows, out, budget, note = [], [], [0], None
    for node, p in roots:
        rect = p["rect"] or [0, 0, 0, 0]
        hwnd = int(p.get("hwnd") or 0)
        windows.append({"path": [hwnd], "app": uia.process_name(p.get("pid")), "toolkit": p["framework"],
                        "title": (p["name"] or "").strip(), "screen": rect, "window": [0, 0, rect[2], rect[3]]})
        index = len(windows) - 1
        bounds = _clip(rect, screen)
        start = len(out)
        seen = _walk(uia, node, p, index, bounds, out, budget)
        tries = CHROMIUM_RETRIES if p["class_name"].startswith(CHROMIUM_WINDOW) else 0
        while tries and not seen["in_documents"] and budget[0] < MAX_NODES:
            # Chromium switches its page tree on when UIA first asks, then builds it.
            tries -= 1
            AD._sleep(CHROMIUM_RETRY_WAIT)
            del out[start:]
            node = uia.window(hwnd) or node
            seen = _walk(uia, node, uia.props(node), index, bounds, out, budget)
        if p["class_name"].startswith(CHROMIUM_WINDOW) and not seen["in_documents"]:
            note = ("this Chromium window (Chrome, Edge or an Electron app) showed no page content; "
                    "for Chrome start it with: ai-desktop launch-chrome --a11y")
    out.sort(key=lambda e: (e["window"], e["rect"][1], e["rect"][0]))
    answer = {"ok": True, "windows": windows, "elements": out, "visited": budget[0], "truncated": budget[0] >= MAX_NODES}
    if note:
        answer["note"] = note
    return answer


def _resolve(uia, path):
    if not path:
        return None, None
    root = uia.window(int(path[0]))
    if root is None:
        return None, None
    node = root
    for index in path[1:]:
        kids = uia.children(node)
        if index >= len(kids):
            return None, root
        node = kids[index]
    return node, root


def _wait_for_focus(uia, node, timeout: float = 0.6) -> bool:
    deadline = AD._now() + timeout
    while True:
        try:
            if uia.has_focus(node):
                return True
        except uia.Error:
            return False
        if AD._now() >= deadline:
            return False
        AD._sleep(0.05)


def a11y_do(req: dict, uia=None) -> dict:
    """Act on the element at `path`, after checking it is still the same one."""
    uia = uia or api()
    try:
        node, root = _resolve(uia, req.get("path"))
    except uia.Error:
        node, root = None, None
    if node is None:
        return {"ok": False, "error": "stale", "detail": "the element is gone"}
    web = is_web(uia.props(root)["class_name"])
    p = uia.props(node)
    role, name = role_of(p, web), (p["name"] or "").strip()
    if role != req.get("role") or name[:200] != req.get("name", ""):
        return {"ok": False, "error": "stale", "detail": f"now {role} {name[:40]!r}"}
    what = req.get("do")
    try:
        if what == "press":
            patterns = p["patterns"]
            if "invoke" in patterns:
                uia.invoke(node)
                return {"ok": True, "did": "invoke"}
            if "toggle" in patterns:
                uia.toggle(node)
                return {"ok": True, "did": "toggle"}
            if "select" in patterns:
                uia.select(node)
                return {"ok": True, "did": "select"}
            if "expand" in patterns:
                return {"ok": True, "did": uia.expand(node)}
            if "legacy" in patterns:
                try:
                    uia.default_action(node)
                    return {"ok": True, "did": "default-action"}
                except uia.Error:
                    pass
            uia.focus(node)
            if _wait_for_focus(uia, node):
                return {"ok": True, "did": "focus"}
            return {"ok": False, "error": "no action", "detail": "the element offers no action and can't take focus"}
        if what == "focus":
            uia.focus(node)
            return {"ok": _wait_for_focus(uia, node), "did": "focus"}
        if what == "set-text":
            if "value" in p["patterns"] and p.get("readonly") is False:
                try:
                    uia.set_value(node, req.get("text", ""))
                    return {"ok": True, "did": "set-text"}
                except uia.Error:
                    pass
            # No writable ValuePattern (rich editors, some web fields): type it instead.
            return {"ok": False, "error": "not editable", "fallback": "paste"}
        if what == "read":
            info = {"ok": True, "role": role, "name": name, "states": states_of(p)}
            if not p.get("password"):
                text = None
                if "value" in p["patterns"] and p["type"] != "Hyperlink":  # a link's value is its URL
                    text = uia.value(node)
                if text is None and "text" in p["patterns"]:
                    text = uia.text(node, 10000)
                if text is None and p["type"] == "Text":
                    text = name
                if text is not None:
                    info["text"] = text
            if "range" in p["patterns"]:
                info["value"] = uia.range_value(node)
            return info
    except uia.Error as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
    return {"ok": False, "error": f"unknown action {what!r}"}


# --- caching ----------------------------------------------------------------------------------


def screen_key(scope: str, uia) -> str | None:
    """Changes whenever the screen, the active window or the scope does."""
    try:
        cap = AD.capture_screen(AD.get_backend("auto"), "all")
    except Exception:
        return None
    digest = hashlib.blake2b(cap.image.tobytes(), digest_size=16).hexdigest()
    return f"{scope}:{uia.foreground()}:{cap.x},{cap.y},{cap.image.width}x{cap.image.height}:{digest}"


def _cache_path():
    return AD.home_dir() / CACHE_FILE


def _forget() -> None:
    try:
        _cache_path().unlink()
    except OSError:
        pass


def cached_tree(req: dict, uia=None) -> dict:
    """The tree, re-read only when the screen has changed since the last read (or after a
    minute, or after acting on an element), like the text recognition cache."""
    uia = uia or api()
    key = screen_key(req.get("scope", "active"), uia)
    if key is not None:
        try:
            saved = json.loads(_cache_path().read_text(encoding="utf-8"))
            if saved.get("key") == key and time.time() - saved.get("taken", 0) < CACHE_MAX_AGE:
                return {**saved["tree"], "cached": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    tree = a11y_tree(req, uia)
    if key is not None and tree.get("ok"):
        try:
            with AD.private_file(AD.private_dir(AD.home_dir()) / CACHE_FILE) as fh:
                json.dump({"key": key, "taken": time.time(), "tree": tree}, fh)
        except OSError:
            pass
    return tree


def handle(req: dict) -> dict:
    """Answer one request of the AT-SPI helper's protocol."""
    try:
        if req.get("op") == "a11y-tree":
            return cached_tree(req)
        if req.get("op") == "a11y-do":
            if req.get("do") != "read":
                _forget()  # acting changes the tree even when the pixels don't
            return a11y_do(req)
        return {"ok": False, "error": f"unknown op {req.get('op')!r}"}
    except ImportError as exc:
        return {"ok": False, "error": f"ModuleNotFoundError: {exc}"}
    except Exception as exc:  # a COM failure must become an error line, not a traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


if __name__ == "__main__" and sys.platform == "win32":  # quick manual check: python ai_desktop_uia.py
    print(json.dumps(a11y_tree({"scope": "active"}), indent=1)[:4000])
