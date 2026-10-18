/**
 * /btw — side questions about the current session
 *
 * Ask a question while pi keeps working. A separate model call answers from
 * the session transcript so far, with no tools, and the exchange never enters
 * the conversation history. Answers appear in a dismissible panel:
 *
 *   /btw what was that config file we changed earlier?
 *   /btw        reopen the panel with the last exchange
 *
 * Panel keys: up/down scroll · x clear history · Enter/Esc close
 *
 * Outside the TUI (RPC clients such as Agents Anywhere) there is no panel, so
 * the exchange is persisted as a ``pi.btw`` custom session entry instead. The
 * entry stays out of the model context; the pi-agents-anywhere connector
 * projects it into the platform timeline. Keep BTW_ENTRY_TYPE and the payload
 * keys in sync with ``src/pi_aa/projection.py``.
 */

import type {
	ExtensionAPI,
	ExtensionContext,
	SessionEntry,
	Theme,
} from "@earendil-works/pi-coding-agent";
import { matchesKey, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";

const SIDE_QUESTION_SYSTEM_PROMPT = [
	"Language rule: always reply in Chinese. Do not switch to English or any other language, even when the side question or the transcript is in English, and even when refusing because the transcript lacks the answer.",
	"You answer a quick side question about an ongoing coding session.",
	"You only see the transcript excerpt provided; you cannot read files, run commands, or search.",
	"Answer from the transcript. If it does not contain the answer, say so briefly instead of guessing.",
	"Answer directly and concisely: a few sentences or compact bullets.",
	"Do not emit tool calls; if one appears anyway, note that nothing was executed.",
].join("\n");

const MAX_TRANSCRIPT_MESSAGES = 30;
const MAX_TRANSCRIPT_CHARS = 16_000;
const MAX_TOOL_RESULT_CHARS = 800;
const MAX_HISTORY_ITEMS = 10;
const MAX_VISIBLE_QUESTIONS = 5;
const MAX_ANSWER_LINES = 18;
const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

// Custom session entry type for headless answers; the pi-agents-anywhere
// connector projects this type into the platform timeline.
const BTW_ENTRY_TYPE = "pi.btw";

interface BtwItem {
	question: string;
	state: "loading" | "answer" | "error";
	answer: string;
	error?: string;
}

interface BtwRuntime {
	items: BtwItem[];
	spinnerFrame: number;
	spinnerTimer?: ReturnType<typeof setInterval>;
	panelOpen: boolean;
	requestRender?: () => void;
	closePanel?: () => void;
}

const runtimes = new Map<string, BtwRuntime>();

function sessionKey(ctx: ExtensionContext): string {
	return ctx.sessionManager.getSessionFile() ?? `memory:${ctx.sessionManager.getSessionId()}`;
}

function runtimeFor(ctx: ExtensionContext): BtwRuntime {
	const key = sessionKey(ctx);
	let runtime = runtimes.get(key);
	if (!runtime) {
		runtime = { items: [], spinnerFrame: 0, panelOpen: false };
		runtimes.set(key, runtime);
	}
	return runtime;
}

// --- transcript -----------------------------------------------------------

type ContentBlock = { type?: string; text?: string; name?: string; arguments?: unknown };
type EntryMessage = { role?: string; content?: unknown; toolName?: string };

function textBlocks(content: unknown): string[] {
	if (typeof content === "string") return [content];
	if (!Array.isArray(content)) return [];
	const out: string[] = [];
	for (const block of content as ContentBlock[]) {
		if (block?.type === "text" && typeof block.text === "string") out.push(block.text);
	}
	return out;
}

function toolCallLines(content: unknown): string[] {
	if (!Array.isArray(content)) return [];
	const out: string[] = [];
	for (const block of content as ContentBlock[]) {
		if (block?.type === "toolCall" && typeof block.name === "string") {
			out.push(`Assistant called ${block.name} with ${JSON.stringify(block.arguments ?? {})}`);
		}
	}
	return out;
}

function clip(text: string, max: number): string {
	return text.length <= max ? text : `${text.slice(0, max)} ...[truncated]`;
}

function buildTranscript(entries: SessionEntry[]): string {
	const recent = entries
		.filter((entry): entry is SessionEntry & { type: "message" } => entry.type === "message")
		.slice(-MAX_TRANSCRIPT_MESSAGES);
	const sections: string[] = [];

	for (const entry of recent) {
		const message = entry.message as EntryMessage;
		if (!message?.role) continue;
		const text = textBlocks(message.content).join("\n").trim();
		const lines: string[] = [];
		switch (message.role) {
			case "user":
				if (text) lines.push(`User: ${text}`);
				break;
			case "assistant":
				if (text) lines.push(`Assistant: ${text}`);
				lines.push(...toolCallLines(message.content));
				break;
			case "toolResult":
				if (text) lines.push(`Tool result from ${message.toolName ?? "tool"}: ${clip(text, MAX_TOOL_RESULT_CHARS)}`);
				break;
			case "bashExecution":
				if (text) lines.push(`User bash output: ${clip(text, MAX_TOOL_RESULT_CHARS)}`);
				break;
			case "custom":
				if (text) lines.push(`Note: ${text}`);
				break;
			case "branchSummary":
			case "compactionSummary":
				if (text) lines.push(`Summary: ${text}`);
				break;
		}
		if (lines.length > 0) sections.push(lines.join("\n"));
	}

	const transcript = sections.join("\n\n");
	if (transcript.length <= MAX_TRANSCRIPT_CHARS) return transcript;
	return `...[earlier session context omitted]\n\n${transcript.slice(-MAX_TRANSCRIPT_CHARS)}`;
}

// --- side question --------------------------------------------------------

async function answerSideQuestion(
	question: string,
	ctx: ExtensionContext,
	item: BtwItem,
	runtime: BtwRuntime | undefined,
): Promise<void> {
	const model = ctx.model;
	if (!model) throw new Error("No model selected.");

	const transcript = buildTranscript(ctx.sessionManager.getBranch());
	const prompt = [
		"Session transcript so far:",
		"<transcript>",
		transcript || "(The session has no messages yet.)",
		"</transcript>",
		"",
		`Side question: ${question}`,
	].join("\n");

	const stream = ctx.modelRegistry.streamSimple(model, {
		systemPrompt: SIDE_QUESTION_SYSTEM_PROMPT,
		messages: [{ role: "user", content: [{ type: "text", text: prompt }], timestamp: Date.now() }],
		sessionId: ctx.sessionManager.getSessionId(),
	});

	let finalText = "";
	for await (const event of stream) {
		if (event.type === "text_delta") {
			item.answer += event.delta;
			runtime?.requestRender?.();
		} else if (event.type === "done") {
			finalText = textBlocks(event.message.content).join("\n").trim();
		} else if (event.type === "error") {
			throw new Error(event.error.errorMessage ?? "The side question failed.");
		}
	}

	if (!item.answer.trim() && finalText) item.answer = finalText;
	if (!item.answer.trim()) item.answer = "(No response received.)";
}

// --- panel ----------------------------------------------------------------

function syncSpinner(runtime: BtwRuntime): void {
	const loading = runtime.items.some((item) => item.state === "loading");
	if (loading && runtime.panelOpen && !runtime.spinnerTimer) {
		runtime.spinnerTimer = setInterval(() => {
			runtime.spinnerFrame = (runtime.spinnerFrame + 1) % SPINNER_FRAMES.length;
			runtime.requestRender?.();
		}, 120);
	} else if ((!loading || !runtime.panelOpen) && runtime.spinnerTimer) {
		clearInterval(runtime.spinnerTimer);
		runtime.spinnerTimer = undefined;
	}
}

class BtwPanel {
	private scrollOffset = 0;

	constructor(
		private readonly theme: Theme,
		private readonly runtime: BtwRuntime,
		private readonly requestRender: () => void,
		private readonly close: () => void,
	) {}

	handleInput(data: string): void {
		if (matchesKey(data, "escape") || matchesKey(data, "enter") || matchesKey(data, "return")) {
			this.close();
			return;
		}
		if (data === "x" || data === "X") {
			this.runtime.items = this.runtime.items.filter((item) => item.state === "loading");
			this.scrollOffset = 0;
			if (this.runtime.items.length === 0) {
				this.close();
				return;
			}
			this.requestRender();
			return;
		}
		if (matchesKey(data, "up")) {
			this.scrollOffset = Math.max(0, this.scrollOffset - 1);
			this.requestRender();
			return;
		}
		if (matchesKey(data, "down")) {
			this.scrollOffset += 1;
			this.requestRender();
		}
	}

	render(width: number): string[] {
		const theme = this.theme;
		const innerWidth = Math.max(12, width);
		const lines: string[] = [theme.fg("accent", "/btw"), ""];

		if (this.runtime.items.length === 0) {
			lines.push(theme.fg("dim", "  No side questions in this session."));
			lines.push("");
			lines.push(theme.fg("dim", "Enter/Esc to close"));
			return lines.map((line) => truncateToWidth(line, width, "...", true));
		}

		const latest = this.runtime.items[this.runtime.items.length - 1]!;
		const earlier = this.runtime.items.slice(-(MAX_VISIBLE_QUESTIONS + 1), -1);
		for (const item of earlier) {
			lines.push(...wrapTextWithAnsi(theme.fg("dim", `  ${item.question}`), innerWidth));
		}
		if (earlier.length > 0) lines.push("");

		lines.push(...wrapTextWithAnsi(theme.fg("accent", `  ❯ ${latest.question}`), innerWidth));
		lines.push("");

		const bodySource = latest.state === "error" ? theme.fg("error", latest.error ?? "Unknown error") : latest.answer;
		const body: string[] = [];
		for (const line of bodySource.split("\n")) {
			body.push(...wrapTextWithAnsi(line.length > 0 ? `    ${line}` : " ", innerWidth));
		}
		if (latest.state === "loading") {
			const frame = SPINNER_FRAMES[this.runtime.spinnerFrame % SPINNER_FRAMES.length];
			body.push("", theme.fg("warning", `    ${frame} Answering...`));
		}

		const maxOffset = Math.max(0, body.length - MAX_ANSWER_LINES);
		this.scrollOffset = Math.min(this.scrollOffset, maxOffset);
		lines.push(...body.slice(this.scrollOffset, this.scrollOffset + MAX_ANSWER_LINES));
		if (body.length > MAX_ANSWER_LINES) {
			lines.push(theme.fg("dim", `    ... ${body.length - this.scrollOffset - MAX_ANSWER_LINES} more line(s)`));
		}

		lines.push("");
		lines.push(theme.fg("dim", "↑/↓ scroll · x clear · Enter/Esc close"));
		return lines.map((line) => truncateToWidth(line, width, "...", true));
	}

	invalidate(): void {}
}

function openPanel(ctx: ExtensionContext, runtime: BtwRuntime): void {
	if (runtime.panelOpen) {
		runtime.requestRender?.();
		syncSpinner(runtime);
		return;
	}
	runtime.panelOpen = true;
	void ctx.ui
		.custom<void>((tui, theme, _keybindings, done) => {
			runtime.requestRender = () => tui.requestRender();
			runtime.closePanel = () => done();
			syncSpinner(runtime);
			return new BtwPanel(theme, runtime, () => tui.requestRender(), () => done());
		})
		.finally(() => {
			runtime.panelOpen = false;
			runtime.requestRender = undefined;
			runtime.closePanel = undefined;
			syncSpinner(runtime);
		});
}

// --- extension ------------------------------------------------------------

async function askBtw(question: string, ctx: ExtensionContext, runtime: BtwRuntime): Promise<void> {
	const item: BtwItem = { question, state: "loading", answer: "" };
	runtime.items.push(item);
	if (runtime.items.length > MAX_HISTORY_ITEMS) {
		runtime.items.splice(0, runtime.items.length - MAX_HISTORY_ITEMS);
	}
	syncSpinner(runtime);
	runtime.requestRender?.();

	try {
		await answerSideQuestion(question, ctx, item, runtime);
		item.state = "answer";
	} catch (error) {
		item.state = "error";
		item.error = error instanceof Error ? error.message : String(error);
	} finally {
		syncSpinner(runtime);
		runtime.requestRender?.();
	}
}

/**
 * Headless /btw: no panel to show, so persist the exchange instead. The
 * custom entry never enters the LLM context and the connector projects it
 * into the platform timeline; a failed side question is recorded as an error.
 * Called fire-and-forget like the TUI path: pi holds the prompt response
 * until the handler returns, and the command must not stay pending for the
 * whole model call. Every failure is caught so the detached promise cannot
 * become an unhandled rejection.
 */
async function askBtwHeadless(
	pi: ExtensionAPI,
	question: string,
	ctx: ExtensionContext,
): Promise<void> {
	try {
		if (!question) {
			ctx.ui.notify("Usage: /btw <question>", "warning");
			return;
		}
		const item: BtwItem = { question, state: "loading", answer: "" };
		try {
			await answerSideQuestion(question, ctx, item, undefined);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			pi.appendEntry(BTW_ENTRY_TYPE, { question, error: message });
			return;
		}
		pi.appendEntry(BTW_ENTRY_TYPE, { question, answer: item.answer });
	} catch (error) {
		// Last resort: the exchange could not be recorded after the handler
		// returned; surface it instead of leaving a floating rejection.
		ctx.ui.notify(`/btw: ${error instanceof Error ? error.message : String(error)}`, "error");
	}
}

export default function btwExtension(pi: ExtensionAPI) {
	pi.registerCommand("btw", {
		description: "Ask a side question about this session (answered without interrupting)",
		handler: async (args, ctx) => {
			const question = args.trim();
			if (ctx.mode !== "tui") {
				void askBtwHeadless(pi, question, ctx);
				return;
			}
			const runtime = runtimeFor(ctx);
			if (!question) {
				if (runtime.items.length === 0) {
					ctx.ui.notify("Usage: /btw <question>", "warning");
					return;
				}
				openPanel(ctx, runtime);
				return;
			}
			openPanel(ctx, runtime);
			void askBtw(question, ctx, runtime);
		},
	});

	pi.on("session_shutdown", () => {
		for (const runtime of runtimes.values()) {
			runtime.closePanel?.();
			if (runtime.spinnerTimer) clearInterval(runtime.spinnerTimer);
		}
		runtimes.clear();
	});
}
