# pi-agents-anywhere

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
- **PiRuntime**：会话清单（扫描 `~/.pi/agent/sessions`）、会话快照、状态、发送/打断/转向、模型目录、命令、扩展 UI 交互。
- **Timeline 投影**：把 pi 会话文件里的消息、工具调用、轮次翻译成 AA 的 `RuntimeTimelineItem`（同样的投影同时用于历史会话与实时推送）。

注入点来自官方代码：`BackendRpcClient(config, agent_runtime_providers=(...))`（`connector/server/client.py`）。

## 安装

官方 Connector v2 尚未发布到 PyPI，需要从源码安装：

```bash
uv pip install /path/to/Agents-Anywhere/connector
uv pip install /path/to/pi-agents-anywhere     # 本项目
```

## 使用

在工作设备（装了 pi 的机器）上运行：

```bash
pi-aa-connector
```

连接参数、配对方式、状态目录与官方 `anywhere-cli` 完全一致（`~/.agents-anywhere`、`AGENT_*` 环境变量）。随后在 AA 工作台里配置 pi Runtime（可执行文件、会话目录等）并创建会话。

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

## 已知限制

- **附件（图片）不支持**：pi RPC 支持 base64 图片输入，本项目尚未接线。
- **无权限目录**：pi 没有运行时工具审批概念（工具权限由启动配置决定），因此没有 permission catalog。
- **非逐 token 流式**：Timeline 在轮次结束（`agent_settled`）时推送，不推送 delta 级文本。
- **`notify` 通知不会被自动清除**：映射为 `open` 状态的 notification notice。
- **空闲进程不回收**：活跃过的会话进程保留到 Runtime 停止；多会话长时间运行会累积进程。
- 仅覆盖 `select/confirm/input/editor` 四种对话框交互；`setStatus`/`setWidget` 等无副作用调用被忽略。

## 开发与测试

项目规则：**所有构建与测试都在容器里执行**，不在宿主机直接跑。

```bash
docker/build-test-env.sh                        # 构建测试镜像（Python 3.12 + Node + 真 pi + pytest）
docker/run-tests.sh                             # 单元测试（fake pi，22 项）
PI_AA_TRUE_PI=1 docker/run-tests.sh             # 追加真 pi 集成测试
PI_AA_TRUE_PI_MODEL=1 docker/run-tests.sh       # 追加一轮真实模型调用
```

`docker/run-tests.sh` 默认从 `../Agents-Anywhere/connector` 读取官方 Connector 源码，可用
`PI_AA_CONNECTOR_SOURCE` 覆盖。测试镜像把真 pi（`@earendil-works/pi-coding-agent`）装进容器，
`tests/test_true_pi.py` 直接驱动它——不依赖模型账号的部分随时可跑；`test_real_model_turn`
会在启用时把宿主 `~/.pi/agent` 复制到容器内使用（复制而不是只读挂载：pi 需要写锁文件）。

### 验证状态

已验证（`27 passed`，含真 pi 与真实模型一轮）：

- 单元：RPC 传输、会话文件解析（含分支树）、Timeline 投影、Provider 配置、Runtime 生命周期与交互流程。
- 真 pi：版本探测、模型目录/命令响应、`pi --mode rpc --session <文件>` 恢复既有会话并读取状态、一轮真实模型的完整投影。

未验证（需要环境）：

- 与真实 AA Server 的端到端（配对自己的自托管实例）。
- AA 各客户端（Web / Desktop / Android / iOS）上的渲染效果。
- Windows 平台；多会话并发压力。
