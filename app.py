"""CC-Proxy:单一 model 热切换透明转发代理。"""
import hmac
import json
import logging
import os
import re
import threading
import time

import requests
from flask import Flask, Response, jsonify, request

# ---------- 常量 ----------
UPSTREAM_MODELS_PATH = "/v1/models"   # New-API 为 OpenAI 兼容网关,标准复数端点;不同网关只改这里
PUBLIC_MODEL_ID = "claude-sonnet-5"   # 客户端固定别名;实际转发模型仍由各 PROXY_KEY 的配置决定
CHUNK_SIZE = 1024
UPSTREAM_TIMEOUT = 300
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE_PATH = os.path.join(_SCRIPT_DIR, "config.json")
LOG_DIR = os.environ.get("LOG_DIR", os.path.join(_SCRIPT_DIR, "logs"))
LOG_BODY_MAX_CHARS = 2000                             # 日志里 body 的最大字符数,超长截断
LOGGER_NAME = "cc-proxy"

UPSTREAM_VERIFY = True   # requests 的 verify 参数:false=跳过上游 HTTPS 证书校验,由 main() 解析 UPSTREAM_SKIP_SSL_VERIFY 后覆盖

# ---------- 日志 ----------
def _setup_logger():
    """按 DEBUG 环境变量决定是否落文件日志;默认 INFO 级(仅控制台)。"""
    is_debug = str(os.environ.get("DEBUG", "")).strip().lower() in ("1", "true", "yes")
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if is_debug else logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
    if is_debug:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = os.path.join(LOG_DIR, time.strftime("%Y%m%d_%H%M%S") + ".log")
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        logger.info("DEBUG 日志已开启: %s", log_file)
    return logger


logger = _setup_logger()   # 模块加载即建,读当时的环境变量;测试通过 monkeypatch DEBUG 后模块已初始化,故路由内用 is_debug_enabled() 实时判断


def is_debug_enabled():
    """实时判断 DEBUG 是否开启(测试可 monkeypatch DEBUG 后生效)。"""
    return str(os.environ.get("DEBUG", "")).strip().lower() in ("1", "true", "yes")


def _mask_header(name, value):
    """Authorization 头脱敏为 Bearer ***;其余头原样。"""
    if name.lower() == "authorization" and value:
        return "Bearer ***"
    return value


def _truncate(body):
    """把 bytes/str 转成可打印的截断文本,超长加 '…(截断)' 标记。"""
    if body is None:
        return ""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    if len(text) > LOG_BODY_MAX_CHARS:
        return text[:LOG_BODY_MAX_CHARS] + "…(截断)"
    return text


def log_received(req, raw, target):
    """记录收到的客户端请求(Authorization 脱敏,body 截断)。"""
    headers = " ".join("%s=%s" % (k, _mask_header(k, v)) for k, v in req.headers.items())
    logger.info("收到 %s %s target=%s headers=[%s] body=%s",
                req.method, req.full_path, target, headers, _truncate(raw))


def log_forwarded(method, url, headers, body):
    """记录转发给上游的请求(Authorization 脱敏,body 截断)。"""
    safe_headers = {k: _mask_header(k, v) for k, v in headers.items()}
    logger.info("转发 %s %s headers=%s body=%s", method, url, safe_headers, _truncate(body))


def log_response(status, elapsed):
    """记录上游响应状态码与耗时。"""
    logger.info("响应 status=%s 耗时=%.2fs", status, elapsed)

# ---------- 环境变量(运行时由 main() 校验并赋值) ----------
NEW_API_BASE_URL = ""
NEW_API_KEY = ""
PROXY_KEYS = []          # 解析后的多 key 列表
# ---------- 配置状态(启动时由 init_config() 填充) ----------
config_lock = threading.Lock()
config = {"keys": {}}    # {"keys": {"<key>": {"model": "..."}}}


def load_config_from_file(path):
    """从磁盘读配置 JSON。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config_atomic(cfg, path):
    """原子写配置:临时文件 + os.replace,防止半截写坏。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def get_model(key):
    with config_lock:
        entry = config["keys"].get(key, {})
        return entry.get("model")


def set_model(key, model):
    """更新指定 key 的 model 并持久化,立即对后续转发生效。"""
    with config_lock:
        config["keys"][key] = {"model": model}
        save_config_atomic(config, CONFIG_FILE_PATH)


def parse_upstream_verify():
    """解析 UPSTREAM_SKIP_SSL_VERIFY:为 true 时返回 False(跳过校验),否则正常校验。"""
    raw = os.environ.get("UPSTREAM_SKIP_SSL_VERIFY", "")
    return raw.strip().lower() not in ("1", "true", "yes", "on")


def fetch_models_from_upstream():
    """从上游拉取可用模型 id 列表。失败时抛异常,由调用方决定处理方式。"""
    url = NEW_API_BASE_URL + UPSTREAM_MODELS_PATH
    resp = requests.get(
        url,
        headers={"Authorization": "Bearer " + NEW_API_KEY},
        timeout=UPSTREAM_TIMEOUT,
        verify=UPSTREAM_VERIFY,
    )
    resp.raise_for_status()
    data = resp.json()
    return [m["id"] for m in data["data"]]


# ---------- 鉴权 ----------
def check_proxy_key(req):
    """校验客户端 Authorization: Bearer <PROXY_KEY>,返回匹配的 key 或 None。"""
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth.removeprefix("Bearer ")
    for k in PROXY_KEYS:
        try:
            if hmac.compare_digest(token, k):
                return k
        except TypeError:
            continue
    return None


# ---------- 请求体 / 头变换 ----------
def rewrite_body(raw, content_type, target):
    """仅当 JSON 且含 model 字段时改写为 target;否则原样返回 raw。"""
    if "application/json" not in content_type.lower():
        return raw
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(payload, dict) or "model" not in payload:
        return raw
    payload["model"] = target
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _make_upstream_headers(req):
    """构造上游请求头:复制客户端头,剔除冲突项,换上上游 key。"""
    drop = {
        "host",
        "content-length",
        "authorization",
        "accept-encoding",      # 让上游返回原始内容,避免转发后 Content-Encoding 被剥导致乱码
        "connection",           # 连接头由 requests 重新计算
        "transfer-encoding",    # 分块编码由 requests 重新计算
    }
    headers = {k: v for k, v in req.headers.items() if k.lower() not in drop}
    headers["Authorization"] = "Bearer " + NEW_API_KEY
    return headers


def _strip_upstream_headers(headers):
    """剔除会与 Flask 响应冲突的传输头。"""
    drop = {"content-encoding", "content-length", "transfer-encoding"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


# ---------- Flask 应用与管理路由 ----------
# 管理接口统一挂到 /_ccs/ 前缀,避免与 Claude Code / New-API 等工具的标准路径(/v1/*、/api/*)冲突
app = Flask(__name__)


@app.get("/v1/models", strict_slashes=False)
def public_list_models():
    """本地返回固定模型别名,不暴露或查询上游模型列表。"""
    if not check_proxy_key(request):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({
        "object": "list",
        "data": [{
            "id": PUBLIC_MODEL_ID,
            "object": "model",
            "created": 0,
            "owned_by": "cc-proxy",
        }],
    })


@app.get("/_ccs/api/model")
def api_get_model():
    key = check_proxy_key(request)
    if not key:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"model": get_model(key)})


@app.post("/_ccs/api/model")
def api_set_model():
    key = check_proxy_key(request)
    if not key:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        return jsonify({"error": "model 不能为空"}), 400
    model = model.strip()
    set_model(key, model)
    return jsonify({"status": "success", "model": model})


@app.get("/_ccs/api/list")
def api_list_models():
    key = check_proxy_key(request)
    if not key:
        return jsonify({"error": "unauthorized"}), 401
    try:
        models = fetch_models_from_upstream()
    except Exception as exc:
        return jsonify({"error": "upstream list failed", "detail": str(exc)}), 502
    return jsonify({"models": models})


# ---------- 透明代理(处理未匹配专用路由的路径) ----------
@app.route("/<path:full_path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
def transparent_proxy(full_path):
    key = check_proxy_key(request)
    if not key:
        return jsonify({"error": "unauthorized"}), 401

    headers = _make_upstream_headers(request)
    raw = request.get_data()
    target = get_model(key)
    if is_debug_enabled():
        log_received(request, raw, target)

    if request.method in ("POST", "PUT", "PATCH"):
        body = rewrite_body(raw, request.headers.get("Content-Type", ""), target)
    else:
        # GET/DELETE 等方法不改写 body(即使意外带了 JSON body)
        body = raw

    # 用 request.full_path 保留 query string;无查询串时去掉 werkzeug 追加的尾随 "?"
    full_path = request.full_path if request.query_string else request.path
    url = NEW_API_BASE_URL + full_path
    if is_debug_enabled():
        log_forwarded(request.method, url, headers, body)

    start = time.monotonic()
    try:
        upstream_resp = requests.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body,
            stream=True,
            timeout=UPSTREAM_TIMEOUT,
            verify=UPSTREAM_VERIFY,
        )
    except requests.RequestException as exc:
        return jsonify({"error": "upstream request failed", "detail": str(exc)}), 502

    if is_debug_enabled():
        log_response(upstream_resp.status_code, time.monotonic() - start)

    resp_headers = _strip_upstream_headers(upstream_resp.headers)

    def stream():
        try:
            for chunk in upstream_resp.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    yield chunk
        finally:
            upstream_resp.close()

    return Response(stream(), status=upstream_resp.status_code, headers=resp_headers)


# ---------- 启动引导 ----------
def get_default_model():
    """从上游拉取模型列表,返回第 1 个模型 id。"""
    models = fetch_models_from_upstream()
    if not models:
        raise RuntimeError("上游模型列表为空,无法确定默认 model")
    return models[0]


def init_config():
    """config.json 存在且格式正确→加载;不存在或格式不正确→重建。
    最后为缺失的 key 补齐默认 model 并持久化。"""
    global config
    if os.path.exists(CONFIG_FILE_PATH):
        try:
            loaded = load_config_from_file(CONFIG_FILE_PATH)
            if isinstance(loaded.get("keys"), dict):
                config = loaded
            else:
                logger.warning("config.json 格式不正确,将重新生成")
        except (json.JSONDecodeError, OSError):
            logger.warning("config.json 读取失败,将从上游重新获取")

    existing = config.get("keys", {})
    missing = [k for k in PROXY_KEYS if k not in existing]
    if missing:
        default = get_default_model()
        for k in missing:
            existing[k] = {"model": default}
        config["keys"] = existing
        save_config_atomic(config, CONFIG_FILE_PATH)


def main():
    global NEW_API_BASE_URL, NEW_API_KEY, PROXY_KEYS, UPSTREAM_VERIFY
    for name in ("NEW_API_BASE_URL", "NEW_API_KEY", "PROXY_KEY"):
        if not os.environ.get(name):
            raise RuntimeError("缺少必填环境变量: " + name)
    NEW_API_BASE_URL = os.environ["NEW_API_BASE_URL"].rstrip("/")
    NEW_API_KEY = os.environ["NEW_API_KEY"]
    PROXY_KEYS = [k.strip() for k in re.split(r'[,\s]+', os.environ["PROXY_KEY"]) if k.strip()]
    if not PROXY_KEYS:
        raise RuntimeError("PROXY_KEY 不能为空,需至少一个 key")
    for k in PROXY_KEYS:
        try:
            k.encode("ascii")
        except UnicodeEncodeError:
            raise RuntimeError("PROXY_KEY 中的 key 只能包含 ASCII 字符: " + k)
    UPSTREAM_VERIFY = parse_upstream_verify()
    init_config()
    app.run(host="0.0.0.0", port=8000, threaded=True)


if __name__ == "__main__":
    main()
