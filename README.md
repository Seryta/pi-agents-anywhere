# pi-agents-anywhere

把 [Pi coding agent](https://github.com/earendil-works/pi) 作为 Runtime 扩展安装到 [Agents Anywhere](https://github.com/anywhere-labs/Agents-Anywhere) 官方 Connector。一个工作设备只运行一个 Connector，同时提供官方 Codex / Claude / DSH 和 Pi。

DSH 的旁支问答插件源码、容器打包与安装说明见 [plugins/dsh-btw](plugins/dsh-btw/README.md)。

## 架构与生命周期

```
AA Server ←→ 官方 Connector 公共服务（systemd，唯一设备身份与凭据）
                  ├─ 官方 Codex / Claude Runtime
                  ├─ PiProvider → pi --mode rpc
                  └─ 官方 DSH Runtime ←→ 官方 DSH 插件 Bridge ←→ DSH

DSH 管理面板 ←→ 私有 Unix socket ←→ Connector 公共服务
```

Pi 通过 Python entry point `agents_anywhere.runtime_providers.v1` 加载；Pi CLI 只委托官方 CLI，不再复制配对、凭据和进程管理。重连状态发布使用公开的 `on_backend_reconnect()` 回调。

**Pi Python 适配层在共享 Connector 进程内运行，没有热重载。** 改源码后先构建并安装新 wheel，再通过 `systemctl --user restart agents-anywhere-connector.service` 重启服务进程；同设备其他 Runtime 会短暂断开。DSH 面板的 Connector 重启只重建进程内后端任务，不能用于加载新 Python 模块；Pi 的 `/reload` 只重载其原生资源。完整边界与操作见[安装、更新与发布](docs/OPERATIONS.md)。

DSH 外部服务模式只建立本机管理连接。退出 DSH、重新加载插件、退出插件账号或重置插件，不停止公共 Connector；主动点击停止或重启会影响这个设备的所有 Agent。DSH 退出期间，其官方 Bridge 不可用，Pi 和其他独立 Runtime 继续工作。服务不可达会明确报错，不启动备用 Connector。

## 兼容版本与上游补丁

此架构需要官方 Connector 的扩展加载入口和 DSH 插件的外部服务模式，当前使用本仓库保存的正式集成补丁，**不能只安装未经补丁的 PyPI anywhere-cli**。

- 上游固定提交、组件版本、协议版本与补丁 SHA256：[upstream.json](integrations/agents-anywhere/upstream.json)。
- 可审查、可重放补丁：[upstream.patch](integrations/agents-anywhere/upstream.patch)。它保留官方 Runtime 实现，并包含此前的进程识别与 DSH 能力缓存回归修复。
- 本机公共服务面向 Linux / systemd；原有官方 CLI 和 stdio RPC 继续保留。Unix socket 模式不承诺 Windows 支持。
- 连接器 Python 包与 DSH Node 插件分别安装；Pi 只需安装在公共 Connector 的 Python 环境中。

更新上游时先更新固定提交、重新审查和生成补丁，再通过 Connector、Pi 与 DSH 跨进程回归并重建产物。不要自动追随浮动 main 或直接覆盖已验证的组件。

## 构建与安装

运行要求 Python 3.12+ 和 uv；Pi 原生 CLI 保持原有安装。直接下载 [v0.3.0 Release](https://github.com/Seryta/pi-agents-anywhere/releases/tag/v0.3.0) 的完整匹配产物，无需本机构建。例如使用 GitHub CLI：

```bash
gh release download v0.3.0 --repo Seryta/pi-agents-anywhere --dir dist/shared-connector
(cd dist/shared-connector && sha256sum --check SHA256SUMS)
```

也可下载 Release 中的 `pi-agents-anywhere-0.3.0-shared-connector.tar.gz` 整套包。需要从源码构建时，额外要求 Docker 和 Git；构建、类型检查与打包均在容器内进行：

```bash
docker/build-shared-connector.sh
```

默认使用相邻 `../Agents-Anywhere` 检出；也可设置 `PI_AA_UPSTREAM_SOURCE`。没有检出时自动下载固定提交到任务临时目录。输出到 `dist/shared-connector/`，包括官方 Connector wheel、Pi 扩展 wheel 与源码包、DSH 插件 tgz、systemd 模板、依赖约束、上游清单、集成验证结果、整套归档及 SHA256 清单。构建从干净的固定提交重放补丁，不依赖开发者工作区的未提交文件。

将两份 wheel 安装到同一个 Python 3.12 环境；使用 `--no-build`，依赖只使用预构建 wheel。新安装示例：

```bash
RELEASE="$HOME/.local/share/agents-anywhere/releases/0.3.0"
mkdir -p "$RELEASE/artifacts"
cp -a dist/shared-connector/. "$RELEASE/artifacts/"
uv venv --python 3.12 "$RELEASE/venv"
uv pip install --python "$RELEASE/venv/bin/python" --no-build \
  -c "$RELEASE/artifacts/constraints.txt" "$RELEASE"/artifacts/*.whl
ln -s "$RELEASE/venv" "$HOME/.local/share/agents-anywhere/venv"
```

已有0.2.x安装按[迁移步骤](docs/OPERATIONS.md#从-02x-迁移)切换，不覆盖运行环境；单独升级PyPI的Pi包不能完成这次迁移。

用 DSH 官方插件管理器安装生成的 `dsh-bridge-next.tgz`，并固定该本地产物来源。它与 npm 原版使用同一包名，应替换当前包，不重复启用两个 Bridge。不要覆盖无关插件或模型设置。

在该插件的配置中设置 `connectorSocketPath` 为当前用户的绝对路径，例如 `/home/<user>/.agents-anywhere/connector.sock`。外部模式不使用 `uvPath` 或 `connectorSourceDir` 启动子进程；留空这些旧的自定义启动配置。

## 配对与常驻运行

已配对设备继续使用 `~/.agents-anywhere/connector.json`，无需创建第二个设备。迁移旧安装时先停旧 Connector，再启动公共服务，保留 connectorId、token、statePath、同步目录和 Agent 原生历史。

首次使用可由 DSH 登录界面授权，公共服务统一注册身份；也可先使用官方配对命令：

```bash
~/.local/share/agents-anywhere/venv/bin/python -m connector.cli pair https://your-server --no-start
```

安装 [systemd user unit](deploy/systemd/agents-anywhere-connector.service)，然后启用：

```bash
mkdir -p ~/.config/systemd/user
cp "$RELEASE/artifacts/agents-anywhere-connector.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agents-anywhere-connector.service
```

需要退出登录后保持运行时，可配置用户 linger。公共服务是唯一托管者；不要同时启用旧的 Pi 服务或 DSH 内置 Connector。`pi-aa-connector` 兼容命令仍可用，但只转交官方 CLI，裸调用等价于 `start`；常驻服务使用 `serve`。

## 诊断与回归契约

```bash
~/.local/share/agents-anywhere/venv/bin/python -m connector.cli doctor --expect-runtime pi --expect-runtime dsh
journalctl --user -u agents-anywhere-connector.service
```

Doctor 输出 JSON，包含 `ok`、已安装 Runtime、版本、本机协议、服务状态和设备身份；不输出凭据。检查失败返回非零退出码。它验证本机安装及服务任务存活，不能代替 AA 在线状态和真实模型收发验证。插件日志面板读取公共服务的有限日志；长期日志由 systemd journal 保存。

回归必须覆盖：一个设备同时出现 Pi 和官方 Runtime、插件重载/退出不重启公共服务、公共服务重启后插件重连、普通授权不轮换凭据、跨账号拒绝替换、Pi 与 DSH 实际会话收发。确定性单测不使用真实账号；真实模型 smoke 另行显式启用。

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
| 实时推送（`timeline_item_upsert`） | `message_update` 的文本/思考与 `tool_execution_*` 工具状态，按约 300ms 聚合；轮次结束由全量投影替换；扩展写入的 `/btw` 问答条目立即单项推送，不替换正在生成的主任务回复，轮次结束后以相同 ID 合并进完整历史 |
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
| `warnLiveSessionBytes` | `524288000` | 单会话内存警告线：进程常驻内存超过 500MB 时记录一次警告日志，回落至线下后可再次触发（0 禁用） |
| `maxLiveSessionBytes` | `1073741824` | 单会话内存上限：超过 1GB 时回收会话进程，避免拖垮整机触发全局 OOM（正在执行的回合最多再等 3 分钟结算，0 禁用） |
| `autoContinueAfterMemoryReclaim` | `true` | 回收中断了执行的回合时，自动重启会话并发送「继续」让模型从中断处恢复；连续最多 3 次后停止并留给用户处理 |
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
问答完成后立即推送到 AA，无需等待主任务结束或会话文件落盘。安装方式相同：

```bash
curl -fsSL -o ~/.pi/agent/extensions/btw.ts \
  https://raw.githubusercontent.com/Seryta/pi-agents-anywhere/main/examples/extensions/btw.ts
```

## 已知限制

- **Pi适配代码更新影响整个共享进程**：没有按扩展热重载或独立worker。Python包升级需要重启共享服务；native会话文件保留，但执行中的任务可能中断。
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
CI 覆盖 pi 0.87.1 / 1.1.0，并在固定 Connector v2.0.3 提交上重放集成补丁。
本地首次准备上游时先运行 `python3 integrations/agents-anywhere/apply.py ../Agents-Anywhere`；
更新版本需要同时审查清单与补丁。基础镜像及系统包仍由上游更新。

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

0.3.0共享服务集成验收（2026-10-19，均在Docker内）：

- 官方Connector：1085通过、2个Darwin用例跳过；Pi：257通过、1个需显式开启的付费模型单测跳过，各有1条已记录警告。
- DSH插件2.0.3-shared.2：200通过、0失败、0跳过；typecheck/build/check:build通过。正式产物构建门禁8项通过，包括真实Node→Python管理接口、Peer兼容和重试能力通知回归。
- 本机实际Gateway0.2.1-rc.2接口探针通过；生产229个Python源文件与最终wheel逐字节一致。
- 使用现有AA服务，Pi/DSH分别完成真实模型对话；重启DSH后共享Connector和3个既有Pi进程保留，原设备身份与凭据不变。仅本任务测试会话被归档。
- 本轮未重跑旧版Pi四组兼容矩阵、实体手机客户端GUI或Windows。既有Python lint/format基线问题仍存在，未将其表述为全部lint通过。

以下为0.2.x历史记录，不代表0.3.0支持未经补丁的旧Connector：

历史验收（2026-10-15）：全部在 Docker 内使用 Python 3.12.13、Node 22.19.0 执行。
开启无模型凭据的真 pi 集成测试后，四组兼容性矩阵均为 `240 passed, 1 skipped`；
唯一跳过项是需显式开启的真实模型付费调用。未开启真 pi 的默认模式本轮未单独重跑。

| 官方 Connector | pi 0.87.1 | pi 1.1.0 |
| --- | --- | --- |
| 2.0.0 | 240 passed, 1 skipped | 240 passed, 1 skipped |
| 2.0.3 | 240 passed, 1 skipped | 240 passed, 1 skipped |

该历史版本的 `ruff check` 与 `ruff format --check` 均通过，覆盖 `src`、`tests` 和
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

## 后续升级与维护

跨仓库关系、同步上游、补丁刷新、验证和发布步骤见[维护手册](docs/MAINTENANCE.md)。Codex 使用 `$pi-aa-upgrade`、Pi agent 使用 `/skill:pi-aa-upgrade`，DSH 使用 `skill` 工具加载同名技能；也可先阅读本仓库 [AGENTS.md](AGENTS.md)。三者共用随仓库保存的技能源文件，安装方法见手册。

## 发布状态

[0.3.0](https://github.com/Seryta/pi-agents-anywhere/releases/tag/v0.3.0) 是共享 Connector 架构版本，不能按 0.2.x 的独立 Connector 方式安装。GitHub Release 提供完整匹配产物，PyPI 提供 Pi 扩展包；单独升级 PyPI 包不能完成安装。具体组件版本、迁移与发布范围见[运维与发布说明](docs/OPERATIONS.md#版本与发布)。
