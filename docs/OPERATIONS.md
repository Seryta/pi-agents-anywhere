# 共享 Connector 的安装、更新与发布

适用于 Pi 扩展 0.3.x 的 Linux/systemd 部署。版本组合以 [upstream.json](../integrations/agents-anywhere/upstream.json) 为准；Pi 版本在 [pyproject.toml](../pyproject.toml) 中声明。0.2.x 的独立 Pi Connector 安装方式不能直接沿用。

## 什么改动需要重启

PiProvider/PiRuntime 是公共 Connector 进程内的 Python 对象，不是独立 Python 服务。entry point 负责发现和加载扩展，没有热重载或按扩展隔离进程的能力。Python 模块加载后会被缓存；重新创建一个 Runtime 实例也不等于重新导入新代码。

| 改动或操作 | 如何生效 | 影响范围 |
| --- | --- | --- |
| 修改源码仓库的 `src/pi_aa/*.py` | 先构建并安装新 wheel，再重启共享服务进程 | 同设备全部 Runtime 短暂断开，正在执行的任务可能中断 |
| 升级已安装的 Pi 扩展或 Connector Python 包 | `systemctl --user restart agents-anywhere-connector.service` | 同上；应先等待相关会话空闲 |
| 在 AA 中修改 Pi Runtime 配置 | 由既有配置流程应用，必要时重建 Pi 实例 | 可能关闭该实例的 Pi 子进程；不会加载新 Python 模块 |
| Pi 原生扩展、指令和资源 | 使用 Pi 支持的资源重载方式；AA 的 `/reload` 需安装示例扩展 | 重载 Pi 资源，不加载 `pi_aa` Python 代码 |
| 更新 DSH 插件代码 | 构建并安装新 tgz，重启对应 DSH 实例 | DSH 暂时断开；共享 Connector、Pi 与 Codex 继续运行 |
| 只修改文档 | 无需重启 | 无运行影响 |

**这里的重启是 systemd 服务进程重启。** DSH 面板的“重启 Connector”当前调用 `connector.restart`，只在现有 Python 进程里停止并重建后端任务；它会影响所有 Runtime，但不能保证加载新 Python 代码。单独重启 Pi 会话、重建 Runtime 或重启 DSH 也不能用于更新 Pi Python 适配层。

当前生产安装使用 wheel。修改源码目录不会直接修改已安装环境。即使开发时使用 editable 安装，已导入的 Python 模块也需要进程重启才能可靠更新。

如果将来要求更新 Pi Python 适配层时保留其他 Runtime 的运行进程，需要另行实现 Pi 适配层的独立 worker 与进程间协议；一个设备身份可以保持不变，但本版本尚无这项能力。

## 安装由哪些部分组成

| 组件 | 安装位置与来源 |
| --- | --- |
| 带扩展接口的官方 Connector wheel | 公共服务的 Python 环境；来自本项目固定上游补丁构建 |
| Pi 扩展 wheel | 与 Connector 安装在同一个 Python 环境；通过 entry point 注册 |
| Pi 原生 CLI | 保留原来的 Pi 安装、账号与原生会话目录；它不包含 Python 适配层 |
| DSH 插件 tgz | 通过 DSH 官方插件管理器安装到目标 Profile，配置外部服务 socket |
| systemd user unit | 唯一常驻服务 `agents-anywhere-connector.service` |

只使用 Pi 时不必安装 DSH 插件；要同时接入 DSH，必须使用支持外部服务模式的匹配插件。当前标准 PyPI 依赖名/版本不能单独表达扩展 API 是否存在，所以**单独执行 `pip install -U pi-agents-anywhere` 不能完成这次架构迁移**。应安装同一产物集合中的 Connector 与 Pi wheel。官方原版插件的普通 npm 安装说明适用于其内置托管模式，不应覆盖本集成的外部服务配置。

`pi-aa-connector` 仅是官方 CLI 的兼容入口，不再是另一套守护进程。`pi-aa-connector.service` 在本机迁移后是共享服务的别名，不应再创建一个独立同名服务。

## 首次安装

优先下载 [v0.3.0 Release](https://github.com/Seryta/pi-agents-anywhere/releases/tag/v0.3.0) 的全部附件并验证校验值：

```bash
gh release download v0.3.0 --repo Seryta/pi-agents-anywhere --dir dist/shared-connector
(cd dist/shared-connector && sha256sum --check SHA256SUMS)
```

也可下载整套归档并解压到 `dist/shared-connector/`。需要自行构建时使用以下入口；所有构建、打包和自动化测试都在 Docker 内执行：

```bash
docker/build-shared-connector.sh
```

确认 `dist/shared-connector/` 含两份 wheel、DSH tgz、依赖约束、上游清单、SHA256 清单与打包集成验证结果。将整套产物保存在固定版本目录；不要把运行环境依赖在会被清理的构建输出目录中。

以下是新安装示例；已有安装按下一节迁移，不直接覆盖运行中的环境：

```bash
RELEASE="$HOME/.local/share/agents-anywhere/releases/0.3.0"
mkdir -p "$RELEASE/artifacts"
cp -a dist/shared-connector/. "$RELEASE/artifacts/"
uv venv --python 3.12 "$RELEASE/venv"
uv pip install --python "$RELEASE/venv/bin/python" --no-build \
  -c "$RELEASE/artifacts/constraints.txt" "$RELEASE"/artifacts/*.whl
ln -s "$RELEASE/venv" "$HOME/.local/share/agents-anywhere/venv"
```

venv 直接创建在最终版本路径，避免移动后 console script 的绝对 shebang 失效。稳定入口 `~/.local/share/agents-anywhere/venv` 指向该环境，systemd 模板使用这个入口。

需要 DSH 时，停止目标 DSH 实例，再通过其官方管理器安装固定本地包；替换 Profile 名称：

```bash
PROFILE=web
dsh plugin --profile "$PROFILE" add --ignore-scripts --save-exact \
  "$RELEASE/artifacts/dsh-bridge-next.tgz"
```

在该 Profile 现有 `agents-anywhere-bridge-next` 条目的 `config` 中合并绝对路径 `connectorSocketPath`，例如 `/home/<user>/.agents-anywhere/connector.sock`；不要重复添加条目或覆盖其他插件配置。清空旧自定义 `uvPath` 转接配置。外部服务模式由公共服务保存设备凭据，DSH 插件保留账号授权和官方 Bridge。

首次设备按 [README 配对与常驻运行](../README.md#配对与常驻运行) 完成配对并安装 unit。已有有效全局配置无需重新配对。

## 从 0.2.x 迁移

1. 备份 Connector 配置、同步状态、旧插件私有绑定与 systemd unit，备份目录保持私有；保留 Pi/DSH 原生会话历史。确认原有 serverUrl、connectorId、账号与有效凭据，不能把不同设备或不同账号的私有配置直接互相覆盖。
2. 等待相关任务空闲，停止旧 Pi Connector 与 DSH 插件托管的 Connector。安装新环境前完成依赖准备，避免长时间停机。
3. 保留原有 `~/.agents-anywhere/connector.json`、statePath 和同步目录。若原凭据失效，使用显式设备恢复流程；不要为迁移自动注册第二个设备。
4. 将旧 `pi-aa-connector.service` 独立服务停用；若该名字对应普通 unit 文件，先备份并移出该路径，以便新 unit 创建兼容别名。保留已有内存限制等有效 drop-in，并应用到新的正式服务名。
5. 安装匹配的 wheel/tgz，配置 DSH 的 `connectorSocketPath`，启用共享 unit。旧插件私有 Connector/绑定移出活动目录保存备份；插件账号保留。移除此前自建的启动转接配置，正式运行无需观察脚本。
6. 验证原设备身份不变、Pi 与官方 Runtime 同时可用，并分别做真实会话收发；重启 DSH 后公共服务和 Pi 继续工作。doctor 通过不能替代这些检查。

本机已完成上述迁移；不要重复执行配对或迁移以更新普通 Pi 代码。

## 后续更新与回退

跨仓库源码升级、上游补丁同步和 agent 调用方式见[维护手册](MAINTENANCE.md)。

Pi Python 代码更新时，在新版本目录构建并安装经过验证的 wheel，记录原入口以便回退。等待全部受影响的会话空闲，停止公共服务，切换稳定 venv 入口，再启动服务。不要在运行中的环境覆盖 Python 包。

完成安装后的进程重启与诊断入口：

```bash
systemctl --user restart agents-anywhere-connector.service
~/.local/share/agents-anywhere/venv/bin/python -m connector.cli doctor \
  --expect-runtime pi --expect-runtime dsh
journalctl --user -u agents-anywhere-connector.service
```

仅使用 Pi 的设备省略 `--expect-runtime dsh`。更新后核对 AA 的实际在线/可用状态及真实会话收发。首次或重启后历史同步可能使 DSH 短暂显示启动中，应观察同步是否持续推进并最终完成，不能只看进程存活。

只更新 Pi 扩展且兼容性清单没有要求变更时，无需重新安装 DSH 插件。只更新 DSH 插件时，无需重启公共 Connector。回退使用旧环境与必要的匹配插件，再按相同范围重启；不删除设备或重置账号。

## 版本与发布

Pi **0.3.0** 于 2026-10-19 发布：从独立 Connector 改为共享 Connector 的进程内扩展，旧安装/启动方式不兼容。GitHub Release 提供完整产物集合，PyPI 发布 Pi 扩展 wheel 与源码包；Connector 和 DSH 补丁构建仅通过本项目 Release 分发。

本次验证的组件组合是 Pi0.3.0、Connector2.0.3+shared.1、DSH插件2.0.3-shared.2。后续只改Pi代码时只需递增Pi版本；改Connector集成补丁或DSH插件时分别递增对应组件版本，再更新兼容清单和补丁校验值。文档勘误不要求重启或额外递增未发布的组件版本。

发布交付物应包含 Pi 包与完整匹配产物集合：两份wheel、Pi源码包、DSH tgz、systemd模板、约束文件、上游/协议清单、SHA256清单及构建门禁结果；整套归档便于一次下载。修改后的官方Connector与DSH插件作为本项目Release的配套产物分发；不要向不属于本项目的上游发布命名空间上传补丁构建。Pi包即使单独分发，也必须明确依赖这套兼容安装，不能宣称独立升级即可使用。

发布前完成版本一致性、固定补丁重放、相关容器回归与安装迁移验收；公开推送前按项目要求完成元数据、当前文件、历史message和历史blob的四层隐私审计。然后分别执行提交、tag、Release/包发布。新工作区文档或本机安装不会自动完成这些发布动作。
