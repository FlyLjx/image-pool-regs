# GPT-REG-TOOLS

独立的 ChatGPT 注册控制台，支持协议与 Playwright Chromium 两种渠道，包含本地管理员登录、自动注册任务、实时日志、账号列表和 JSON 持久化。

## 运行

环境要求：

- Python 3.11+
- Node.js 20+（用于 Sentinel VM）
- Playwright + Camoufox（浏览器渠道，正式 Chrome 可选）

安装并启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m camoufox fetch
.\start.ps1
```

访问 `http://127.0.0.1:8090`。

云服务器 Docker 部署：

```bash
REG_DATA_DIR_HOST=/opt/image-pool-regs-data docker compose up -d --build
```

Compose 默认将服务发布到 `8090`，注册与云端请求均直连，并同时启动 FlareSolverr 容器作为 Cloudflare 异常兜底。GitHub Actions 的 `Deploy` 工作流会优先复用服务器 `image-pool_default` 网络中的 `image-pool-flaresolverr`，不存在时再由 Compose 启动同栈实例。工作流需要配置 `DEPLOY_HOST`、`DEPLOY_USER`、`DEPLOY_SSH_KEY`，可选配置 `DEPLOY_PORT` 和 `DEPLOY_PATH`。

首次启动账号：

```text
用户名：admin
密码：admin123
```

首次账号可通过环境变量 `REG_ADMIN_USERNAME`、`REG_ADMIN_PASSWORD` 指定。用户建立后以密码哈希写入 `data/users.json`，后续启动不会覆盖。

## 配置

登录后在「系统设置」填写：

- YYDS Mail API 地址和 API Key
- Outlook 邮箱池：每行 `邮箱----密码----Client ID----Refresh Token`，默认将每个基础邮箱分裂为 5 个带 12 位随机标签的注册地址；验证码统一从该 Outlook 收件箱读取，刷新后的 Refresh Token 和分裂标签会写回 `data/outlook_mailboxes.json`
- 邮箱域名池，默认在 `team.edu.yccc.me` 与 `auto` 中逐次随机选择
- 代理地址（可留空）
- Chrome/Windows 或 Safari/macOS 指纹
- 注册渠道：协议模式或浏览器模式；浏览器模式默认使用参考项目同款 Camoufox + Playwright，也可切换本机正式 Chrome，每个账号使用全新环境
- Sentinel SO Token 与 FlareSolverr 异常兜底开关
- 云端地址、管理员密钥、容量模型和成功账号上传开关

FlareSolverr 默认容器地址为 `http://flaresolverr:8191`。正常协议响应不会调用该服务；直连网络异常或明确出现 Cloudflare 验证页时才启动兜底。容器使用注册代理时会把本机回环地址自动转换为 `host.docker.internal`，并按代理出口复用验证 Cookie。

启用云端后，开始注册会先读取 `GET /api/image-pool/capacity?limit=60`。容量状态为 `idle`、`enough` 或 `saturated` 时跳过本轮；状态为 `shortage` 时按 `recommended_register_accounts` 与手动数量的较小值执行。成功账号通过 `POST /api/accounts` 逐个上传。

注册机与 IMAGE POOL 的适配边界是：注册机只负责邮箱注册和获取账号凭证，IMAGE POOL 负责账号验证、额度、冷却、参考图能力和并发调度。上传请求使用云端管理员密钥，并发送 `accounts` 数组中的完整账号对象（至少包含 `email`、`access_token`，推荐同时包含 `refresh_token`、`id_token`、`password`、`cookies`、`user_agent`、`source_type`、`registration_channel` 和 `created_at`）。本地的健康计数、存活计时和同步标记不会上传。

云端返回的 `added`、`skipped`、`refreshed` 和 `errors` 会写回本地账号。无验证错误时标记为 `cloud_sync_status=synced`、`cloud_validation_status=verified`；有验证错误或网络失败时标记为 `failed`，后续可按该状态重试。参考图账号是否冷却不由注册机判断，IMAGE POOL 会在参考图请求返回 429 时只暂停该账号的参考图能力，文生图继续由号池调度。

自动监听开启后会按配置间隔持续读取容量。缺口连续达到确认次数后，按云端建议数量和单批上限自动启动注册；注册任务运行期间监听器只等待，不会叠加新批次。

账号页支持检测全部或单个账号。检测接口为 `https://chatgpt.com/backend-api/me`：正常响应标记存活；明确封禁标记为封禁并停止恢复；`401` 进入独立的并发恢复队列，使用保存的邮箱和密码重新登录，必要时读取 YYDS 登录验证码，并将新 Token 原子写回 `data/accounts.json`。检测请求遇到 Cloudflare 挑战页时才调用 FlareSolverr，导入验证 Cookie 后携带原 Token 重新检测；同一批任务会复用验证结果。账号会保存本轮存活起止、上轮/累计存活秒数与恢复次数，恢复成功后从恢复时间开始新一轮存活计时。

协议链路依次执行 OAuth/PKCE 初始化、邮箱密码提交、邮件验证码、账号资料创建和 OAuth Token 换取。浏览器链路复用同一邮箱池，通过独立 Chromium Profile 完成页面注册，并从浏览器会话导出 Token、Cookie 和 User-Agent。单个账号成功后立即写盘。

## JSON 文件

运行时数据默认位于 `data/`：

| 文件 | 内容 |
| --- | --- |
| `accounts.json` | 完整账号、密码和 Token |
| `settings.json` | 注册、邮箱、网络和验证配置 |
| `users.json` | 管理员用户名与密码哈希 |
| `app.json` | 本地会话签名密钥 |

可用 `REG_DATA_DIR` 修改数据目录。账号页面展示脱敏字段，「导出 JSON」下载完整 `accounts.json` 数据。

## 测试

```powershell
python -m pytest
python -m compileall -q app
node --check web\app.js
node --check tools\openai_so_vm.mjs
```
