# pi-agents-anywhere

[![License: MIT](https://img.shields.io/badge/license-MIT-222222?style=flat)](LICENSE)

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
- **Timeline 投影**：把 pi 会话文件里的消息、工具调用、轮次翻译成 AA 的 `RuntimeTimelineItem`（同样的投影同时用于历史会话与实时推送）。

注入点来自官方代码：`BackendRpcClient(config, agent_runtime_providers=(...))`（`connector/server/client.py`）。

## 安装

官方 Connector 自 `anywhere-cli` 2.0.0 起可从 PyPI 安装（依赖较重，建议用独立虚拟环境）：

```bash
cd /path/to/pi-agents-anywhere
uv venv .venv --python 3.12
uv pip install -e .               # 依赖会从 PyPI 解析（含官方 anywhere-cli）
```

如需跟随官方仓库的最新源码：

```bash
uv pip install -e /path/to/Agents-Anywhere/connector
uv pip install -e . --no-deps     # 依赖已由 connector 提供
```

## 使用

### 1. 配对（只需一次）

```bash
.venv/bin/pi-aa-connector pair https://your-server --no-start
```

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
# 把 %h/pi-agents-anywhere 换成实际 checkout 路径；
# 裸跑 `pi-aa-connector`（不带子命令）等价于 `start`。
ExecStart=%h/pi-agents-anywhere/.venv/bin/pi-aa-connector start
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
| `start_turn` | `prompt`（会话忙时自动用 `streamingBehavior=followUp` 排队） |
| `steer_turn` | `steer`（空闲时退化为普通 prompt） |
| `interrupt_session` | `abort` |
| `update_session_selections` | `set_model` / `set_thinking_level` |
| `get_session_state` | `get_state` + 进程内存态（运行/空闲/等待交互） |
| `runtime.attachment` | 从平台下载附件，把图片按 base64 `ImageContent` 附加到 `prompt`/`steer`（非图片跳过） |
| `get_session_snapshot` | 读会话文件 → 时间线投影 |
| `list_model_catalog` | `get_available_models`（独立工具进程） |
| `list_commands` / `execute_command` | `get_commands`；执行 = 发送 `/命令` |
| `get_session_notices` / `respond_interaction` | 扩展 UI 的 `select`/`confirm`/`input`/`editor` → interaction notice；`notify` → notification |
| `session_turn_ended` | 收到 `agent_settled` 时上报 |

同步模型：**轮询为主 + 结束即推**。Connector 默认每 30s 扫描一次；Runtime 在 `agent_settled` 时立刻重读会话文件并推送一次快照，因此工作台的延迟主要取决于这一条链路，而不是轮询周期。

## 配置项

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `executablePath` | `pi` | pi 可执行文件路径或命令名 |
| `sessionsDir` | `~/.pi/agent/sessions` | 会话目录；会作为 `--session-dir` 传给子进程 |
| `defaultCwd` | `~` | 未指定工作目录时使用的目录 |
| `requestTimeoutMs` | `60000` | 单条 RPC 命令超时 |
| `idleTimeoutSeconds` | `600` | 空闲回收：会话进程空闲超过该时长后被关闭（0 禁用），会话文件保留，下一条消息自动恢复 |

### 在 AA 里执行 pi 的内置命令

pi 的内置 slash 命令（`/reload`、`/settings` 等）只存在于 TUI；RPC 客户端只能执行
`get_commands` 报告的命令（扩展 / 模板 / skill）。要让 AA 里也能用某个内置命令，
把它注册为一个扩展命令即可——例如 `examples/extensions/reload.ts` 把 `/reload`
带入 `get_commands`：

```bash
cp examples/extensions/reload.ts ~/.pi/agent/extensions/
```

之后在 AA 的输入框里输入 `/reload`，pi 会像 TUI 一样重载配置、扩展、指令与资源
（先提示“正在重载”，重载完成后提示“重载完成”；10 秒内无事件则静默清理标记，
不会在重启或后续会话里误报）。

## 已知限制

- **附件仅支持图片**：pi RPC 的输入是文本 + `ImageContent`，非图片附件（PDF 等）会被跳过。
- **无权限目录**：pi 没有运行时工具审批概念（工具权限由启动配置决定），因此没有 permission catalog。
- **非逐 token 流式**：Timeline 在轮次结束（`agent_settled`）时推送，不推送 delta 级文本。
- **`notify` 通知不会被自动清除**：映射为 `open` 状态的 notification notice。
- **时间线跟随 pi 的活跃分支**：在 pi 中切换分支（`/tree`）后，旧分支的远端时间线项会被移除；切回该分支后会重新投影恢复。pi 的会话文件始终保留全部分支，此行为可逆。
- 仅覆盖 `select/confirm/input/editor` 四种对话框交互；`setStatus`/`setWidget` 等无副作用调用被忽略。

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

自定义命令以参数形式传递，例如 `docker/run-tests.sh python -m pytest tests/test_cli.py`。

`docker/run-tests.sh` 默认从 `../Agents-Anywhere/connector` 读取官方 Connector 源码，可用
`PI_AA_CONNECTOR_SOURCE` 覆盖。测试镜像把真 pi（`@earendil-works/pi-coding-agent`）装进容器，
`tests/test_true_pi.py` 直接驱动它——不依赖模型账号的部分随时可跑；`test_real_model_turn`
会在启用时把宿主 `~/.pi/agent` 复制到容器内使用（复制而不是只读挂载：pi 需要写锁文件）。

### 验证状态

已验证（`43 passed, 1 skipped`；含真 pi 集成在列，真实模型轮次为可选项）：

- 单元：RPC 传输、会话文件解析（含分支树）、Timeline 投影、Provider 配置、Runtime 生命周期与交互流程、图片附件转发（下载、base64 编码与命令载荷）、CLI 参数解析与错误映射。
- 真 pi：版本探测、模型目录/命令响应、`pi --mode rpc --session <文件>` 恢复既有会话并读取状态、真实模型的完整投影。
- 端到端：与自托管 AA Server 2.0.0 的连接、既有会话批量同步与时间线投影、模型目录上报。

未验证（需要环境）：

- AA 各客户端（Web / Desktop / Android / iOS）上的完整渲染验收。
- Windows 平台；多会话并发压力。
