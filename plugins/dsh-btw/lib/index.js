/**
 * dsh-btw — 旁支问答（/btw）host 半部。
 *
 * 参考 pi agent 的 `/btw` 扩展：主任务继续跑，另起一次「只看当前会话记录、
 * 不给工具」的模型调用回答旁支问题；问答不进入模型上下文，只作为
 * `plugin:dsh-btw/*` 可忽略日志记录落盘，由 client 半部渲染成可关闭的浮条。
 *
 * 关键边界：
 * - 命令 handler 同步返回，真正的模型调用在后台跑，主 agent loop 不受影响；
 * - 读取 `session.deriveMessages()`（与主模型看到的派生历史一致，压缩后自动生效），
 *   而不是把问题塞进会话；
 * - 路由取会话最新的 `request/header`（与主请求同一 provider/model），
 *   新会话尚未发过请求时退回 agent options / 全局默认模型；
 * - 每次调用独立 AbortController + 超时，插件卸载时统一中止。
 */

import { randomUUID } from 'node:crypto'
import { appendPluginRecord, pluginRecordOf } from '@deepseek-ai/dsh-session'
import { BlockAssembler, createUserMessage, errorChain } from '@deepseek-ai/dsh-llm'

export const name = 'dsh-btw'
export const inject = ['commands', 'llm']

// 与 Agents Anywhere Bridge 的时间线投影契约保持同步
// （Agents-Anywhere/dsh-bridge-next/src/host/dsh-runtime/history.ts）：
//   start  { btwId, question, provider, model, startedAt }
//   end    { btwId, question, answer | error, provider, model, elapsedMs, usage? }
//   reopen { btwId, question, answer | error, ... , reopenedFrom }
const START = 'plugin:dsh-btw/start'
const END = 'plugin:dsh-btw/end'
const REOPEN = 'plugin:dsh-btw/reopen'

const DEFAULT_SETTINGS = {
  maxOutputTokens: 1500,
  timeoutMs: 120_000,
  maxConcurrent: 3,
  maxTranscriptMessages: 30,
  maxTranscriptChars: 16_000,
  maxToolResultChars: 800,
  maxToolCallChars: 300,
}

// pi btw.ts 的硬约定：无论问题/转录语言，必须中文回答。测试锁定这一条。
const SIDE_QUESTION_SYSTEM_PROMPT = [
  'Language rule: always reply in Chinese. Do not switch to English or any other language, even when the side question or the transcript is in English, and even when refusing because the transcript lacks the answer.',
  'You answer a quick side question about an ongoing coding session.',
  'You only see the transcript excerpt provided; you cannot read files, run commands, or search.',
  'Answer from the transcript. If it does not contain the answer, say so briefly instead of guessing.',
  'Answer directly and concisely: a few sentences or compact bullets.',
  'Do not emit tool calls; if one appears anyway, note that nothing was executed.',
].join('\n')

/**
 * 归一化插件配置（loader 可能传 undefined / 空对象）。
 * @param {unknown} raw
 */
function normalizeSettings(raw) {
  const value = raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
  const positive = (key, fallback) => {
    const n = Number(value[key])
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback
  }
  return {
    maxOutputTokens: positive('maxOutputTokens', DEFAULT_SETTINGS.maxOutputTokens),
    timeoutMs: positive('timeoutMs', DEFAULT_SETTINGS.timeoutMs),
    maxConcurrent: positive('maxConcurrent', DEFAULT_SETTINGS.maxConcurrent),
    maxTranscriptMessages: positive('maxTranscriptMessages', DEFAULT_SETTINGS.maxTranscriptMessages),
    maxTranscriptChars: positive('maxTranscriptChars', DEFAULT_SETTINGS.maxTranscriptChars),
    maxToolResultChars: positive('maxToolResultChars', DEFAULT_SETTINGS.maxToolResultChars),
    maxToolCallChars: positive('maxToolCallChars', DEFAULT_SETTINGS.maxToolCallChars),
  }
}

/** @param {unknown} value */
function asRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : undefined
}

/** @param {unknown} value */
function asString(value) {
  return typeof value === 'string' && value.length > 0 ? value : undefined
}

/** 只保留 JSON 安全的 usage 数字字段（plugin record 会拒绝 undefined）。 */
function pickUsage(usage) {
  if (!usage || typeof usage !== 'object') return undefined
  const out = {}
  for (const key of ['inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens']) {
    const n = usage[key]
    if (Number.isFinite(n) && n >= 0) out[key] = n
  }
  return Object.keys(out).length > 0 ? out : undefined
}

/** @param {number} length */
function clip(text, length) {
  return text.length <= length ? text : `${text.slice(0, length)} …[truncated]`
}

/**
 * 把派生消息压缩成旁支模型可读的文本记录（只取末尾若干条）。
 *
 * DSH 会把工作区规则 / 技能目录 / 运行时上下文作为 user-role 的
 * 注入消息写进历史（source.kind 不是 user）。这些块体量很大，若原样
 * 进 16k 字符预算，会把真正的用户提问挤出窗口——因此只保留短摘要。
 *
 * @param {readonly unknown[]} messages
 * @param {typeof DEFAULT_SETTINGS} settings
 */
function buildTranscript(messages, settings) {
  const recent = messages.slice(-settings.maxTranscriptMessages)
  /** @type {Map<string, string>} */
  const callNames = new Map()
  /** @type {string[]} */
  const sections = []
  /** 注入上下文只留一句摘要；时间快照对旁支问题没有价值，直接跳过。 */
  const CONTEXT_CLIP = 240
  const SKIP_CONTEXT = new Set(['time-context'])

  for (const raw of recent) {
    const message = asRecord(raw)
    if (!message || typeof message.role !== 'string') continue
    const blocks = Array.isArray(message.content) ? message.content : []
    const lines = []
    switch (message.role) {
      case 'user': {
        const text = blocks
          .filter(block => asRecord(block)?.type === 'text')
          .map(block => block.text)
          .filter(text => typeof text === 'string' && text.trim().length > 0)
          .join('\n')
          .trim()
        if (!text) break
        const source = asRecord(message.source)
        const sourceKind = asString(source?.kind)
        const human = sourceKind === 'user' || asString(source?.rpcId) !== undefined
        if (human) {
          lines.push(`User: ${text}`)
        } else if (sourceKind === undefined || !SKIP_CONTEXT.has(sourceKind)) {
          lines.push(`Context (${sourceKind ?? 'injected'}): ${clip(text, CONTEXT_CLIP)}`)
        }
        break
      }
      case 'assistant': {
        const text = blocks
          .filter(block => asRecord(block)?.type === 'text')
          .map(block => block.text)
          .filter(text => typeof text === 'string' && text.trim().length > 0)
          .join('\n')
          .trim()
        if (text) lines.push(`Assistant: ${text}`)
        for (const block of blocks) {
          const candidate = asRecord(block)
          if (candidate?.type !== 'tool-call' || typeof candidate.name !== 'string') continue
          if (typeof candidate.id === 'string') callNames.set(candidate.id, candidate.name)
          const args = typeof candidate.arguments === 'string' ? candidate.arguments : JSON.stringify(candidate.arguments ?? {})
          lines.push(`Assistant called ${candidate.name} with ${clip(args, settings.maxToolCallChars)}`)
        }
        break
      }
      case 'tool': {
        const text = blocks
          .filter(block => asRecord(block)?.type === 'text')
          .map(block => block.text)
          .filter(text => typeof text === 'string' && text.trim().length > 0)
          .join('\n')
          .trim()
        if (text) {
          const callName = typeof message.toolCallId === 'string' ? callNames.get(message.toolCallId) : undefined
          const label = callName ? `Tool result from ${callName}` : 'Tool result'
          lines.push(`${label}${message.isError === true ? ' (error)' : ''}: ${clip(text, settings.maxToolResultChars)}`)
        }
        break
      }
      default:
        break
    }
    if (lines.length > 0) sections.push(lines.join('\n'))
  }

  const transcript = sections.join('\n\n')
  // UTF-16 裁剪可能切开 emoji；孤立代理项会让模型接口拒绝 JSON（HTTP 400）。
  if (transcript.length <= settings.maxTranscriptChars) return transcript.toWellFormed()
  return `...[earlier session context omitted]\n\n${transcript.slice(-settings.maxTranscriptChars)}`.toWellFormed()
}

/**
 * 解析旁支调用路由：会话最新请求头 → agent 初始 options → 全局默认模型。
 * @param {any} ctx
 * @param {any} agent
 */
function resolveRoute(ctx, agent) {
  const header = typeof agent?.session?.requestHeader === 'function' ? agent.session.requestHeader() : undefined
  const config = asRecord(header?.config)
  if (config && typeof config.provider === 'string' && typeof config.model === 'string') {
    return {
      provider: config.provider,
      model: config.model,
      ...(typeof config.reasoningEffort === 'string' ? { reasoningEffort: config.reasoningEffort } : {}),
    }
  }
  const options = asRecord(agent?.options)
  if (options && typeof options.provider === 'string' && typeof options.model === 'string') {
    return { provider: options.provider, model: options.model }
  }
  try {
    const fallback = ctx.get?.('agentDefaultModel')?.currentSelection?.()
    if (fallback && typeof fallback.provider === 'string' && typeof fallback.model === 'string') {
      return {
        provider: fallback.provider,
        model: fallback.model,
        ...(typeof fallback.reasoningEffort === 'string' ? { reasoningEffort: fallback.reasoningEffort } : {}),
      }
    }
  } catch {
    /* 可选服务缺失不是错误 */
  }
  return undefined
}

/** 流终止原因 → 错误；正常 stop 返回 undefined。 */
function finishError(finish) {
  if (!finish || finish.kind === 'stop') return undefined
  if (finish.kind === 'error' || finish.kind === 'aborted') {
    const failure = asRecord(finish.failure)
    const error = new Error(asString(failure?.message) ?? `模型调用以 ${finish.kind} 结束`)
    if (typeof failure?.code === 'string') error.code = failure.code
    return error
  }
  if (finish.kind === 'max-tokens') return new Error('旁支回答达到 maxOutputTokens 上限')
  if (finish.kind === 'tool-calls') return new Error('模型意外请求了工具；旁支问答不提供任何工具')
  return new Error(`未知的模型终止原因：${String(finish.kind)}`)
}

/**
 * 记录结束事件；会话已销毁等落盘失败只记日志，不向主流程抛错。
 * @param {any} ctx
 * @param {any} session
 * @param {Record<string, unknown>} data
 */
function appendEnd(ctx, session, data) {
  try {
    appendPluginRecord(session, END, data)
  } catch (error) {
    ctx.logger?.warn?.('[dsh-btw] 无法写入回答记录: %s', errorChain(error))
  }
}

/**
 * 后台执行一次旁支模型调用，并把结果写成 `plugin:dsh-btw/end`。
 * @param {any} ctx
 * @param {any} agent
 * @param {string} btwId
 * @param {{provider: string, model: string, reasoningEffort?: string}} route
 * @param {string} question
 * @param {AbortController} controller
 * @param {ReturnType<typeof normalizeSettings>} settings
 */
async function runSideQuestion(ctx, agent, btwId, route, question, controller, settings) {
  const session = agent.session
  const startedAt = Date.now()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, settings.timeoutMs)

  try {
    const transcript = buildTranscript(session.deriveMessages(), settings)
    const prompt = [
      'Session transcript so far:',
      '<transcript>',
      transcript || '(The session has no messages yet.)',
      '</transcript>',
      '',
      `Side question: ${question}`,
    ].join('\n')

    const user = createUserMessage({
      content: [{ type: 'text', text: prompt }],
      source: { kind: 'user' },
    })

    const assembler = new BlockAssembler()
    const stream = ctx.llm.stream({
      provider: route.provider,
      model: route.model,
      ...(route.reasoningEffort === undefined ? {} : { reasoningEffort: route.reasoningEffort }),
      system: SIDE_QUESTION_SYSTEM_PROMPT,
      messages: [user],
      temperature: 0.2,
      maxTokens: settings.maxOutputTokens,
      sessionId: session.id,
      signal: controller.signal,
    })
    for await (const chunk of stream) {
      assembler.push(chunk)
    }

    if (timedOut) throw new Error(`旁支回答超时（${Math.round(settings.timeoutMs / 1000)} 秒）`)
    const failure = finishError(assembler.finish)
    if (failure) throw failure

    const answer = assembler
      .blocks()
      .filter(block => block.type === 'text')
      .map(block => block.text)
      .join('')
      .trim()
    if (answer.length === 0) throw new Error('模型没有返回文本回答')

    const usage = pickUsage(assembler.usage)
    appendEnd(ctx, session, {
      btwId,
      question,
      provider: route.provider,
      model: route.model,
      elapsedMs: Date.now() - startedAt,
      answer,
      ...(usage === undefined ? {} : { usage }),
    })
  } catch (error) {
    const reason = timedOut
      ? `旁支回答超时（${Math.round(settings.timeoutMs / 1000)} 秒）`
      : errorChain(error)
    appendEnd(ctx, session, {
      btwId,
      question,
      provider: route.provider,
      model: route.model,
      elapsedMs: Date.now() - startedAt,
      error: reason,
    })
  } finally {
    clearTimeout(timer)
  }
}

/**
 * 无参数 `/btw`：把最近一次已完成的旁支问答重新贴到对话末尾（新的 btwId，
 * 内容自包含），client 浮条会重新打开。
 * @param {any} agent
 */
function reopenLast(agent) {
  const session = agent.session
  const starts = new Map()
  let latestEnd
  // 0.2.x 用 snapshotEvents()；保留 events getter 兼容旧版。
  const events = typeof session.snapshotEvents === 'function' ? session.snapshotEvents() : session.events
  for (const event of events) {
    const record = pluginRecordOf(event)
    if (record === undefined) continue
    const data = asRecord(record.data)
    if (!data) continue
    const btwId = asString(data.btwId)
    if (!btwId) continue
    if (record.type === START) {
      starts.set(btwId, data)
    } else if (record.type === END && (latestEnd === undefined || record.seq > latestEnd.seq)) {
      latestEnd = { ...data, seq: record.seq }
    }
  }

  if (latestEnd === undefined) {
    return { kind: 'error', text: '还没有可重开的旁支问答。用法：/btw 你的问题' }
  }

  const start = starts.get(latestEnd.btwId)
  const question = asString(start?.question) ?? '（原问题已不在当前日志窗口）'
  const provider = asString(latestEnd.provider) ?? asString(start?.provider)
  const model = asString(latestEnd.model) ?? asString(start?.model)
  const startedAt = Number.isFinite(start?.startedAt) ? start.startedAt : Date.now()
  const reopenedId = randomUUID()
  const data = {
    btwId: reopenedId,
    question,
    startedAt,
    reopenedFrom: latestEnd.btwId,
    reopenedAt: Date.now(),
    ...(provider === undefined ? {} : { provider }),
    ...(model === undefined ? {} : { model }),
    ...(Number.isFinite(latestEnd.elapsedMs) ? { elapsedMs: latestEnd.elapsedMs } : {}),
    ...(asRecord(latestEnd.usage) === undefined ? {} : { usage: latestEnd.usage }),
    ...(asString(latestEnd.answer) === undefined ? {} : { answer: latestEnd.answer }),
    ...(asString(latestEnd.error) === undefined ? {} : { error: latestEnd.error }),
  }
  try {
    appendPluginRecord(session, REOPEN, data)
  } catch (error) {
    return { kind: 'error', text: `无法重开旁支问答：${errorChain(error)}` }
  }
  return { kind: 'success' }
}

/**
 * 插件入口。
 * @param {any} ctx
 * @param {unknown} config
 */
export function apply(ctx, config) {
  const settings = normalizeSettings(config)
  const inflight = new Set()

  ctx.effect(() => ctx.commands.register({
    name: 'btw',
    description: '旁支问答：基于当前会话记录单独回答，不打断主任务、不进入模型上下文',
    input: { hint: '问题（留空则重开上次问答）' },
    recordInput: false,
    handler: ({ agent, rawInput }) => {
      const question = rawInput.trim()

      if (question.length === 0) return reopenLast(agent)

      if (inflight.size >= settings.maxConcurrent) {
        return { kind: 'error', text: `已有 ${inflight.size} 个旁支问答在进行，请稍后再试` }
      }

      const route = resolveRoute(ctx, agent)
      if (route === undefined) {
        return { kind: 'error', text: '当前会话还没有可用的模型路由：先发一条消息，或在设置里选择默认模型' }
      }

      const btwId = randomUUID()
      try {
        appendPluginRecord(agent.session, START, {
          btwId,
          question,
          provider: route.provider,
          model: route.model,
          startedAt: Date.now(),
          ...(route.reasoningEffort === undefined ? {} : { reasoningEffort: route.reasoningEffort }),
        })
      } catch (error) {
        return { kind: 'error', text: `无法记录旁支问答：${errorChain(error)}` }
      }

      const controller = new AbortController()
      inflight.add(controller)
      void runSideQuestion(ctx, agent, btwId, route, question, controller, settings)
        .catch(error => {
          ctx.logger?.warn?.('[dsh-btw] 旁支问答后台任务失败: %s', errorChain(error))
        })
        .finally(() => {
          inflight.delete(controller)
        })

      return { kind: 'success' }
    },
  }), 'dsh-btw: /btw command')

  ctx.effect(() => () => {
    for (const controller of inflight) controller.abort()
    inflight.clear()
  }, 'dsh-btw: abort in-flight side questions')
}

/** 内部测试入口（不参与插件装配）。 */
export const __test = {
  START,
  END,
  REOPEN,
  buildTranscript,
  normalizeSettings,
  resolveRoute,
  reopenLast,
  finishError,
}
