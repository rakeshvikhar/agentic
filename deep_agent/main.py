"""
Deep Agent — Azure AI Foundry Hosted Agent
==========================================
A self-contained LangGraph ReAct agent with built-in tools.
No external MCP dependency — deploy independently to Foundry.

Built-in tools:
  • calculator        — evaluate math expressions safely
  • get_current_date  — return today's date/time (UTC)
  • word_count        — count words/chars in a text
  • summarize_request — structured summary of a multi-part user request

Local test:
    python main.py
    curl -X POST http://localhost:8089/responses \
         -H "Content-Type: application/json" \
         -d '{"input": "What is 42 * 17 and today date?", "stream": false}'

Deploy to Foundry:
    python deploy.py
"""

import asyncio
import logging
import math
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from langchain_azure_ai.agents.hosting._responses_host import ResponsesHostServer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def _load_kv_secret() -> None:
    """Pull AZURE_OPENAI_API_KEY from Azure Key Vault when KEY_VAULT_URL is set.

    Uses DefaultAzureCredential, which resolves to the hosted agent's
    system-assigned managed identity when running in Foundry — no key
    ever needs to live in .env or an environment variable in the portal.
    """
    kv_url = os.environ.get("KEY_VAULT_URL")
    if not kv_url or os.environ.get("AZURE_OPENAI_API_KEY"):
        return
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=kv_url, credential=DefaultAzureCredential())
        secret_name = os.environ.get("KEY_VAULT_SECRET_NAME", "azure-openai54-api-key")
        os.environ["AZURE_OPENAI_API_KEY"] = client.get_secret(secret_name).value
        log.info("Loaded AZURE_OPENAI_API_KEY from Key Vault secret '%s'", secret_name)
    except Exception as e:
        log.warning("Key Vault unavailable, falling back to env var: %s", e)


_load_kv_secret()

# ── Config ────────────────────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")
AZURE_OPENAI_API_KEY = os.environ["AZURE_OPENAI_API_KEY"]   # set directly or via Key Vault
PORT = int(os.environ.get("PORT", "8089"))

SYSTEM_PROMPT = """You are a Deep Reasoning Assistant. You think step-by-step before answering.

You have access to the following tools:
- calculator: evaluate mathematical expressions
- get_current_date: get the current UTC date and time
- word_count: count words and characters in a block of text
- summarize_request: produce a structured breakdown of a complex user request

Always use the most appropriate tool(s) before forming your final answer.
Show your reasoning clearly. If a question has multiple parts, address each part separately."""


# ── Built-in Tools ────────────────────────────────────────────────────────────

@tool
def calculator(expression: str) -> str:
    """
    Evaluate a mathematical expression and return the result.
    Supports: +, -, *, /, **, sqrt, abs, round, floor, ceil, log, sin, cos, tan, pi, e.
    Example: '42 * 17', 'sqrt(144)', 'round(3.14159, 2)'
    """
    safe_globals = {
        "__builtins__": {},
        "abs": abs,
        "round": round,
        "sqrt": math.sqrt,
        "floor": math.floor,
        "ceil": math.ceil,
        "log": math.log,
        "log10": math.log10,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "pi": math.pi,
        "e": math.e,
        "pow": pow,
    }
    try:
        result = eval(expression.strip(), safe_globals, {})   # noqa: S307 — restricted namespace
        return f"{expression} = {result}"
    except Exception as exc:
        return f"Error evaluating '{expression}': {exc}"


@tool
def get_current_date() -> str:
    """Return the current UTC date and time in ISO-8601 format."""
    now = datetime.now(timezone.utc)
    return now.strftime("Today is %A, %Y-%m-%d. Current UTC time: %H:%M:%S.")


@tool
def word_count(text: str) -> str:
    """
    Count the number of words and characters in the provided text.
    Returns word count, character count (with spaces), and character count (without spaces).
    """
    words = text.split()
    chars_with_spaces = len(text)
    chars_no_spaces = len(text.replace(" ", ""))
    return (
        f"Words: {len(words)} | "
        f"Characters (with spaces): {chars_with_spaces} | "
        f"Characters (no spaces): {chars_no_spaces}"
    )


@tool
def summarize_request(user_request: str) -> str:
    """
    Produce a structured breakdown of a complex user request into distinct sub-tasks.
    Use this when the user asks multiple things at once or the request is ambiguous.
    Returns a numbered list of identified sub-tasks.
    """
    sentences = [s.strip() for s in user_request.replace("?", "?.").split(".") if s.strip()]
    if not sentences:
        return "No distinct sub-tasks identified."
    parts = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(sentences))
    return f"Identified {len(sentences)} sub-task(s):\n{parts}"


TOOLS = [calculator, get_current_date, word_count, summarize_request]


# ── Model & Graph ─────────────────────────────────────────────────────────────

def _make_model() -> ChatOpenAI:
    return ChatOpenAI(
        model=AZURE_OPENAI_DEPLOYMENT,
        base_url=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_API_KEY,
        temperature=0.1,
    )


def _make_graph():
    return create_react_agent(
        _make_model(),
        tools=TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )


# ── Server ────────────────────────────────────────────────────────────────────

async def main_async():
    log.info("Building Deep Agent graph with %d built-in tools...", len(TOOLS))
    graph = _make_graph()

    app_insights = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    server = ResponsesHostServer(
        graph,
        applicationinsights_connection_string=app_insights,
    )

    log.info("Starting Deep Agent server on port %d", PORT)
    await server.run_async(port=PORT)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
