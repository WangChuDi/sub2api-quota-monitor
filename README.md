# Sub2API Quota Monitor / Sub2API 额度监控

这是一个独立的 New API/Sub2API 额度监控服务。它把采样快照保存到 SQLite，提供 New API 风格的历史曲线和额度周期分析，并可在使用量曲线出现统计显著的斜率拐点时发送 Bark 通知。

This is a standalone quota monitor for a New API/Sub2API deployment. It stores snapshots in SQLite, serves a New API-style history page, estimates remaining capacity, and can send Bark alerts when the usage curve has a statistically significant slope change.

## 中文

### Docker 快速开始

```sh
cp config.example.toml config.toml
# 编辑 config.toml；不要把它提交到 Git。
docker login ghcr.io
docker compose pull
docker compose up -d
```

Compose 使用已发布的镜像：`ghcr.io/wangchudi/newapi-about-monitor:latest`。仓库和镜像目前是私有的，拉取镜像需要具有 `read:packages` 权限的 GitHub PAT。页面默认地址为 `http://127.0.0.1:8320/`，历史数据库位于 `./data/history.db`。

#### `config.toml` 配置

复制 `config.example.toml` 为 `config.toml` 后编辑。Compose 会把这个文件只读挂载到容器的 `/app/config.toml`；配置文件包含密钥，建议设置为仅运行用户可读，并且不要提交到 Git。

必填变量：

- `[sub2api].base_url`：monitor 容器可访问的 Sub2API 基础地址。
- `[sub2api].admin_key`：Sub2API 管理员密钥；也可以把密钥写入 `[sub2api].admin_key_file` 指向的 Docker secret 文件。

常用选填变量：

- `[sub2api].main_account_id`、`[sub2api].spark_account_id`：账号 ID，默认分别为 `571`、`576`。
- `[bark].enabled`：是否启用通知，默认 `false`。
- `[bark].url`：Bark 完整地址；仅当启用通知且没有使用 `[bark].url_file` 时必填。
- `[server].frame_ancestors`：允许哪些网页来源嵌入 monitor 的 iframe，默认只允许同源的 `'self'`。
- `[sub2api].quota_url`、`usage_url`、`spark_quota_url`、`spark_usage_url`：上游路由不是标准路径时使用的完整 URL 覆盖项。
- `[server]`、`[sampling]`、`[history]` 中的端口、采样间隔、阈值、超时和保留天数：都有安全默认值，按需调整即可。

`frame_ancestors` 不是上游地址。例如 monitor 被 `https://newapi.example.com` 的 About 页面嵌入时，写成 `frame_ancestors = ["https://newapi.example.com"]`；多个来源写在同一个数组中。只允许实际的父页面来源，不要填 `*`。

### 内网 Sub2API

可以使用内网地址。`[sub2api].base_url` 是由 monitor 容器后端访问的，浏览器不会直接请求 Sub2API，因此用户浏览器只需要能打开 monitor 页面。

- Sub2API 在同一个 Docker 网络中：使用 Docker 服务名，例如 `http://sub2api:3000`，并让两个 Compose 项目加入同一个 external network。
- Sub2API 在同一台 NAS 或局域网内：使用容器能够路由到的局域网 IP 或内网域名，例如 `http://192.168.x.x:port`。
- 不要在容器中使用 `http://127.0.0.1:port` 指向宿主机；该地址表示 monitor 容器自身。只有 Sub2API 和 monitor 在同一个容器时才可这样使用。

如果内网域名使用 HTTPS，自签名证书必须被容器信任；否则应使用受信任证书或可路由的 HTTP 内网地址。将页面嵌入 New API 时，还要把父页面来源加入 `[server].frame_ancestors`，并确保反向代理把 monitor 路径转发到本容器。

### 二进制

GitHub Actions 会构建 Linux、Windows 和 macOS 的单文件二进制。二进制会从可执行文件旁边的 `data` 目录读取/写入 SQLite，并将网页资源打包在程序内。程序默认读取可执行文件旁边的 `config.toml`，不会读取 `.env`。

Linux/macOS：

```sh
cp config.example.toml config.toml
# 编辑 config.toml；独立运行时可将 [server].port 改为 8320。
./newapi-about-monitor
```

Windows PowerShell：

```powershell
Copy-Item config.example.toml config.toml
# 编辑 config.toml 后启动
./newapi-about-monitor.exe
```

也可以通过 `CONFIG_FILE` 环境变量指定其他 TOML 路径，或由 systemd、Unraid、Windows 服务直接管理该配置文件。

目标机器不需要安装 Python；构建使用 PyInstaller。
每个 GitHub Release 还附带 `config.example.toml` 配置模板。

### GitHub Actions 与发布

- 推送匹配 `v*` 的标签会运行测试、构建多平台二进制，并将多架构镜像推送到 `ghcr.io/wangchudi/newapi-about-monitor`。
- 同一个标签会创建 GitHub Release，附带二进制和 `SHA256SUMS`。
- `workflow_dispatch` 可运行检查和构建，但不会创建 Release 或推送镜像。

运行时凭据只放在目标主机的 `config.toml` 或 secret 管理器中，不放入 GitHub 源码。Actions 仅使用 `GITHUB_TOKEN` 发布镜像和 Release 资产。

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
cp config.example.toml config.toml
# Edit config.toml. Never commit it.
docker login ghcr.io
docker compose pull
docker compose up -d
```

Compose uses the published image `ghcr.io/wangchudi/newapi-about-monitor:latest`. The repository and image are currently private, so pulling the image requires a GitHub PAT with `read:packages`. The page is available at `http://127.0.0.1:8320/`; persistent history is stored in `./data/history.db`.

#### `config.toml` configuration

Copy `config.example.toml` to `config.toml` and edit it. Compose mounts this file read-only at `/app/config.toml`; it contains credentials, so keep it private and never commit it.

Required variables:

- `[sub2api].base_url`: the Sub2API base URL reachable from the monitor container.
- `[sub2api].admin_key`: the Sub2API administrator key; alternatively use the Docker secret file configured by `[sub2api].admin_key_file`.

Common optional variables:

- `[sub2api].main_account_id` and `spark_account_id`, defaulting to `571` and `576`.
- `[bark].enabled`, defaulting to `false`.
- `[bark].url`, required only when Bark is enabled and `[bark].url_file` is not used.
- `[server].frame_ancestors`, defaulting to `'self'`, to control which web origins may embed the page in an iframe.
- The four `[sub2api]` URL overrides for non-standard upstream routes.
- The `[server]`, `[sampling]`, and `[history]` settings, which all have defaults.

`frame_ancestors` is not an upstream URL. If New API embeds the page from `https://newapi.example.com`, write `frame_ancestors = ["https://newapi.example.com"]`; put multiple origins in the same array. Do not use `*`.

### Using an internal Sub2API URL

An internal URL works. `[sub2api].base_url` is contacted by the monitor backend inside the container; the browser does not contact Sub2API directly, so the browser only needs access to the monitor page.

- If both services share a Docker network, use the service DNS name such as `http://sub2api:3000` and attach both Compose projects to the same external network.
- If Sub2API is on the NAS or LAN, use a LAN IP or internal DNS name reachable from the container.
- Do not use `http://127.0.0.1:port` for a host service from inside the monitor container; that points back to the monitor container itself.

For internal HTTPS, the certificate must be trusted by the container. When embedding the page in New API, add the parent origin to `[server].frame_ancestors` and route the monitor path to this container through the reverse proxy.

### Binary, Actions, sampling, and slope model

GitHub Actions builds one-file Linux, Windows, and macOS binaries with the same TOML configuration. The binary reads `config.toml` beside the executable and does not parse `.env` automatically.

On Linux/macOS:

```sh
cp config.example.toml config.toml
# Edit config.toml; for a standalone process you may set [server].port to 8320.
./newapi-about-monitor
```

On Windows PowerShell:

```powershell
Copy-Item config.example.toml config.toml
# Edit config.toml, then start the binary
.\newapi-about-monitor.exe
```

`CONFIG_FILE` can point to an alternate TOML file when needed. systemd, Unraid, Windows Services, and other secret managers can also manage the file directly.
Each GitHub Release also includes the `config.example.toml` configuration template.

Tags matching `v*` also build and publish the multi-architecture image and create a Release with `SHA256SUMS`; `workflow_dispatch` runs checks and builds without publishing.

The collector normally records every five minutes. It switches to one-minute sampling when the main-account amount or request delta, normalized to a five-minute interval, crosses its configured threshold, and holds fast mode for `FAST_HOLD_SECONDS`.

The usage/percentage chart groups repeated rounded percentages, applies a three-point median smoother, fits ordinary least squares, and recursively detects significant two-sided slope changes. A change requires five observations on each side, at least a 10% relative slope change, and adjusted `p <= 0.05`. The selected slope is the segment after the latest change. Exhaustion time uses remaining amount divided by a median positive amount-per-second rate from recent multi-sample spans.

Existing markers are baselined at startup, new markers are notified once, and transient Bark failures are retried while the marker remains current.

### HTTP endpoints

- `GET /healthz` - container health check.
- `GET /api/status` - latest cache, source status, and sampling policy.
- `GET /api/history?hours=168` - snapshots and detected quota cycles.
- `POST /api/refresh` - force one collection immediately.
