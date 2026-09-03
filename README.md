# CC-Proxy

单一 `model` 热切换的透明转发反向代理:转发 OpenAI 兼容请求到上游 New-API,并按需改写请求体里的 `model` 字段。支持多个 `PROXY_KEY`,每个 key 独立维护自己的 model。

## 环境变量

- `NEW_API_BASE_URL` — 上游 New-API 网关地址,如 `https://your-new-api.com`
- `NEW_API_KEY` — 访问上游网关的 API Key
- `PROXY_KEY` — 客户端访问本代理时使用的 Key,支持逗号分隔多个 key(仅 ASCII 字符)
- `DEBUG` — 可选,设为 `true` 开启调试日志(落文件到 `logs/` 目录)
- `UPSTREAM_SKIP_SSL_VERIFY` — 可选,设为 `true` 跳过上游 HTTPS 证书校验。用于自签名证书的上游,不安全,仅建议测试环境

## 运行

### Docker Compose

编辑 `.env` 填入真实值后:

```bash
docker compose up -d
```

如需开启 DEBUG 日志或跳过上游证书校验,在 `.env` 中设置 `DEBUG=true` / `UPSTREAM_SKIP_SSL_VERIFY=true` 后重启容器。

后续更新:

```bash
git pull && docker compose up -d --build
```

### 本地运行

```bash
cp .env.example .env  # 编辑填入真实值
pip install -r requirements.txt
python app.py
```

服务监听 `0.0.0.0:8000`。

## 客户端模型

客户端统一填写 `claude-sonnet-5`。`GET /v1/models` 经代理 Key 鉴权后在本地返回这个固定别名,不会请求上游模型列表:

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer $PROXY_KEY"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "claude-sonnet-5",
      "object": "model",
      "created": 0,
      "owned_by": "cc-proxy"
    }
  ]
}
```

此名称仅为代理别名。实际转发时,请求体中的 `model` 仍按当前 `PROXY_KEY` 的配置替换为上游模型,例如 DeepSeek V4 或 GLM-5.3。通过管理接口切换后端模型后,客户端继续使用同一别名。

上下文、图片和思考参数支持取决于实际后端与上游网关;别名不会增加这些能力。当前代理仅改写模型名称,不转换协议、适配思考参数或为图片请求单独选择视觉模型。

## 管理接口

> 管理接口统一挂在 `/_ccs/` 前缀下,避免与 Claude Code / New-API 等工具的标准路径(`/v1/*`、`/api/*`)冲突。所有管理接口需携带 `Authorization: Bearer $PROXY_KEY`。

查看当前模型:

```bash
curl http://localhost:8000/_ccs/api/model \
  -H "Authorization: Bearer $PROXY_KEY"
```

切换模型(立即生效并持久化):

```bash
curl -X POST http://localhost:8000/_ccs/api/model \
  -H "Authorization: Bearer $PROXY_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-reasoner"}'
```

拉取上游模型列表:

```bash
curl http://localhost:8000/_ccs/api/list \
  -H "Authorization: Bearer $PROXY_KEY"
```

## 配置文件

配置持久化在 `app.py` 同目录下的 `config.json`,格式:

```json
{
  "keys": {
    "sk-key-aaa": {"model": "deepseek-reasoner"},
    "sk-key-bbb": {"model": "claude-sonnet-4-20250514"}
  }
}
```

首次启动时自动从上游拉取模型列表,为每个 key 使用第 1 个;配置格式不正确时会自动重新生成。

## 反向代理要求

如需在前方再加一层 Nginx,必须关闭缓冲,否则 SSE 流式响应会被缓冲:

```nginx
location / {
    proxy_buffering off;
}
```
