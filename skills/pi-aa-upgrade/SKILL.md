---
name: pi-aa-upgrade
description: 维护和升级 pi-agents-anywhere 的共享 Connector 集成，涵盖 Pi 扩展、AA 上游补丁、DSH 外部服务插件，以及对应验证、安装和发布。用户提到升级 Pi/AA/DSH 集成、同步上游或更新共享 Connector 时使用；普通模型配置和其他项目的 Pi 升级不适用。
---

# Pi / AA / DSH 集成维护

本技能为通用 Agent Skill，Codex 使用 `$pi-aa-upgrade`，Pi agent 使用 `/skill:pi-aa-upgrade`，DSH 按名称 `pi-aa-upgrade` 调用 `skill` 工具加载。完整安装与调用说明见仓库维护手册。

先找到本技能所在的 `pi-agents-anywhere` 仓库。若从本机全局技能目录调用，先用 `realpath` 解析技能目录的符号链接，再向上两层定位仓库根；检验 `pyproject.toml` 的项目名和 `integrations/agents-anywhere/upstream.json`。仓库移动或技能为独立副本时，从当前工作区或用户已有检出定位，不凭旧绝对路径推测。

读取仓库的 `AGENTS.md` 和 [docs/MAINTENANCE.md](../../docs/MAINTENANCE.md)。先按手册判断请求针对 Pi 原生 CLI、Pi Python 扩展、AA Connector、DSH 核心还是 Bridge 插件，再只执行本次所需部分。安装 / 重启时额外读 [docs/OPERATIONS.md](../../docs/OPERATIONS.md)。

- 两个源码仓库、三个分发组件；AA 工作树用于开发，Pi 仓库的固定上游提交与补丁定义正式发行输入。
- 先核实当前版本、分支、远端、工作区和任务认领。上游实现以实际源码为准；不要照抄旧清单或恢复已被上游替代的补丁。
- Pi 日常修改通常只涉及 Pi 仓库；AA / DSH 适配按维护手册更新版本、清单、CI 引用和补丁，并验证干净基线重放。
- 保持单设备身份和官方 Runtime；DSH 外部管理与共享服务生命周期分离，Python 代码升级需要服务进程重启。
- 使用现有容器入口验证和构建。正式发行的少量集成门禁与真实会话验收有不同边界，按变更补充必要检查。
- 升级、安装重启、提交推送、公开发布按当前用户任务授权执行；已有授权直接沿用，调用技能本身不扩大动作范围。
- 结束时交代实际变更、版本 / 提交、运行过的验证和未覆盖项；有部署时记录重启范围与回退位置。

操作细节和历史故障经验只维护在仓库手册中，技能入口不另存一份流程。
