# Sub2API Quota Monitor / Sub2API 额度监控

这是一个独立的 New API/Sub2API 额度监控服务。它把采样快照保存到 SQLite，提供 New API 风格的历史曲线和额度周期分析，并可在使用量曲线出现统计显著的斜率拐点时发送 Bark 通知。

This is a standalone quota monitor for a New API/Sub2API deployment. It stores snapshots in SQLite, serves a New API-style history page, estimates remaining capacity, and can send Bark alerts when the usage curve has a statistically significant slope change.

## 中文

### Docker 快速开始

```sh
cp .env.example .env
# 编辑 .env；不要把它提交到 Git。
docker login ghcr.io
docker compose pull
docker compose up -d
```

Compose 使用已发布的镜像：`ghcr.io/wangchudi/newapi-about-monitor:latest`。仓库和镜像目前是私有的，拉取镜像需要具有 `read:packages` 权限的 GitHub PAT。页面默认地址为 `http://127.0.0.1:8320/`，历史数据库位于 `./data/history.db`。

#### `.env` 配置

`cp .env.example .env` 后编辑 `.env`。Compose 会自动读取该文件，并把其中的变量传给容器；`.env` 不要提交到 Git。

必填变量：

- `SUB2API_BASE_URL`：monitor 容器可访问的 Sub2API 基础地址。
- `SUB2API_ADMIN_KEY`：Sub2API 管理员密钥。也可以改用 Docker secret 文件 `SUB2API_ADMIN_KEY_FILE`。

常用选填变量：

- `MAIN_ACCOUNT_ID`、`SPARK_ACCOUNT_ID`：账号 ID，默认分别为 `571`、`576`。
- `BARK_ENABLED`：是否启用通知，默认 `false`。
- `BARK_URL`：Bark 完整地址；仅当 `BARK_ENABLED=true` 且没有使用 `BARK_URL_FILE` 时必填。
- `FRAME_ANCESTORS`：允许哪些网页来源嵌入 monitor 的 iframe，默认只允许同源的 `'self'`。
- `QUOTA_URL`、`USAGE_URL`、`SPARK_QUOTA_URL`、`SPARK_USAGE_URL`：上游路由不是标准路径时使用的完整 URL 覆盖项。
- `PORT`、采样间隔、快速采样阈值、请求超时和 `HISTORY_RETENTION_DAYS`：都有安全默认值，按需调整即可。

`FRAME_ANCESTORS` 不是上游地址。例如 monitor 被 `https://newapi.example.com` 的 About 页面嵌入时，设置 `FRAME_ANCESTORS=https://newapi.example.com`；多个来源用空格分隔。只允许实际的父页面来源，不要填 `*`。

### 内网 Sub2API

可以使用内网地址。`SUB2API_BASE_URL` 是由 monitor 容器后端访问的，浏览器不会直接请求 Sub2API，因此用户浏览器只需要能打开 monitor 页面。

- Sub2API 在同一个 Docker 网络中：使用 Docker 服务名，例如 `http://sub2api:3000`，并让两个 Compose 项目加入同一个 external network。
- Sub2API 在同一台 NAS 或局域网内：使用容器能够路由到的局域网 IP 或内网域名，例如 `http://192.168.x.x:port`。
- 不要在容器中使用 `http://127.0.0.1:port` 指向宿主机；该地址表示 monitor 容器自身。只有 Sub2API 和 monitor 在同一个容器时才可这样使用。

如果内网域名使用 HTTPS，自签名证书必须被容器信任；否则应使用受信任证书或可路由的 HTTP 内网地址。将页面嵌入 New API 时，还要把父页面来源加入 `FRAME_ANCESTORS`，并确保反向代理把 monitor 路径转发到本容器。

### 二进制

GitHub Actions 会构建 Linux、Windows 和 macOS 的单文件二进制。二进制会从可执行文件旁边的 `data` 目录读取/写入 SQLite，并将网页资源打包在程序内。程序通过操作系统的进程环境变量读取配置，不会自动解析旁边的 `.env` 文件。

Linux/macOS 可以先加载 `.env` 再启动：

```sh
set -a
. ./.env
set +a
./newapi-about-monitor
```

Windows PowerShell 可以这样加载：

```powershell
Get-Content .env | Where-Object { $_ -and -not $_.StartsWith('#') } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
}
./newapi-about-monitor.exe
```

也可以直接在 systemd、Unraid、Windows 服务或其他 secret 管理器中注入同名环境变量。

目标机器不需要安装 Python；构建使用 PyInstaller。

### GitHub Actions 与发布

- 推送匹配 `v*` 的标签会运行测试、构建多平台二进制，并将多架构镜像推送到 `ghcr.io/wangchudi/newapi-about-monitor`。
- 同一个标签会创建 GitHub Release，附带二进制和 `SHA256SUMS`。
- `workflow_dispatch` 可运行检查和构建，但不会创建 Release 或推送镜像。

运行时凭据只放在目标主机的环境变量或 secret 管理器中，不放入 GitHub 源码。Actions 仅使用 `GITHUB_TOKEN` 发布镜像和 Release 资产。

### 采样策略

采集器通常每 5 分钟记录一次（`NORMAL_INTERVAL_SECONDS=300`）。当主账号在上一个实际采样间隔内的使用额度变化，折算到 5 分钟后达到以下任一条件，就切换为每 1 分钟采样：

- `FAST_USAGE_AMOUNT_THRESHOLD`：使用额度变化阈值；
- `FAST_USAGE_REQUEST_THRESHOLD`：请求数变化阈值。

快速模式保持 `FAST_HOLD_SECONDS`，期间再次出现较大变化会延长快速模式。计算使用实际采样间隔归一化，因此切换到 1 分钟不会凭空制造用量尖峰。

### 斜率与预计用完时间

使用额度/已用百分比图只使用主账号数据：

1. 相同的四舍五入百分比先分组，再用三点窗口做中位数平滑。
2. 使用普通最小二乘拟合 `amount = intercept + slope * used_percent`，斜率表示每增加一个百分点对应的额度变化。
3. 递归的双侧变点检测要求变点两侧各至少 5 个观测点、斜率相对变化至少 10%，且校正后的双侧正态检验 `p <= 0.05`，最多保留 4 个变点。
4. 当前有效斜率取最后一个显著变点之后的分段；图表标记所有变点，Bark 只对新出现的标记发送一次通知。
5. 预计总额度由当前分段回归得到；预计用完时间使用剩余额度除以近期多采样跨度的正额度速率中位数，不再用整数百分比跳变直接外推。

容器启动时会为已有变点建立基线，因此开启 Bark 不会重复发送历史通知。新变点只发送一次；临时发送失败会在该标记仍为当前标记时重试。

### HTTP 接口

- `GET /healthz`：容器健康检查。
- `GET /api/status`：最新缓存、上游状态和采样策略。
- `GET /api/history?hours=168`：历史快照和检测出的额度周期。
- `POST /api/refresh`：立即强制采集一次。

服务同时提供 `app/html/index.html` 及 VChart、字体等网页资源。

## English

### Quick start with Docker

```sh
cp .env.example .env
# Edit .env. Never commit it.
docker login ghcr.io
docker compose pull
docker compose up -d
```

Compose uses the published image `ghcr.io/wangchudi/newapi-about-monitor:latest`. The repository and image are currently private, so pulling the image requires a GitHub PAT with `read:packages`. The page is available at `http://127.0.0.1:8320/`; persistent history is stored in `./data/history.db`.

#### `.env` configuration

Run `cp .env.example .env` and edit the new file. Docker Compose automatically reads `.env` and passes the values into the container. Never commit this file.

Required variables:

- `SUB2API_BASE_URL`: the Sub2API base URL reachable from the monitor container.
- `SUB2API_ADMIN_KEY`: the Sub2API administrator key; alternatively use the Docker secret file configured by `SUB2API_ADMIN_KEY_FILE`.

Common optional variables:

- `MAIN_ACCOUNT_ID` and `SPARK_ACCOUNT_ID`, defaulting to `571` and `576`.
- `BARK_ENABLED`, defaulting to `false`.
- `BARK_URL`, required only when Bark is enabled and `BARK_URL_FILE` is not used.
- `FRAME_ANCESTORS`, defaulting to `'self'`, to control which web origins may embed the page in an iframe.
- `QUOTA_URL`, `USAGE_URL`, `SPARK_QUOTA_URL`, and `SPARK_USAGE_URL` for non-standard upstream routes.
- `PORT`, sampling intervals, fast-mode thresholds, timeouts, and `HISTORY_RETENTION_DAYS`, all of which have defaults.

`FRAME_ANCESTORS` is not an upstream URL. If New API embeds the page from `https://newapi.example.com`, set `FRAME_ANCESTORS=https://newapi.example.com`; separate multiple origins with spaces. Do not use `*`.

### Using an internal Sub2API URL

An internal URL works. The monitor backend inside the container contacts Sub2API; the browser does not contact Sub2API directly, so the browser only needs access to the monitor page.

- If both services share a Docker network, use the service DNS name such as `http://sub2api:3000` and attach both Compose projects to the same external network.
- If Sub2API is on the NAS or LAN, use a LAN IP or internal DNS name reachable from the container.
- Do not use `http://127.0.0.1:port` for a host service from inside the monitor container; that points back to the monitor container itself.

For internal HTTPS, the certificate must be trusted by the container. When embedding the page in New API, add the parent origin to `FRAME_ANCESTORS` and route the monitor path to this container through the reverse proxy.

### Binary, Actions, sampling, and slope model

GitHub Actions builds one-file Linux, Windows, and macOS binaries with the same environment-variable configuration. The binary reads the operating system process environment and does not parse a neighboring `.env` file automatically.

On Linux/macOS, load `.env` before starting it:

```sh
set -a
. ./.env
set +a
./newapi-about-monitor
```

On Windows PowerShell, load each non-comment line into the current process environment, then run `./newapi-about-monitor.exe`. The same variables can also be supplied by systemd, Unraid, Windows Services, or another secret manager.

Tags matching `v*` also build and publish the multi-architecture image and create a Release with `SHA256SUMS`; `workflow_dispatch` runs checks and builds without publishing.

The collector normally records every five minutes. It switches to one-minute sampling when the main-account amount or request delta, normalized to a five-minute interval, crosses its configured threshold, and holds fast mode for `FAST_HOLD_SECONDS`.

The usage/percentage chart groups repeated rounded percentages, applies a three-point median smoother, fits ordinary least squares, and recursively detects significant two-sided slope changes. A change requires five observations on each side, at least a 10% relative slope change, and adjusted `p <= 0.05`. The selected slope is the segment after the latest change. Exhaustion time uses remaining amount divided by a median positive amount-per-second rate from recent multi-sample spans.

Existing markers are baselined at startup, new markers are notified once, and transient Bark failures are retried while the marker remains current.

### HTTP endpoints

- `GET /healthz` - container health check.
- `GET /api/status` - latest cache, source status, and sampling policy.
- `GET /api/history?hours=168` - snapshots and detected quota cycles.
- `POST /api/refresh` - force one collection immediately.
