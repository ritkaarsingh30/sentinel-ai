# Contributing to SentinelAI

## Dev environment

```bash
git clone <repo-url> && cd sentinel-ai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your values
```

There is no formal test suite. The primary validation path is:

```bash
python scripts/test_local.py
```

This runs the full agent graph with mock CloudWatch data — no AWS credentials required. All agents must complete without errors before opening a PR.

## Making changes

**Agent logic** lives in `agents/graph.py`. Each node receives the full `IncidentState` and returns only the fields it modifies. The supervisor enforces the pipeline order — agents should not assume a specific call sequence.

**Tools** (`tools/`) must respect the `USE_MOCK_DATA` flag. When `true`, return hardcoded data so the graph runs offline. When `false`, make real AWS calls.

**Config** — all env vars go through `config.py`. Never call `os.getenv()` directly in other files.

**Deploy script** (`scripts/deploy.py`) is idempotent — every resource creation is wrapped in a try/except that handles the "already exists" case. Keep it that way.

## Code style

- No comments unless the *why* is non-obvious
- No type annotations beyond what `state.py` already establishes
- Keep `_call_llm()` calls to the minimum needed — Groq free tier is 12,000 TPM and 5 agents can approach that

## PR checklist

- [ ] `python scripts/test_local.py` passes end-to-end
- [ ] No new `os.getenv()` calls outside `config.py`
- [ ] `USE_MOCK_DATA=true` path works without AWS credentials
- [ ] `scripts/deploy.py` is still idempotent (re-running does not error)
- [ ] No week numbers or in-progress task references left in code or docstrings
