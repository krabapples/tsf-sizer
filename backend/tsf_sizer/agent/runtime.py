"""The agent loop: plan, call tools, ask the engineer, submit a verified verdict.

One run answers one request (the first start, or a message from the engineer). The conversation
and every tool call are stored, so the next run continues where the last one stopped. A run ends
when the agent asks a question (waiting), submits a verdict (done), answers in plain text
(done), hits the step budget, or the model endpoint fails (error).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from .. import db
from . import config as cfgmod
from . import ledger, playbook, prompts, verifier
from .gateway import GatewayClient, GatewayError
from .redact import redact
from .tools import SPEC_BY_NAME, SPECS, Toolbox, ToolError, summarize, validate

log = logging.getLogger("tsf_sizer.agent")

MAX_TOOL_CHARS = 20000
MAX_VERDICT_ATTEMPTS = 3
MAX_MESSAGE = 2000
START_TEXT = (
    "A new analysis is ready. The engineer already gave the sizing input that is in the "
    "assumptions ledger (source 'form') and the notes below: do not ask for it again. Read the "
    "evidence, tell me briefly what you see, and ask only what is still open."
)
KIND_TEXT = {
    "auth": "The AI gateway refused the credentials. Check the gateway key and its permissions.",
    "blocked": "The AI security policy blocked this request. The run was stopped and nothing "
    "was retried.",
    "rate": "The model endpoint is rate limiting requests. Try again in a moment.",
    "unreachable": "The model endpoint could not be reached.",
    "bad_response": "The model endpoint sent an answer the application could not use.",
    "http": "The model endpoint returned an error.",
}


def add_message(
    conn: sqlite3.Connection,
    analysis_id: int,
    run_id: int | None,
    role: str,
    content: str | None,
    kind: str = "text",
    data: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO agent_message (analysis_id, run_id, role, kind, content, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (analysis_id, run_id, role, kind, content, json.dumps(data, default=str) if data else None),
    )
    conn.commit()
    return cur.lastrowid


def start_run(
    conn: sqlite3.Connection,
    analysis_id: int,
    user_text: str | None,
    notes: str | None = None,
) -> int:
    """Create a run and store what starts it. The caller then submits run_agent()."""
    cfg = cfgmod.from_env()
    run_id = conn.execute(
        "INSERT INTO agent_run (analysis_id, status, mode, model) VALUES (?, 'running', ?, ?)",
        (analysis_id, cfg.mode, cfg.model),
    ).lastrowid
    if user_text is None:
        text = START_TEXT + (f"\n\nThe engineer's notes: {notes}" if notes else "")
        add_message(
            conn, analysis_id, run_id, "user", text, kind="start",
            data={"notes": notes} if notes else None,
        )  # fmt: skip
    else:
        add_message(conn, analysis_id, run_id, "user", user_text[:MAX_MESSAGE])
    conn.commit()
    return run_id


def active_run(conn: sqlite3.Connection, analysis_id: int):
    return conn.execute(
        "SELECT * FROM agent_run WHERE analysis_id=? AND status='running' "
        "AND created_at > datetime('now', '-30 minutes') ORDER BY id DESC LIMIT 1",
        (analysis_id,),
    ).fetchone()


def _tool_text(payload: dict | str) -> str:
    text = payload if isinstance(payload, str) else json.dumps(redact(payload), default=str)
    if len(text) > MAX_TOOL_CHARS:
        text = text[:MAX_TOOL_CHARS] + '..."[truncated]"'
    return text


def _history(conn: sqlite3.Connection, analysis_id: int, run_id: int) -> list[dict]:
    """The stored conversation in the model's format. Tool results of earlier runs are
    shortened to their step line: the ids stay valid, the bulk is not resent."""
    out: list[dict] = []
    for r in conn.execute(
        "SELECT * FROM agent_message WHERE analysis_id=? AND role IN ('user','assistant','tool') "
        "AND kind != 'error' ORDER BY id",
        (analysis_id,),
    ):
        data = json.loads(r["data_json"]) if r["data_json"] else {}
        if r["role"] == "user":
            out.append({"role": "user", "content": r["content"] or ""})
        elif r["role"] == "assistant":
            calls = data.get("tool_calls")
            if calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": r["content"] or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["arguments"]),
                                },
                            }
                            for c in calls
                        ],
                    }
                )
            else:
                prefix = "Question to the engineer: " if r["kind"] == "question" else ""
                out.append({"role": "assistant", "content": prefix + (r["content"] or "")})
        else:
            content = r["content"] or ""
            if r["run_id"] != run_id:
                content = f"[{data.get('result_id', '')}] {data.get('summary', 'earlier result')}"
            out.append(
                {"role": "tool", "tool_call_id": data.get("tool_call_id"), "content": content}
            )
    return out


def _messages(conn, analysis_id, run_id, system: str) -> list[dict]:
    msgs = [{"role": "system", "content": system}, *_history(conn, analysis_id, run_id)]
    for m in msgs:
        if isinstance(m.get("content"), str):
            m["content"] = redact(m["content"]) if m["role"] != "system" else m["content"]
    return msgs


def _finish(conn, run_id: int, status: str, outcome: str, steps: int) -> None:
    conn.execute(
        "UPDATE agent_run SET status=?, outcome=?, steps=?, finished_at=datetime('now') WHERE id=?",
        (status, outcome[:300], steps, run_id),
    )
    conn.commit()


def next_verdict_version(conn: sqlite3.Connection, analysis_id: int) -> int:
    r = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM verdict WHERE analysis_id=?", (analysis_id,)
    ).fetchone()
    return r[0] + 1


def run_agent(
    db_path: str,
    analysis_id: int,
    run_id: int,
    cfg: cfgmod.LLMConfig | None = None,
    client_factory=GatewayClient,
) -> None:
    """Background job. Never raises: the outcome is stored on the run."""
    conn = db.connect(db_path)
    steps = 0
    try:
        cfg = cfg or cfgmod.from_env()
        if not cfg.enabled:
            add_message(
                conn, analysis_id, run_id, "notice",
                "No LLM is configured. Set TSF_SIZER_LLM_MODE and the gateway variables "
                "(see the Settings page).", kind="error",
            )  # fmt: skip
            _finish(conn, run_id, "error", "no LLM configured", 0)
            return
        steps = _run(conn, analysis_id, run_id, cfg, client_factory(cfg))
    except GatewayError as e:
        _record_gateway_error(conn, analysis_id, run_id, e, steps)
    except Exception as e:  # noqa: BLE001 - any failure must reach the engineer
        log.exception("agent run failed")
        add_message(
            conn, analysis_id, run_id, "notice",
            f"The agent stopped because of an internal error: {type(e).__name__}: {e}",
            kind="error",
        )  # fmt: skip
        _finish(conn, run_id, "error", f"{type(e).__name__}: {e}", steps)
    finally:
        conn.close()


def _record_gateway_error(conn, analysis_id, run_id, e: GatewayError, steps: int) -> None:
    text = f"{KIND_TEXT.get(e.kind, 'The model endpoint failed.')} {e}"
    if e.request_id:
        text += f" (request id {e.request_id})"
    text += " The quick result is still available."
    add_message(conn, analysis_id, run_id, "notice", text, kind="error", data={"kind": e.kind})
    _finish(conn, run_id, "error", f"{e.kind}: {e}", steps)


def _run(conn, analysis_id: int, run_id: int, cfg: cfgmod.LLMConfig, client: GatewayClient) -> int:
    box = Toolbox(conn, analysis_id, run_id)
    attempts = 0
    steps = 0
    while steps < cfg.max_steps:
        system = prompts.build(
            playbook.active(conn), ledger.effective(conn, analysis_id), box.current
        )
        reply = client.chat(_messages(conn, analysis_id, run_id, system), SPECS)
        if client.fell_back and not getattr(client, "_said_fallback", False):
            client._said_fallback = True
            add_message(
                conn, analysis_id, run_id, "notice",
                "The model endpoint does not accept native tool calls; switched to JSON tool "
                "calls for this run.", kind="text",
            )  # fmt: skip
        if not reply.tool_calls:
            text = reply.content.strip() or "(no answer)"
            add_message(conn, analysis_id, run_id, "assistant", text)
            _finish(conn, run_id, "done", "answered", steps)
            return steps
        executed = []
        results: list[tuple[dict, str, dict]] = []  # (call, content, data)
        ended: str | None = None
        for tc in reply.tool_calls:
            steps += 1
            data = {"tool": tc.name, "tool_call_id": tc.id}
            if tc.name == "ask_user":
                executed.append(tc)
                try:
                    q = validate(tc.arguments, SPEC_BY_NAME["ask_user"]["parameters"])
                except ToolError as e:
                    results.append((tc, json.dumps({"error": str(e)}), data | {"summary": str(e)}))
                    continue
                results.append(
                    (tc, json.dumps({"asked": True}), data | {"summary": "Asked the engineer"})
                )
                ended = "question"
                question = q
                break
            if tc.name == "submit_verdict":
                executed.append(tc)
                content, vdata, accepted = _verdict(conn, analysis_id, run_id, tc, attempts)
                attempts += 1
                results.append((tc, content, data | vdata))
                if accepted:
                    ended = "verdict"
                continue
            if tc.name == "record_assumption":
                executed.append(tc)
                try:
                    a = validate(tc.arguments, SPEC_BY_NAME["record_assumption"]["parameters"])
                    res = box.record_assumption(a["key"], a["value"], a["reason"])
                except ToolError as e:
                    res = {"id": "", "ok": False, "error": str(e), "args": tc.arguments}
                if res["ok"] and res["payload"].get("change"):
                    _refresh_quick(conn, analysis_id)
            elif tc.name == "record_evidence":
                executed.append(tc)
                try:
                    a = validate(tc.arguments, SPEC_BY_NAME["record_evidence"]["parameters"])
                    res = box.record_evidence(
                        a["field"], a["value"], a.get("kind", "estimate"), a.get("note")
                    )
                except ToolError as e:
                    res = {"id": "", "ok": False, "error": str(e), "args": tc.arguments}
                if res["ok"]:
                    _refresh_quick(conn, analysis_id)
            else:
                executed.append(tc)
                res = box.call(tc.name, tc.arguments)
            body = res.get("payload") if res["ok"] else {"error": res.get("error")}
            results.append(
                (
                    tc,
                    _tool_text(body),
                    data
                    | {
                        "result_id": res["id"],
                        "ok": res["ok"],
                        "summary": summarize(
                            tc.name, res["args"], res.get("payload"), res.get("error")
                        ),
                    },
                )
            )
        # the assistant turn and its tool results, in the order the model expects them
        add_message(
            conn, analysis_id, run_id, "assistant", reply.content.strip() or None,
            data={"tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in executed
            ], "request_id": reply.request_id},
        )  # fmt: skip
        verdict_message = None
        for _tc, content, data in results:
            verdict_message = data.pop("verdict_message", None) or verdict_message
            add_message(conn, analysis_id, run_id, "tool", content, data=data)
        if verdict_message:
            add_message(
                conn, analysis_id, run_id, "assistant", verdict_message["content"],
                kind="verdict", data=verdict_message["data"],
            )  # fmt: skip
        if ended == "question":
            add_message(
                conn, analysis_id, run_id, "assistant", question["question"], kind="question",
                data={"options": question.get("options") or [], "default": question.get("default")},
            )  # fmt: skip
            _finish(conn, run_id, "waiting", "question", steps)
            return steps
        if ended == "verdict":
            _finish(conn, run_id, "done", "verdict", steps)
            return steps
    add_message(
        conn, analysis_id, run_id, "notice",
        f"The agent used its step budget ({cfg.max_steps} tool calls) without reaching a verdict. "
        "Send a message to let it continue, or raise TSF_SIZER_AGENT_MAX_STEPS.", kind="error",
    )  # fmt: skip
    _finish(conn, run_id, "done", "step budget", steps)
    return steps


def _verdict(conn, analysis_id, run_id, tc, attempts: int) -> tuple[str, dict, bool]:
    """Validate and verify a verdict. Returns (tool message, extra data, accepted)."""
    try:
        verdict = validate(tc.arguments, SPEC_BY_NAME["submit_verdict"]["parameters"])
    except ToolError as e:
        return (
            json.dumps({"accepted": False, "problems": [str(e)]}),
            {"summary": f"Verdict rejected: {e}"},
            False,
        )
    problems = verifier.verify(conn, analysis_id, verdict)
    if problems and attempts + 1 < MAX_VERDICT_ATTEMPTS:
        return (
            json.dumps(
                {
                    "accepted": False,
                    "problems": problems,
                    "instruction": "Fix these and call submit_verdict again.",
                }
            ),
            {"summary": f"Verdict rejected by the verifier ({len(problems)} problem(s))"},
            False,
        )
    version = next_verdict_version(conn, analysis_id)
    conn.execute(
        "INSERT INTO verdict (analysis_id, version, run_id, verdict_json, verified, problems_json, "
        "ledger_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            analysis_id,
            version,
            run_id,
            json.dumps(verdict),
            0 if problems else 1,
            json.dumps(problems) if problems else None,
            json.dumps(ledger.values(conn, analysis_id), default=str),
        ),
    )
    conn.commit()
    note = "Verdict saved" if not problems else "Verdict saved with unverified claims"
    return (
        json.dumps({"accepted": True, "version": version, "unverified": problems}),
        {
            "summary": f"{note} (version {version})",
            # shown after the tool results, so the conversation reads in order
            "verdict_message": {
                "content": verdict.get("summary"),
                "data": {"version": version, "verified": not problems, "problems": problems},
            },
        },
        True,
    )


def _refresh_quick(conn: sqlite3.Connection, analysis_id: int) -> None:
    from ..pipeline import refresh_quick_result

    refresh_quick_result(conn, analysis_id)
