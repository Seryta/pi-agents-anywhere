/**
 * dsh-btw host 半部回归测试。
 *
 * 重点验证语义（而不只是返回值）：
 * 1. 命令 handler 不等模型返回（非阻塞）；
 * 2. 开始/结束记录落在 plugin record（log-only，不派生模型消息）；
 * 3. 回答从会话派生历史生成，工具调用/结果被压缩进 transcript；
 * 4. 失败路径写 error；无参数 /btw 重开最近一次问答。
 *
 * 运行：从仓库根执行 docker/build-dsh-btw.sh（容器内运行测试）
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { Session, SessionId } from '@deepseek-ai/dsh-session'
import { createAssistantMessage, createToolResultMessage, createUserMessage } from '@deepseek-ai/dsh-llm'
import { apply, __test } from '../lib/index.js'

const { START, END, REOPEN, buildTranscript, normalizeSettings } = __test

/** 等待条件成立（默认 2s）。 */
async function waitFor(predicate, timeoutMs = 2000) {
  const started = Date.now()
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) throw new Error('waitFor timed out')
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

/** 造一个只提供 commands/llm/effect/logger 的 ctx。 */
function createHost(streamFactory) {
  const commands = new Map()
  const disposers = []
  const ctx = {
    commands: {
      register(definition) {
        commands.set(definition.name, definition)
        return () => commands.delete(definition.name)
      },
    },
    llm: { stream: streamFactory },
    effect(fn) {
      const dispose = fn()
      disposers.push(dispose)
      return () => {}
    },
    logger: { info() {}, warn() {} },
    get() { return undefined },
  }
  apply(ctx, {})
  return {
    ctx,
    commands,
    dispose() {
      for (const dispose of disposers.reverse()) {
        if (typeof dispose === 'function') dispose()
      }
    },
  }
}

function createAgent(session, options = { provider: 'fake', model: 'fake-model' }) {
  return { session, options }
}

function records(session, type) {
  return session.snapshotEvents()
    .filter(event => event.type === type)
    .map(event => event.data)
}

/** 正常的流：先卡在 gate 上，释放后返回 text。 */
function gatedStream(gate) {
  return async function* stream() {
    await gate
    yield { type: 'text-delta', index: 0, text: '42' }
    yield { type: 'finish', reason: { kind: 'stop' } }
    yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 2 } }
  }
}

test('handler 立即返回，回答在后台落盘，且不派生模型消息', async () => {
  const session = Session.create(SessionId('btw-nonblocking'))
  let release
  const gate = new Promise(resolve => { release = resolve })
  const host = createHost(gatedStream(gate))
  const command = host.commands.get('btw')
  assert.ok(command, 'btw command registered')
  assert.equal(command.recordInput, false)

  const startedAt = performance.now()
  const result = await command.handler({
    agent: createAgent(session),
    rawInput: ' 我们刚才改了什么？',
    signal: new AbortController().signal,
  })
  const elapsed = performance.now() - startedAt

  assert.equal(result.kind, 'success')
  assert.ok(elapsed < 100, `handler returned in ${elapsed.toFixed(1)}ms (must not await the model)`)
  assert.equal(records(session, START).length, 1, 'start record written synchronously')
  assert.equal(records(session, END).length, 0, 'no end record before the model finishes')
  assert.equal(session.deriveMessages().length, 0, 'side question must not enter model history')

  release()
  await waitFor(() => records(session, END).length === 1)

  const [start] = records(session, START)
  const [end] = records(session, END)
  assert.equal(start.question, '我们刚才改了什么？')
  assert.equal(start.provider, 'fake')
  assert.equal(start.model, 'fake-model')
  assert.equal(end.btwId, start.btwId)
  assert.equal(end.answer, '42')
  assert.deepEqual(end.usage, { inputTokens: 10, outputTokens: 2 })
  assert.equal(session.deriveMessages().length, 0, 'still no model-visible message after completion')
  host.dispose()
})

test('旁支调用携带 pi 的「必须中文回复」约定，且不挂工具', async () => {
  const session = Session.create(SessionId('btw-chinese'))
  let captured
  const host = createHost(async function* stream(options) {
    captured = options
    yield { type: 'text-delta', index: 0, text: '中文回答' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })
  const command = host.commands.get('btw')
  await command.handler({
    agent: createAgent(session),
    rawInput: 'What is the passphrase?',
    signal: new AbortController().signal,
  })
  await waitFor(() => records(session, END).length === 1)

  assert.ok(captured, 'the side call reached ctx.llm.stream')
  assert.match(captured.system, /always reply in Chinese/)
  assert.match(captured.system, /Do not switch to English/)
  assert.equal(captured.tools, undefined, 'side call must not expose tools')
  assert.equal(captured.messages.length, 1)
  assert.match(captured.messages[0].content[0].text, /Side question: What is the passphrase\?/)
  assert.equal(records(session, END)[0].answer, '中文回答')
  host.dispose()
})

test('模型错误写进 error，而不是抛到主流程', async () => {  const session = Session.create(SessionId('btw-error'))
  const host = createHost(async function* stream() {
    yield { type: 'finish', reason: { kind: 'error', failure: { message: 'provider exploded', code: 'BOOM' } } }
  })
  const command = host.commands.get('btw')

  const result = await command.handler({
    agent: createAgent(session),
    rawInput: '会失败吗？',
    signal: new AbortController().signal,
  })
  assert.equal(result.kind, 'success')

  await waitFor(() => records(session, END).length === 1)
  const [end] = records(session, END)
  assert.match(end.error, /provider exploded/)
  assert.equal(session.deriveMessages().length, 0)
  host.dispose()
})

test('无参数 /btw 重开最近一次已完成问答', async () => {
  const session = Session.create(SessionId('btw-reopen'))
  const host = createHost(async function* stream() {
    yield { type: 'text-delta', index: 0, text: '暗号是 42' }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })
  const command = host.commands.get('btw')
  const agent = createAgent(session)

  await command.handler({ agent, rawInput: '暗号是什么？', signal: new AbortController().signal })
  await waitFor(() => records(session, END).length === 1)

  const reopened = await command.handler({ agent, rawInput: '   ', signal: new AbortController().signal })
  assert.equal(reopened.kind, 'success')
  const [reopen] = records(session, REOPEN)
  assert.ok(reopen, 'reopen record written')
  assert.equal(reopen.question, '暗号是什么？')
  assert.equal(reopen.answer, '暗号是 42')
  assert.equal(session.deriveMessages().length, 0)

  // 没有历史时给出用法，而不是抛错。
  const emptySession = Session.create(SessionId('btw-empty'))
  const empty = await command.handler({
    agent: createAgent(emptySession),
    rawInput: '',
    signal: new AbortController().signal,
  })
  assert.equal(empty.kind, 'error')
  assert.match(empty.text, /用法/)
  host.dispose()
})

test('transcript 保留 user/assistant/tool 事实并压缩工具结果', () => {
  const settings = normalizeSettings({})
  const user = createUserMessage({
    content: [{ type: 'text', text: '暗号是 42' }],
    source: { kind: 'user' },
  })
  const assistant = createAssistantMessage({
    content: [
      { type: 'text', text: '我来确认一下' },
      { type: 'tool-call', id: 'call-1', name: 'bash', arguments: '{"command":"echo 42"}' },
    ],
    source: { provider: 'fake', model: 'fake-model' },
  })
  const tool = createToolResultMessage({
    callId: 'call-1',
    content: [{ type: 'text', text: 'x'.repeat(2000) }],
    isError: false,
  })

  const transcript = buildTranscript([user, assistant, tool], settings)
  assert.match(transcript, /User: 暗号是 42/)
  assert.match(transcript, /Assistant: 我来确认一下/)
  assert.match(transcript, /Assistant called bash with \{"command":"echo 42"\}/)
  assert.match(transcript, /Tool result from bash:/)
  assert.ok(transcript.length < 2000, 'long tool result must be clipped')
})

test('巨型注入上下文不会把真正的用户提问挤出预算', () => {
  const settings = normalizeSettings({})
  const prompt = createUserMessage({
    content: [{ type: 'text', text: '请记住：暗号是 42' }],
    source: { kind: 'user', rpcId: 'rpc-1' },
  })
  const injected = createUserMessage({
    content: [{ type: 'text', text: `<system-reminder>${'x'.repeat(50_000)}</system-reminder>` }],
    source: { kind: 'agent-instructions', form: 'instructions' },
  })
  const time = createUserMessage({
    content: [{ type: 'text', text: `Time sampled while preparing turn 1: ${'y'.repeat(5_000)}` }],
    source: { kind: 'time-context', form: 'snapshot' },
  })

  const transcript = buildTranscript([prompt, injected, time], settings)
  assert.match(transcript, /User: 请记住：暗号是 42/, 'the human prompt must survive the budget')
  assert.ok(transcript.length < 2_000, 'injected context must be summarized, not carried whole')
  assert.doesNotMatch(transcript, /Time sampled while preparing/, 'time snapshots are skipped')
})


test('transcript 裁剪 Unicode 字符后仍可发送给模型接口', () => {
  // Emoji occupies two UTF-16 code units. Each budget cuts between them.
  const cases = [
    ['injected context', { role: 'user', source: { kind: 'agent-instructions' }, content: [{ type: 'text', text: 'x'.repeat(239) + '😀tail' }] }, {}],
    ['tool arguments', { role: 'assistant', content: [{ type: 'tool-call', id: 'call-unicode', name: 'bash', arguments: 'x'.repeat(299) + '😀tail' }] }, {}],
    ['tool result', { role: 'tool', content: [{ type: 'text', text: 'x'.repeat(799) + '😀tail' }] }, {}],
    ['transcript tail', { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '😀tail' }] }, { maxTranscriptChars: 5 }],
  ]
  for (const [label, message, config] of cases) {
    const transcript = buildTranscript([message], normalizeSettings(config))
    assert.ok(transcript.isWellFormed(), `${label}: no lone surrogate may reach the provider`)
    assert.ok(transcript.includes('…[truncated]') || transcript.includes('[earlier session context omitted]'), `${label}: the budget must still apply`)
  }
})
