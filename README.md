# pi-agents-anywhere

[![License: MIT](https://img.shields.io/badge/license-MIT-222222?style=flat)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/pi-agents-anywhere?style=flat)](https://pypi.org/project/pi-agents-anywhere/)

把 [pi coding agent](https://github.com/earendil-works/pi) 接入 [Agents Anywhere](https://github.com/anywhere-labs/Agents-Anywhere) 工作台的连接器适配器。

Agents Anywhere（下称 AA）官方 Connector 只内置 Codex / Claude / DSH 三个 Runtime。本项目提供一个 **pi** Runtime：通过官方注入点把 `PiProvider` / `PiRuntime` 挂进 Connector，用 `pi --mode rpc` 驱动本机 pi，并把 pi 会话投影为平台 Timeline。**不修改官方 Connector 源码。**

## 架构

```
AA 客户端（Web/桌面/手机）
        │
AA Server （云端或自托管）
        │  Connector RPC
pi-aa-connector（本进程 = 官方 BackendRpcClient + PiProvider 注入）
        │  Runtime RPC
pi-aa 的 PiRuntime
        │  JSONL over stdin/stdout
pi --mode rpc （每个活跃会话一个子进程）
```

- **PiProvider**：发现本机 pi（`pi --version`）、配置校验、按配置创建 Runtime 实例。
- **PiRuntime**：会话清单（扫描 `~/.pi/agent/sessions`）、会话快照、状态、发送/打断/转向、图片附件、模型目录、命令、扩展 UI 交互。
- **Timeline 投影**：把 pi 会话文件里的消息、工具调用、轮次翻译成 AA 的 `RuntimeTimelineItem`（同样的投影同时用于历史会话与实时推送）。运行时还会把 `message_update` 的文本/思考增量与 `tool_execution_*` 的工具状态按约 300ms 聚合，通过 `timeline_item_upsert` 实时推送到工作台；轮次结束时由全量投影替换为正式时间线项。注意：pi 投影生成的 `turn.start`/`turn.end` 分界项与 `turnId` 会被官方 Connector/Server 2.0.x 的边界主动过滤，工作台时间线不显示轮次分界（属官方 2.0.x 的 session-only boundary 设计，非缺陷）。

注入点来自官方代码：`BackendRpcClient(config, agent_runtime_providers=(...))`（`connector/server/client.py`）。

## 前置要求

- **pi（本机）**：用官方脚本安装 `curl -fsSL https://pi.dev/install.sh | sh`，或 npm 安装（Node.js >= 22.19）：
  `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`。确认 `pi --version` 可用，并启动 `pi` 登录至少一个模型供应商（`/login`）。
- **Python 3.12+**：运行 Connector。依赖较重（含官方 `anywhere-cli`），建议用独立环境或 `uv tool`。
- **Agents Anywhere Server 地址**：云端或自托管均可。

## 安装

从 PyPI 安装（需要 [uv](https://docs.astral.sh/uv/)）：

```bash
uv tool install --python 3.12 pi-agents-anywhere
```

也可以直接从 GitHub 源码安装（等同最新 main）：

```bash
uv tool install --python 3.12 "git+https://github.com/Seryta/pi-agents-anywhere"
```

装完 `pi-aa-connector` 在 PATH 里可直接调用（`pipx install pi-agents-anywhere` 等价）。

已通过 `uv tool` 从 PyPI 安装的用户，可升级到最新发布版本（本次为 0.2.1），并重启
Connector 使其生效：

```bash
uv tool upgrade --reinstall pi-agents-anywhere
systemctl --user restart pi-aa-connector  # 若采用下方的 systemd user service
```

版本变更见 [CHANGELOG.md](https://github.com/Seryta/pi-agents-anywhere/blob/main/CHANGELOG.md)。

开发或需要跟随最新源码时，用可编辑安装：

```bash
git clone https://github.com/Seryta/pi-agents-anywhere
cd pi-agents-anywhere
uv venv .venv --python 3.12
uv pip install -e .               # 依赖会从 PyPI 解析（含官方 anywhere-cli）
```

如需跟随官方 Connector 仓库的最新源码：

```bash
uv pip install -e /path/to/Agents-Anywhere/connector
uv pip install -e . --no-deps     # 依赖已由 connector 提供
```

## 使用

### 1. 配对（只需一次）

```bash
pi-aa-connector pair https://your-server --no-start
```

源码安装时用 `.venv/bin/pi-aa-connector`；`uv tool` / `pipx` 安装则直接在 PATH 里。

命令会打印配对码，在 AA 工作台的「添加设备」里输入该码；凭据保存到
`~/.agents-anywhere/connector.json`（与官方 CLI 同一配置）。

### 2. 常驻运行

测试/构建在容器里，运行在工作设备上。推荐 systemd user service：

```ini
# ~/.config/systemd/user/pi-aa-connector.service
[Unit]
Description=Agents Anywhere Connector (with Pi runtime)
After=network-online.target

[Service]
# 路径按安装方式取其一：
#   uv tool / pipx → %h/.local/bin/pi-aa-connector
#   源码 checkout  → %h/pi-agents-anywhere/.venv/bin/pi-aa-connector
# 裸跑 `pi-aa-connector`（不带子命令）等价于 `start`。
ExecStart=%h/.local/bin/pi-aa-connector start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now pi-aa-connector
loginctl enable-linger "$USER"   # 无登录会话时也保持运行
```

连接参数、配对方式、状态目录与官方 `anywhere-cli` 完全一致（`~/.agents-anywhere`、`AGENT_*` 环境变量）。
随后在 AA 工作台里配置 pi Runtime（可执行文件、会话目录等）并创建会话。

## 能力映射

| AA Runtime 协议 | pi |
| --- | --- |
| `list_sessions` / `list_complete_session_inventory` | 扫描会话目录的 JSONL 头与名称 |
| `create_and_start_session` | 启动新的 `pi --mode rpc` 子进程并发送消息 |
| `start_turn` | `prompt`（会话忙时自动用 `streamingBehavior=followUp` 排队；排队内容通过状态元数据可见） |
| `steer_turn` | `steer`（空闲时退化为普通 prompt） |
| `interrupt_session` | `clear_queue` + `abort`（与 TUI 的 Esc 一致：丢弃排队消息再中断，被清除文本记入状态元数据） |
| `update_session_selections` | `set_model` / `set_thinking_level` |
| `get_session_state` | `get_state` + 进程内存态（运行/空闲/等待交互）+ 队列事实（`pendingMessageCount` / `pendingSteering` / `pendingFollowUp`） |
| `runtime.attachment` | 从平台下载附件：图片按 base64 `ImageContent` 附加到 `prompt`/`steer`；非图片保存到连接器附件目录（单文件 ≤64 MiB），并把路径写入 prompt 供 pi 读取 |
| `get_session_snapshot` | 读会话文件 → 时间线投影（edit 结果附 unified diff `changes`，供工作台 diff 面板与文件预览） |
| 实时推送（`timeline_item_upsert`） | `message_update` 的文本/思考与 `tool_execution_*` 工具状态，按约 300ms 聚合；轮次结束由全量投影替换；扩展写入的 `/btw` 问答条目在会话空闲时立即触发一次全量时间线刷新（流式进行中则随该轮 settle 推送） |
| `list_model_catalog` | `get_available_models`（独立工具进程） |
| `list_commands` / `execute_command` | `get_commands`（扩展/模板/skill）；另代理 4 个 TUI 内置命令：`compact` / `name` / `stats` / `export`（connector 直接调用对应 RPC，不走 prompt 文本）；其余命令 = 发送 `/命令`，结果上报 `executionState=accepted`（2.0.3 命令契约）。扩展命令声明 `metadata.ui.allowedStatuses` 含 `running`（pi 对流式中的扩展命令立即派发），所以 `/btw` 这类命令在会话运行中也能执行；模板/skill 与内置代理仍仅空闲可执行 |
| 会话统计 | `get_session_stats`：每轮 `agent_settled` 后刷新 token/成本/`contextUsage` 进状态元数据；上下文 ≥90% 时发预警 notice，回落自动 resolved |
| `get_session_notices` / `respond_interaction` | 扩展 UI 的 `select`/`confirm`/`input`/`editor` → interaction notice；`notify` → notification（轮次结束/进程退出时 resolved）；`setStatus` → 状态元数据 `extensionStatus` |
| `session_turn_ended` | 收到 `agent_settled` 时上报 |

同步模型：**流式增量 + 轮询兜底 + 结束即推**。消息与工具状态按约 300ms 聚合实时 upsert；Connector 默认每 30s 扫描一次，扫描命中上次已同步的投影前缀时只推送新增尾部（`complete=false`，尾部若干项因 pi 定稿回填会随 delta 重推），改写或压缩后回退整份替换；Runtime 在 `agent_settled` 时立刻重读会话文件并推送一次全量快照（替换流式临时项），最终一致性由会话文件保证。

## 配置项

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `executablePath` | `pi` | pi 可执行文件路径或命令名 |
| `sessionsDir` | `~/.pi/agent/sessions` | 会话目录；会作为 `--session-dir` 传给子进程 |
| `defaultCwd` | `~` | 未指定工作目录时使用的目录 |
| `requestTimeoutMs` | `60000` | 单条 RPC 命令超时 |
| `idleTimeoutSeconds` | `600` | 空闲回收：会话进程空闲超过该时长后被关闭（0 禁用），会话文件保留，下一条消息自动恢复 |
| `attachmentRetentionDays` | `30` | 保留 N 天后清理连接器附件目录中非活跃会话的文件（0 禁用） |

### 在 AA 里执行 pi 的内置命令

pi 的内置 slash 命令（`/reload`、`/settings` 等）只存在于 TUI；RPC 客户端只能执行
`get_commands` 报告的命令（扩展 / 模板 / skill）。要让 AA 里也能用某个内置命令，
把它注册为一个扩展命令即可——例如 `examples/extensions/reload.ts` 把 `/reload`
带入 `get_commands`。注意 PyPI 的 wheel 只包含运行时包（`pi_aa/`），不包含
`examples/`，需要从源码仓库获取该文件（源码 checkout 或直接下载）：

```bash
# 方式一：源码 checkout 后复制
git clone --depth 1 https://github.com/Seryta/pi-agents-anywhere /tmp/pi-aa \
  && cp /tmp/pi-aa/examples/extensions/reload.ts ~/.pi/agent/extensions/

# 方式二：直接下载该文件
mkdir -p ~/.pi/agent/extensions
curl -fsSL -o ~/.pi/agent/extensions/reload.ts \
  https://raw.githubusercontent.com/Seryta/pi-agents-anywhere/main/examples/extensions/reload.ts
```

之后在 AA 的输入框里输入 `/reload`，pi 会像 TUI 一样重载配置、扩展、指令与资源
（先提示“正在重载”，重载完成后提示“重载完成”；10 秒内无事件则静默清理标记，
不会在重启或后续会话里误报）。

`examples/extensions/btw.ts` 是另一个例子：在 pi 继续干活的同时，用一次旁路模型
调用（无工具、不进入对话历史）回答关于当前会话的临时问题。会话运行中也能执行
（pi 对流式中的扩展命令立即派发，不会打断当前任务）。TUI 里答案显示在浮层
面板；RPC 模式下扩展把问答存成 `pi.btw` 自定义会话条目，Connector 投影为时间线项，
因此 AA 里也能看到答案。安装方式相同：

```bash
curl -fsSL -o ~/.pi/agent/extensions/btw.ts \
  https://raw.githubusercontent.com/Seryta/pi-agents-anywhere/main/examples/extensions/btw.ts
```

## 已知限制

- **全新会话里的 `/btw` 可能延迟出现在时间线**：pi 对尚无消息的会话文件采用内存缓冲（首条 user/assistant 消息才触发落盘）；此时 `/btw` 写入的自定义条目要等会话文件真正生成后才随全量快照可见。已有对话的会话不受影响。
- **附件大小上限**：单个附件（含图片）超过 64 MiB 会被跳过（防磁盘滥用），日志有记录。
- **无权限目录**：pi 没有运行时工具审批概念（工具权限由启动配置决定），因此没有 permission catalog。
- **流式为消息级聚合**：文本/思考在生成过程中按约 300ms 聚合推送（非逐 token），工具输出同样节流；轮次结束后临时项由会话文件的全量投影替换。
- **对话框覆盖范围**：`select`/`confirm`/`input`/`editor` 映射为 interaction notice；`setStatus` 映射为状态元数据；`setWidget`/`setTitle`/`set_editor_text` 仍被忽略（RPC 模式无对应展示位置）。
- **超过 64 MiB 的会话文件会被跳过**：超过 `MAX_SESSION_FILE_BYTES`（`src/pi_aa/sessions.py`，64 MiB）的会话文件不参与投影，会从会话列表消失且无提示（仅超大单文件场景；正常会话远小于此）。
- **时间线跟随 pi 的活跃分支**：在 pi 中切换分支（`/tree`）后，旧分支的远端时间线项会被移除；切回该分支后会重新投影恢复。pi 的会话文件始终保留全部分支，此行为可逆。

## 开发与测试

项目规则：**所有构建与测试都在容器里执行**，不在宿主机直接跑。

```bash
docker/build-test-env.sh                        # 构建测试镜像（Python 3.12 + Node + 真 pi + pytest）
docker/run-tests.sh                             # 单元测试（fake pi）
PI_AA_TRUE_PI=1 docker/run-tests.sh             # 追加真 pi 集成测试
PI_AA_TRUE_PI_MODEL=1 docker/run-tests.sh       # 追加一轮真实模型调用
```

镜像构建默认走官方源；受限网络可通过 `PI_AA_NODE_DIST` / `PI_AA_NPM_REGISTRY` /
`PI_AA_PIP_INDEX_URL` 环境变量（或 `--build-arg`）指向镜像源。

测试镜像默认使用 Node 22.19.0（满足 pi 的最低版本要求）和 pi 0.87.1，Python 测试依赖
在 `docker/test-requirements.txt` 固定版本。验证其他 pi 版本可运行
`docker/build-test-env.sh -t pi-aa-test-env:pi-1.1.0 --build-arg PI_VERSION=1.1.0`，
然后设置 `PI_AA_TEST_IMAGE=pi-aa-test-env:pi-1.1.0` 运行测试。
CI 覆盖 pi 0.87.1 / 1.1.0，并固定 Connector v2.0.3 的提交；仓库变量
`PI_AA_CONNECTOR_REF` 可覆盖该提交以验证其他版本。基础镜像及系统包仍由上游更新。

长会话响应性可重复验证：先运行
`docker/run-tests.sh python scripts/benchmark-session.py --doctor` 检查隔离环境和依赖，再运行
`docker/run-tests.sh python scripts/benchmark-session.py --max-loop-lag-ms 100`。
脚本用固定 JSONL 输入（默认 12,000 条、每条 4 KiB 文本）和 32 MiB 附件，测量投影、
文件读取、附件落盘、scanner prepare+commit 的耗时及 1 ms 心跳的最大延迟，向 stdout
输出 JSON；`ok=false` 时退出码为 1。可用 `--entries`、`--text-bytes`、`--attachment-mib`、
`--repeats` 调整负载，`--max-loop-lag-ms` 设置门禁。临时文件均在一次性容器内生成，
不需要 pi 进程、模型凭据或网络。性能脚本复用 `tests/` 中的测试 Host，运行时须挂载完整源码（含 `tests/`）。

上一轮集成版本实测（本次消息排序修复后未重跑；3 轮，50,695,864 字节会话，
12,000 条记录，32 MiB 附件）：

| 路径 | 总耗时中位数 | 最大事件循环延迟 |
| --- | ---: | ---: |
| 投影 | 604.585 ms | 31.618 ms |
| 文件读取 | 156.729 ms | 25.968 ms |
| 附件落盘 | 34.618 ms | 7.069 ms |
| scanner prepare+commit | 1727.263 ms | 53.893 ms |

doctor 检查通过，100 ms 延迟门禁通过（JSON `ok=true`）。同负载的优化前基线中，
scanner 最大事件循环延迟为 1194.930 ms，总耗时中位数为 1253.065 ms；最终集成版本
还包含同步与并发修复，以上对照不能单独归因于线程卸载。优化目标是保持交互响应，
总耗时增加的代价仍需承担。
这些数据只代表本机确定性负载，不覆盖真实网络和慢盘；Python GIL、GC 与机器负载仍会影响延迟。

自定义命令以参数形式传递，例如 `docker/run-tests.sh python -m pytest tests/test_cli.py`。

`docker/run-tests.sh` 默认从 `../Agents-Anywhere/connector` 读取官方 Connector 源码，可用
`PI_AA_CONNECTOR_SOURCE` 覆盖。测试镜像把真 pi（`@earendil-works/pi-coding-agent`）装进容器，
`tests/test_true_pi.py` 直接驱动它——不依赖模型账号的部分随时可跑；`test_real_model_turn`
会在启用时把宿主 `~/.pi/agent` 复制到容器内使用（复制而不是只读挂载：pi 需要写锁文件）。

### 验证状态

本轮验收（2026-10-15）：全部在 Docker 内使用 Python 3.12.13、Node 22.19.0 执行。
开启无模型凭据的真 pi 集成测试后，四组兼容性矩阵均为 `240 passed, 1 skipped`；
唯一跳过项是需显式开启的真实模型付费调用。未开启真 pi 的默认模式本轮未单独重跑。

| 官方 Connector | pi 0.87.1 | pi 1.1.0 |
| --- | --- | --- |
| 2.0.0 | 240 passed, 1 skipped | 240 passed, 1 skipped |
| 2.0.3 | 240 passed, 1 skipped | 240 passed, 1 skipped |

`ruff check` 与 `ruff format --check` 均通过，覆盖 `src`、`tests` 和
`scripts/benchmark-session.py`（27 个文件）。本轮验证包括：

- 单元：RPC 传输、会话文件解析（含分支树）、Timeline 投影（含 edit diff 的 `changes`）、流式推送（节流聚合、running→终态收敛（done/cancelled/failed）、工具终态与 exitCode、任务不泄漏）、消息队列与 `clear_queue` 中断语义、内置命令代理（compact/name/stats/export）、命令 id 消毒后按 pi 原生名派发、命令 `sourceInfo` 元数据、`/btw` 自定义条目投影与 `entry_appended` 实时推送、会话统计与上下文预警、扩展 `setStatus`/`notify` 生命周期、非图片附件落盘与乐观消息对账、附件目录保留策略、Provider 配置、Runtime 生命周期与交互流程、图片附件转发、CLI 参数解析与错误映射。
- 回归：会话路径越界拒绝、进程重启恢复与回收、RPC 生命周期顺序、原生中断/失败终态、同步成功确认与失败重试、等长时间线和元数据更新、跨轮同步交付互斥、非图片附件能力声明、事件循环响应性；用户消息确认先于 agent 回复、确认失败重试、相同文本的队列消息按原生 entry ID 对账，以及队列清除和进程重启后的身份恢复。
- 真 pi：版本探测、模型目录/命令响应、`pi --mode rpc --session <文件>` 恢复既有会话并读取状态；使用容器内本地确定性假模型供应商，验证真实 pi 的首条用户消息在流式回复之前完成确认，且与最终会话投影使用同一个原生 ID，不需要账号或付费调用。
- 协议与客户端源码：将实际 pi 事件交给官方 Server 的时间线纯规则、从源码提取的序号分配分支，以及 Web / Desktop 的实际 TypeScript 合并与排序逻辑执行，确认正式用户消息先于新回复且不与乐观消息重复；此项不等同于实际浏览器渲染或服务端数据库端到端验证。

历史验证记录（本轮未重跑）：真实模型调用及完整投影；与自托管 AA Server 2.0.0
的连接、既有会话批量同步、时间线投影和模型目录上报；PyPI `anywhere-cli` 2.0.3 wheel
与对应源码的一致性。

未验证（需要环境）：

- AA 各客户端（Web / Desktop / Android / iOS）上的完整渲染验收。
- 本轮未执行包含实际客户端、Server 数据库和 Redis 的完整端到端流程。
- Windows 平台；多会话并发压力。
