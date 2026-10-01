# TSF Sizer

A web tool that runs in a Docker container. Upload a PAN-OS Tech Support File (TSF) and it produces a sizing report: how loaded the current firewall is, what a replacement needs, and which model(s) from the current portfolio fit, with the reason for every model it rejects.

## Run it

You need Docker (Docker Desktop on Mac/Windows).

```bash
git clone <this repo> tsf-sizer && cd tsf-sizer
docker compose up -d --build
```

Open <http://localhost:8088>.

**First time only:** go to **Portfolio**, import the *Features & Capacities* workbook (.xlsx), check the import report, and click **Activate**. The workbook is internal, so it is never part of the repo or the image; it lives in the container's data volume.

Then, on **Analyses**, upload a TSF (the `.tgz` as exported, up to 1 GB). The report is ready a few seconds after the upload finishes. Use **Print / PDF** in the report for a document to share.

Stop with `docker compose down`. Analyses and the portfolio survive restarts (Docker volume `tsf-sizer-data`); `docker compose down -v` deletes them.

### Options (environment variables, e.g. in a `.env` file next to `docker-compose.yml`)

| Variable | Default | Purpose |
|---|---|---|
| `TSF_SIZER_USER`, `TSF_SIZER_PASSWORD` | not set | Require a login (HTTP basic auth). Set both. |
| `TSF_SIZER_MAX_UPLOAD_MB` | `1024` | Upload limit per file |
| `TSF_SIZER_PORT` | `8088` | Port on your machine (if 8088 is taken too) |
| `TSF_SIZER_LLM_PROVIDER`, `_MODEL`, `_BASE_URL`, `_API_KEY`, `_TIMEOUT` | off | Optional AI summary, see below |

By default the app is only reachable from your own machine (`127.0.0.1:8088`). For a shared team server, put a reverse proxy with HTTPS in front of it, set a login, and change the port mapping in `docker-compose.yml`.

### AI summary with a local LLM (optional)

The sizing itself never uses an LLM: it is rule-based and computed in the container. Optionally, an LLM writes a short justification (summary, why this model, alternatives, checks before quoting) at the top of the report. Configure it on the **AI settings** page or with environment variables. Supported:

| Provider | Server URL (default) | Notes |
|---|---|---|
| **Ollama** (recommended for customer data) | `http://host.docker.internal:11434` | Runs locally; nothing leaves your network |
| **OpenAI-compatible** | `http://host.docker.internal:1234/v1` | LM Studio, vLLM, LocalAI, llama.cpp server, or a hosted API; include `/v1` |
| **Anthropic** | `https://api.anthropic.com` | Hosted; needs `TSF_SIZER_LLM_API_KEY` |

**Ollama already installed on your machine:**

```bash
ollama pull llama3.1:8b          # or qwen2.5:14b, mistral-nemo, …
docker compose up -d
```

On **AI settings**: choose *Ollama*, leave the URL empty, click **Find models**, pick one, **Save & test**. On Linux, Ollama listens on 127.0.0.1 only by default; let containers reach it with `OLLAMA_HOST=0.0.0.0` (systemd: `systemctl edit ollama`).

**Ollama in Docker next to the app:**

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull llama3.1:8b
```

and use server URL `http://ollama:11434`. Without a GPU, an 8B model needs about 8 GB RAM and a minute or so per summary; raise the timeout under *Advanced* for bigger models.

Or set it up front in `.env`:

```bash
TSF_SIZER_LLM_PROVIDER=ollama
TSF_SIZER_LLM_MODEL=llama3.1:8b
# TSF_SIZER_LLM_BASE_URL=http://ollama:11434
# TSF_SIZER_LLM_API_KEY=...   (OpenAI-compatible servers that need one, Anthropic)
```

What is sent and checked:

- Only the computed figures go to the LLM: models, counts, capacities, assumptions and warnings. Never the TSF, hostnames, IP addresses, serial numbers or object names. *AI settings* shows the exact data for the latest analysis and the instructions given to the model.
- Every number and model name in the answer is checked against that data; anything else is flagged in the report. **Rewrite** regenerates the summary with the current settings.
- API keys are only read from `TSF_SIZER_LLM_API_KEY`, never stored or shown.
- An unreachable or failing LLM never fails the analysis; the report shows the error and a *Try again* button. Untick *Write an AI summary* on the upload form to skip it for one analysis.

### Building behind a TLS-inspecting proxy

If the build fails with `CERTIFICATE_VERIFY_FAILED` while installing Python packages, pass your company's CA certificate as a build secret (it is not stored in the image):

```bash
docker build --secret id=ca,src=/path/to/company-ca.pem -t tsf-sizer:latest .
docker compose up -d
```

## What the report contains

- **AI summary** (optional): a written justification by the LLM you configured, fact-checked against the sizing data.
- **Recommendation**: the smallest quotable model that passes every rule, plus a *Better* (next step up) and *Best* (next family up) option, each with its closest limits.
- **Check before quoting**: everything that makes the numbers less certain: short history after a reboot, snapshot-only throughput, expired licenses, unconfirmed capacity values, ports whose type had to be assumed.
- **Current usage** and a **requirements table**: usage of the current model per capacity (with meters), the requirement after growth, and whether the recommended model covers it.
- **Interfaces**: ports in use (traffic/HA) and how they map onto the recommended model.
- **Models that don't qualify**, each with the reasons.
- **Load history** (CPU and session table peaks up to 13 weeks) and **licenses**.

### Sizing rules

Defaults can be changed per analysis under *Sizing assumptions and known peaks*.

- Capacities must cover usage × growth (default 20 %/year over 3 years).
- Throughput, connections/s and sessions are sized to stay under the target utilization (default 70 %), and must be **higher** than the current model's.
- Object, rule and interface counts must be **at least** the current model's. Per-item limits (members per address group, members per aggregate) only need to cover actual usage.
- Ports: by default at least the current model's full port layout; optionally only the ports in use. HA links on data ports are freed when the new model has dedicated HA ports.
- Features in use (HA mode, GTP, SCTP, …) must be supported.
- Unreleased (NPI) models and chassis cards are never recommended.
- **Previous generations** (e.g. PA-400, succeeded by PA-500) are left out unless *Include previous-generation models* is ticked on the analysis form.
- **PoE:** dedicated PoE models (e.g. PA-545-POE, PA-555-POE) are only recommended when *Customer needs PoE* is ticked; then only models with PoE ports qualify (which includes models with PoE as standard, like the PA-1400 series). It switches on automatically when the TSF shows PoE devices powered on the current firewall.
- **No oversizing:** models with more than 5× the current model's throughput (or 5× the requirement, if that is higher) are listed as "too large" instead of recommended. Change the factor under *Sizing assumptions*.

### Family settings (Portfolio page)

Team knowledge the workbook doesn't contain is set per family on the Portfolio page and kept across workbook imports:

- **Quotable**: release a family the sheet still marks as NPI, or block one. Default: PA-500 released.
- **Succeeded by**: mark a family as previous generation. Default: PA-400 succeeded by PA-500.

The same is available on the command line inside the container: `docker exec tsf-sizer tsf-sizer set-family PA-5400 --superseded-by PA-5500`.

**The TSF only has a throughput/CPS snapshot**: enter the customer's known peaks for a reliable performance sizing. The report is a draft for the engineer to review.

## Privacy

- Only the files needed are read from the TSF (CLI output, config XML); logs are skipped.
- Uploaded files are deleted as soon as the analysis is done, also when it fails.
- Results keep counts and metrics only, no hostnames, serial numbers, IP addresses, admin names or object names.
- Nothing is sent outside the container, unless you configure a hosted LLM for the AI summary; even then only aggregated figures are sent (see above). With Ollama or another local server, nothing leaves your network.

## Development

See [backend/README.md](backend/README.md) for the command-line tools and tests, and [docs/tsf-sizing-agent-plan.md](docs/tsf-sizing-agent-plan.md) for the design.

```bash
cd backend && pip install -e ".[dev]"
uvicorn --factory tsf_sizer.web.app:create_app --reload --port 8088
pytest && ruff check .
```
