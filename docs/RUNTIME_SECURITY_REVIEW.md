# 2026-09-17 服务试运行与安全复查

> 历史报告：密钥配对／会话认证描述已被本机免登录、Host／Origin 校验取代。当前行为与应用分层、学习对照机制以 [ARCHITECTURE](ARCHITECTURE.md) 和 [升级说明](LEARNING_ARCHITECTURE_UPGRADE.md) 为准。

> 以下保留首次试运行的发现与证据。随后已按用户授权修复并切回原 OKX Demo 账户，最新状态见 [运行修复记录](RUNTIME_REMEDIATION.md)。

## 结论与范围

前端生产构建通过，服务已启动，真实浏览器登录与工作台渲染成功。试运行发现并修复一个 Windows 密钥初始化启动错误；仍有一个监控内存增长问题、一个低风险路径信息暴露，以及需要处理的行情连接异常。不能据此认定系统没有其他漏洞或可以稳定自动交易。

启动前只读检查发现原数据库的自动化 enabled=true、模式 OKX_DEMO，持仓表有 13 条记录。为避免恢复原账户任务，本次使用 `F:\Cosmos\output\security-trial-20260917\data` 作为独立数据目录，在 `127.0.0.1:8000` 启动同一份应用代码。原数据库和原账户凭证没有修改；没有启动自动化、下单或触发 LLM 决策。未执行 pytest 等完整测试套件。

## 发现

### R01 / 中风险：未认证请求可导致监控指标无界增长（未修复）

- 位置：`backend/cosmos_quant/api.py:3188`、`backend/cosmos_quant/observability.py:12`。
- 中间件在未匹配路由时直接将原始 URL 路径作为指标标签，内存计数器不设数量上限或过期机制。认证拦截前后均会计数。
- 实际发送 3 个不同的 `/api/audit-probe-unknown-*` 匿名请求，均返回 401，但 `/metrics` 中新增了 6 个永久指标序列。
- 影响：能够访问此 HTTP 服务的调用方可持续制造不同路径，使进程内存和指标输出持续增长。本次只用了 3 个请求，没有进行耗尽资源的攻击。默认仅监听回环地址，不能将其描述为互联网直接可利用。
- 建议：未匹配或未认证请求使用固定标签，匹配后只使用路由模板；必要时给指标序列设置上限。

### R02 / 稳定性：行情代理连接异常，浏览器行情请求超时（未修复）

- 项目调用位置：`backend/cosmos_quant/websocket_market.py:106`、`backend/cosmos_quant/liquidation_stream.py:164`。
- 运行环境安装 websockets 16.1.1，属于项目声明的 `>=15,<17` 范围。
- 服务日志出现 `HTTPProxyConnection` 的 `protocol.eof_received()` 异常，随后为 `AttributeError: 'NoneType' object has no attribute 'status_code'` 及 `EOFError: stream ended`。已查看本机依赖源码，异常位于代理 HTTP 响应解析和 EOF 处理路径。
- 登录后浏览器显示“行情离线 / signal timed out”，观察期间没有完成行情加载。服务进程及工作台权限接口仍正常响应。
- 这是已观察到的运行故障；代理网络条件与依赖异常处理共同参与，尚未证明两项现象的完整因果关系。没有执行交易链路验证，不能声称行情稳定或已修复。
- 建议：核对试运行代理连通性，处理代理提前断开时的依赖兼容问题，再验证重连与报价恢复；不要只依据任务仍存活判断行情就绪。

### R03 / 低风险隐私信息：匿名返回本机绝对路径（未修复）

- 位置：`backend/cosmos_quant/api.py:2462`，`operator_session_info` 返回 `access_key_file`。
- 无会话请求 `/api/session` 返回 200，并包含密钥文件的完整本地路径；不包含密钥内容。
- 此设计用于首次本机配对，但会暴露目录结构；当数据目录位于用户目录时也可能暴露用户名。当前仅回环监听且有 Host 校验，风险范围有限。
- 建议：匿名接口仅显示文件名及通用定位说明，完整路径由本机启动器展示。

### R04 / 启动阻断：Windows 用户名编码导致密钥初始化失败（已修复并重新启动确认）

- 位置：`backend/cosmos_quant/operator_session.py:39`。
- 首次启动使用 PYTHONUTF8=1 时，`whoami` 的非 UTF-8 输出造成后台读取线程 UnicodeDecodeError，随后 `.stdout.strip()` 抛出 AttributeError，API 无法启动。
- 已改为读取 `whoami /user /fo csv /nh` 原始字节，从中提取 ASCII SID，并通过 `icacls` 的 SID 形式设置权限，不再依赖用户名的控制台编码。
- 失败没有留下空的正式密钥文件。修改后在同一个独立数据目录重试启动成功；真实密钥登录成功，AST 与 Ruff F/B 静态检查通过。

## 已验证的保护与实际表现

- 匿名 settings、AI settings、overview、audit、metrics 请求返回 401。
- 非许可 Host 返回 400；外站 Origin 登录、退出请求返回 403；缺少 Origin 的会话退出请求返回 403。
- 正确本机密钥可登录，Cookie 包含 HttpOnly、SameSite=Strict、8 小时有效期；退出后请求及旧 Cookie 重放均返回 401。
- WebSocket 未登录和外站 Origin 均拒绝握手，合法登录同源握手成功。
- `.env`、数据库路径及密钥文件的 HTTP 请求均返回 404。
- 本次采样响应和运行日志没有出现试运行访问密钥；登录后的页面和 localStorage 也没有保存该密钥。本次未接入原账户，不能用此结果替代真实账户数据的隐私审查。
- 首页存在 X-Frame-Options、nosniff、Referrer-Policy 等响应头；未设置 CSP。文档和 OpenAPI 可匿名访问，此处作为加固项记录，不将其单独认定为已证实的权限绕过。
- 浏览器观察约 18 秒，未捕获 JavaScript pageerror 或 HTTP 4xx/5xx；但行情 fetch 在客户端超时，因此“无错误响应”不代表行情成功。
- 空状态概览采样约 74 ms，设置约 14 ms；不代表原 7.7 GB 数据库的性能。
- 空请求体向原始行情写入端点发送 POST 返回 422（模型校验先执行），此探针不算成功验证业务层的 403 拒绝路径。

## 证据与运行状态

- `output/security-trial-20260917/http-audit.json`：HTTP、Cookie、WebSocket 检查摘要，无密钥或会话值。
- `output/security-trial-20260917/runtime-findings.json`：指标标签复现及日志密钥检查。
- `output/security-trial-20260917/browser-audit.json`：页面异常、请求状态与页面文字摘要。
- `output/security-trial-20260917/workspace.png`：登录后的工作台截图。
- `output/cosmos-managed.stderr.log`：本次成功启动及代理异常日志。

报告完成时保留独立试运行服务，自动化关闭。该服务使用独立数据库，不能当作原账户实例；恢复原账户服务前应先停止这个实例。
