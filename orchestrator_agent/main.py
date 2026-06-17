"""
Helpdesk Orchestrator Agent — Foundry Hosted Agent
====================================================
Receives a user's IT issue and drives the full resolution workflow by calling
the helpdesk specialist agent (helpdesk-langgraph-agent) via A2A as a tool.

The LLM decides how many turns are needed — no hardcoded turn count. Works for
2-step flows today and 10+ step flows as the app grows.

Local test:
    python main.py
    curl -X POST http://localhost:8088/responses \
         -H "Content-Type: application/json" \
         -d '{"input": "Hi, I am alice@corp.com. My VPN keeps disconnecting.", "stream": false}'

Deploy:
    python deploy.py
"""
import asyncio
import json
import logging
import os
import time

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from openai import OpenAI
from azure.identity import DefaultAzureCredential

from langchain_azure_ai.agents.hosting._responses_host import ResponsesHostServer

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _load_kv_secrets() -> None:
    """Pull secrets from Azure Key Vault into os.environ when KEY_VAULT_URL is set."""
    kv_url = os.environ.get("KEY_VAULT_URL")
    if not kv_url:
        return
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=kv_url, credential=DefaultAzureCredential())
        mapping = {
            "AZURE_AI_PROJECT_KEY": "foundry-api-key",
        }
        for env_var, secret_name in mapping.items():
            try:
                os.environ[env_var] = client.get_secret(secret_name).value
                log.info("Loaded secret %s from Key Vault", secret_name)
            except Exception as e:
                log.warning("Could not load KV secret %s: %s", secret_name, e)
    except Exception as e:
        log.warning("Key Vault unavailable, using env vars: %s", e)


_load_kv_secrets()

# ── Environment ────────────────────────────────────────────────────────────────
FOUNDRY_ENDPOINT = (
    os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    or os.environ["FOUNDRY_PROJECT_ENDPOINT"]
)
MODEL_NAME              = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")
HELPDESK_AGENT_ENDPOINT = os.environ["HELPDESK_AGENT_ENDPOINT"]
FOUNDRY_API_KEY         = os.environ.get("AZURE_AI_PROJECT_KEY", "")
PORT                    = int(os.environ.get("PORT", "8088"))

ORCHESTRATOR_PROMPT = """You are a helpdesk session manager responsible for resolving IT issues end-to-end.

You have one tool: helpdesk_agent — an IT helpdesk specialist who creates tickets, diagnoses
issues, looks up assets, assigns work, and closes tickets.

For every user request follow this workflow:
1. Call helpdesk_agent with the user's issue to open a ticket and start diagnosis.
2. Make additional calls as needed — gather more info, run diagnostics, look up assets.
3. When the issue is understood, call helpdesk_agent to close the ticket with a resolution.
4. Call helpdesk_agent to send a notification email to the user.
5. Return a concise final summary: ticket ID, what was done, and resolution.

IMPORTANT — conversation continuity:
- The helpdesk_agent tool returns a JSON object with "reply" and "conversation_id".
- Always pass the conversation_id from the previous response into the next call.
- Leave conversation_id empty only on the very first call for a new issue.

Drive every issue to full resolution. Do not stop until the ticket is closed and the user is notified."""


# ── A2A client — calls the helpdesk specialist agent ──────────────────────────

def _make_a2a_client() -> OpenAI:
    base = HELPDESK_AGENT_ENDPOINT.rstrip("/")
    if "localhost" in base:
        return OpenAI(api_key="local", base_url=base)

    base_url = base + "/protocols/openai"
    try:
        token = DefaultAzureCredential().get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token
    except Exception:
        token = FOUNDRY_API_KEY

    return OpenAI(
        api_key=token,
        base_url=base_url,
        default_headers={
            "api-key": FOUNDRY_API_KEY,
            "responses-protocol-version": "1.0.0",
        },
        default_query={"api-version": "v1"},
    )


def _extract_text(output: list) -> str:
    parts = []
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "\n".join(parts)


# ── Helpdesk agent as a LangChain tool ────────────────────────────────────────

@tool
def helpdesk_agent(message: str, conversation_id: str = "") -> str:
    """
    Call the IT helpdesk specialist agent.

    Args:
        message: The message to send — user issue, follow-up question, or
                 instruction to close/notify.
        conversation_id: The conversation_id returned by the previous call.
                         Pass this on every call after the first to maintain
                         conversation continuity with the specialist.

    Returns:
        JSON string with two fields:
          "reply"           — the specialist's response text
          "conversation_id" — pass this into the next call to continue the thread
    """
    client = _make_a2a_client()
    kwargs: dict = {"model": "", "input": message}
    if conversation_id:
        kwargs["previous_response_id"] = conversation_id

    for attempt in range(5):
        try:
            resp = client.responses.create(**kwargs)
            reply = (
                getattr(resp, "output_text", "")
                or _extract_text(getattr(resp, "output", []))
            )
            rid = getattr(resp, "id", "") or getattr(resp, "response_id", "")
            return json.dumps({"reply": reply, "conversation_id": rid})
        except Exception as e:
            if "session_not_ready" in str(e) and attempt < 4:
                wait = 20 * (attempt + 1)
                log.info(
                    "Helpdesk agent cold-starting — retrying in %ds (attempt %d/5)",
                    wait, attempt + 1,
                )
                time.sleep(wait)
            else:
                log.error("helpdesk_agent tool error: %s", e)
                return json.dumps({"error": str(e), "conversation_id": ""})


# ── LangGraph orchestrator graph ───────────────────────────────────────────────

def _make_model() -> ChatOpenAI:
    base_url = FOUNDRY_ENDPOINT.rstrip("/") + "/openai/v1"
    api_key  = os.environ.get("AZURE_AI_PROJECT_KEY", "placeholder")
    return ChatOpenAI(model=MODEL_NAME, base_url=base_url, api_key=api_key, temperature=0.2)


def _make_graph():
    return create_react_agent(
        _make_model(),
        tools=[helpdesk_agent],
        prompt=ORCHESTRATOR_PROMPT,
        checkpointer=MemorySaver(),
    )


# ── Server startup ─────────────────────────────────────────────────────────────

async def main_async():
    app_insights = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")

    log.info("Building orchestrator graph (model=%s)...", MODEL_NAME)
    graph = _make_graph()

    server = ResponsesHostServer(
        graph,
        applicationinsights_connection_string=app_insights,
    )

    log.info(
        "Orchestrator server starting on port %d — helpdesk endpoint: %s",
        PORT, HELPDESK_AGENT_ENDPOINT,
    )
    await server.run_async(port=PORT)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
