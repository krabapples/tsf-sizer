"""Evaluate the agent against golden cases, with the model behind the configured gateway.

    python -m tsf_sizer.agent.evals --workbook Features_Capacities.xlsx --cases evals/

A case is a JSON file with the aggregated result of an analysis (what the app stores, never a
TSF), optional ledger values, the answers to give when the agent asks a question, and what a
good run looks like:

    {"name": "...", "result": {...}, "params": {"growth_pct_per_year": 20},
     "answers": ["A smaller box"],
     "expect": {"models_any_of": ["PA-1510-POE"], "keywords_any": ["CPU"], "max_steps": 30}}

Each case is scored on: a verdict was reached; the recommended model is an acceptable one; every
figure verified; the expected anomalies were mentioned; no identifier left in any request; the
step budget kept. Cases built from customer data belong in a gitignored directory
(evals/private/); the repository carries one synthetic case.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..portfolio.importer import import_workbook
from ..sizing.engine import SizingParams
from . import config as cfgmod
from . import ledger, runtime
from .gateway import GatewayClient
from .redact import redact_text

MAX_TURNS = 6
LEAK = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|[\w.+-]+@[\w-]+\.[\w.]+")


@dataclass
class CaseResult:
    name: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


class Recorder:
    """A client factory that remembers every request body for the privacy check."""

    def __init__(self, inner=GatewayClient):
        self.inner = inner
        self.bodies: list[str] = []

    def __call__(self, cfg):
        client = self.inner(cfg)
        original = client._post

        def post(body):
            self.bodies.append(json.dumps(body))
            return original(body)

        client._post = post
        return client


def run_case(
    case: dict, workbook: Path, cfg: cfgmod.LLMConfig | None = None, factory=GatewayClient
) -> CaseResult:
    res = CaseResult(case["name"])
    expect = case.get("expect", {})
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "eval.db")
        conn = db.connect(path)
        import_workbook(conn, workbook, activate=True)
        params = SizingParams(**case.get("params", {}))
        with conn:
            conn.execute(
                "INSERT INTO analysis (id, filename, status, params_json, result_json) "
                "VALUES (1, 'eval', 'done', ?, ?)",
                (json.dumps(params.__dict__), json.dumps(case["result"])),
            )
            ledger.seed(conn, 1, params)
        conn.close()
        cfg = cfg or cfgmod.from_env()
        recorder = Recorder(factory)
        answers = list(case.get("answers", []))
        text: str | None = None
        for _ in range(MAX_TURNS):
            c = db.connect(path)
            run_id = runtime.start_run(c, 1, text)
            c.close()
            runtime.run_agent(path, 1, run_id, cfg=cfg, client_factory=recorder)
            c = db.connect(path)
            run = c.execute("SELECT * FROM agent_run WHERE id=?", (run_id,)).fetchone()
            verdict = c.execute("SELECT * FROM verdict ORDER BY version DESC LIMIT 1").fetchone()
            c.close()
            if run["status"] == "error":
                res.notes.append(f"run failed: {run['outcome']}")
                break
            if verdict is not None or run["status"] != "waiting":
                break
            c = db.connect(path)
            q = c.execute(
                "SELECT content, data_json FROM agent_message WHERE kind='question' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            c.close()
            data = json.loads(q["data_json"] or "{}")
            text = (
                answers.pop(0)
                if answers
                else data.get("default") or (data.get("options") or ["Yes"])[0]
            )
        c = db.connect(path)
        verdict = c.execute("SELECT * FROM verdict ORDER BY version DESC LIMIT 1").fetchone()
        steps = c.execute("SELECT COALESCE(SUM(steps), 0) FROM agent_run").fetchone()[0]
        c.close()
    res.checks["verdict reached"] = verdict is not None
    if verdict is not None:
        v = json.loads(verdict["verdict_json"])
        rec = (v.get("recommendation") or {}).get("model")
        if "models_any_of" in expect:
            res.checks["acceptable model"] = rec in expect["models_any_of"]
            res.notes.append(f"recommended: {rec}")
        res.checks["every figure verified"] = bool(verdict["verified"])
        if not verdict["verified"]:
            res.notes.append("unverified: " + (verdict["problems_json"] or ""))
        if "keywords_any" in expect:
            blob = json.dumps(v).lower()
            res.checks["anomalies mentioned"] = any(
                k.lower() in blob for k in expect["keywords_any"]
            )
    res.checks["step budget"] = steps <= expect.get("max_steps", cfg.max_steps)
    leaks = [m for b in recorder.bodies for m in LEAK.findall(b) if redact_text(m) != m]
    res.checks["nothing identifying sent"] = not leaks
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the agent against golden cases")
    ap.add_argument("--workbook", required=True, type=Path, help="capacity workbook (.xlsx)")
    ap.add_argument("--cases", required=True, type=Path, help="directory of case .json files")
    args = ap.parse_args(argv)
    cfg = cfgmod.from_env()
    if not cfg.enabled:
        print(
            "No LLM configured: set TSF_SIZER_LLM_MODE and the gateway variables.", file=sys.stderr
        )
        return 2
    files = sorted(args.cases.rglob("*.json"))
    if not files:
        print(f"No cases in {args.cases}", file=sys.stderr)
        return 2
    failed = 0
    for f in files:
        r = run_case(json.loads(f.read_text()), args.workbook, cfg)
        failed += not r.passed
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.name}")
        for k, ok in r.checks.items():
            print(f"      {'ok ' if ok else 'NO '} {k}")
        for n in r.notes:
            print(f"      note: {n}")
    print(f"\n{len(files) - failed}/{len(files)} cases passed ({cfg.label})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
