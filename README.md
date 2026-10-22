# CC-Proxy

将 OpenAI 兼容请求转发到上游网关，并把请求 JSON 中的 `model` 替换为当前选中的模型。支持模型热切换和流式响应。

项目提供两个独立启动入口：

| 对比项 | `app.py`：服务端入口 | `tray.py`：Windows 托盘入口 |
| --- | --- | --- |
| 适用场景 | 终端运行、服务器、Docker | Windows 桌面，在托盘中查看和切换模型 |
| 配置方式 | 环境变量；Docker Compose 从 `.env` 注入 | `tray.json`，无需环境变量 |
| 客户端鉴权 | 必须填写配置中的 `PROXY_KEY` | 不校验客户端 Key，可留空或任意填写 |
| 模型状态 | 多个代理 Key，每个 Key 独立设置模型 | 共用一个当前模型 |
| 模型切换 | 管理 API | 托盘菜单或管理 API |
| 持久化文件 | `config.json` | `tray.json` |
| 默认监听 | `0.0.0.0:8000` | `127.0.0.1:8000`，可在 JSON 中修改 |
| 上游证书校验 | 默认开启 | 默认关闭，可在 JSON 中开启 |

运行任意一个入口即可。`tray.py` 会自行启动代理服务，源码运行时需要保留同目录的 `app.py`。

## 一、app.py：服务端入口

### 配置环境变量

| 环境变量 | 说明 |
| --- | --- |
| `NEW_API_BASE_URL` | 必填，上游根地址，例如 `https://api.example.com`，不用添加 `/v1` |
| `NEW_API_KEY` | 必填，上游 API Key |
| `PROXY_KEY` | 必填，客户端访问本代理使用的 Key；支持逗号或空白分隔多个 ASCII Key |
| `PUBLIC_MODEL_ID` | 可选，对客户端显示的固定模型名称；未设置或为空时，显示对应 Key 的当前实际模型 |
| `DEBUG` | 可选，设为 `true` 开启请求调试日志，默认关闭 |
| `LOG_DIR` | 可选，调试日志目录，默认是程序旁的 `logs/` |
| `UPSTREAM_SKIP_SSL_VERIFY` | 可选，设为 `true` 跳过上游 HTTPS 证书校验；默认进行校验 |

### 使用 Docker Compose

复制配置模板，填写其中的上游地址、上游 Key 和代理 Key：

```bash
cp .env.example .env
```

编辑完成后启动：

```bash
docker compose up -d --build
```

现有 Docker 配置启动的是 `app.py`。项目目录挂载到容器中，`config.json` 和日志保存在本地项目目录。

修改 `.env` 后重新执行上述启动命令，使新的环境设置生效。更新代码后也可使用该命令重新构建和启动。

### 直接运行 Python

先安装依赖：

```bash
python -m pip install -r requirements.txt
```

Linux / macOS 的 Bash 示例：

```bash
export NEW_API_BASE_URL="https://api.example.com"
export NEW_API_KEY="sk-your-upstream-key"
export PROXY_KEY="sk-local-one,sk-local-two"
python app.py
```

Windows PowerShell 示例：

```powershell
$env:NEW_API_BASE_URL = "https://api.example.com"
$env:NEW_API_KEY = "sk-your-upstream-key"
$env:PROXY_KEY = "sk-local-one,sk-local-two"
python app.py
```

**直接运行 `python app.py` 不会自动读取 `.env`。** `.env` 由 Docker Compose 注入；本地运行需要先设置进程环境变量。

服务监听 `0.0.0.0:8000`。客户端通常配置为：

| 客户端选项 | 填写内容 |
| --- | --- |
| API 地址 / Base URL | `http://127.0.0.1:8000/v1`，远程访问时换成服务器地址 |
| API Key | `PROXY_KEY` 中的一个 Key，例如 `sk-local-one` |
| 模型名称 | `PUBLIC_MODEL_ID` 有值时使用该名称，否则使用当前实际模型名 |

### 模型保存与切换

`config.json` 保存在 `app.py` 同目录，仅记录各代理 Key 的当前模型：

```json
{
  "keys": {
    "sk-local-one": {"model": "model-a"},
    "sk-local-two": {"model": "model-b"}
  }
}
```

某个代理 Key 尚无配置时，启动会从上游获取模型列表，使用第一个模型并保存。后续通过管理接口切换模型会立即生效，并写入该文件。

例如，切换 `sk-local-one` 使用的模型：

```bash
curl -X POST http://127.0.0.1:8000/_ccs/api/model \
  -H "Authorization: Bearer sk-local-one" \
  -H "Content-Type: application/json" \
  -d '{"model":"model-b"}'
```

## 二、tray.py：Windows 专用托盘入口

全部操作通过系统托盘菜单完成，没有独立操作窗口。配置来自 JSON，不需要设置环境变量。

### 源码运行

安装 Python 及原项目依赖，保留本项目文件，然后运行：

```powershell
python -m pip install -r requirements.txt
pythonw tray.py
```

需要查看控制台输出时，可以使用 `python tray.py`。托盘由 Windows 原生接口实现，无需额外安装托盘组件。

首次启动若没有 `tray.json`，会自动在 `tray.py` 旁边生成默认配置，并保留托盘供重新加载。填写 `upstream.base_url` 和 `upstream.api_key` 后，右键托盘图标，点击“重新加载 tray.json”。已有配置不会被覆盖。

也可以指定其他配置路径，父目录不存在时会自动创建：

```powershell
pythonw tray.py --config "C:\path\tray.json"
```

### JSON 配置

```json
{
  "upstream": {
    "base_url": "https://api.example.com",
    "api_key": "sk-your-upstream-key",
    "verify_ssl": false,
    "timeout": 300
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8000
  },
  "model": "",
  "display_model": "",
  "debug": false
}
```

| 配置项 | 说明 |
| --- | --- |
| `upstream.base_url` | 必填，上游根地址，不用添加 `/v1` |
| `upstream.api_key` | 必填，真实上游 Key，仅用于代理访问上游 |
| `upstream.verify_ssl` | 是否校验上游 HTTPS 证书，默认 `false`；设为 `true` 时开启校验 |
| `upstream.timeout` | 转发超时，单位为秒，默认 300；获取模型列表的超时为 15 秒 |
| `server.host` | 默认 `127.0.0.1`，仅允许本机访问 |
| `server.port` | 本地监听端口，默认 8000 |
| `model` | 当前实际模型，可留空或省略；启动后自动获取上游第一个模型并保存 |
| `display_model` | 对客户端 `/v1/models` 显示的模型名称；为空时返回当前实际模型名 |
| `debug` | 默认 `false`；设为 `true` 时，将调试日志写入配置文件旁的 `logs/tray.log` |

JSON 使用 UTF-8 编码、英文双引号，不支持注释和末尾多余的逗号。

每次启动都会向上游获取候选模型列表，列表仅保存在内存中，不保存 `models` 字段。已有的当前模型会保留；仅在 `model` 为空时自动选取第一个模型。获取失败可从菜单重试。

`verify_ssl: false` 表示连接时不验证上游证书真实性；有可信 HTTPS 证书的上游可以设为 `true`。

### 托盘菜单

| 菜单项 | 作用 |
| --- | --- |
| 运行状态、监听地址、当前模型 | 查看代理是否启动，以及使用的端口和模型 |
| 选择模型 | 在子菜单中切换，当前模型带勾选标记；切换立即作用于后续请求并保存 |
| 刷新上游模型列表 | 后台获取候选列表，失败时保留已有列表 |
| 重新加载 tray.json | 加载手动编辑后的配置 |
| 状态 | 查看启动、配置、刷新或保存错误 |
| 退出 | 关闭托盘和代理，释放监听端口 |

图标可能收纳在任务栏右下角的“显示隐藏的图标”中。

修改 `model`、`display_model` 或 `debug` 后可重新加载。修改上游设置或监听地址、端口后，需要从菜单退出，再重新启动。

手动编辑 JSON 后，请先重新加载，再从托盘切换模型。程序检测到文件被外部修改时，会阻止保存，避免覆盖尚未加载的修改。

### Agent / AI 客户端怎么填

以下以默认端口 8000 为例。更改 `server.port` 后，客户端中的端口也要同步修改。

| 客户端选项 | 填写内容 |
| --- | --- |
| API 地址 / Base URL | `http://127.0.0.1:8000/v1` |
| API Key | 可以留空；工具要求必填时，填写 `local` 等任意值 |
| 模型名称 | `display_model` 有值时使用该名称，否则使用托盘当前实际模型名 |

如果客户端自动添加 `/v1`，则填写 `http://127.0.0.1:8000`，避免路径重复。

本地代理和管理接口均不校验客户端 Key。转发时，`Authorization` 始终使用 JSON 中的真实上游 Key；客户端带有 `x-api-key` 时，该字段也会替换为真实上游 Key。客户端没有该字段时，不额外添加。

例如设置 `"display_model": "my-model"` 后，客户端查询模型列表时看到 `my-model`，实际请求仍使用托盘选中的模型。聊天响应中的模型字段由上游返回。

可以在 PowerShell 中检查代理，无需携带 Key：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/models"
```

刚启动且默认模型尚未获取完成时，接口会暂时返回 503，等待片刻或从托盘刷新后再试。

默认仅本机访问。如果将 `server.host` 改为 `0.0.0.0`，其他电脑可能访问到代理及管理接口，且它们仍不校验客户端 Key。

### 打包成 EXE

在 Windows 中安装打包工具，并执行：

```powershell
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name CC-Proxy --hidden-import app tray.py
```

生成的文件为 `dist/CC-Proxy.exe`，无需安装 Python 即可运行。`--hidden-import app` 用于把动态加载的代理模块一起打包。

EXE 读取同目录的 `tray.json`，缺少时自动生成。复制或分发程序时，可保留 EXE 并让使用者首次启动后填写配置，也可以提供一份空白配置模板。真实上游 Key 不应包含在分发模板中。

更新时先退出托盘程序，再替换 EXE，保留自己的 JSON 配置。`dist/` 和项目根目录的 `tray.json` 已加入 Git 忽略规则。

## 管理接口对照

两种入口使用相同路径，但鉴权方式不同：

- `app.py`：每个请求必须携带 `Authorization: Bearer <PROXY_KEY>`，操作对应 Key 的模型。
- `tray.py`：无需携带 Key，操作唯一的当前模型。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/v1/models` | 本地返回对客户端显示的模型名称，不查询上游 |
| GET | `/_ccs/api/model` | 查看当前实际模型 |
| POST | `/_ccs/api/model` | 以 JSON `{"model":"model-b"}` 切换并保存模型 |
| GET | `/_ccs/api/list` | 查询上游模型列表 |

管理路径使用 `/_ccs/` 前缀，避免与上游标准路径冲突。其余已匹配的代理路径透明转发到上游。

## 模型能力与流式响应

上下文、图片和思考能力由实际模型及上游网关决定。`PUBLIC_MODEL_ID` 和 `display_model` 只控制客户端模型列表的显示名称，不会增加模型能力。

代理只改写 JSON 中的模型名称，不转换协议、适配思考参数或为图片请求另选模型。

如果在代理前方使用 Nginx，应关闭响应缓冲，避免影响 SSE 流式输出：

```nginx
location / {
    proxy_buffering off;
}
```
