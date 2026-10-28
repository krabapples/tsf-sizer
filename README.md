# TSF Sizer

A web tool that runs in a Docker container. Upload a PAN-OS Tech Support File (TSF) and it produces a sizing report: how loaded the current firewall is, what a replacement needs, and which model(s) from the current portfolio fit, with the reason for every model it rejects.

## Run it

You need Docker (Docker Desktop on Mac/Windows).

```bash
git clone https://github.com/krabapples/tsf-sizer.git && cd tsf-sizer
docker compose up -d --build
```

Open <http://localhost:8088>.

**First time only:** go to **Portfolio**, import your capacity workbook (.xlsx), check the import report, and click **Activate**. The workbook is not part of the repo or the image; it lives in the container's data volume.

Then, on **Analyses**, first answer the questions about the customer (like for like or a smaller box, optics, extra ports, notes, models to include or leave out; they shape the first result and the agent does not ask them again), then upload a TSF (the `.tgz` as exported, up to 1 GB). The report is ready a few seconds after the upload finishes. Use **Print / PDF** in the report for a document to share.

Stop with `docker compose down`. Analyses and the portfolio survive restarts (Docker volume `tsf-sizer-data`); `docker compose down -v` deletes them.

### Options (environment variables, e.g. in a `.env` file next to `docker-compose.yml`)

| Variable | Default | Purpose |
|---|---|---|
| `TSF_SIZER_USER`, `TSF_SIZER_PASSWORD` | not set | Require a login (HTTP basic auth). Set both. |
| `TSF_SIZER_MAX_UPLOAD_MB` | `1024` | Upload limit per file |
| `TSF_SIZER_PORT` | `8088` | Port on your machine (if 8088 is taken too) |
| `TSF_SIZER_BIND` | `0.0.0.0` | Address to listen on. `0.0.0.0`: reachable from the network; `127.0.0.1`: this machine only |
| `TSF_SIZER_LLM_MODE` and `TSF_SIZER_GATEWAY_*` | off | The agent's LLM through the AI gateway, see below |

By default the app listens on every network address of the machine, so colleagues can open it at `http://<machine-ip>:8088` (e.g. `http://192.168.2.170:8088`). It handles customer data and has **no login unless you set one**: put this in a `.env` file next to `docker-compose.yml`, then run `docker compose up -d`:

```bash
TSF_SIZER_USER=sizer
TSF_SIZER_PASSWORD=choose-a-password
```

To keep the app reachable from this machine only, add `TSF_SIZER_BIND=127.0.0.1`. If other machines still cannot connect, check the host's firewall (e.g. `sudo ufw allow 8088/tcp`). For a shared team server, put a reverse proxy with HTTPS in front of it.

### Without a Tech Support File

Some customers cannot provide one. On the upload page choose **No file: I enter what I know**: give the current model and whatever you know (high availability, throughput and whether it is a peak, a typical value or an estimate, connections per second, concurrent sessions, interfaces in use by type, PoE, configuration counts), plus the same sizing input as for a file. Only the model is required. The analysis is marked as entered, not measured: nothing is guessed, a figure you leave out is listed as unverified, and the report says that CPU history, counters and licenses are unknown. With an LLM, the agent asks for the figures that matter most (peak throughput, sessions, interfaces, HA mode) and records your answers as evidence (`record_evidence`), then sizes as usual; it keeps its confidence at medium or lower unless the figures are peaks, and tells you to confirm with a Tech Support File before quoting.

### The agent (optional, needs an LLM through the AI gateway)

Without an LLM the app gives a **quick result**: fixed rules applied to your assumptions, as before. With an LLM, every analysis also starts an **agent** that reasons with you before reaching a verdict:

1. It reads the evidence from the TSF (usage, CPU history, resource counters, licenses, ports, HA) and tells you what it sees.
2. It asks the questions whose answer changes the result (like for like or a smaller box? optics? a known peak?), with options and a default.
3. It investigates what does not fit (CPU peaks, allocation failures, expired licenses, ports with link up and no configuration).
4. It checks every candidate with deterministic **tools**: requirements, limit by limit with headroom, ports, features, PoE. The tools return facts and "no data"; they never decide.
5. It submits a **verdict**: a recommendation (or none), alternatives, rejected models with the limit that blocks them, open points for before you quote, and a confidence level. A **verifier** checks that every model was checked and every figure in the text matches a tool result. Claims it cannot match are shown as *unverified*.
6. You challenge it: correct an assumption, ask why, ask for a what-if. Each verdict is kept as a version.

The analysis page has the conversation on top and four tabs: **Verdict**, **Quick result**, **Ledger** (every assumption with its source and reason: form, you, agent or default; editable, with undo) and **Evidence** (what was read from the TSF).

The team's sizing policy (70 % target utilization, "match or beat the current model" versus "usage only", variants, ports, known exceptions such as PA-800 and the Legacy Routing Engine) is a **playbook**, plain text you can edit on the **Settings** page. The agent applies it, may deviate when it explains why, and records the deviation in the ledger.

**Connecting the gateway.** All model calls go through the Prisma AIRS AI Gateway, an OpenAI-style endpoint. Set the variables on the container (`.env`, see `.env.example`); credentials are never entered in the UI or stored:

```bash
TSF_SIZER_LLM_MODE=gateway
TSF_SIZER_GATEWAY_URL=https://gateway.example.com/v1
TSF_SIZER_GATEWAY_API_KEY=...            # or TSF_SIZER_GATEWAY_API_KEY_FILE=/run/secrets/key
TSF_SIZER_GATEWAY_MODEL=model-name-as-the-gateway-exposes-it
```

| Variable | Default | Purpose |
|---|---|---|
| `TSF_SIZER_LLM_MODE` | `off` | `gateway`, `direct` (development: any OpenAI-compatible server via `TSF_SIZER_DIRECT_URL`, `_MODEL`, `_API_KEY`) or `off` |
| `TSF_SIZER_GATEWAY_URL` | | Base URL including the version path |
| `TSF_SIZER_GATEWAY_API_KEY` | | The key issued for this application (secret) |
| `TSF_SIZER_GATEWAY_MODEL` | | Model name as the gateway exposes it |
| `TSF_SIZER_GATEWAY_AUTH_HEADER`, `_AUTH_SCHEME` | `Authorization`, `Bearer` | Header that carries the key and its prefix (empty scheme = key as is) |
| `TSF_SIZER_GATEWAY_HEADERS` | | Extra headers as JSON, e.g. to select a routing configuration or security profile (secret) |
| `TSF_SIZER_GATEWAY_CHAT_PATH` | `/chat/completions` | Path added to the URL |
| `TSF_SIZER_GATEWAY_CA_BUNDLE`, `_VERIFY_TLS` | system store, `true` | Private certificates; `false` is a last resort |
| `TSF_SIZER_GATEWAY_TIMEOUT_S`, `_MAX_RETRIES` | `120`, `2` | Per request timeout; retries on timeouts, 429 and 5xx |
| `TSF_SIZER_GATEWAY_TOOL_MODE` | `auto` | `native` tool calls, `json` (tool calls as JSON text), or `auto` (try native, fall back once) |
| `TSF_SIZER_AGENT_MAX_STEPS` | `40` | Tool calls per run |

The **Settings** page lists these variables (secrets only as set or not set), has a **Test connection** button (a plain request and a request with a tool) and the playbook editor. If the gateway's security profile blocks a request, the agent stops and says so: it does not retry or rephrase. A 401 or 403 names the variables to check; 429 and transient errors are retried.

What is sent: aggregated facts and tool results (model, counts, capacity values, assumptions) and your messages. Never the TSF, host names, IP addresses, serial numbers, user names or object and rule names. Every message is also passed through a filter that masks addresses, host names, e-mail addresses and serial-like numbers. The uploaded TSF is deleted after parsing, so the agent cannot read raw text from it.

### Building behind a TLS-inspecting proxy

If the build fails with `CERTIFICATE_VERIFY_FAILED` while installing Python packages, pass your company's CA certificate as a build secret (it is not stored in the image):

```bash
docker build --secret id=ca,src=/path/to/company-ca.pem -t tsf-sizer:latest .
docker compose up -d
```

## What the report contains

- **Verdict** (with an LLM): the agent's recommendation, alternatives, rejected models, open points and every limit of the recommended model, each figure verified against tool results. Earlier versions stay readable.
- **Ledger**: every assumption behind the answer, who set it and why.
- **Quick result**, the rule-based recommendation: the smallest quotable model that passes every rule, plus a *Better* (next step up) and *Best* (next family up) option, each with its closest limits.
- **Check before quoting**: everything that makes the numbers less certain: short history after a reboot, snapshot-only throughput, expired licenses, unconfirmed capacity values, ports whose type had to be assumed.
- **Current usage** and a **requirements table**: usage of the current model per capacity (with meters), the requirement after growth, and whether the recommended model covers it.
- **Interfaces**: ports in use (traffic/HA) and how they map onto the recommended model.
- **Models that don't qualify**, each with the reasons.
- **Load history** (CPU and session table peaks up to 13 weeks) and **licenses**.

### Sizing rules

These are the rules of the quick result (the agent's playbook has the same defaults). Change them per analysis in the **Ledger** tab or on the upload form.

- Capacities must cover usage × growth (default 20 %/year over 3 years).
- Throughput, connections/s and sessions are sized to stay under the target utilization (default 70 %), and must be **higher** than the current model's.
- Object, rule and interface counts must be **at least** the current model's. Per-item limits (members per address group, members per aggregate) only need to cover actual usage.
- Ports: by default at least the current model's full port layout; optionally only the ports in use. HA links on data ports are freed when the new model has dedicated HA ports.
- Features in use (HA mode, GTP, SCTP, …) must be supported. One exception: a newer model that no longer supports the Legacy Routing Engine is **not** excluded; it gets a *not blocking* warning to plan the move to the Advanced Routing Engine.
- When the current firewall is a **PA-800** (PA-820/850), the *maximum aggregate interfaces* limit never excludes a replacement. If the candidate has fewer than the PA-800, the card, the requirements table and *Check before quoting* say so.
- Unreleased (NPI) models and chassis cards are never recommended.
- **Previous generations** (e.g. PA-400, succeeded by PA-500) are left out unless *Include previous-generation models* is ticked on the analysis form.
- **PoE:** dedicated PoE models (e.g. PA-545-POE, PA-555-POE) are only recommended when *Customer needs PoE* is ticked; then only models with PoE ports qualify (which includes models with PoE as standard, like the PA-1400 series). It switches on automatically when the TSF shows PoE devices powered on the current firewall.
- **No oversizing:** models with more than 5× the current model's throughput (or 5× the requirement, if that is higher) are listed as "too large" instead of recommended. Change the factor in the Ledger.

### Family settings (Portfolio page)

Team knowledge the workbook doesn't contain is set per family on the Portfolio page and kept across workbook imports:

- **Quotable**: release a family the sheet still marks as NPI, or block one. Default: PA-500 released.
- **Succeeded by**: mark a family as previous generation. Default: PA-400 succeeded by PA-500.

The same is available on the command line inside the container: `docker exec tsf-sizer tsf-sizer set-family PA-5400 --superseded-by PA-5500`.

### Models that are not in the workbook (PA-800, PA-3200, PA-5200)

The current workbook only covers current platforms. So that a TSF from an older firewall can still be compared with its own limits, these models are built in as fixed data: **PA-820, PA-850, PA-3220, PA-3250, PA-3260, PA-5220, PA-5250, PA-5260, PA-5280**. They are added to every imported workbook version (and to existing ones at start-up) and are **never recommended**. If a workbook ever includes one of these models, the workbook's values are used instead.

- Limits, sessions, port layouts and dedicated HA ports come from an older capacity workbook (PAN-OS 11.0), extracted once with `backend/tools/build_supplement.py` into `backend/tsf_sizer/portfolio/supplement_data.json` (numbers and Yes/No only).
- Throughput, new sessions per second and max sessions come from the datasheets: PA-800 Series (PAN-OS 11.0), PA-3200 Series (11.1) and PA-5200 Series (11.2). Session counts also come from the datasheets (the older workbook's binary figures, e.g. 3,145,728 for the PA-3260, are about 5% higher than the published 2.2M).
- Where a model had two port options (PA-850: 8 SFP, or 4 SFP + 4 SFP+) the first is stored. Shared SFP/SFP+ cages (PA-3200, PA-5200) are stored as SFP+; the sizing lets SFP needs use them.

**The TSF only has a throughput/CPS snapshot**: enter the customer's known peaks for a reliable performance sizing. The report is a draft for the engineer to review.

## Privacy

- Only the files needed are read from the TSF (CLI output, config XML); logs are skipped.
- Uploaded files are deleted as soon as the analysis is done, also when it fails.
- Results keep counts and metrics only, no hostnames, serial numbers, IP addresses, admin names or object names.
- Nothing is sent outside the container unless you configure an LLM; then only aggregated facts, tool results and your messages go to the AI gateway, filtered as described above.

## Development

See [backend/README.md](backend/README.md) for the command-line tools and tests, and [docs/tsf-sizing-agent-plan.md](docs/tsf-sizing-agent-plan.md) for the design.

```bash
cd backend && pip install -e ".[dev]"
uvicorn --factory tsf_sizer.web.app:create_app --reload --port 8088
pytest && ruff check .
```

**Evaluating the agent** with the model behind your gateway: golden cases (the aggregated result of an analysis, the answers to give, what a good run looks like) are run end to end and scored on reaching a verdict, an acceptable model, verified figures, mentioned anomalies, the step budget and nothing identifying sent. Cases built from customer data go in the gitignored `backend/evals/private/`; one synthetic case is included.

```bash
cd backend && TSF_SIZER_LLM_MODE=gateway TSF_SIZER_GATEWAY_URL=... TSF_SIZER_GATEWAY_API_KEY=... \
  TSF_SIZER_GATEWAY_MODEL=... python -m tsf_sizer.agent.evals --workbook /path/to/capacity.xlsx --cases evals/
```

## License

[MIT](LICENSE). Third-party UI skills under `.claude/skills/` keep their own license, see `.claude/skills/THIRD_PARTY.md`. This is an independent project, not affiliated with or endorsed by Palo Alto Networks; product names belong to their owners.
