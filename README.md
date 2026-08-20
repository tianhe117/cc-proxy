# CC-Proxy

单一 `model` 热切换的透明转发反向代理:转发 OpenAI 兼容请求到上游 New-API,并按需改写请求体里的 `model` 字段。

## 环境变量

- `NEW_API_BASE_URL` — 上游 New-API 网关地址,如 `https://your-new-api.com`
- `NEW_API_KEY` — 访问上游网关的 API Key
- `PROXY_KEY` — 客户端访问本代理时使用的 Key(仅 ASCII 字符)

## 运行

### Docker Compose

编辑 `.env` 填入真实值后:

```bash
docker compose up -d
```

如需开启 DEBUG 日志,在 `.env` 中设置 `DEBUG=true` 后重启容器。

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
{"model": "deepseek-reasoner"}
```

首次启动时自动从上游拉取模型列表并使用第 1 个;若文件不存在或读取失败也会自动重新获取。

## 反向代理要求

如需在前方再加一层 Nginx,必须关闭缓冲,否则 SSE 流式响应会被缓冲:

```nginx
location / {
    proxy_buffering off;
}
```
