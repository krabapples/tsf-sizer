# TSF Sizing Agent: Build Plan

**Goal:** a presales engineer uploads a customer's Tech Support File (TSF). The agent pulls out the firewall's configuration, usage and interface layout, compares them with the portfolio spreadsheet, and recommends a replacement model with the reasoning written out. The engineer reviews it before it goes to the customer.

**Hard rule from the business:** the proposed model must have **more performance, higher object limits, and at least the same interface configuration** as the current firewall, preferably better.

---

## 1. Design principle: deterministic core, LLM on the edges

Sizing is arithmetic plus rules. It must be repeatable, auditable and correct, so do **not** let the LLM parse the TSF or do the comparison. Split the work like this:

| Layer | Who does it | Why |
|---|---|---|
| Unpack and parse the TSF | Code (Python) | Exact values, no hallucinations, testable |
| Normalize the portfolio spreadsheet | Code | Units and column names must be consistent |
| Compute requirements and filter/rank models | Code (rules engine) | Enforces the hard rule every time |
| Explain the choice, flag risks, write the proposal | LLM (Claude) | This is where language adds value |
| Follow-up questions ("what if they add decryption?") | LLM agent calling the code as tools | Interactive what-if without re-coding |

Every number in the final report should trace back to a TSF field or a spreadsheet cell.

---

## 2. Architecture: one Docker container with a web frontend

The whole tool ships as **one Docker image**. A colleague runs it with a single command and opens it in a browser. There are no external services to install besides the LLM endpoint.

```
┌──────────────────────── docker container: tsf-sizer ────────────────────────┐
│                                                                             │
│  Web frontend (browser)                                                     │
│   • Upload page: drag & drop TSF + optional inputs                          │
│   • Job progress (parsing → sizing → writing)                               │
│   • Result page: recommendation, comparison table, interface map,           │
│     warnings, chat box for what-if questions                                │
│   • Admin page: upload/refresh portfolio spreadsheet, default parameters    │
│   • History: previous analyses (per user)                                   │
│            │  HTTP/JSON                                                     │
│            ▼                                                                │
│  Backend: Python + FastAPI                                                  │
│   ├── parser/     TSF (.tgz) → metrics JSON                                 │
│   ├── portfolio/  spreadsheet → normalized model catalogue                  │
│   ├── sizing/     requirements, hard filters, interface matching, ranking   │
│   ├── llm/        Claude write-up + agent tools for follow-up questions     │
│   ├── reports/    HTML → PDF (WeasyPrint) and DOCX (python-docx)            │
│   └── jobs        background worker (in-process), status polling           │
│            │                                                                │
│            ▼                                                                │
│  /data (Docker volume)                                                      │
│   ├── app.db          SQLite: analyses, audit log, settings                 │
│   ├── portfolio/      current + previous spreadsheet versions               │
│   └── tmp/            uploaded TSFs (deleted after processing)              │
└─────────────────────────────────────────────────────────────────────────────┘
                     │ HTTPS (only the aggregated metrics JSON)
                     ▼
          Approved Claude endpoint (Anthropic API / company tenant / cloud-hosted)
```

### Technology choices
| Part | Choice | Why |
|---|---|---|
| Backend | Python 3.12 + FastAPI | TSF parsing, XML and spreadsheets are easiest in Python; FastAPI gives a clean JSON API and auto docs |
| Frontend | React + Vite + Tailwind, built to static files and served by FastAPI | Interactive result page and chat; one origin, no extra web server. *(Simpler option: server-rendered Jinja + HTMX, with no Node build step.)* |
| Spreadsheet | `openpyxl` / `pandas` | Reads the team's .xlsx directly |
| LLM | Anthropic Python SDK (Claude) | Tool use for the what-if agent; the endpoint is configurable via env vars |
| Storage | SQLite on a mounted volume | No database server; survives container restarts |
| Background jobs | In-process worker (asyncio / thread pool) | TSFs take seconds to a minute to parse; no Redis needed for the MVP |
| Reports | WeasyPrint (PDF), python-docx (DOCX) | Proposal-ready exports |
| Image | Multi-stage Dockerfile: Node stage builds the frontend, then a slim Python runtime stage | Small image with nothing to build at runtime; runs as non-root |

### How colleagues run it
```bash
# one-time
docker pull <registry>/tsf-sizer:latest        # or: docker build -t tsf-sizer .

# run
docker run -d --name tsf-sizer -p 8080:8080 \
  -e ANTHROPIC_API_KEY=... \
  -v tsf-sizer-data:/data \
  <registry>/tsf-sizer:latest
# → open http://localhost:8080
```
A `docker-compose.yml` ships as well, with the same settings and an optional reverse proxy (Caddy/Traefik) for HTTPS when hosted centrally.

**Two deployment modes, same image:**
1. **Local (per SE laptop):** each person runs it themselves; nothing leaves their machine except the LLM call. Easiest to get started.
2. **Shared (team server):** one instance on an internal VM with HTTPS and SSO (OIDC via env vars). Everyone uses the same spreadsheet version and history.

### Configuration (env vars)
`ANTHROPIC_API_KEY` (or the company-endpoint settings), `LLM_MODEL`, `LLM_ENABLED` (false = the tool still sizes, just without a written argument), `AUTH_MODE` (none / basic / oidc), `OIDC_*`, `PORTFOLIO_PATH`, `TSF_RETENTION_HOURS`, `MAX_UPLOAD_MB`.

### How the portfolio spreadsheet gets into the container
- Upload it on the admin page. The backend validates it, shows a diff against the previous version ("PA-XXXX: max sessions changed"), and stores it versioned in `/data/portfolio/`.
- Alternatively, mount a file (`-v ./portfolio.xlsx:/data/portfolio/portfolio.xlsx:ro`), or (later) sync from SharePoint/Google Sheets on a schedule.

---

## 3. What to extract from the TSF

Step 0 is to **collect 10–20 real TSFs** (sanitized, from different models, PAN-OS 9.1/10.x/11.x, standalone and HA, Panorama-managed and local) and map exactly which file and command output holds each metric. File paths and output formats change between PAN-OS versions, so check each one against a real TSF.

### 3.1 Identity / platform
- Model, serial, PAN-OS version, uptime (`show system info` output inside the TSF)
- HA mode and peer (active/passive, active/active)
- Multi-vsys enabled, number of vsys in use
- Licenses and subscriptions (Threat, URL, WildFire, DNS, GlobalProtect, decryption usage)

### 3.2 Configuration / object counts (from `running-config.xml`)
- Address objects, address groups, service objects, service groups
- Security rules, NAT rules, decryption rules, PBF rules, QoS rules
- Zones, virtual routers / logical routers, static routes, BGP/OSPF peers
- IPSec tunnels, IKE gateways, GlobalProtect gateways/portals (+ concurrent users if visible)
- FQDN objects, EDLs (count + total entries), custom App-IDs, tags
- **Panorama-managed devices:** objects and policies pushed from Panorama may sit in separate pushed-config files, not in the local running config. The parser must merge them or the counts will be too low.

### 3.3 Interfaces (from config + `show interface all`)
- Every physical port: name, media (copper/SFP/SFP+/SFP28/QSFP+/QSFP28), configured speed, link state, and whether it's **used** (has an IP, zone or subinterface, or is an aggregate member)
- Aggregate groups (AE) and member counts, subinterface/VLAN counts
- Dedicated HA ports (HA1/HA2/HSCI) and management
- Output: a count of **used ports per speed class and media type**. This is what the new model must cover.

### 3.4 Performance / utilization
- Sessions: max supported vs active vs peak (`show session info`)
- Connections per second, throughput (kbps), packet rate
- Dataplane CPU: hourly/daily/weekly averages and maxima (`show running resource-monitor`)
- Management plane CPU/memory
- Decryption session count if SSL decryption is on
- Global counters of interest (drops caused by resource exhaustion, session-table full, packet buffer)

> **Snapshot caveat:** a TSF shows *one moment* plus the recent resource-monitor history. If the TSF was taken off-peak, usage will look lower than it is. The report must state the data window and let the engineer enter a known peak (or a separate SLR/monitoring figure) to override it.

---

## 4. The portfolio spreadsheet

Make the spreadsheet machine-readable **without changing how the team maintains it**:

1. Define a **canonical schema**, one row per model, with columns such as:
   - `model`, `family`, `form_factor_ru`, `status` (current / EoS / EoL), `eos_date`
   - Throughput: `fw_throughput_gbps`, `threat_throughput_gbps`, `ipsec_vpn_gbps`, `decryption_throughput_gbps` (if kept)
   - `max_sessions`, `new_sessions_per_sec`, `max_decryption_sessions`
   - Objects: `max_address_objects`, `max_address_groups`, `max_service_objects`, `max_security_rules`, `max_nat_rules`, `max_decryption_rules`, `max_zones`, `max_virtual_routers`, `max_ipsec_tunnels`, `max_gp_users`, `max_vsys_base`, `max_vsys_licensed`, …
   - Interfaces: `ports_1g_copper`, `ports_1g_sfp`, `ports_10g_sfpplus`, `ports_25g_sfp28`, `ports_40g_qsfp`, `ports_100g_qsfp28`, `dedicated_ha_ports`, `sfpplus_supports_1g` (true/false), …
   - `list_price` or `price_tier` (optional, for ranking)
2. Write a **column-mapping config** (YAML) from the team's column headers to this schema, so a renamed column doesn't break the tool.
3. Normalize units (Mbps→Gbps, "64K"→64000, "N/A"→null) and **fail loudly** on an unparseable cell instead of guessing.
4. Keep the sheet in one place (SharePoint/OneDrive Excel or Google Sheets) and record the sheet version/date in every report.

---

## 5. Sizing logic (rules engine)

### 5.1 Compute the requirement per dimension
For each performance metric:
```
required = observed_peak × (1 + growth_rate)^years ÷ target_utilization
```
Defaults (editable per run): growth 20%/year, 3–5 years, target utilization 70%.

For each object/config metric:
```
required = max(configured_count × (1 + growth_rate)^years,  current_model_limit)
```

The `current_model_limit` term enforces the business rule that the new box is never smaller than the old one on any published limit, even when the customer uses only a fraction of it.

**Throughput basis:** if Threat Prevention subscriptions are active, size on **threat-prevention throughput**, not firewall/App-ID throughput. If decryption is on (or planned), apply decryption throughput or flag it for the official sizing tool.

### 5.2 Hard filters (a candidate is rejected if any fails)
1. `status` is current (not EoS/EoL)
2. Every performance spec ≥ the current model's spec **and** ≥ the requirement
3. Every object limit ≥ the current model's limit **and** ≥ the requirement
4. **Interface coverage:** every used port on the current box maps to a port on the new box:
   - same or higher speed class, compatible media (copper vs fiber)
   - a higher-speed port can take a lower-speed need only if the sheet says it supports that speed (e.g., SFP+ running 1G)
   - solve it as a greedy assignment from the highest speed class down, and count leftover ports
   - dedicated HA ports needed if the customer runs HA
5. vsys: licensable vsys ≥ vsys in use
6. Features in use (GlobalProtect, SD-WAN, …) are supported on the candidate

### 5.3 Rank the survivors
- Main recommendation: the **smallest/cheapest model that passes every filter** (right-sizing)
- Also show a **"Better"** (next model up) and optionally a **"Best"** option
- Tie-breakers: highest *minimum* headroom across dimensions, then price, then same family as today (familiar to the customer)

### 5.4 Output (JSON, the input to the LLM step)
```json
{
  "current": { "model": "PA-3220", "panos": "10.2.9", "ha": "active-passive", ... },
  "requirements": { "threat_throughput_gbps": 4.1, "max_sessions": 850000, ... },
  "recommendation": "PA-3410",
  "alternatives": ["PA-3420", "PA-5410"],
  "comparison": [
    { "metric": "threat_throughput_gbps", "current_spec": 2.2, "observed_peak": 1.9,
      "required": 4.1, "proposed_spec": 5.0, "headroom_pct": 22, "pass": true },
    ...
  ],
  "interface_mapping": [ { "current": "ethernet1/9 (10G SFP+)", "proposed": "ethernet1/13 (10G SFP+)" } ],
  "warnings": ["TSF captured at 03:10 local — peak may be understated", "Decryption enabled on 40% of rules — confirm with sizing tool"],
  "sources": { "tsf_file": "...", "sheet_version": "2026-09-15" }
}
```
*(Models and numbers above are placeholders, not real specs.)*

---

## 6. LLM step (Claude)

- Model: current Claude Sonnet for cost/speed, Opus for the hardest cases; set it in one place.
- **Input:** only the JSON from §5.4, never the raw TSF (see §8).
- **System prompt rules:**
  - Use only numbers present in the input JSON; don't invent specs.
  - Explain *why* the recommended model was chosen, per dimension, citing current / required / proposed.
  - Say why smaller models were rejected (the first filter each one failed).
  - Surface every warning prominently.
  - Output: an executive summary (3–5 lines), a comparison table, the interface migration map, risks & assumptions, and next steps.
- **Agent tools** (Claude tool use, called from the backend) for follow-ups in the result page's chat box:
  `get_metric(name)`, `get_model_spec(model)`, `compare(model_a, model_b)`, `rerun_sizing(growth, years, target_util, decryption)`.
- **Guardrail:** after generation, a code check verifies that every model name and number in the text appears in the JSON. If one doesn't, regenerate or flag it.

---

## 7. Web frontend / UX

- **Upload page:** drag and drop the TSF, plus customer name/opportunity ID and optional overrides (growth, years, target utilization, known peak throughput, decryption planned, HA pair with a second TSF for the peer). The upload streams with a progress bar; big files are fine.
- **Progress:** each step shows its status (unpacking → parsing → sizing → writing). Parsing results appear before the LLM text is ready.
- **Result page:**
  - recommendation card (Good / Better / Best) with a one-line reason each
  - comparison table (current spec / observed / required / proposed / headroom %), coloured by margin
  - interface migration map (old port → new port)
  - warnings and assumptions, prominently shown
  - "rejected models" section with the reason for each
  - sliders to change growth/years/utilization, which re-run the sizing instantly (no LLM needed)
  - chat box for questions ("what if they enable decryption on all outbound traffic?")
  - export buttons: PDF, DOCX, JSON (audit trail)
- **History:** earlier analyses, searchable by customer or opportunity.
- **Admin:** portfolio spreadsheet upload with diff and version history, default parameters, retention settings.
- **Human-in-the-loop:** the report is a *draft for the SE*, never sent to the customer automatically.

---

## 8. Security & compliance (address before building)

TSFs contain sensitive customer data: IP plans, rule bases, usernames, certificates and possibly hashed credentials and keys.

- Check **PANW's internal policy on AI tools and customer data** first: which LLM endpoints are approved (e.g., an enterprise Claude tenant or cloud-hosted Claude under a company agreement), and whether customer TSFs may be processed at all.
- Run the container **inside company-controlled infrastructure**: an SE laptop or an internal VM, not a public cloud host on a personal account.
- **Data minimization:** only aggregated counts and metrics (§5.4 JSON) go to the LLM. No IPs, names, rule contents or secrets.
- Delete the uploaded TSF and extracted files once processing finishes (or after N days), and log who uploaded what.
- In shared mode, require login (OIDC/SSO) and scope history per user. Local mode binds to `localhost` only by default.
- Harden the container: non-root user, read-only root filesystem except `/data`, safe tar extraction (size and file-count limits, no path traversal or symlinks), upload size limit, dependency and image scanning in CI.
- Check whether an existing internal tool already parses TSFs (e.g., the tooling behind the Best Practice Assessment) and could supply a parser or metric definitions to reuse.

---

## 9. Phased delivery

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Discovery** (1 wk) | Collect sample TSFs + spreadsheet; map metric → file path per PAN-OS version; agree on the canonical schema and default sizing parameters with 1–2 senior SEs; get security sign-off | Metric map + schema doc approved |
| **1. Parser** (1–2 wk) | Python `tsf-parser`: untar, parse config XML + CLI outputs, Panorama-pushed merge, interface inventory → JSON. Unit tests per sample TSF | Parser output matches manual extraction on every sample |
| **2. Portfolio + sizing engine** (1 wk) | Spreadsheet loader + mapping YAML + normalization; requirement calc, hard filters, interface assignment, ranking | On 10+ historical deals the engine picks the model the SE chose, or a defensible one |
| **3. Container + web MVP** (1–2 wk) | FastAPI endpoints, background jobs, upload and result pages, Claude write-up, Dockerfile + compose, SQLite history | `docker run` → upload a real TSF → report in < 2 min, on a colleague's laptop |
| **4. Agent + what-if** (1 wk) | Chat box with Claude tool use, parameter sliders, number-verification guardrail, PDF/DOCX export | SEs can ask "what if +50% growth" and get a consistent answer |
| **5. Hardening** (ongoing) | Shared-server mode with SSO, image published to an internal registry via CI, HA pair / multi-firewall consolidation, VM-Series sizing (vCPU-based), audit log, retention, feedback button ("recommendation was right/wrong") | Pilot with 3–5 SEs, feedback loop in place |

---

## 10. Testing & quality

- **Golden set:** 10–20 past opportunities (TSF + the model the SE actually proposed). Rerun it on every change to the parser, engine, spreadsheet or prompt.
- **Parser tests:** one fixture per PAN-OS major version and per platform family; test missing sections (partial TSFs, older versions).
- **Engine tests:** table-driven cases for edge conditions: exactly at limit, interface shortage by one port, HA ports, vsys, EoS model filtered out.
- **LLM eval:** check that reports contain no numbers outside the JSON and always list the warnings.

---

## 11. Suggested repo layout

```
tsf-sizer/
├── backend/
│   ├── app/
│   │   ├── main.py             # FastAPI app, serves API + built frontend
│   │   ├── api/                # routes: /analyses, /portfolio, /chat, /settings
│   │   ├── jobs.py             # background job runner + status
│   │   ├── db.py               # SQLite models (SQLModel)
│   │   └── config.py           # env-var settings
│   ├── parser/                 # TSF → metrics JSON
│   │   ├── extract.py          # safe tar extraction (size limits, path traversal checks)
│   │   ├── config_xml.py       # object/rule/interface counts from running config
│   │   ├── cli_outputs.py      # session info, resource monitor, interface status
│   │   └── panorama_merge.py
│   ├── portfolio/
│   │   ├── mapping.yaml        # sheet column → canonical schema
│   │   └── loader.py
│   ├── sizing/
│   │   ├── requirements.py
│   │   ├── filters.py
│   │   ├── interfaces.py       # port assignment solver
│   │   └── rank.py
│   ├── llm/
│   │   ├── report.py           # write-up generation + number check
│   │   ├── agent.py            # tool-use loop for chat
│   │   └── prompts/
│   ├── reports/                # PDF/DOCX templates
│   └── tests/                  # fixtures: sanitized sample TSF extracts, golden set
├── frontend/                   # React + Vite + Tailwind
│   └── src/pages/              # Upload, Progress, Result, History, Admin
├── Dockerfile                  # multi-stage: build frontend → slim Python runtime
├── docker-compose.yml
├── .env.example
└── README.md                   # how to run in 2 commands
```

---

## 12. Open questions for the team

1. Which spreadsheet columns exist today, and is there one authoritative copy?
2. Default growth rate, planning horizon and target utilization: is there a team standard?
3. Should the tool also recommend VM-Series / CN-Series / cloud NGFW, or only hardware?
4. How should decryption-heavy customers be handled: rules of thumb, or always a handoff to the official sizing tool?
5. Is pricing available to the tool, or should ranking be by model tier only?
6. Which LLM endpoint and hosting location are approved for customer-derived data?
