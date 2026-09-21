"""M3.0: Agent loop — LLM drives the read-only Tool API.

The loop (one ``run_task`` call = one user task):

    user message -> LLM (tools=...) -> assistant tool_calls?
        yes -> execute tools, append results, back to LLM
        no  -> final text answer, stop

Guardrails (M3 spec, fixed):
- ``max_steps``: hard cap on total LLM round trips per task.
- ``max_tool_calls``: hard cap on total tool executions per task.
- ``max_seconds``: wall-clock budget for the whole task.
- ``max_result_chars``: each tool result sent back to the model is
  truncated to this size (a big search result can't blow up the context).
- Repeated-identical call detection: the same tool with the same arguments
  twice in a row is short-circuited (the model is echoing, not progressing).

The LLM backend is any OpenAI-compatible ``/v1/chat/completions`` endpoint
(this machine: llama-server + Qwen3.8-27B). No SDK required — stdlib urllib.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from tools import call_tool, tool_definitions

DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_TOOL_CALLS = 20
DEFAULT_MAX_SECONDS = 180.0
DEFAULT_MAX_RESULT_CHARS = 6000
DEFAULT_MAX_TOKENS = 1024

# System prompt: role + the hard read-only boundary (M3.0 has no mutating
# tools, but the model must say so when asked to delete/move files).
SYSTEM_PROMPT = """\
你是 photo-agent 的照片库助手。你能通过工具查询本地照片库（已扫描的元数据、\
质量分、重复分组），然后综合这些信息回答用户。

规则：
1. 涉及照片的事实问题，必须先调用工具获取数据，不要凭空编造照片路径、\
质量分或数量。
2. rel_path 只能使用工具返回的精确值，不要自己拼接或猜测。
3. 你当前只有只读工具（搜索/查看/分析/报告）。如果用户要求删除、移动、\
重命名照片，你不能执行——要基于工具结果给出一个清晰的「操作计划」\
（列出涉及的照片、建议动作和理由），并明确告诉用户：当前版本不会改动\
任何文件，需要确认后才能执行。
4. 缺少完成任务所必需的信息时（如没说哪一年、没说保留哪张），不要反问，\
  基于现有信息给出尽可能完整的回答，并在末尾用一句话指出你还缺哪些信息。\
  （当前版本没有澄清工具，无法向用户提问。）
5. 回答用中文，简洁，引用照片时用工具返回的 rel_path。
"""


class AgentError(RuntimeError):
    """Agent loop failed: backend unreachable, bad response, or limit hit."""


class AgentResult:
    """Outcome of one task: final text + audit trail of every step."""

    def __init__(self) -> None:
        self.final_text: str = ""
        self.tool_calls: list[dict] = []  # {name, args, result_chars}
        self.llm_rounds: int = 0
        self.seconds: float = 0.0
        self.notes: list[str] = []


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise AgentError(f"LLM 后端 HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AgentError(f"LLM 后端不可达 {url}: {exc}") from exc


def _truncate(result: object, limit: int) -> str:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"…[截断：完整结果 {len(text)} 字符，已截断到 {limit}]"
    )


def run_task(
    conn,
    backend: dict,
    user_message: str,
    history: list[dict] | None = None,
    on_event=None,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> AgentResult:
    """Run one user task against the tool API.

    ``history`` is the session message list from previous tasks (Q18:
    "这些照片…" refers to the previous turn). It is NOT mutated here;
    callers pass ``history + new messages`` back in for the next task.
    """
    base_url = (backend.get("base_url") or "").rstrip("/")
    model = backend.get("model")
    if not base_url or not model:
        raise AgentError("LLM 后端未配置：config.yaml 缺少 llama.base_url / llama.model")

    extra = backend.get("extra") or {}
    max_tokens = int(backend.get("max_tokens", DEFAULT_MAX_TOKENS))
    http_timeout = float(backend.get("http_timeout", 120))

    # Session history holds prior user/assistant turns. Tool call/results
    # messages are task-local: a new task re-derives what it needs via tools
    # (cheaper and less error-prone than replaying stale results).
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    result = AgentResult()
    started = time.monotonic()
    tool_call_budget = max_tool_calls
    seen_call_keys: list[str] = []

    for _step in range(max_steps):
        if time.monotonic() - started > max_seconds:
            result.notes.append(f"超时中止（>{max_seconds:.0f}s）")
            result.final_text = result.final_text or "任务超时中止，未完成。"
            break

        payload = {
            "model": model,
            "messages": messages,
            "tools": tool_definitions(),
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            **extra,
        }
        resp = _post_json(f"{base_url}/v1/chat/completions", payload, http_timeout)
        result.llm_rounds += 1

        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            result.final_text = msg.get("content") or ""
            break

        unprocessed: list[dict] = []
        for idx, tc in enumerate(tool_calls):
            if time.monotonic() - started > max_seconds:
                result.notes.append("超时中止（工具执行中）")
                result.final_text = result.final_text or "任务超时中止，未完成。"
                unprocessed = tool_calls[idx:]
                break
            if tool_call_budget <= 0:
                result.notes.append(
                    f"达到工具调用上限 {max_tool_calls} 次，请缩小范围后重新提问"
                )
                unprocessed = tool_calls[idx:]
                break
            fn = (tc.get("function") or {})
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            call_key = json.dumps([name, args], ensure_ascii=False, sort_keys=True)
            duplicate = bool(seen_call_keys) and call_key == seen_call_keys[-1]
            seen_call_keys.append(call_key)

            if duplicate:
                tool_result = {
                    "note": "与上一次完全相同的调用，结果与上次相同，请基于已有信息继续。"
                }
                result.notes.append(f"重复调用被短路：{name}")
            else:
                tool_result = call_tool(conn, name, args, extra=extra)

            result.tool_calls.append(
                {
                    "name": name,
                    "args": args,
                    "result_chars": len(_truncate(tool_result, 10**9)),
                }
            )
            if on_event:
                on_event(
                    {
                        "type": "tool_call",
                        "name": name,
                        "args": args,
                        "result": tool_result,
                        "duplicate": duplicate,
                    }
                )
            tool_call_budget -= 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": _truncate(tool_result, max_result_chars),
                }
            )

        else:
            continue  # all tool calls handled -> next LLM round

        # Inner loop broke early (time/budget) with unprocessed tool calls.
        # The assistant message already promised tool_calls, so every one of
        # them needs a tool response before we can ask for the final answer.
        for tc in unprocessed:
            fn = (tc.get("function") or {})
            name = fn.get("name") or "unknown"
            content = (
                '{"note": "'
                f"未执行：{name} 未能在本轮限额内执行，请基于已有的工具结果给出最终回答。"
                '"}'
            )
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id"), "content": content}
            )

        # Force one final round WITHOUT tools so the model must produce a
        # text answer from whatever it has gathered so far.
        try:
            resp = _post_json(
                f"{base_url}/v1/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "tool_choice": "none",
                    **extra,
                },
                http_timeout,
            )
            result.llm_rounds += 1
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            messages.append(msg)
            result.final_text = msg.get("content") or result.final_text
        except AgentError as exc:
            result.notes.append(f"收尾回答失败：{exc}")
        break

    else:
        result.notes.append(f"达到 LLM 轮次上限 {max_steps}，任务可能未完成")
        if not result.final_text:
            result.final_text = "达到步骤上限，任务未完成；请缩小范围后重试。"

    result.seconds = time.monotonic() - started
    return result


def summarize(result: AgentResult) -> str:
    """One-line audit trail for the CLI footer."""
    calls = ", ".join(f"{c['name']}" for c in result.tool_calls) or "无"
    return (
        f"[agent] {result.llm_rounds} 轮 LLM，{len(result.tool_calls)} 次工具调用 "
        f"({calls})，{result.seconds:.1f}s"
    )
