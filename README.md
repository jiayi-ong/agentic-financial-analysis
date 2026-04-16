# Agentic Financial Analyst

A production-grade multi-agent system that performs preliminary financial
analysis of a single stock.  The system collects data from multiple
sources, applies quantitative and qualitative analysis, critiques its own output,
and delivers an evidence-backed analytical hypothesis through a real-time web UI.

> **Disclaimer:** Generated content is for informational purposes only and does
> not constitute financial advice.  Consult a licensed financial professional
> before making investment decisions.

---

## High-Level Architecture

```
User (Browser)
    │  WebSocket
    ▼
FastAPI (app/main.py)
    │
    ▼
FinancialOrchestrator  ←── controls the full pipeline
    │
    ├── [parallel] MacroAnalyst     ── crawl_news
    ├── [parallel] FinancialAnalyst ── yfinance + financial_metrics + code_executor
    ├── [parallel] SentimentAnalyst ── crawl_news
    └── [parallel] RatingsAnalyst   ── SEC EDGAR + yfinance
           │
           ▼
       SynthesisAgent  (no tools — reasoning only)
           │
           ▼
       CritiqueAgent   (returns typed CritiqueOutput)
           │
           ├── severity=none → Final output
           ├── severity=low/medium → targeted re-run → re-synthesise → re-critique
           └── severity=high @ max retries → Abstain
```

All agents are Google ADK `LlmAgent` instances backed by Vertex AI Gemini
(default: `gemini-2.0-flash`, configurable via `GEMINI_MODEL` env var).

---

## Key Components

| Component | Path | Purpose |
|---|---|---|
| Orchestrator | `app/agents/orchestrator.py` | Pipeline coordinator: Analyze → Critique → Triage loop |
| Macro Analyst | `app/agents/macro_analyst.py` | Economy-wide & geopolitical analysis |
| Financial Analyst | `app/agents/financial_analyst.py` | Financials, valuation, DCF, Altman Z |
| Sentiment Analyst | `app/agents/sentiment_analyst.py` | News & investor sentiment |
| Ratings Analyst | `app/agents/ratings_analyst.py` | Analyst ratings & SEC filings |
| Synthesis Agent | `app/agents/synthesis_agent.py` | Combines specialist outputs |
| Critique Agent | `app/agents/critique_agent.py` | Quality gate with typed issues |
| System Prompts | `app/prompts/*.md` | Human-editable prompt files |
| Tools | `app/tools/` | Data collection & analysis tools |
| Schemas | `app/schemas/` | Pydantic v2 data contracts |
| Session Manager | `app/session/manager.py` | In-memory session lifecycle |
| Ticker Validation | `app/utils/ticker.py` | Deterministic CSV + fuzzy match |
| Tracing | `app/tracing/tracer.py` | OpenTelemetry (console + Cloud Trace) |
| Frontend | `frontend/` | Two-tab HTML UI with WebSocket |

---

## Data Sources

| Source | Tool | Data type |
|---|---|---|
| **Reuters / CNBC / Yahoo Finance News** | `web_crawler.py` | News articles (crawled) |
| **Yahoo Finance** (`yfinance`) | `yahoo_finance.py` | Prices, financials, analyst recs |
| **SEC EDGAR** (public API) | `sec_edgar.py` | 10-K, 10-Q, 8-K filings |
| **Pre-written metrics** | `financial_metrics.py` | DCF, P/E, Altman Z, DuPont |
| **Dynamic code** | `code_executor.py` | Custom analysis & charts (sandboxed) |

---

## Agent Workflow Detail

```
1. User enters ticker → deterministic validation against data/tickers.csv
2. Orchestrator starts → emits real-time events via WebSocket
3. 4 specialists run in PARALLEL via asyncio.gather:
   a. MacroAnalyst    → crawls news for macro/geopolitical factors
   b. FinancialAnalyst → fetches financials, runs valuation models
   c. SentimentAnalyst → crawls news for sentiment signals
   d. RatingsAnalyst   → pulls SEC filings + analyst consensus
4. Each specialist returns SpecialistOutput (claims + evidence + confidence)
5. SynthesisAgent receives all specialist outputs, writes analytical narrative
6. CritiqueAgent evaluates: severity ∈ {none, low, medium, high}
   - none → final output with disclaimer appended
   - low/medium → re-run affected specialists (up to MAX_CRITIQUE_RETRIES)
   - high @ max retries → abstain with issue list
7. Final narrative streamed to frontend, disclaimer appended deterministically
```

---

## Local Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- A Google Cloud project with Vertex AI API enabled
- `gcloud auth application-default login` (or a service account JSON)

### Setup

```bash
# Clone and enter the repo
cd agentic-financial-analysis

# Create .env from template
cp .env.example .env
# Edit .env and set GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION

# Install dependencies
uv sync

# Run the server
uv run uvicorn app.main:app --reload --port 8080
```

Open `http://localhost:8080` in your browser.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | *(required)* | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region |
| `GOOGLE_APPLICATION_CREDENTIALS` | *(optional)* | Path to service account JSON |
| `GEMINI_MODEL` | `gemini-2.0-flash` | Gemini model ID for all agents |
| `MAX_CRITIQUE_RETRIES` | `2` | Max re-run cycles before accepting/abstaining |
| `MAX_SPECIALIST_TOOL_CALLS` | `7` | Max tool calls per specialist per critic cycle; resets on re-run |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `ENABLE_CLOUD_TRACE` | `false` | Export OTel spans to Google Cloud Trace |
| `CRAWL_RATE_LIMIT_SECONDS` | `1.0` | Delay between outbound crawl requests |
| `CRAWL_MAX_ARTICLES` | `5` | Max articles per crawl call |
| `SEC_USER_AGENT` | *(required)* | `"AppName admin@email.com"` per SEC policy |

### Running Tests

```bash
uv run pytest tests/ -v
```

---

## Deployment to Google Cloud Run

### One-time setup

```bash
# Enable APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

# Create Artifact Registry repo
gcloud artifacts repositories create agentic-financial-analysis \
  --repository-format=docker --location=us-central1

# Grant Cloud Run SA access to Vertex AI
gcloud projects add-iam-policy-binding YOUR_PROJECT \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### Deploy

```bash
# Build and deploy via Cloud Build
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_REGION=us-central1,_REPO=agentic-financial-analysis,_IMAGE=financial-analyst,_SERVICE=financial-analyst

# Or deploy manually after docker build/push:
gcloud run deploy financial-analyst \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT/agentic-financial-analysis/financial-analyst:latest \
  --region us-central1 --allow-unauthenticated --memory 2Gi --timeout 600
```

### Environment variables on Cloud Run

**How authentication works:** Cloud Run automatically injects credentials for the
attached service account.  No `GOOGLE_APPLICATION_CREDENTIALS` file is needed at
runtime — omit it entirely.

**Two ways to set env vars:**

1. **`cloudbuild.yaml` `--set-env-vars`** — applied automatically on every Cloud
   Build deploy.  Currently sets:
   `ENABLE_CLOUD_TRACE`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL`.

2. **`gcloud run services update`** — one-time or out-of-band updates that persist
   across future deploys (Cloud Run stores them as service-level overrides):

```bash
gcloud run services update financial-analyst \
  --region us-central1 \
  --set-env-vars "\
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT,\
GEMINI_MODEL=gemini-2.0-flash,\
ENABLE_CLOUD_TRACE=true,\
SEC_USER_AGENT=AgenticFinancialAnalysis your@email.com,\
CORS_ORIGINS=https://YOUR_SERVICE_URL.run.app,\
MAX_SPECIALIST_TOOL_CALLS=7"
```

**Production vars to set manually** (not in `cloudbuild.yaml`):

| Variable | Why |
|---|---|
| `SEC_USER_AGENT` | Replace `admin@example.com` with a real contact email per SEC EDGAR policy |
| `CORS_ORIGINS` | Add the Cloud Run service URL; defaults to `localhost` only |
| `MAX_SPECIALIST_TOOL_CALLS` | Optional — cap specialist tool calls per cycle (default: 7) |

**If future data-source tools require API keys**, store them in Secret Manager —
never embed secrets in `cloudbuild.yaml` or container images:

```bash
# 1. Create the secret
echo -n "my-api-key" | gcloud secrets create MY_API_KEY --data-file=-

# 2. Grant the Cloud Run service account read access
gcloud secrets add-iam-policy-binding MY_API_KEY \
  --member="serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# 3. Mount it as an env var in cloudbuild.yaml — add to the deploy step:
#   --set-secrets "MY_API_KEY=MY_API_KEY:latest"
# Or inject at runtime:
gcloud run services update financial-analyst \
  --region us-central1 \
  --set-secrets "MY_API_KEY=MY_API_KEY:latest"
```

---

## Extending the System

### Adding a new data source tool

1. Create `app/tools/my_tool.py` with `@tool_wrapper(...)` decorated functions.
2. Register as `FunctionTool(func=my_function)` in the relevant agent factory.
3. Add few-shot examples to the agent's prompt file in `app/prompts/`.

### Swapping the LLM model

Set `GEMINI_MODEL=gemini-1.5-pro` (or any Vertex AI model ID) in `.env` or Cloud Run env vars.  No code changes needed.

### Editing agent prompts

All system prompts live in `app/prompts/*.md` as plain Markdown files.
Edit them directly — changes take effect on the next server restart.

### Adding a new specialist

1. Create `app/agents/my_specialist.py` following the pattern of `macro_analyst.py`.
2. Add a prompt file `app/prompts/my_specialist.md`.
3. Register the specialist in `FinancialOrchestrator._specialists` in `orchestrator.py`.

---

## Security Notes

- API keys are loaded from environment variables only — never hardcoded.
- The `code_executor` uses `RestrictedPython` to sandbox agent-generated code.
  `os`, `sys`, `subprocess`, `open`, and `importlib` are blocked.
- SEC EDGAR calls include the required `User-Agent` header per SEC policy.
- The standard disclaimer is appended **deterministically** by the orchestrator —
  it is never LLM-generated and cannot be suppressed.
