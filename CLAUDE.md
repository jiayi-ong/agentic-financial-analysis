[Task overview]
You are an expert Python programmer, software developer, computer scientist, and AI engineer. You will build an efficient, production-grade, scalable multi-agent system in Python that performs preliminary analysis on one single technology stock at a time. The system will collect data, apply data preprocessing and exploratory analysis, and communicate an investment-related hypothesis backed by evidence.

[Feature 1 - Data collection]
- Agents can only choose to get data from pre-defined sources below, but make the design extensible to more sources in the future
- source 1 (web crawling): search a few reputable news websites for latest news and sentiment (use web-crawling Python tools, not APIs)
- source 2 (public API - based): Yahoo finance API to get numeric, structured data. SEC API to get textual (but still well-formated) financial statements
- pre-define standard code for the sources, but agent use them as tools with dynamic values to arguments
- for each source, add few-shot examples of how the API/tool is used for in-context learning, to be appended to agent system prompt
- which stock to search data for, and which source to use, is dynamic at runtime and dependent on user request (e.g. which ticker, which date range, which filings, which news pages)

[Feature 2 - Preprocessing]
- Fixed, pre-written, generic data processing functions for categories of data sources and data type or structure: e.g. text cleaning tools for ingested textual data
- well-defined error handling and signalling, for agent to re-try data collection tool use if needed

[Feature 3 - Exploratory data analysis]
- quantitative analysis agents write Python code on-the-go to analyze the ingested structured data files. E.g. perform discounted cashflow analysis, compute financial metrics, generate graphs and tables
- qualitative agents analyze and summarize text data, extract key insights
- synthesis agent puts them together into a coherent and concise result and draw overall conclusion, not just a summary, but insights derived from the analyses, citing the specialist results and references (links that the user can click to manually verify on data sources)
- final output to user: evidence-backed analytical hypothesis about company performance and growth drivers
- must avoid giving investment advice like how to trade stocks (e.g. which stock to buy)

[Agentic design]
- Agentic design pattern: router + agent-as-a-tool architecture, with a light critique layer before final output to user
- Have specialist agents for different tasks: (1) economy-wide and geopolitical analysis (2) firm-level technical analysis of financial statements, valuation (3) latest news, investor and market sentiment analysis (4) external analyst rating and outlook summary
- specialists must provide references to all sources used
- A synthesis agent to combine all specialist output to produce an overall conclusion, retaining references
- each specialist agent returns claims, evidence snippets, source URL or filing identifier, extraction timestamp, and confidence, to aid synthesis
- A single critique agent evaluating result and producing typed output: severity of issue, category of issue, quotes and tags to identify the issue in the result
- Orchestrator controls the general agentic workflow: Analyze → Critique → Triage → Targeted rerun → Re-synthesize → Re-critique → Final / Abstain. Limit total re-tries.
- Allow for partial failures and corrections: if issue is found only in a subset of specialist agents, re-run those only and not the whole pipeline. If a subset cannot produce a good result within limits, omit from final result but clearly stating the issue to user.
- store system prompts for each agent in isolated files so a human expert can review and edit

[Frontend]
- HTML webpage with multiple tabs
- page 1 (main): standard conversation page with agent - one stock ticker input text box at the top, agent output history on the left, user on the right, scrollable history, auto scroll to latest, input text box at the bottom, title and description at top, display current processing step (e.g. which agent is working, what tool is being called)
- page 1: simple stock ticker check and output to user if invalid stock ticker is given. User must input stock name or ticker and press 'start conversation'. Check with deterministic program against a database with string matching is done, if input invalid, output a standard message.
- page 2: scrollable history of key intermediate reasoning steps and tool outputs (still user-facing)
- Will be deployed onto Google's Cloud Run

[Other important requirements]
- For each specified data source, you need to lookup the API usage and output structure to write the correct ingestion and processing code
- Adopt programming best practices and software design principles that are most-suited to this use case to write efficient, production-grade, and scalable code
- Adopt industry-standards for system configuration, session management, schema enforcement, LLM tracing, temporary data files management, data contract between modules etc.
- Focus on building a working end-to-end system first, omit fine-tuning and tool-use training utilities for now but keep that area extensible
- Add utilities or features within code or prompt as necessary to minimize hallucination, use the latest data, give clear lines of reasoning behind every conclusion
- Error handling: allow for multiple tool calls on failures, propagate failure message to agent so that it learns in context. This is useful since we are not fine-tuning agents for tool use. Might need a tool wrapper for this to add the error and success feedback
- out-of-scope handling: simple prompting mechanism to reply with standard response for user requests that are not related to financial analysis, cover multiple stocks and not one
- Always add a deterministic disclaimer about not being a certified financial professional and anything generated are for reference only
- Primary toolkits: Google's ADK and Vertex AI SDK
- add a README.md that includes the high-level design, main agents and workflow, and key components, and instructions on how to run it locally.
- use uv for package management
- handle API keys securely


NOTE: Always mention "CLAUDE_MD_LOADED" in your first response