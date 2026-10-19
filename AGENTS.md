# Pi / Agents Anywhere 维护入口

本项目将 Pi 作为共享官方 Connector 的 Runtime 扩展，并分发带集成补丁的 Connector 与 DSH 插件。

- 升级 Pi、AA Connector、DSH 插件或固定上游版本时，先读 [维护手册](docs/MAINTENANCE.md)，再读 [安装与运维](docs/OPERATIONS.md)。
- 版本事实以 `pyproject.toml`、`integrations/agents-anywhere/upstream.json` 和当前发布产物为准；不照抄旧聊天中的版本、分支或测试数字。
- AA fork 承担 Connector / DSH 源码开发；本仓库的固定上游提交和补丁定义发行构建输入。修改 AA 后重新生成补丁，不独立手改两套实现。
- 保持单设备身份、单共享 Connector 和官方 Runtime。DSH 插件退出不停止共享服务；Pi Python 包升级需重启共享服务进程。
- 编译、打包、lint、自动化测试全部在容器或虚机内执行；使用现有 `docker/` 入口，检查嵌套命令的实际执行环境。
- 改仓库前检查 `.agent/claims.md` 并登记自己的范围；`.agent/` 为被忽略的本地协调状态。
- 本次任务已授权的步骤直接执行。安装重启、公开推送、包发布按当前用户指令的范围处理；技能本身不授予这些权限。
- Codex、Pi agent 与 DSH 共用的技能源文件为 [skills/pi-aa-upgrade/SKILL.md](skills/pi-aa-upgrade/SKILL.md)。没有技能支持的 agent 直接遵循维护手册。
