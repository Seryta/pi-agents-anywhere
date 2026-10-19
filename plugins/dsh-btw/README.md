# DSH `/btw` 旁支问答

主任务继续运行时，基于当前会话记录回答一个临时问题。固定使用中文，无工具调用；问答只写 `plugin:dsh-btw/*` 日志，不加入主模型消息历史。参考本仓库的 [Pi `/btw` 扩展](../../examples/extensions/btw.ts)。

`lib/index.js` 是宿主插件，`lib/client.js` 是 DSH 浏览器模块。两者均为 JavaScript，无需转译。插件源码与 AA 集成维护在现有两个仓库中：本目录保存插件，AA fork 的 `dsh-bridge-next` 保存命令桥接和时间线投影；后者的固定补丁位于本仓库 `integrations/agents-anywhere`。

## 用法

| 输入或操作 | 行为 |
| --- | --- |
| `/btw 问题` | 后台回答；命令立即确认，主任务继续 |
| 无参数 `/btw` | 重开最近一次已完成的问答；原生输入框可按空格后提交 |
| 原生浮条 `‹` / `›` | 翻看本会话的旁支问答 |
| 原生浮条 `×` | 关闭浮条；新问题会重新打开 |

DSH 原生 Web 界面在输入框上方显示浮条，AA 中则显示独立时间线条目。当前配套组合为 Connector `2.0.3+shared.2`、Bridge `2.0.3-shared.3`；宿主 SDK 验证基线为 DSH `0.2.1-rc.2`。仅安装本插件不会给旧版 AA Bridge 增加命令支持。

## 构建、验证与安装

从仓库根执行：

```bash
docker/build-dsh-btw.sh
```

所有依赖安装、测试和打包都在 Docker 内执行。输出到 `dist/dsh-btw/`：插件 tgz、源码与打包后两份 TAP 结果、带 SHA256 的 `verification.json`。容器固定 Session / LLM SDK 版本，验证非阻塞、中文提示约束、无工具、模型历史隔离、失败落盘、重开和转录预算；另检查浏览器模块语法。它不调用真实模型，也不验证浏览器渲染。

停止目标 DSH 实例后，使用其官方插件管理器安装 tgz，然后启动实例：

```bash
dsh plugin --profile web add --ignore-scripts --save-exact \
  "$PWD/dist/dsh-btw/dsh-btw-0.2.0.tgz"
```

包自带 `cordis.patch.yml`；管理器将其登记到目标 Profile 的依赖和 bundle 列表，重启后仍会加载。插件管理器运行环境需要能找到 `pnpm`；独立 systemd 安装任务应显式提供正确 PATH。运行时注入只用于开发，不能代替持久安装。

宿主必须与插件共享同一套 DSH SDK，尤其是 `dsh-session` 的 Session 类。不要将测试容器的 `node_modules` 打入安装包；两份 SDK 即使版本相同也可能因类私有身份不同而拒绝写入记录。通过 DSH 官方加载器运行，并在安装后验证真实命令，不能只凭 tgz 生成成功认定安装可用。

当前包为私有分发包，不发布到 npm；本次提交不代表已发布新 Release。CI 提供可下载的 tgz。修改本目录后重新构建并安装新包，再重启目标 DSH。开发机器原有的本地插件安装不会随着仓库提交自动改变。

## 事件契约与 AA

| 事件 | 数据 |
| --- | --- |
| `plugin:dsh-btw/start` | `btwId, question, provider, model, startedAt` |
| `plugin:dsh-btw/end` | `btwId, question, answer 或 error, provider, model, elapsedMs, usage?` |
| `plugin:dsh-btw/reopen` | 自包含问答与 `reopenedFrom`，使用新的 `btwId` |

AA Bridge 仅在原生 registry 注册了 `btw` 时暴露命令。start/end 更新同一时间线条目；旁支不属于主 turn，主回合结束不能关闭仍在回答的旁支。Bridge 投影 v4、Connector 支持列表和 `contracts/dsh-bridge/1.0` 的同步批次及检查点 Schema 必须一起更新。

原生 client 使用公开的 Conversation Definition 和 `conversation.input.dock` 插槽；回答以纯文本展示。模型输出不作为 HTML 执行。命令生命周期记录仍会落盘，问题保存在插件记录中。

## 配置

在目标 Profile 的 `dsh-btw` patch entry 的 `config` 中设置：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `maxOutputTokens` | 1500 | 输出 token 上限，含 reasoning |
| `timeoutMs` | 120000 | 超时后记录错误 |
| `maxConcurrent` | 3 | 本插件实例的并发旁支数 |
| `maxTranscriptMessages` | 30 | 最近消息数 |
| `maxTranscriptChars` | 16000 | 转录字符预算 |
| `maxToolResultChars` | 800 | 单条工具结果字符上限 |
| `maxToolCallChars` | 300 | 单条工具参数字符上限 |

路由优先取最近一次 `request/header`，然后取 agent options 和全局默认模型。选择模型后尚未发过主消息时，最近请求头可能仍是旧路由。旁支继承该请求头的 reasoning effort；错误直接显示，不自动重试。调用绕过 agent loop，旁支用量不进入主会话账本。

安装后的真实验收：主任务运行时提交英文侧问题，确认中文回答先于主任务完成；检查 AA 条目唯一、主任务继续、无参数重开成功，并核对主模型派生历史没有新增旁支问答。浏览器浮条还需验证刷新恢复、翻页和关闭。这些运行验收与容器单测是不同检查。
