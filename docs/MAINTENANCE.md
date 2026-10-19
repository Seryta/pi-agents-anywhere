# Pi / AA / DSH 集成升级手册

本文供下一次维护的 agent 使用。操作说明见 [OPERATIONS.md](OPERATIONS.md)，本文重点保存两个仓库之间的关系、升级决策和验收边界。

## 下次可以这样下达任务

三种 agent 共用 `skills/pi-aa-upgrade/SKILL.md`，入口语法不同：

| Agent | 调用方式 |
| --- | --- |
| Codex | `使用 $pi-aa-upgrade ...` |
| Pi agent | `/skill:pi-aa-upgrade ...` |
| DSH | `请调用 skill 工具加载 pi-aa-upgrade，再按维护手册 ...` |

以下自然语言示例使用 Codex 语法；Pi / DSH 替换为对应入口即可。

仅检查上游变化：

> 使用 $pi-aa-upgrade 检查 Pi、AA 和 DSH 是否需要升级，先阅读仓库 AGENTS.md 和 docs/MAINTENANCE.md，报告建议版本与影响范围。

开发并验证：

> 使用 $pi-aa-upgrade 将共享 Connector 集成升级到指定版本（或最新稳定版），按维护手册同步上游和补丁，在容器中完成回归并更新文档。

需要完整交付时，在任务后明确追加所需操作，例如：

> 同时安装到本机，允许重启受影响的 Connector / DSH 并中断相关会话；验证真实收发后提交推送，发布 GitHub Release 和 Pi PyPI 包。

不支持技能的 agent：把第一句替换为“在 pi-agents-anywhere 仓库按 AGENTS.md 和 docs/MAINTENANCE.md 执行”。调用技能只提供流程，实际动作仍以本次用户任务为准；已有授权不重复索取。

## 仓库与事实来源

| 位置 | 职责 | 应先读取 |
| --- | --- | --- |
| `pi-agents-anywhere` | Pi Python 扩展、固定补丁、构建与统一发行 | `pyproject.toml`、`src/pi_aa/__init__.py`、`integrations/agents-anywhere/upstream.json`、`.github/workflows/tests.yml` |
| `Agents-Anywhere` 的维护者 fork | 官方 Connector 扩展入口与公共服务、DSH 外部管理模式和连接修复 | 该仓库 `AGENTS.md`、`connector/`、`dsh-bridge-next/`、`contracts/connector-local-control/` |

仓库地址以实际 `git remote -v` 为准。当前公开来源：

- Pi 与整套 Release：<https://github.com/Seryta/pi-agents-anywhere>。
- AA 个人 fork：<https://github.com/Seryta/Agents-Anywhere>；上游为 <https://github.com/anywhere-labs/Agents-Anywhere>。
- AA 仓库中 `origin` 是官方上游，`fork` 是个人仓库；不能凭 remote 名称假定推送目标。

0.3.0 发布时：Pi `main` / `v0.3.0` 对应 `5df74d5ec7c71b38bd304dcccd694bd15f79b0cd`；AA 集成分支为个人 fork 的 `codex/shared-connector-runtime`。这只是初始定位信息，后续先核实当前分支、远端和清单。

Pi 原生 CLI、Pi Python 扩展、官方 Connector、DSH 核心及 DSH Bridge 插件是不同组件。用户说“升级 Pi”但对象有实质歧义时，先区分对象，不连带升级整套依赖。当前架构没有修改 Pi 上游、DSH 核心或 AA 各客户端。

## 必须保留的运行关系

- 一台工作设备一个共享 Connector、一个设备身份；官方 Codex / Claude / DSH Provider 和 Pi 扩展共存。
- Pi 由 `agents_anywhere.runtime_providers.v1` 加载；扩展入口和 `on_backend_reconnect()` 回调目前来自集成补丁，不能假定任意官方发行版都已具备。
- DSH 保留官方 Bridge / Runtime，通过私有 Unix socket 管理共享服务。插件关闭、重载、退出账号和重置只解除管理连接；显式停止 / 重启服务会影响全部 Runtime。
- 全局进程所有权必须在改写凭据前取得；普通连接复用设备身份。保留账号、凭据、同步状态和原生会话历史，不为升级重新配对或自动注册第二个设备。
- Python 扩展升级需要重启共享服务进程。`connector.restart` 和 DSH 面板重启只重建进程内任务；Pi `/reload` 只加载原生资源。
- 固定正式 wheel / tgz 和版本目录，不以临时转接脚本、猴子补丁或开发目录充当常驻安装。

## 选择升级范围

| 请求 | 主要工作 | 重启边界 |
| --- | --- | --- |
| 只改 Pi Python 适配层 | 修改 Pi 仓库，递增 Pi 版本，沿用未变的集成补丁 | 安装新 wheel 后重启共享 Connector |
| 只升级 Pi 原生 CLI | 核实 CLI / RPC 源码变化，更新实际覆盖的测试版本与安装 | 按原生进程加载方式处理，不假定旧进程自动换版本 |
| 升级官方 AA Connector | 在 AA 工作分支适配补丁，更新清单、依赖、测试与发行产物 | 更新 Python 环境后重启共享 Connector |
| 升级 DSH 核心或 Bridge 插件 | 核实实际 Gateway / Peer / Bridge 契约，适配插件并测试 | 仅插件代码变化时重启目标 DSH |
| 文档勘误 | 更新相应文档 | 无需重启 |

## DSH `/btw` 插件源码

核心插件位于本仓库 `plugins/dsh-btw/`，不另建第三个仓库。AA 的命令转发与时间线投影仍在 AA fork；本仓库保存其固定补丁。构建与安装见[插件说明](../plugins/dsh-btw/README.md)，容器入口为 `docker/build-dsh-btw.sh`。本机旧的本地插件目录是安装副本，后续修改以仓库源码为准，打包安装后重启目标 DSH；提交源码不会自动更新已加载的模块。

## 同步 AA 上游与集成补丁

1. 检查两个仓库的分支、工作区、认领和远端。保存旧清单、完整补丁和可回退的已发布产物；隔离他人改动，优先创建新的工作分支 / worktree。
2. 从官方实际源码及发布信息确定目标提交，检查上游是否已实现扩展加载、公共服务或外部插件模式。已经具备的能力验证后复用；只删除确实已由上游覆盖的补丁。
3. 在目标上游提交上移植旧集成改动。需要时对旧补丁使用 `git apply --3way`，先确保旧 / 新基线的 Git 对象可用；逐项解决源码和契约冲突。不能在旧集成树上只改清单的 commit 字段冒充上游升级。
4. 更新组件版本及所需契约。Pi 版本在 `pyproject.toml` 和 `src/pi_aa/__init__.py`；Connector 在 AA 的 `connector/pyproject.toml`；插件在 `dsh-bridge-next/package.json`。Pi 依赖的 Connector 基础版本也要匹配。
5. 更新 Pi 清单中的目标上游 commit、组件 / 协议版本，并同步 CI 中 `PI_AA_CONNECTOR_REF`。遵循 AA 仓库的版本规则，不把本机集成补丁擅自包装为官方产品版本。
6. AA 改动按逻辑提交后，从 Pi 仓库运行以下维护入口；`AA_WORKTREE` 指向已适配的 AA 工作树：

```bash
python3 integrations/agents-anywhere/update.py "$AA_WORKTREE"
python3 integrations/agents-anywhere/update.py "$AA_WORKTREE" --check
```

这两个命令只生成 / 检查 Git 补丁，可在宿主运行。工具从清单中的上游基线取差异，允许 AA HEAD 已包含集成提交；不要求 HEAD 仍等于基线。AA 文件由未跟踪变为已提交时，补丁排列可能变化，应在最终 AA 提交后再次生成。

7. 在另一份干净的目标基线工作树，用 `apply.py` 或正式发行构建重放补丁。`apply.py` 要求 HEAD 恰为清单基线；不要向已经包含集成提交的开发分支错误调用它。

变更 DSH 历史投影版本时，同步更新 Connector 支持列表、持久检查点、`contracts/dsh-bridge/1.0` 的批次与检查点 Schema，以及协议说明。补丁维护入口必须收集该契约目录；仅修改 Host 与 Python 版本检查会使真实后端集成的 Schema 校验失败。

发布输入是 Pi 仓库里的上游 commit 加完整补丁。正式构建会忽略相邻 AA 工作树的本地修改；漏更新补丁时，即使开发树测试通过，发布包仍可能缺少修复。

## 容器验证与验收

先读当前 Dockerfile、脚本、上游测试入口及直接调用者。以下从 Pi 仓库执行，`AA_WORKTREE` 必须是已应用集成的源码树：

```bash
PI_AA_CONNECTOR_SOURCE="$AA_WORKTREE/connector" docker/build-test-env.sh
PI_AA_CONNECTOR_SOURCE="$AA_WORKTREE/connector" docker/run-tests.sh   bash -c 'python -m ruff check src tests && python -m ruff format --check src tests'
PI_AA_CONNECTOR_SOURCE="$AA_WORKTREE/connector" PI_AA_TRUE_PI=1 docker/run-tests.sh
PI_AA_UPSTREAM_SOURCE="$AA_WORKTREE" docker/build-shared-connector.sh
```

按改动范围补官方 Connector 测试与 DSH 完整测试，实际负载全部在容器 / 虚机内运行。CI 的 Pi 版本矩阵要与声称支持的版本一致。DSH 测试指定容器内的 `AA_TEST_PYTHON`，准备其真实 Python / Server 夹具依赖；禁用 Node 安装脚本时，注意 uv 可执行文件的准备。既有串行运行曾规避短超时用例在并发下的波动，失败时先核实原因。uv 的 `--with-editable` 夹具会额外解析依赖，可将 `UV_CONSTRAINT` 指向本次约束文件，并按相同 UID 复用可写缓存。以非 root UID 运行完整 DSH 夹具时，容器必须有对应 passwd 用户记录；原生测试通过 `os.userInfo()` 确定用户路径。

正式构建执行 typecheck、build、check:build 及打包后的跨语言回归。发行门禁只覆盖明确列出的场景，不能替代 DSH 全量测试或真实服务验收；以本次输出为准，不把 0.3.0 的测试数字复制成新版本结果。

需要覆盖的业务行为：

- 一个设备同时提供 Pi 与官方 Runtime；共享连接断开恢复后重新发布状态。
- DSH 插件退出 / 重载不重启共享 Connector，Pi 会话继续；共享服务重启后 DSH 可重连。
- 普通账号连接保留身份和凭据，跨账号替换被拒绝，所有权先于凭据操作。
- Gateway 交互流及 `$events/result` 回复使用同一显式 Peer；必须同时验证打开和回复两条路径。只验证订阅成功会漏掉审批回复引发的重连风暴。重试不反复广播未变化的能力，AA 最终离开“启动中”。
- DSH `/btw` 依赖持久安装的 `dsh-btw` 插件；仅运行时注入不会跨 DSH 重启保留。Bridge 投影版本 4 需要接受该版本的配套 Connector，先准备两侧产物再切换；中文旁支回答在主任务运行时出现在 AA，主回合结束后不重复，也不进入主模型历史。
- 实际安装后，在原设备上完成 Pi / DSH 真实会话收发，检查已有会话可恢复；只测试自己创建的会话。

医生入口见 [运维说明](OPERATIONS.md#后续更新与回退)。doctor 验证本机状态，不能替代真实收发。需要账号、模型调用或运行环境而本次未获授权 / 无法完成的检查，明确写为未验证。

## 版本、安装与发布

只为实际变化的组件递增版本。但 DSH 插件会把 Connector 的源码和 README 打包进 `lib/bundled-connector/`：Connector 变化也可能改变插件 tgz，发布前必须检查，不能用已发布的同一插件版本装入不同内容。

- 更新 CHANGELOG、安装说明、版本清单、固定补丁与对应 CI 引用。只有文档变化无需另发软件版本。
- 使用新版本目录安装完整配套产物；venv 在最终路径创建，避免移动后 shebang 失效。保留原稳定入口和旧产物以便回退，按当前用户授权范围重启受影响服务。
- GitHub Release 位于 Pi 仓库，分发匹配的两份 wheel、Pi sdist、DSH tgz、systemd unit、约束、上游清单、集成结果、整套归档及校验文件。当前构建会生成这些附件。
- PyPI 只上传 `pi_agents_anywhere-版本-py3-none-any.whl` 和 `pi_agents_anywhere-版本.tar.gz`。不要使用覆盖全部附件的上传通配符，不向官方 Connector / DSH 的发布命名空间上传修改包。
- 使用支持实际元数据格式的发布工具；0.3.0 的 Metadata 2.5 经 Twine 7.0.0 验证，旧 Twine 6.2 无法检查。凭据只从现有安全配置注入，不写入仓库、日志或说明。
- 公开推送前核对目标可见性，完成提交身份、当前文件 / HEAD、历史 message 和全部待推送历史 blob 的四层隐私审计。只推所需分支 / 标签，私有备份引用不发布。
- 提交、打标签和发布对应同一验证过的提交；等待相关 CI。可先上传 GitHub 草稿附件并核对服务端摘要，发布完成后重新下载校验，并核对 PyPI 与 GitHub 的包摘要一致。

GitHub 草稿可能无法通过按 tag 查询的 REST 入口读取；从 release 列表定位 ID，再查询具体草稿。HTTPS OAuth 推送若因 workflow scope 被拒绝，可使用已获授权的现有 SSH 身份向同一目标重试，不改写或绕过仓库规则。

交付时记录最终提交 / 标签、组件和基线版本、实际检查结果及跳过项、安装路径、重启范围、设备身份是否保留和回退入口。真实账号、设备标识、主机地址及私有日志留在受保护的本地记录，不进入公开维护文档。

## 在新机器安装技能入口

技能源文件随仓库版本管理；本机技能只是指向它的入口。设置 `PI_REPO` 为实际检出目录后：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -s "$PI_REPO/skills/pi-aa-upgrade" "${CODEX_HOME:-$HOME/.codex}/skills/pi-aa-upgrade"
```

若目标已存在，先查看并确认其来源，不覆盖其他技能。

Pi agent 使用 `~/.pi/agent/settings.json` 的 `skills` 数组：追加 `PI_REPO/skills/pi-aa-upgrade` 的实际绝对路径，保留已有条目和其他设置。JSON 不展开 shell 变量，不能把字面量 `$PI_REPO` 写进去。这个入口避免改动用户可能单独纳入 Git 的 `~/.pi/agent/skills` 仓库。新会话会加载配置；已有 Pi 会话可用 `/reload` 重新加载资源和设置。

DSH 使用用户技能目录入口：

```bash
mkdir -p "$HOME/.dsh/skills"
ln -s "$PI_REPO/skills/pi-aa-upgrade" "$HOME/.dsh/skills/pi-aa-upgrade"
```

配置了自定义 DSH home 或禁用默认技能来源时，以当前技能文件系统 Provider 配置为准。DSH 文件系统 Provider 支持符号链接；调用时按名称 `pi-aa-upgrade` 使用 `skill` 工具。当前安装不要求重启共享 Connector；正在运行的 DSH 会话是否已经刷新目录需要单独核实，新会话可重新读取技能。

本机三个入口都引用同一个仓库目录，避免分别复制后产生版本分歧。未安装技能时仍可直接使用本仓库的 AGENTS.md 和本文。
