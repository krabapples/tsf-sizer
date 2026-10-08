"""The assistant that refines a sizing result through conversation.

Two LLM calls per turn, with the deterministic engine in between:

  1. interpret: the engineer's message (+ the current state) -> a small JSON list of actions.
     The model never picks firewalls and never does arithmetic.
  2. the actions are validated and applied in code (sizing/adjust.py) and the engine
     recomputes the recommendation.
  3. reply: the model explains the outcome in a few sentences, from the engine's figures only
     (numbers and model names are fact-checked like the written summary).

If the model is unreachable nothing changes; if only the reply fails the change is still
applied and described by a fixed template. Only aggregated facts and the engineer's own
messages leave the app, as for the written summary.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .. import db
from ..pipeline import resize
from ..portfolio import catalog
from ..portfolio.mapping import TSF_METRIC_MAP
from ..sizing.adjust import PORT_CLASSES, VARIANT_TAGS, apply_actions, describe
from ..sizing.engine import SizingParams
from . import settings as llm_settings
from .providers import LLMConfig, LLMError, chat
from .writeup import build_facts, fact_check
from .writeup import write as write_summary

HISTORY_TURNS = 6
MAX_MESSAGE = 1000

INTERPRET_SYSTEM = f"""You are the interpreter inside a firewall sizing tool. A presales engineer
refines a replacement recommendation by chatting. You do NOT choose firewall models and you
never invent numbers: you translate the engineer's message into actions for the sizing engine,
which then recomputes the recommendation.

Answer with ONE JSON object and nothing else:
{{"actions": [ ... ], "question": null}}
"question" is a short question for the engineer when you need more information, else null.

Actions (use only these):
  {{"action": "set", "name": N, "value": V}}
      N is one of: growth_pct (0-200, percent per year), years (1-10), target_util_pct
      (10-100), peak_throughput_mbps, peak_cps, peak_sessions (a number, or null to clear),
      max_size_factor (above 1, or 0 for no limit), need_poe (true/false),
      include_superseded (true/false: previous generations such as the PA-400),
      port_rule ("all" = same port layout as today, "used" = only the ports in use)
  {{"action": "ports", "drop": [...], "keep": [...], "add": {{"CLASS": count}}, "reset": false}}
      "drop": port types no longer needed; "optics" means every SFP/SFP+/SFP28/QSFP port.
      "keep": cancels an earlier drop. "add": ports needed on top of today's layout.
      CLASS is one of: {", ".join(PORT_CLASSES)}
  {{"action": "ignore", "metric": KEY}}   a limit that may fall short without excluding a model
  {{"action": "enforce", "metric": KEY}}  undoes "ignore"
  {{"action": "exclude", "models": [...]}}  model names, families (e.g. "PA-3400") or
      "variant:5G" / "variant:rugged" / "variant:PoE"
  {{"action": "include", "models": [...]}}  undoes "exclude"
  {{"action": "reset"}}                     clears all port / ignore / exclude adjustments
  {{"action": "explain", "model": NAME}}    the engineer asks why a model was or was not chosen

Rules:
- "does not need the optics / fibre any more" -> ports drop ["optics"].
  "needs optics / fibre / SFP now": ports add. If the number of ports or the speed is not
  given, ask a question instead ("How many ports, and which speed: 1G SFP, 10G SFP+ ...?").
  Typical fibre uplinks are 10G_SFP+.
- "needs PoE" -> set need_poe true. "no 5G models" -> exclude ["variant:5G"].
- Several things in one message -> several actions.
- A question about a model ("why not the PA-560?") -> explain. Use no other action for it.
- If the message maps to nothing you can do, return no actions and ask what to change.
- KEY and model names must come from the lists in the state. Never output anything but JSON.

Examples:
"The customer no longer needs the optics"
  {{"actions": [{{"action": "ports", "drop": ["optics"]}}], "question": null}}
"He now needs two 10G fibre ports and expects 40% growth"
  {{"actions": [{{"action": "ports", "add": {{"10G_SFP+": 2}}}},
    {{"action": "set", "name": "growth_pct", "value": 40}}], "question": null}}
"Now he needs optics"
  {{"actions": [], "question": "How many optical ports, and which speed (1G SFP, 10G SFP+)?"}}
"Ignore the aggregate interface limit, they do not use LACP"
  {{"actions": [{{"action": "ignore", "metric": "config.aggregate_interfaces"}}], "question": null}}
"""

REPLY_SYSTEM = """You are a senior presales engineer helping a colleague refine a firewall
replacement. You get JSON with what the colleague asked, what the sizing engine changed, and
the engine's new result. Reply in at most 120 words of plain text (no headings, no tables):
what changed, which model is recommended now and what it was before, and any entries of
not_blocking_issues of the recommended model. If asked_about is present, explain it from its
reasons. If refused is not empty, say what could not be done and why.
If smaller_models_blocked_by lists a model blocked by one or two reasons, add one sentence
offering to relax that reason (for example "Ignore the routing table limit?"); the engineer
decides, never relax anything yourself.
Use ONLY numbers and model names that appear in the JSON. Never invent specifications."""


# --------------------------------------------------------------------------- state for the LLM


def metric_catalog() -> dict[str, str]:
    """Metric key -> readable name, for every limit that may be ignored."""
    return {metric: name for metric, _cat, name, _cmp in TSF_METRIC_MAP}


def model_catalog(conn: sqlite3.Connection) -> dict[str, str]:
    """UPPERCASE model or family name -> canonical name."""
    out: dict[str, str] = {}
    for m in catalog.list_models(conn):
        out[m["name"].upper()] = m["name"]
        if m["family"]:
            out.setdefault(m["family"].upper(), m["family"])
    return out


def _models_of(sizing: dict) -> dict[str, dict]:
    shown = [sizing.get("recommended"), *sizing.get("alternatives", [])]
    shown += sizing.get("rejected", []) + sizing.get("too_large", [])
    return {c["model"]: c for c in shown if c}


def state_facts(result: dict, params: SizingParams, metrics: dict[str, str]) -> dict:
    """What the interpreter gets to see: aggregated figures only."""
    facts = build_facts(result)
    sizing = result.get("sizing") or {}
    return {
        "current_model": facts.get("current_model"),
        "ports_needed_now": (sizing.get("ports_needed") or {}).get("baseline"),
        "recommended": (facts.get("recommended") or {}).get("model"),
        "alternatives": [a["model"] for a in facts.get("alternatives", [])],
        "assumptions": facts.get("assumptions"),
        "adjustments_in_effect": describe(params, metrics),
        "requirement_keys": {k: v for k, v in metrics.items()},
        "variants": sorted(VARIANT_TAGS),
    }


def extract_json(text: str) -> dict | None:
    """The first JSON object in a model answer (models like to wrap it in prose or fences)."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                try:
                    out = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break
                return out if isinstance(out, dict) else None
        start = text.find("{", start + 1)
    return None


def interpret(
    cfg: LLMConfig, history: list[dict], user_text: str, state: dict
) -> tuple[list[dict], str | None]:
    """Engineer message -> (actions, clarifying question). Raises LLMError."""
    convo = "\n".join(
        f"{'Engineer' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in history[-HISTORY_TURNS:]
        if m.get("content")
    )
    prompt = (
        "Current state (JSON):\n"
        + json.dumps(state, ensure_ascii=False)
        + ("\n\nConversation so far:\n" + convo if convo else "")
        + f"\n\nEngineer says: {user_text}\n\nAnswer with the JSON object only."
    )
    last = ""
    for attempt in range(2):
        extra = "" if attempt == 0 else "\n\nYour previous answer was not valid JSON. " + last
        reply = chat(cfg, INTERPRET_SYSTEM, prompt + extra, json_mode=True)
        data = extract_json(reply.text)
        if data is not None and isinstance(data.get("actions", []), list):
            q = data.get("question")
            return data.get("actions", []), (str(q).strip() or None) if q else None
        last = 'Reply with one JSON object like {"actions": [...], "question": null}.'
    raise LLMError("The model did not return usable JSON actions. Try rephrasing.")


# --------------------------------------------------------------------------- one turn


def closest_blocked(sizing: dict, limit: int = 4) -> list[dict]:
    """Models smaller than the recommendation that miss only one or two rules: the first
    things the engineer may want to relax."""
    rec = sizing.get("recommended") or {}
    top = rec.get("sort_key")
    out = []
    for c in sorted(sizing.get("rejected", []), key=lambda c: -(c.get("sort_key") or 0)):
        fails = c.get("failures", [])
        if top is None or c.get("sort_key", 0) > top or not 1 <= len(fails) <= 2:
            continue
        out.append({"model": c["model"], "blocked_by": fails})
        if len(out) == limit:
            break
    return out


def _verdict(name: str, sizing: dict, params: SizingParams) -> dict:
    rec = (sizing.get("recommended") or {}).get("model")
    alts = [a["model"] for a in sizing.get("alternatives", [])]
    cand = _models_of(sizing).get(name)
    if cand is None:
        why = (
            "left out at the engineer's request"
            if name.upper() in params.exclude_models
            else (
                "not a candidate: end of sale, a previous generation that is switched off, or not "
                "in the capacity workbook"
            )
        )
        return {"verdict": "not considered", "reasons": [why]}
    if name == rec:
        v = "recommended"
    elif name in alts:
        v = "alternative"
    elif cand.get("too_large"):
        v = "too large"
    else:
        v = "rejected"
    reasons = [cand["too_large"]] if cand.get("too_large") else cand.get("failures", [])[:6]
    return {"verdict": v, "reasons": reasons, "not_blocking_issues": cand.get("notices", [])}


def _fallback_reply(applied, before: str | None, now: str | None, question: str | None) -> str:
    parts = []
    if applied.changes:
        parts.append("Applied: " + "; ".join(applied.changes) + ".")
    if now and now != before:
        parts.append(f"The recommendation is now {now}" + (f" (was {before})." if before else "."))
    elif now:
        parts.append(f"The recommendation stays {now}.")
    elif applied.changed:
        parts.append("No model meets every rule with these adjustments.")
    if applied.errors:
        parts.append("Could not apply: " + "; ".join(applied.errors) + ".")
    if question:
        parts.append(question)
    return " ".join(parts) or "Nothing to change."


def run_turn(db_path: str, analysis_id: int, user_id: int, assistant_id: int) -> None:
    """Background job for one chat turn. Never raises: the outcome goes on the assistant row."""
    conn = db.connect(db_path)
    try:
        _turn(conn, analysis_id, user_id, assistant_id)
    except Exception as e:  # noqa: BLE001 - the engineer must see what went wrong
        text = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
        _finish(conn, assistant_id, "error", text)
    finally:
        conn.close()


def _finish(conn, assistant_id: int, status: str, content: str, **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    with conn:
        conn.execute(
            f"UPDATE analysis_message SET status=?, content=?{', ' + sets if sets else ''} "
            "WHERE id=?",
            [status, content, *cols.values(), assistant_id],
        )


def _turn(conn: sqlite3.Connection, analysis_id: int, user_id: int, assistant_id: int) -> None:
    row = conn.execute("SELECT * FROM analysis WHERE id=?", (analysis_id,)).fetchone()
    if row is None or not row["result_json"]:
        raise LLMError("This analysis has no result to refine.")
    result = json.loads(row["result_json"])
    if not result.get("sizing"):
        raise LLMError("There is no sizing yet: import and activate the capacity workbook first.")
    cfg = llm_settings.load(conn)
    if not cfg.enabled:
        raise LLMError("No LLM is configured: set one up on the AI settings page.")
    params = SizingParams(**json.loads(row["params_json"]))

    user_text = conn.execute(
        "SELECT content FROM analysis_message WHERE id=?", (user_id,)
    ).fetchone()["content"]
    history = [
        dict(m)
        for m in conn.execute(
            "SELECT role, content FROM analysis_message WHERE analysis_id=? AND id<? "
            "AND status='done' ORDER BY id",
            (analysis_id, user_id),
        )
    ]

    metrics = metric_catalog()
    models = model_catalog(conn)
    before = ((result["sizing"] or {}).get("recommended") or {}).get("model")

    actions, question = interpret(cfg, history, user_text, state_facts(result, params, metrics))
    applied = apply_actions(params, actions, metrics, models)

    new_result = result
    if applied.changed:
        new_result = resize(conn, result, applied.params)
    sizing = new_result["sizing"] or {}
    now = (sizing.get("recommended") or {}).get("model")
    recommended = sizing.get("recommended") or {}

    asked = {m: _verdict(m, sizing, applied.params) for m in applied.explain}
    facts = {
        "engineer_asked": user_text,
        "applied_changes": applied.changes,
        "refused": applied.errors,
        "recommended_before": before,
        "recommended_now": now,
        "alternatives_now": [a["model"] for a in sizing.get("alternatives", [])],
        "not_blocking_issues": recommended.get("notices", []),
        "adjustments_in_effect": describe(applied.params, metrics),
        "asked_about": asked or None,
        "smaller_models_blocked_by": closest_blocked(sizing),
        "result": build_facts(new_result),
    }
    unverified = {"unverified_numbers": [], "unverified_models": []}
    if question and not applied.changes and not applied.explain and not applied.errors:
        text = question  # nothing to compute: just ask
    else:
        try:
            text = chat(cfg, REPLY_SYSTEM, json.dumps(facts, ensure_ascii=False, indent=1)).text
            unverified = fact_check(text, facts)
            if question:
                text += "\n\n" + question
        except LLMError:
            text = _fallback_reply(applied, before, now, question)

    changes = {
        "applied": applied.changes,
        "refused": applied.errors,
        "recommended_before": before,
        "recommended_now": now,
        "adjustments": describe(applied.params, metrics),
        "unverified": unverified,
    }
    if applied.changed:
        if "writeup" in result:  # the written summary described the old numbers
            new_result["writeup"] = write_summary(cfg, new_result)
        with conn:
            conn.execute(
                "UPDATE analysis SET params_json=?, result_json=?, finished_at=datetime('now') "
                "WHERE id=?",
                (
                    json.dumps(asdict(applied.params)),
                    json.dumps(new_result, default=str),
                    analysis_id,
                ),
            )
    _finish(
        conn,
        assistant_id,
        "done",
        text,
        changes_json=json.dumps(changes),
        params_before_json=json.dumps(asdict(params)) if applied.changed else None,
    )
