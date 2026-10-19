/**
 * dsh-btw — 旁支问答（/btw）client 半部。
 *
 * 零构建手写 bundle（与 dsh-node-nav / dsh-file-guard 同款形态）：
 * - 注册一个 target=chat 的 Conversation Definition，把 plugin:dsh-btw/* 日志记录
 *   折成隐藏节点（不占聊天流），只作为浮条的数据源；
 * - 注册 conversation.input.dock，在当前会话输入框上方渲染可关闭的「旁支问答」
 *   浮条：跑动中显示加载态，完成后显示回答，可 ‹ › 翻历史、× 关闭；
 * - 新问答到达时自动重新打开（与 pi 的 /btw 面板一致）。
 *
 * 所有记录都是 log-only 的 plugin record，不进模型上下文；client 只读展示。
 */
window.__ModuleLoader__.load({
  id: 'dsh-btw',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')
    const h = React.createElement

    const START = 'plugin:dsh-btw/start'
    const END = 'plugin:dsh-btw/end'
    const REOPEN = 'plugin:dsh-btw/reopen'
    const NODE_KIND = 'btw'
    const CSS_ID = 'dsh-btw-style'

    const inject = ['uiConversation', 'slots']

    const CSS = `
.dsh-btw-dock {
  margin: 0 0 8px;
  border: 1px solid rgba(127, 127, 127, 0.28);
  border-radius: 10px;
  background: rgba(127, 127, 127, 0.07);
  padding: 9px 11px 8px;
  font-size: 12.5px;
  line-height: 1.55;
  color: inherit;
}
.dsh-btw-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 20px;
}
.dsh-btw-title {
  font-weight: 600;
  letter-spacing: 0.02em;
}
.dsh-btw-meta {
  color: rgba(127, 127, 127, 0.95);
  font-size: 11.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1 1 auto;
  min-width: 0;
}
.dsh-btw-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex: none;
}
.dsh-btw-actions button {
  border: 0;
  background: transparent;
  color: inherit;
  opacity: 0.65;
  cursor: pointer;
  font: inherit;
  line-height: 1;
  padding: 3px 5px;
  border-radius: 5px;
}
.dsh-btw-actions button:hover:not(:disabled) {
  opacity: 1;
  background: rgba(127, 127, 127, 0.18);
}
.dsh-btw-actions button:disabled {
  opacity: 0.25;
  cursor: default;
}
.dsh-btw-q {
  margin-top: 5px;
  font-weight: 500;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.dsh-btw-a {
  margin-top: 4px;
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.dsh-btw-a.is-error {
  color: #c0392b;
}
.dsh-btw-loading {
  color: rgba(127, 127, 127, 0.95);
}
.dsh-btw-loading::after {
  content: '…';
  animation: dsh-btw-pulse 1.2s ease-in-out infinite;
}
@keyframes dsh-btw-pulse {
  0%, 100% { opacity: 0.2; }
  50% { opacity: 1; }
}
/* /btw 的 command 生命周期行不进入聊天流：内容由浮条展示。 */
div:has(> [data-dsh-btw-command]) {
  display: none !important;
}
`

    /** @param {unknown} value */
    function asRecord(value) {
      return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : undefined
    }

    /** @param {unknown} value */
    function asString(value) {
      return typeof value === 'string' && value.length > 0 ? value : undefined
    }

    /** @param {unknown} value */
    function asNumber(value) {
      return typeof value === 'number' && Number.isFinite(value) ? value : undefined
    }

    function ensureStyle() {
      if (typeof document === 'undefined') return undefined
      const existing = document.getElementById(CSS_ID)
      if (existing !== null) return existing
      const style = document.createElement('style')
      style.id = CSS_ID
      style.dataset.plugin = 'dsh-btw'
      style.textContent = CSS
      document.head.appendChild(style)
      return style
    }

    /** 把一条 end 记录折叠到状态上；无法识别时给出错误态。 */
    function applyEnd(state, data) {
      const answer = asString(data.answer)
      const error = asString(data.error) ?? (answer === undefined ? '旁支问答返回了无法识别的结果' : undefined)
      return {
        ...state,
        status: error !== undefined ? 'error' : 'answer',
        ...(answer === undefined ? {} : { answer }),
        ...(error === undefined ? {} : { error }),
        ...(asNumber(data.elapsedMs) === undefined ? {} : { elapsedMs: data.elapsedMs }),
        ...(asRecord(data.usage) === undefined ? {} : { usage: data.usage }),
      }
    }

    /** target=chat 的隐藏 Conversation Node：只给浮条提供可观察数据。 */
    const btwDefinition = {
      kind: NODE_KIND,
      target: 'chat',
      match(event) {
        const type = event.type
        if (type !== START && type !== END && type !== REOPEN) return null
        const data = asRecord(event.data)
        const btwId = asString(data?.btwId)
        if (!data || btwId === undefined) return null
        return { id: btwId, role: type === END ? 'update' : 'start' }
      },
      start(_context, match) {
        const data = asRecord(match.event.data) ?? {}
        const btwId = asString(data.btwId)
        const question = asString(data.question) ?? '（问题内容缺失）'
        const base = {
          btwId,
          question,
          provider: asString(data.provider),
          model: asString(data.model),
          startedAt: asNumber(data.startedAt) ?? match.event.time,
        }
        if (match.event.type === START) return { ...base, status: 'running' }
        const merged = applyEnd({ ...base, status: 'running' }, data)
        return { ...merged, reopenedFrom: asString(data.reopenedFrom) }
      },
      update(context, match) {
        if (match.event.type !== END) return context.state
        const data = asRecord(match.event.data)
        if (!data) return context.state
        return applyEnd(context.state, data)
      },
      buildViewNode(context) {
        if (context.start === undefined) return null
        return {
          key: context.key,
          kind: NODE_KIND,
          id: context.id,
          target: 'chat',
          anchorSeq: context.start.event.seq,
          location: context.start.location,
          visibility: 'hidden',
          data: { ...context.state },
        }
      },
    }

    /** 从 Chat target 快照里取出全部旁支节点，按日志顺序排序。 */
    const EMPTY_ITEMS = Object.freeze([])

    function collectItems(chat) {
      if (!chat || !chat.nodes || typeof chat.nodes.values !== 'function') return EMPTY_ITEMS
      const items = []
      for (const node of chat.nodes.values()) {
        if (!node || node.kind !== NODE_KIND) continue
        const data = asRecord(node.data)
        if (!data || asString(data.btwId) === undefined) continue
        items.push(node)
      }
      items.sort((a, b) => (asNumber(a.anchorSeq) ?? 0) - (asNumber(b.anchorSeq) ?? 0))
      return items.length === 0 ? EMPTY_ITEMS : items
    }

    /** 节点引用不变就不重渲染：主回答流式输出时不跟着每个 token 刷新浮条。 */
    function sameNodeList(a, b) {
      if (a === b) return true
      if (a.length !== b.length) return false
      for (let index = 0; index < a.length; index += 1) {
        if (a[index] !== b[index]) return false
      }
      return true
    }

    function metaText(data) {
      const parts = []
      if (data.provider && data.model) parts.push(`${data.provider}/${data.model}`)
      else if (data.model) parts.push(String(data.model))
      parts.push('不进入模型上下文')
      if (data.status === 'running') parts.push('回答中')
      else if (asNumber(data.elapsedMs) !== undefined) parts.push(`${(data.elapsedMs / 1000).toFixed(1)}s`)
      const usage = asRecord(data.usage)
      if (usage && asNumber(usage.outputTokens) !== undefined) parts.push(`↓${usage.outputTokens} tokens`)
      return parts.join(' · ')
    }

    /** 关闭状态按会话持久化：关掉后刷新不复活，新问答会自动重新打开。 */
    function dismissedStorageKey(sessionId) {
      return `dsh-btw:dismissed:${String(sessionId)}`
    }

    function readDismissed(sessionId) {
      try {
        return window.sessionStorage.getItem(dismissedStorageKey(sessionId)) ?? undefined
      } catch {
        return undefined
      }
    }

    function writeDismissed(sessionId, btwId) {
      try {
        if (btwId === undefined) window.sessionStorage.removeItem(dismissedStorageKey(sessionId))
        else window.sessionStorage.setItem(dismissedStorageKey(sessionId), btwId)
      } catch {
        /* 隐私模式等场景静默降级 */
      }
    }

    /** 输入框上方的浮条：始终跟随当前会话的最新旁支问答。 */
    function BtwDock({ sessionId, useConversation }) {
      const items = typeof useConversation === 'function'
        ? useConversation(snapshot => collectItems(snapshot.views.get('chat')), sameNodeList)
        : EMPTY_ITEMS
      const latest = items.length > 0 ? items[items.length - 1] : undefined
      const latestId = latest && asRecord(latest.data) ? asString(latest.data.btwId) : undefined

      const [closedId, setClosedId] = React.useState(undefined)
      const [offset, setOffset] = React.useState(0)

      React.useEffect(() => {
        setOffset(0)
        const dismissed = readDismissed(sessionId)
        // 新问答（新的 btwId）优先于旧的关闭状态：自动重新打开。
        if (dismissed !== latestId) setClosedId(undefined)
        else setClosedId(latestId)
      }, [latestId, sessionId])

      if (latest === undefined || latestId === undefined || closedId === latestId) return null

      const clampedOffset = Math.min(offset, items.length - 1)
      const index = items.length - 1 - clampedOffset
      const node = items[index]
      const data = asRecord(node.data)
      if (!data) return null

      const status = data.status
      const hasHistory = items.length > 1

      return h('div', { className: 'dsh-btw-dock', 'data-dsh-btw': 'dock', role: 'status' },
        h('div', { className: 'dsh-btw-head' },
          h('span', { className: 'dsh-btw-title' }, '旁支问答'),
          h('span', { className: 'dsh-btw-meta' }, metaText(data)),
          h('div', { className: 'dsh-btw-actions' },
            h('button', {
              type: 'button',
              title: '上一条',
              disabled: !hasHistory,
              onClick: () => setOffset(current => Math.min(items.length - 1, current + 1)),
            }, '‹'),
            h('button', {
              type: 'button',
              title: '下一条',
              disabled: !hasHistory,
              onClick: () => setOffset(current => Math.max(0, current - 1)),
            }, '›'),
            hasHistory ? h('span', { className: 'dsh-btw-meta' }, `${index + 1}/${items.length}`) : null,
            h('button', {
              type: 'button',
              title: '关闭（/btw 可重开）',
              onClick: () => {
                setClosedId(latestId)
                writeDismissed(sessionId, latestId)
              },
            }, '×'),
          ),
        ),
        h('div', { className: 'dsh-btw-q' }, `问：${asString(data.question) ?? '（问题内容缺失）'}`),
        status === 'running'
          ? h('div', { className: 'dsh-btw-a' }, h('span', { className: 'dsh-btw-loading' }, '正在从当前会话记录回答'))
          : status === 'error'
            ? h('div', { className: 'dsh-btw-a is-error' }, `答：${asString(data.error) ?? '未知错误'}`)
            : h('div', { className: 'dsh-btw-a' }, `答：${asString(data.answer) ?? '（空回答）'}`),
      )
    }

    /** 占位渲染器：把 /btw 的 command 行从聊天流里藏掉（CSS 隐藏父行）。 */
    function BtwCommandHider() {
      return h('span', { 'data-dsh-btw-command': 'hidden' })
    }

    /** @param {any} ctx */
    function apply(ctx) {
      const style = ensureStyle()
      ctx.effect(() => () => {
        if (style && style.isConnected) style.remove()
      }, 'dsh-btw: remove style')

      ctx.effect(
        () => ctx.uiConversation.events.register(btwDefinition),
        'dsh-btw: conversation definition',
      )

      ctx.effect(
        () => ctx.slots.inject('conversation.input.dock', () => ctx.slots.register({
          name: 'conversation.input.dock',
          id: 'btw',
          order: 30,
        }, BtwDock)),
        'dsh-btw: dock',
      )

      ctx.effect(
        () => ctx.slots.inject('conversation.chat.commandview', () => ctx.slots.register({
          name: 'conversation.chat.commandview',
          key: 'btw',
        }, BtwCommandHider)),
        'dsh-btw: hide command row',
      )

      console.info('[dsh-btw] client ready')
    }

    exports.apply = apply
    exports.inject = inject
    return module.exports
  },
})
