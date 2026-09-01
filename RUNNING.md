# Running the dashboard

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (package/environment manager)

Install uv (once), then reopen the terminal:

```bash
# option A — official installer (fastest)
curl -LsSf https://astral.sh/uv/install.sh | sh

# option B — Homebrew
brew install uv

uv --version   # confirm it's installed
```

## Setup

From the project root (where `pyproject.toml` is):

```bash
uv sync
```

This creates `.venv`, installs the dependencies (streamlit, pandas, httpx, …) and
generates `uv.lock`.

## Configuration (Sentry — needed once sentry.py is wired)

The Sentry API token is read from the environment (never hard-coded). Set it before
running, e.g.:

```bash
export SENTRY_TOKEN="your_token_here"
export SENTRY_ORG="sentry"
export SENTRY_PROJECT="4"
export SENTRY_BASE_URL="https://sentry02.aptoide.com"
```

(or put them in a local `.env` file that `pydantic-settings` reads — never commit it)

## Run

```bash
uv run streamlit run src/error_dashboard/app.py
```

Opens at **http://localhost:8501**. Stop with **Ctrl+C**.

## Handy

```bash
uv add <package>       # add a new dependency
uv run python -c "..." # run Python in the project env
```

In the running app (top-right ⋮ menu): **Rerun** (R), **Clear cache** (C).
After editing a file, save it and Streamlit prompts to **Rerun**.


streamlit run src/error_dashboard/app.py