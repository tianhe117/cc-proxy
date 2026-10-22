"""CC-Proxy Windows 托盘入口，仅依赖原项目的 flask / requests。

首次运行若缺少 tray.json，会在程序旁边生成默认配置。
填写其中的 upstream 后，从托盘重新加载（model 留空时自动获取首个模型）：
    pythonw tray.py                   # 无控制台；python tray.py 可查看诊断
    pythonw tray.py --config C:\\path\\tray.json

右键图标直接选择模型、刷新列表、重新加载配置或退出，无独立 GUI。
客户端无需 API Key，也可填写任意值；上游请求始终使用 upstream.api_key。
display_model 是对客户端显示的模型名称，
留空时返回当前实际模型。候选模型每次从上游获取，不写入 JSON。
上游或监听设置改变需退出后重启；其余设置可通过菜单重新加载。
模型切换会保存 tray.json。该文件含密钥，请勿提交填写后的文件。
"""
import argparse
import copy
import ctypes
from ctypes import wintypes as wt
import hashlib
import importlib
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urlsplit


DEFAULT_CONFIG = {
    "upstream": {"base_url": "", "api_key": "", "verify_ssl": False, "timeout": 300},
    "server": {"host": "127.0.0.1", "port": 8000},
    "model": "", "display_model": "", "debug": False,
}


def ensure_default_config(path):
    """配置不存在时生成默认文件；已有配置（包括无效 JSON）保持原样。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except FileExistsError:
        return False
    return True


def validate_config(value):
    """严格校验，错误不回显配置值，保留未识别字段。"""
    if not isinstance(value, dict):
        raise ValueError("tray.json 必须是 JSON 对象")
    cfg = {**copy.deepcopy(DEFAULT_CONFIG), **copy.deepcopy(value)}
    # 旧配置自动迁移；后续保存只写新字段，不保存候选模型列表。
    if "display_model" not in value:
        cfg["display_model"] = cfg.get("public_model_id", "")
    cfg.pop("public_model_id", None)
    cfg.pop("models", None)
    for section in ("upstream", "server"):
        if not isinstance(cfg[section], dict):
            raise ValueError(section + " 必须是 JSON 对象")
        cfg[section] = {**DEFAULT_CONFIG[section], **cfg[section]}
    upstream, server = cfg["upstream"], cfg["server"]
    url = upstream["base_url"]
    if not isinstance(url, str):
        raise ValueError("请填写 upstream.base_url")
    try:
        parsed = urlsplit(url.strip())
        parsed.port  # 触发非法端口校验。
        valid_url = (parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("upstream.base_url 必须是有效的 HTTP(S) 上游根地址")
    upstream["base_url"] = url.strip().rstrip("/")
    key = upstream["api_key"]
    if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("请填写 upstream.api_key（不含空格的 ASCII 字符）")
    if type(upstream["verify_ssl"]) is not bool or type(cfg["debug"]) is not bool:
        raise ValueError("verify_ssl 和 debug 必须是 true 或 false")
    timeout = upstream["timeout"]
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout:
        raise ValueError("upstream.timeout 必须是正数")
    if not isinstance(server["host"], str) or not server["host"].strip():
        raise ValueError("server.host 不能为空")
    server["host"] = server["host"].strip()
    if type(server["port"]) is not int or not 1 <= server["port"] <= 65535:
        raise ValueError("server.port 必须是 1 到 65535 的整数")
    if not isinstance(cfg["model"], str):
        raise ValueError("model 必须是字符串，可留空自动获取")
    cfg["model"] = cfg["model"].strip()
    if not isinstance(cfg["display_model"], str):
        raise ValueError("display_model 必须是字符串，可留空显示实际模型")
    cfg["display_model"] = cfg["display_model"].strip()
    return cfg


def read_config(path):
    raw = path.read_bytes()
    try:
        cfg = validate_config(json.loads(raw.decode("utf-8-sig")))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("tray.json 必须是有效的 UTF-8 JSON") from None
    # Python 的 JSON 解码器允许 NaN / Infinity，但它们不能安全持久化。
    try:
        json.dumps(cfg, allow_nan=False)
    except ValueError:
        raise ValueError("tray.json 不支持 NaN 或 Infinity") from None
    return cfg, hashlib.sha256(raw).digest()


def save_atomic(path, cfg):
    raw = (json.dumps(cfg, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tray-", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).digest()


class ProxyRuntime:
    """在内存中适配 app.py；所有配置及模型写入仅指向 tray.json。"""

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.lock = threading.RLock()
        self.settings = None
        self.digest = None
        self.remote_models = []
        self.status = "正在启动"
        self.refreshing = False
        self.server = None
        self.thread = None
        self.proxy = None
        self.closed = False
        self.on_change = lambda: None
        self.logger = logging.getLogger("cc-proxy-tray")
        self.logger.addHandler(logging.NullHandler())

    def snapshot(self):
        with self.lock:
            cfg = copy.deepcopy(self.settings)
            candidates = set(self.remote_models)
            if cfg:
                if cfg["model"]:
                    candidates.add(cfg["model"])
            running = self.thread is not None and self.thread.is_alive()
            return cfg, sorted(candidates, key=str.casefold), self.status, self.refreshing, running

    def report(self, status):
        with self.lock:
            self.status = status
        self.on_change()

    def configure_logger(self):
        logger = logging.getLogger("cc-proxy")
        logger.propagate = False
        # 关闭 app.py 模块加载时建立的旧日志处理器，运行设置完全以 JSON 为准。
        handlers = []
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler())
        if self.settings["debug"]:
            folder = self.path.parent / "logs"
            folder.mkdir(exist_ok=True)
            handlers.append(logging.FileHandler(folder / "tray.log", encoding="utf-8"))
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        for handler in handlers or [logging.NullHandler()]:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if self.settings["debug"] else logging.INFO)
        self.logger = logger

    def prepare_backend(self):
        # app.py 在导入时就读取 DEBUG 并建立文件日志。仅在首次导入期间
        # 屏蔽该旧行为，避免继承的环境设置影响 JSON 入口；随后恢复环境。
        if "app" in sys.modules:
            proxy = sys.modules["app"]
        else:
            inherited_debug = os.environ.get("DEBUG")
            try:
                os.environ["DEBUG"] = "false"
                proxy = importlib.import_module("app")
            finally:
                if inherited_debug is None:
                    os.environ.pop("DEBUG", None)
                else:
                    os.environ["DEBUG"] = inherited_debug
        self.proxy = proxy
        cfg = self.settings
        upstream = cfg["upstream"]
        proxy.NEW_API_BASE_URL = upstream["base_url"]
        proxy.NEW_API_KEY = upstream["api_key"]
        proxy.PROXY_KEYS = [upstream["api_key"]]
        proxy.UPSTREAM_VERIFY = upstream["verify_ssl"]
        proxy.UPSTREAM_TIMEOUT = upstream["timeout"]
        proxy.CONFIG_FILE_PATH = str(self.path)
        # 单模型托盘模式不校验客户端凭据，统一使用配置中的上游 Key。
        # 原路由收到固定内部标识后，继续复用现有模型查询和转发逻辑。
        proxy.check_proxy_key = lambda req: self.settings["upstream"]["api_key"]
        proxy.get_model = self.get_model
        proxy.set_model = self.set_model
        proxy.get_public_model_id = self.display_model
        proxy.is_debug_enabled = lambda: self.settings["debug"]
        proxy.fetch_models_from_upstream = self.fetch_models

        if not hasattr(proxy, "_tray_original_upstream_headers"):
            proxy._tray_original_upstream_headers = proxy._make_upstream_headers
        original_headers = proxy._tray_original_upstream_headers

        def upstream_headers(req):
            headers = original_headers(req)
            for name in headers:
                if name.lower() == "x-api-key":
                    headers[name] = proxy.NEW_API_KEY
            return headers

        proxy._make_upstream_headers = upstream_headers
        if not hasattr(proxy, "_tray_original_mask_header"):
            proxy._tray_original_mask_header = proxy._mask_header
        original_mask = proxy._tray_original_mask_header

        def mask_header(name, value):
            if name.lower() == "x-api-key" and value:
                return "***"
            return original_mask(name, value)

        proxy._mask_header = mask_header

        # 原接口的参数校验不变，写入失败返回明确 JSON 错误。
        if not hasattr(proxy, "_tray_original_set_model_view"):
            proxy._tray_original_set_model_view = proxy.app.view_functions["api_set_model"]
        original = proxy._tray_original_set_model_view

        def api_set_model():
            try:
                return original()
            except OSError:
                return proxy.jsonify(error="保存配置失败，模型未更改；请检查托盘状态"), 500

        proxy.app.view_functions["api_set_model"] = api_set_model
        if not hasattr(proxy, "_tray_model_guard"):
            def require_model():
                if proxy.request.endpoint in ("public_list_models", "transparent_proxy"):
                    key = proxy.check_proxy_key(proxy.request)
                    if key and not proxy.get_model(key):
                        return proxy.jsonify(error="正在获取默认模型，请稍后重试或从托盘刷新模型列表"), 503
            # 直接维护钩子列表，兼容同进程内重新加载适配器。
            proxy.app.before_request_funcs.setdefault(None, []).append(require_model)
            proxy._tray_model_guard = require_model
        self.configure_logger()

    def get_model(self, key):
        with self.lock:
            return self.settings["model"] if key == self.settings["upstream"]["api_key"] else None

    def display_model(self, key):
        with self.lock:
            return self.settings["display_model"] or self.get_model(key)

    def set_model(self, key, model):
        with self.lock:
            if self.closed:
                raise OSError("代理已退出")
            if key != self.settings["upstream"]["api_key"]:
                raise ValueError("无效 Key")
            if not isinstance(model, str) or not model.strip():
                raise ValueError("模型不能为空")
            try:
                # 防止托盘或管理接口覆盖用户刚手动编辑的 JSON。
                if hashlib.sha256(self.path.read_bytes()).digest() != self.digest:
                    raise OSError("配置文件已修改，请先重新加载")
                updated = copy.deepcopy(self.settings)
                updated["model"] = model.strip()
                digest = save_atomic(self.path, updated)
            except OSError:
                self.status = "保存失败；如已编辑 JSON，请先重新加载配置"
                self.on_change()
                raise
            self.settings = updated
            self.digest = digest
            self.status = "模型已保存并生效"
        self.on_change()

    def select_model(self, model):
        try:
            self.set_model(self.settings["upstream"]["api_key"], model)
        except (OSError, ValueError):
            pass  # 详细失败状态由 set_model 展示在菜单中。

    def reload(self):
        success = False
        with self.lock:
            try:
                if ensure_default_config(self.path):
                    raise ValueError("已生成 tray.json，请填写后重新加载")
                cfg, digest = read_config(self.path)
                if self.server is not None and any(cfg[k] != self.settings[k] for k in ("upstream", "server")):
                    raise ValueError("上游或监听设置已变化，请退出后重启")
                previous = self.settings
                self.settings = cfg
                try:
                    if self.server is None:
                        self.prepare_backend()
                    else:
                        self.configure_logger()
                    if self.server is None:
                        from werkzeug.serving import make_server
                        try:
                            server = make_server(cfg["server"]["host"], cfg["server"]["port"], self.proxy.app, threaded=True)
                        except (OSError, SystemExit):
                            raise OSError("监听失败") from None
                        self.thread = threading.Thread(target=server.serve_forever, name="cc-proxy-server", daemon=True)
                        self.server = server
                        try:
                            self.thread.start()
                        except Exception:
                            server.server_close()
                            self.server = None
                            self.thread = None
                            raise
                except Exception:
                    self.settings = previous
                    raise
                self.digest = digest
                self.status = "正常" if cfg["model"] else "等待获取默认模型"
                success = True
            except ValueError as exc:
                self.status = "配置：" + str(exc)
            except ImportError:
                self.status = "缺少依赖，请先 pip install -r requirements.txt"
            except OSError:
                self.status = "启动或加载失败，请检查文件权限及监听端口"
        self.on_change()
        return success

    def fetch_models(self):
        import requests
        with self.lock:
            upstream = copy.deepcopy(self.settings["upstream"])
        with requests.get(
            upstream["base_url"] + "/v1/models",
            headers={"Authorization": "Bearer " + upstream["api_key"]},
            timeout=15, verify=upstream["verify_ssl"],
        ) as response:
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("上游模型列表格式无效")
        models = [m.get("id") for m in data["data"] if isinstance(m, dict)]
        if any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError("上游模型名称无效")
        return list(dict.fromkeys(m.strip() for m in models))

    def refresh(self):
        with self.lock:
            if self.refreshing or self.settings is None or self.closed:
                return
            self.refreshing = True
            self.status = "正在刷新上游模型列表"
        self.on_change()

        def worker():
            result = "刷新失败，保留已有模型；请检查上游配置或网络"
            try:
                models = self.fetch_models()
                with self.lock:
                    if self.closed:
                        return
                    self.remote_models = models
                    if not self.settings["model"]:
                        if models:
                            # 保留上游顺序；不覆盖获取期间由用户手动设定的模型。
                            self.set_model(self.settings["upstream"]["api_key"], models[0])
                        else:
                            result = "上游模型列表为空，请稍后刷新或在 JSON 中填写 model"
                    else:
                        result = "已获取 %d 个上游模型" % len(models)
            except Exception:
                pass
            finally:
                with self.lock:
                    self.refreshing = False
                    # 不覆盖刷新期间产生的保存失败、配置错误或重启提示。
                    if self.status == "正在刷新上游模型列表":
                        self.status = result
                if not self.closed:
                    self.on_change()

        threading.Thread(target=worker, name="cc-proxy-models", daemon=True).start()

    def close(self):
        with self.lock:
            self.closed = True
            self.on_change = lambda: None
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=2)
            self.server.server_close()


class WindowsTray:
    """Win32 隐藏消息窗口 + 通知区图标 + 原生弹出菜单。"""

    WM_ICON = 0x8001
    WM_UPDATE = 0x8002

    def __init__(self, runtime):
        self.runtime = runtime
        self.hwnd = None
        self.menu_open = False
        self.icon_added = False
        self.instance = None
        self.atom = None
        self.icon = None
        self.class_name = "CCProxyTray_%d" % os.getpid()
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._declare_api()
        self.callback = self.WNDPROC(self.window_proc)  # 保持引用直到窗口注销。
        self.taskbar_created = self.user.RegisterWindowMessageW("TaskbarCreated")

    def _declare_api(self):
        ptr = ctypes.c_void_p
        uint_ptr, long_ptr = ctypes.c_size_t, ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(long_ptr, wt.HWND, wt.UINT, uint_ptr, long_ptr)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", self.WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                        ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
                        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                        ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
                        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]

        self.WNDCLASS, self.NOTIFYICONDATA = WNDCLASS, NOTIFYICONDATA
        specs = [
            (self.kernel.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE),
            (self.user.RegisterClassW, [ctypes.POINTER(WNDCLASS)], wt.ATOM),
            (self.user.UnregisterClassW, [wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
            (self.user.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, ptr], wt.HWND),
            (self.user.DefWindowProcW, [wt.HWND, wt.UINT, uint_ptr, long_ptr], long_ptr),
            (self.user.DestroyWindow, [wt.HWND], wt.BOOL),
            (self.user.PostQuitMessage, [ctypes.c_int], None),
            (self.user.PostMessageW, [wt.HWND, wt.UINT, uint_ptr, long_ptr], wt.BOOL),
            (self.user.GetMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], ctypes.c_int),
            (self.user.TranslateMessage, [ctypes.POINTER(wt.MSG)], wt.BOOL),
            (self.user.DispatchMessageW, [ctypes.POINTER(wt.MSG)], long_ptr),
            (self.user.RegisterWindowMessageW, [wt.LPCWSTR], wt.UINT),
            (self.user.CreatePopupMenu, [], wt.HMENU),
            (self.user.AppendMenuW, [wt.HMENU, wt.UINT, uint_ptr, wt.LPCWSTR], wt.BOOL),
            (self.user.DestroyMenu, [wt.HMENU], wt.BOOL),
            (self.user.GetCursorPos, [ctypes.POINTER(wt.POINT)], wt.BOOL),
            (self.user.SetForegroundWindow, [wt.HWND], wt.BOOL),
            (self.user.TrackPopupMenu, [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
             ctypes.c_int, wt.HWND, ctypes.POINTER(wt.RECT)], wt.UINT),
            (self.user.CreateIcon, [wt.HINSTANCE, ctypes.c_int, ctypes.c_int, wt.BYTE, wt.BYTE, ptr, ptr], wt.HICON),
            (self.user.DestroyIcon, [wt.HICON], wt.BOOL),
            (self.user.SetTimer, [wt.HWND, uint_ptr, wt.UINT, ptr], uint_ptr),
            (self.shell.Shell_NotifyIconW, [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)], wt.BOOL),
        ]
        for function, args, result in specs:
            function.argtypes, function.restype = args, result

    @staticmethod
    def label(text):
        return text.replace("&", "&&").replace("\r", " ").replace("\n", " ").replace("\t", " ")

    def create_icon(self):
        # 32 位 BGRA 图标：蓝底白色双箭头，不需要额外图片或 Pillow。
        pixels = bytearray()
        for y in range(32):
            for x in range(32):
                arrow = (abs(x - (7 + abs(y - 16))) <= 1 or abs(x - (25 - abs(y - 16))) <= 1) and 9 <= y <= 23
                pixels.extend((255, 255, 255, 255) if arrow else (235, 99, 37, 255))
        mask = (ctypes.c_ubyte * 128)()
        color = (ctypes.c_ubyte * len(pixels)).from_buffer_copy(pixels)
        icon = self.user.CreateIcon(self.instance, 32, 32, 1, 32, mask, color)
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return icon

    def notify(self, operation):
        data = self.NOTIFYICONDATA()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd, data.uID = self.hwnd, 1
        data.uFlags, data.uCallbackMessage, data.hIcon = 7, self.WM_ICON, self.icon
        cfg, _, status, _, running = self.runtime.snapshot()
        text = "CC-Proxy · " + ((cfg["model"] + " · ") if cfg else "") + (status if running else "未运行 · " + status)
        data.szTip = text.encode("utf-16-le")[:254].decode("utf-16-le", errors="ignore")
        ok = self.shell.Shell_NotifyIconW(operation, ctypes.byref(data))
        if operation == 0:
            self.icon_added = bool(ok)
        return ok

    def build_menu(self):
        cfg, models, status, refreshing, running = self.runtime.snapshot()
        menu = self.user.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        actions = {}

        def append(parent, text, action=None, checked=False, submenu=None):
            flags = 0x10 if submenu else (0x8 if checked else 0)
            if action is None and submenu is None:
                flags |= 0x1  # MF_GRAYED
            identifier = submenu or len(actions) + 100
            if not self.user.AppendMenuW(parent, flags, identifier, self.label(text)):
                raise ctypes.WinError(ctypes.get_last_error())
            if action is not None:
                actions[identifier] = action

        try:
            append(menu, "CC-Proxy · " + ("运行中" if running else "未运行"))
            if cfg:
                append(menu, "监听：%s:%d" % (cfg["server"]["host"], cfg["server"]["port"]))
            append(menu, "当前模型：" + ((cfg["model"] or "等待获取") if cfg else "未配置"))
            self.user.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            if cfg and models:
                selection = self.user.CreatePopupMenu()
                append(menu, "选择模型", submenu=selection)
                # 大列表按 25 项分页，避免菜单超出屏幕。
                for offset in range(0, len(models), 25):
                    page = selection
                    if len(models) > 25:
                        page = self.user.CreatePopupMenu()
                        append(selection, "%d–%d" % (offset + 1, min(offset + 25, len(models))), submenu=page)
                    for model in models[offset:offset + 25]:
                        append(page, model, lambda m=model: self.runtime.select_model(m), checked=model == cfg["model"])
            else:
                append(menu, "选择模型（等待获取列表）")
            append(menu, "刷新上游模型列表" if not refreshing else "正在刷新模型列表…",
                   self.runtime.refresh if cfg and not refreshing else None)
            append(menu, "重新加载 tray.json", self.reload)
            append(menu, "状态：" + status)
            self.user.AppendMenuW(menu, 0x800, 0, None)
            append(menu, "退出", lambda: self.user.PostMessageW(self.hwnd, 0x10, 0, 0))
            return menu, actions
        except BaseException:
            self.user.DestroyMenu(menu)
            raise

    def show_menu(self):
        if self.menu_open:
            return
        self.menu_open = True
        menu = None
        try:
            menu, actions = self.build_menu()
            point = wt.POINT()
            self.user.GetCursorPos(ctypes.byref(point))
            self.user.SetForegroundWindow(self.hwnd)
            command = self.user.TrackPopupMenu(menu, 0x100 | 0x80 | 0x2, point.x, point.y, 0, self.hwnd, None)
            self.user.PostMessageW(self.hwnd, 0, 0, 0)
            if command in actions:
                actions[command]()
        finally:
            if menu:
                self.user.DestroyMenu(menu)
            self.menu_open = False

    def reload(self):
        if self.runtime.reload():
            self.runtime.refresh()

    def window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.WM_ICON and lparam in (0x205, 0x202, 0x7B):
                self.show_menu()
                return 0
            if message == self.WM_UPDATE:
                self.notify(1 if self.icon_added else 0)
                return 0
            if message == self.taskbar_created:
                self.notify(0)
                return 0
            if message == 0x113:  # WM_TIMER：Explorer 尚未就绪时重新注册图标。
                if not self.icon_added:
                    self.notify(0)
                return 0
            if message == 0x11:  # WM_QUERYENDSESSION
                return 1
            if message == 0x10 or (message == 0x16 and wparam):
                self.user.DestroyWindow(hwnd)
                return 0
            if message == 0x2:
                self.user.PostQuitMessage(0)
                return 0
        except Exception:
            self.runtime.report("托盘操作失败，请重试或检查配置")
        return self.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def run(self):
        try:
            self.instance = self.kernel.GetModuleHandleW(None)
            cls = self.WNDCLASS()
            cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self.callback, self.instance, self.class_name
            self.atom = self.user.RegisterClassW(ctypes.byref(cls))
            if not self.atom:
                raise ctypes.WinError(ctypes.get_last_error())
            self.icon = self.create_icon()
            self.hwnd = self.user.CreateWindowExW(0, self.class_name, "CC-Proxy", 0, 0, 0, 0, 0, None, None, self.instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            self.runtime.on_change = lambda: self.user.PostMessageW(self.hwnd, self.WM_UPDATE, 0, 0)
            self.notify(0)
            self.user.SetTimer(self.hwnd, 1, 3000, None)
            self.reload()
            message = wt.MSG()
            while True:
                result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
                self.user.TranslateMessage(ctypes.byref(message))
                self.user.DispatchMessageW(ctypes.byref(message))
        finally:
            self.runtime.close()
            if self.hwnd:
                self.notify(2)
                self.user.DestroyWindow(self.hwnd)
            if self.icon:
                self.user.DestroyIcon(self.icon)
            if self.atom:
                self.user.UnregisterClassW(self.class_name, self.instance)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CC-Proxy Windows 托盘（仅使用 tray.json）")
    default_config = (Path(sys.executable).parent / "tray.json" if getattr(sys, "frozen", False)
                      else Path(__file__).with_suffix(".json"))
    parser.add_argument("--config", type=Path, default=default_config)
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("托盘入口仅支持 Windows")
    try:
        runtime = ProxyRuntime(args.config)
        # 先生成文件，再初始化托盘，即使桌面初始化失败也能找到配置模板。
        ensure_default_config(runtime.path)
        WindowsTray(runtime).run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        if sys.stderr is not None:
            print("CC-Proxy 托盘启动失败，请检查 Windows 桌面会话及运行环境。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
