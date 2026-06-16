# Multi-Agent IT Helpdesk — Solution Blueprint

> **Purpose**: Hand this file to Claude or any AI code assistant to recreate the full solution from scratch.  
> All keys, connection strings, and URLs are placeholders — replace with your own values.

---

## 1. What This Builds

A **multi-agent IT helpdesk system** on Azure AI Foundry using:

| Pattern | Implementation |
|---|---|
| Orchestrator → Agent (A2A) | Local Python orchestrator calls a Foundry-hosted agent over the Responses protocol |
| Agent → Tools (MCP) | Hosted LangGraph agent calls an Azure Function App that implements MCP (JSON-RPC over SSE) |
| Tool persistence | Azure Table Storage (tickets, events, assets tables) |
| LLM | `gpt-5.4` deployed in Azure AI Foundry (East US 2, Global Standard) |
| Agent framework | LangGraph ReAct (`create_react_agent`) wrapped by `ResponsesHostServer` |

---

## 2. Azure Components to Create

Create all resources in **East US 2** (Hosted Agents are only available in East US 2 and a few other regions — verify at https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents).

### 2.1 Azure AI Foundry Project

1. Go to https://ai.azure.com → **New project**
2. Region: **East US 2**
3. Note the **project endpoint** (format: `https://<hub>.services.ai.azure.com/api/projects/<project>`)
4. Note the **API key** from the project settings
5. Deploy model **`gpt-5.4`** (Global Standard deployment) inside the project

### 2.2 Azure Storage Account (for MCP tool data)

```bash
# Resource group — add required tags per your org policy
az group create \
  --name rg-aifoundry-eus2 \
  --location eastus2 \
  --tags createdBy=<your-alias> Project=AIFoundry

# Storage account
az storage account create \
  --name <storage-account-name> \
  --resource-group rg-aifoundry-eus2 \
  --location eastus2 \
  --sku Standard_LRS \
  --kind StorageV2
```

Tables are created automatically by `ticket_store.py` on first run: `tickets`, `events`, `assets`.

### 2.3 Azure Function App (MCP Server)

```bash
# Function App (Python 3.11, Linux, Consumption plan)
az functionapp create \
  --resource-group rg-aifoundry-eus2 \
  --consumption-plan-location eastus2 \
  --runtime python \
  --runtime-version 3.11 \
  --functions-version 4 \
  --name <function-app-name> \
  --storage-account <storage-account-name> \
  --os-type Linux
```

After deploying the code (section 5.3), set the storage connection string so tools persist data:

```bash
CONN_STR=$(az storage account show-connection-string \
  --name <storage-account-name> \
  --resource-group rg-aifoundry-eus2 \
  --query connectionString -o tsv)

az functionapp config appsettings set \
  --name <function-app-name> \
  --resource-group rg-aifoundry-eus2 \
  --settings AZURE_STORAGE_CONNECTION_STRING="$CONN_STR"
```

Get the function key after deployment:

```bash
az functionapp keys list \
  --resource-group rg-aifoundry-eus2 \
  --name <function-app-name> \
  --query "functionKeys.default" -o tsv
```

### 2.4 Foundry Tool Registration (optional UI step)

In the Azure AI Foundry project UI → **Tools** → **Add tool** → select the Function App (`<function-app-name>`). This registers it under name `helpdesk-mcp-tools-eus2` so it can be referenced from prompt agents in the UI. The hosted LangGraph agent calls the MCP URL directly, so this step is optional for the code path.

---

## 3. Local Environment Setup

### 3.1 Conda Environment

```bash
conda create -n foundry_env python=3.12
conda activate foundry_env
```

> Python 3.12 locally; the **remote container** runs Python 3.13 (only `python_3_13` and `python_3_14` are supported runtimes for Hosted Agents).

### 3.2 pip Install

```bash
pip install \
  azure-ai-projects==2.2.0 \
  azure-identity \
  azure-data-tables \
  openai \
  langchain-azure-ai==1.2.6 \
  langchain-mcp-adapters==0.3.0 \
  langchain-openai==1.3.2 \
  "langchain>=0.3.0" \
  langgraph==1.2.5 \
  "mcp==1.27.2" \
  python-dotenv \
  "httpx>=0.27.0" \
  azure-ai-agentserver-core==2.0.0b6 \
  azure-ai-agentserver-invocations==1.0.0b5 \
  azure-ai-agentserver-responses==1.0.0b7 \
  azure-functions
```

> **Critical**: `azure-ai-agentserver-{core,invocations,responses}` are pre-release packages required by `langchain-azure-ai`'s `ResponsesHostServer`. They are not pulled in automatically — you must list them explicitly in both the local install and `hosted_agent/requirements.txt`.

### 3.3 Azure CLI Login

```bash
az login
# Verify correct subscription
az account show
```

`DefaultAzureCredential` (used by the deploy script and orchestrator) reads from this login cache.

---

## 4. Project Layout

```
aifoundry/
├── .env                          # local secrets (never commit)
├── agents/
│   └── auth.py                   # AIProjectClient + OpenAI client helpers
├── hosted_agent/
│   ├── main.py                   # LangGraph agent (deployed to Foundry)
│   ├── requirements.txt          # remote container pip dependencies
│   ├── deploy.py                 # deploys main.py to Foundry Hosted Agent
│   └── orchestrator.py           # local orchestrator — calls hosted agent via A2A
└── mcp_server/
    ├── function_app.py           # Azure Function — MCP server
    ├── ticket_store.py           # Table Storage / in-memory ticket CRUD
    ├── host.json                 # Azure Functions host config
    └── requirements.txt          # function app pip dependencies
```

---

## 5. Code Files

### 5.1 `.env` (local secrets — never commit)

```ini
# Azure AI Foundry project
AZURE_AI_PROJECT_ENDPOINT=https://<hub>.services.ai.azure.com/api/projects/<project>
AZURE_AI_PROJECT_KEY=<your-project-api-key>
AZURE_OPENAI_MODEL=gpt-5.4

# Aliases used internally
FOUNDRY_PROJECT_ENDPOINT=https://<hub>.services.ai.azure.com/api/projects/<project>
AZURE_AI_MODEL_DEPLOYMENT_NAME=gpt-5.4

# MCP server (East US 2 Function App)
MCP_SERVER_URL=https://<function-app-name>.azurewebsites.net/api/mcp
MCP_SERVER_KEY=<function-app-host-key>

# Hosted agent endpoint (auto-appended by deploy.py after first deploy)
HELPDESK_AGENT_ENDPOINT=https://<hub>.services.ai.azure.com/api/projects/<project>/agents/helpdesk-langgraph-agent/endpoint
```

---

### 5.2 `agents/auth.py`

```python
"""
Authentication helpers for azure-ai-projects v2.2.0.

Uses DefaultAzureCredential (Entra ID) for AIProjectClient — required for
Hosted Agent deployment APIs which reject API key auth.
Uses API key for the OpenAI inference endpoint.
"""
import os
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from openai import OpenAI


def get_project_client() -> AIProjectClient:
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    return AIProjectClient(
        endpoint=endpoint,
        credential=DefaultAzureCredential(),
        allow_preview=True,
    )


def get_openai_client(project_client: AIProjectClient | None = None) -> OpenAI:
    """Return an OpenAI client pointed at the AI Foundry project endpoint."""
    api_key = os.environ["AZURE_AI_PROJECT_KEY"]
    if project_client is not None:
        return project_client.get_openai_client(api_key=api_key)
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    base_url = endpoint.rstrip("/") + "/openai/v1"
    return OpenAI(api_key=api_key, base_url=base_url)
```

---

### 5.3 `mcp_server/requirements.txt`

```
azure-functions
azure-data-tables>=12.4.0
```

### 5.4 `mcp_server/host.json`

```json
{
  "version": "2.0",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true
      }
    }
  }
}
```

### 5.5 `mcp_server/ticket_store.py`

```python
"""
Ticket and asset store.

When AZURE_STORAGE_CONNECTION_STRING is set (deployed Function App), data is
persisted in Azure Table Storage. Otherwise falls back to in-memory dicts
(local dev / test).
"""
import os
import uuid
import datetime

_USE_AZURE = bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))

if _USE_AZURE:
    from azure.data.tables import TableServiceClient, UpdateMode

    _svc = TableServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    )
    for _t in ("tickets", "events", "assets"):
        try:
            _svc.create_table(_t)
        except Exception:
            pass

    _tickets_tbl = _svc.get_table_client("tickets")
    _events_tbl  = _svc.get_table_client("events")
    _assets_tbl  = _svc.get_table_client("assets")

    _SEED_ASSETS = {
        "A001": {"name": "Dell XPS Laptop",    "user": "alice@corp.com", "status": "in-use"},
        "A002": {"name": "USB-C Hub",           "user": "bob@corp.com",   "status": "in-use"},
        "A003": {"name": 'Monitor 27"',         "user": None,             "status": "available"},
        "A004": {"name": "Mechanical Keyboard", "user": None,             "status": "available"},
    }
    for _aid, _aval in _SEED_ASSETS.items():
        try:
            _assets_tbl.get_entity("assets", _aid)
        except Exception:
            _assets_tbl.create_entity({
                "PartitionKey": "assets",
                "RowKey": _aid,
                **{k: (v or "") for k, v in _aval.items()},
            })
else:
    _tickets: dict = {}
    _events:  dict = {}
    _assets: dict = {
        "A001": {"name": "Dell XPS Laptop",    "user": "alice@corp.com", "status": "in-use"},
        "A002": {"name": "USB-C Hub",           "user": "bob@corp.com",   "status": "in-use"},
        "A003": {"name": 'Monitor 27"',         "user": None,             "status": "available"},
        "A004": {"name": "Mechanical Keyboard", "user": None,             "status": "available"},
    }


def create_ticket(title: str, description: str, priority: str) -> dict:
    tid = f"TKT-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "id": tid, "title": title, "description": description,
        "priority": priority, "status": "open",
        "created_at": datetime.datetime.utcnow().isoformat(),
        "assigned_to": None, "resolution": None,
    }
    if _USE_AZURE:
        _tickets_tbl.create_entity({
            "PartitionKey": "tickets", "RowKey": tid,
            **{k: (v or "") for k, v in record.items()},
        })
    else:
        _tickets[tid] = record
    return record


def get_ticket_status(ticket_id: str) -> dict:
    if _USE_AZURE:
        try:
            e = _tickets_tbl.get_entity("tickets", ticket_id)
            return {k: (v if v != "" else None) for k, v in e.items()
                    if k not in ("PartitionKey", "RowKey", "etag", "Timestamp")}
        except Exception:
            return {"error": f"Ticket {ticket_id} not found"}
    return _tickets.get(ticket_id, {"error": f"Ticket {ticket_id} not found"})


def get_asset_info(asset_id: str) -> dict:
    if _USE_AZURE:
        try:
            e = _assets_tbl.get_entity("assets", asset_id)
            return {k: (v if v != "" else None) for k, v in e.items()
                    if k not in ("PartitionKey", "RowKey", "etag", "Timestamp")}
        except Exception:
            return {"error": f"Asset {asset_id} not found"}
    return _assets.get(asset_id, {"error": f"Asset {asset_id} not found"})


def assign_ticket(ticket_id: str, agent_name: str) -> dict:
    if _USE_AZURE:
        try:
            e = _tickets_tbl.get_entity("tickets", ticket_id)
            e["assigned_to"] = agent_name
            e["status"] = "in-progress"
            _tickets_tbl.update_entity(e, mode=UpdateMode.REPLACE)
            return get_ticket_status(ticket_id)
        except Exception:
            return {"error": f"Ticket {ticket_id} not found"}
    if ticket_id not in _tickets:
        return {"error": f"Ticket {ticket_id} not found"}
    _tickets[ticket_id].update({"assigned_to": agent_name, "status": "in-progress"})
    return _tickets[ticket_id]


def close_ticket(ticket_id: str, resolution: str) -> dict:
    if _USE_AZURE:
        try:
            e = _tickets_tbl.get_entity("tickets", ticket_id)
            e["status"] = "closed"
            e["resolution"] = resolution
            _tickets_tbl.update_entity(e, mode=UpdateMode.REPLACE)
            return get_ticket_status(ticket_id)
        except Exception:
            return {"error": f"Ticket {ticket_id} not found"}
    if ticket_id not in _tickets:
        return {"error": f"Ticket {ticket_id} not found"}
    _tickets[ticket_id].update({"status": "closed", "resolution": resolution})
    return _tickets[ticket_id]


def log_event(ticket_id: str, event: str) -> dict:
    ts = datetime.datetime.utcnow().isoformat()
    entry = {"timestamp": ts, "event": event}
    if _USE_AZURE:
        row_key = f"{ticket_id}_{ts.replace(':', '-')}"
        _events_tbl.create_entity({
            "PartitionKey": ticket_id, "RowKey": row_key,
            "event": event, "timestamp": ts,
        })
    else:
        _events.setdefault(ticket_id, []).append(entry)
    return {"ticket_id": ticket_id, "logged": entry}


def send_notification(recipient: str, subject: str, message: str) -> dict:
    # Production: replace with Azure Communication Services or SendGrid
    print(f"  [NOTIFY] To={recipient} | Subject={subject}")
    print(f"           {message}")
    return {"status": "sent", "recipient": recipient, "subject": subject}


def list_all_tickets() -> dict:
    if _USE_AZURE:
        tickets = [
            {k: (v if v != "" else None) for k, v in e.items()
             if k not in ("PartitionKey", "RowKey", "etag", "Timestamp")}
            for e in _tickets_tbl.list_entities()
        ]
        return {"tickets": tickets, "total": len(tickets)}
    return {"tickets": list(_tickets.values()), "total": len(_tickets)}
```

### 5.6 `mcp_server/function_app.py`

```python
"""
Azure Function App — MCP server for IT Helpdesk tools.

Implements the MCP "Streamable HTTP" transport:
  POST /api/mcp  →  JSON-RPC request body  →  text/event-stream SSE response

Deploy:
  az functionapp deployment source config-zip \
    --resource-group <rg> --name <app> --src mcp_server.zip
"""
import json
import azure.functions as func
from ticket_store import (
    create_ticket, get_ticket_status, get_asset_info,
    assign_ticket, close_ticket, log_event,
    send_notification, list_all_tickets,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

TOOL_DEFINITIONS = [
    {
        "name": "create_ticket",
        "description": "Create a new IT helpdesk ticket",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Short issue title"},
                "description": {"type": "string", "description": "Full description"},
                "priority":    {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": ["title", "description", "priority"],
        },
    },
    {
        "name": "get_ticket_status",
        "description": "Get current status and details of a ticket",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    },
    {
        "name": "get_asset_info",
        "description": "Look up an IT asset by its ID (A001, A002, A003, A004)",
        "inputSchema": {
            "type": "object",
            "properties": {"asset_id": {"type": "string"}},
            "required": ["asset_id"],
        },
    },
    {
        "name": "assign_ticket",
        "description": "Assign a ticket to a support agent",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id":  {"type": "string"},
                "agent_name": {"type": "string"},
            },
            "required": ["ticket_id", "agent_name"],
        },
    },
    {
        "name": "close_ticket",
        "description": "Close a ticket with a resolution note",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id":  {"type": "string"},
                "resolution": {"type": "string"},
            },
            "required": ["ticket_id", "resolution"],
        },
    },
    {
        "name": "log_event",
        "description": "Log a diagnostic step or note to a ticket",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "event":     {"type": "string"},
            },
            "required": ["ticket_id", "event"],
        },
    },
    {
        "name": "send_notification",
        "description": "Send an email notification to a user about their ticket",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string"},
                "subject":   {"type": "string"},
                "message":   {"type": "string"},
            },
            "required": ["recipient", "subject", "message"],
        },
    },
    {
        "name": "list_all_tickets",
        "description": "List all tickets in the system",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

DISPATCH = {
    "create_ticket":     create_ticket,
    "get_ticket_status": get_ticket_status,
    "get_asset_info":    get_asset_info,
    "assign_ticket":     assign_ticket,
    "close_ticket":      close_ticket,
    "log_event":         log_event,
    "send_notification": send_notification,
    "list_all_tickets":  list_all_tickets,
}


def _handle_one(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    def ok(result):
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "helpdesk-tools", "version": "1.0.0"},
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return ok({"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        fn = DISPATCH.get(name)
        if not fn:
            return err(-32601, f"Unknown tool: {name}")
        try:
            result = fn(**args)
            return ok({"content": [{"type": "text", "text": json.dumps(result)}]})
        except TypeError as e:
            return err(-32602, f"Bad arguments for {name}: {e}")

    return err(-32601, f"Unsupported method: {method}")


def _to_sse(responses: list[dict]) -> str:
    return "".join(f"data: {json.dumps(r)}\n\n" for r in responses)


@app.route(route="mcp", methods=["POST"])
def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = json.loads(req.get_body().decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid JSON: {e}"}),
            status_code=400, mimetype="application/json",
        )

    messages = body if isinstance(body, list) else [body]
    responses = [r for msg in messages if (r := _handle_one(msg)) is not None]

    if not responses:
        return func.HttpResponse("", status_code=202, mimetype="text/event-stream")

    return func.HttpResponse(
        _to_sse(responses),
        status_code=200,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
```

---

### 5.7 `hosted_agent/requirements.txt`

> This file is zipped and uploaded to Foundry. The remote build installs these into the Python 3.13 container.

```
langchain-azure-ai==1.2.6
langchain-mcp-adapters==0.3.0
langchain-openai==1.3.2
langchain>=0.3.0
langgraph==1.2.5
mcp==1.27.2
python-dotenv>=1.0.0
httpx>=0.27.0
azure-ai-agentserver-core==2.0.0b6
azure-ai-agentserver-invocations==1.0.0b5
azure-ai-agentserver-responses==1.0.0b7
```

### 5.8 `hosted_agent/main.py`

```python
"""
Helpdesk Hosted Agent — LangGraph + MCP
========================================
Deployed to Azure AI Foundry as a Hosted Agent.
Exposes POST /responses via ResponsesHostServer (Responses protocol v1.0.0).

The server starts immediately with a placeholder graph (fast /readiness),
then swaps in the real MCP-equipped graph in a background task. This avoids
session_not_ready errors caused by slow cold-start if MCP loading blocks startup.

Local test:
    python main.py
    curl -X POST http://localhost:8088/responses \
         -H "Content-Type: application/json" \
         -d '{"input": "My VPN is down", "stream": false}'

Deploy:
    python deploy.py
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_azure_ai.agents.hosting._responses_host import ResponsesHostServer

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Environment variables injected by deploy.py into the container
FOUNDRY_ENDPOINT = (
    os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    or os.environ["FOUNDRY_PROJECT_ENDPOINT"]   # raises clearly if both missing
)
MODEL_NAME     = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4")
MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
MCP_SERVER_KEY = os.environ.get("MCP_SERVER_KEY", "")
PORT           = int(os.environ.get("PORT", "8088"))

SYSTEM_PROMPT = """You are an IT Helpdesk Assistant with access to a ticket management system.

You can:
- Create tickets for reported issues (use create_ticket)
- Check ticket status (use get_ticket_status)
- Look up IT assets like laptops, monitors (use get_asset_info — IDs: A001-A004)
- Assign tickets to specialist teams (use assign_ticket)
- Log diagnostic steps (use log_event)
- Close resolved tickets (use close_ticket)
- Send email notifications to users (use send_notification)
- List all tickets (use list_all_tickets)

Always create a ticket first, share the ticket ID with the user, then work through the issue.
Be concise, professional, and log every diagnostic action you take."""


def _make_model() -> ChatOpenAI:
    base_url = FOUNDRY_ENDPOINT.rstrip("/") + "/openai/v1"
    api_key  = os.environ.get("AZURE_AI_PROJECT_KEY", "placeholder")
    return ChatOpenAI(model=MODEL_NAME, base_url=base_url, api_key=api_key, temperature=0.2)


def _make_graph(tools: list):
    return create_react_agent(
        _make_model(),
        tools=tools,
        prompt=SYSTEM_PROMPT,
        checkpointer=MemorySaver(),
    )


async def _load_mcp_tools() -> list:
    mcp_headers = {"x-functions-key": MCP_SERVER_KEY} if MCP_SERVER_KEY else {}
    mcp_client = MultiServerMCPClient({
        "helpdesk": {
            "transport": "streamable_http",
            "url":       MCP_SERVER_URL,
            "headers":   mcp_headers or None,
        }
    })
    try:
        tools = await asyncio.wait_for(mcp_client.get_tools(), timeout=30.0)
        log.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
        return tools
    except Exception as exc:
        log.warning("MCP tools unavailable: %s", exc)
        return []


async def main_async():
    app_insights = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")

    # Step 1: start server immediately with placeholder graph — /readiness → 200 fast
    log.info("Building placeholder graph (no MCP tools)...")
    placeholder_graph = _make_graph([])
    server = ResponsesHostServer(
        placeholder_graph,
        applicationinsights_connection_string=app_insights,
    )

    # Step 2: load MCP tools in background, swap graph once ready
    async def upgrade_to_mcp():
        tools = await _load_mcp_tools()
        if tools:
            server._graph = _make_graph(tools)
            log.info("Graph upgraded: %d MCP tools active", len(tools))
        else:
            log.warning("Running without MCP tools.")

    asyncio.create_task(upgrade_to_mcp())

    # Step 3: run server (blocks forever)
    log.info("Starting server on port %d", PORT)
    await server.run_async(port=PORT)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
```

### 5.9 `hosted_agent/deploy.py`

```python
"""
Deploy the Helpdesk Hosted Agent to Azure AI Foundry.

Prerequisites:
  - az login  (DefaultAzureCredential reads from this)
  - .env populated with AZURE_AI_PROJECT_ENDPOINT, AZURE_AI_PROJECT_KEY,
    MCP_SERVER_URL, MCP_SERVER_KEY, AZURE_OPENAI_MODEL

Run:
    python deploy.py           # deploy / update (creates a new version)
    python deploy.py --delete  # remove the agent
"""
import argparse
import hashlib
import os
import pathlib
import sys
import tempfile
import zipfile

from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
from auth import get_project_client

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

AGENT_NAME = "helpdesk-langgraph-agent"
AGENT_DIR  = pathlib.Path(__file__).parent


def get_env_vars() -> dict[str, str]:
    """Environment variables injected into the remote container."""
    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    return {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": os.environ.get("AZURE_OPENAI_MODEL", "gpt-5.4"),
        "MCP_SERVER_URL":                 os.environ["MCP_SERVER_URL"],
        "MCP_SERVER_KEY":                 os.environ.get("MCP_SERVER_KEY", ""),
        "AZURE_AI_PROJECT_KEY":           os.environ["AZURE_AI_PROJECT_KEY"],
        "FOUNDRY_PROJECT_ENDPOINT":       endpoint,
        "AZURE_AI_PROJECT_ENDPOINT":      endpoint,
    }


def create_source_zip() -> tuple[str, str]:
    """Zip main.py + requirements.txt; return (zip_path, sha256_of_zip_bytes)."""
    files = [AGENT_DIR / "main.py", AGENT_DIR / "requirements.txt"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if f.exists():
                data = f.read_bytes()
                zf.writestr(f.name, data)
                print(f"  + {f.name}  ({len(data)/1024:.1f} KB)")
    zip_bytes = pathlib.Path(tmp.name).read_bytes()
    sha256 = hashlib.sha256(zip_bytes).hexdigest()
    print(f"  -> zip: {len(zip_bytes)/1024:.1f} KB  sha256={sha256[:16]}...")
    return tmp.name, sha256


def deploy():
    from azure.ai.projects.models import (
        CreateAgentVersionFromCodeContent,
        CreateAgentVersionFromCodeMetadata,
        HostedAgentDefinition,
        CodeConfiguration,
        CodeDependencyResolution,
        ProtocolVersionRecord,
    )

    client = get_project_client()
    print(f"Deploying '{AGENT_NAME}' to AI Foundry...\n")

    print("Packing source files:")
    zip_path, sha256 = create_source_zip()

    definition = HostedAgentDefinition(
        cpu="1",
        memory="2Gi",
        code_configuration=CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
        ),
        protocol_versions=[
            ProtocolVersionRecord(protocol="responses", version="1.0.0"),
        ],
        environment_variables=get_env_vars(),
    )

    zip_bytes_data = pathlib.Path(zip_path).read_bytes()
    content = CreateAgentVersionFromCodeContent(
        metadata=CreateAgentVersionFromCodeMetadata(
            description="IT Helpdesk agent - LangGraph + MCP + Azure AI Foundry",
            definition=definition,
        ),
        code=("agent.zip", zip_bytes_data, "application/zip"),
    )

    try:
        print("\nUploading to Foundry...")
        result = client.beta.agents.create_version_from_code(
            AGENT_NAME, content, code_zip_sha256=sha256,
        )
        version = getattr(result, "version", "?")
        print(f"\nDeployed successfully!")
        print(f"  Agent   : {AGENT_NAME}")
        print(f"  Version : {version}")

        endpoint_base = os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/")
        agent_endpoint = f"{endpoint_base}/agents/{AGENT_NAME}/endpoint"
        print(f"  Endpoint: {agent_endpoint}")

        env_path = pathlib.Path(__file__).parent.parent / ".env"
        existing = env_path.read_text(encoding="utf-8")
        if "HELPDESK_AGENT_ENDPOINT" not in existing:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nHELPDESK_AGENT_ENDPOINT={agent_endpoint}\n")
            print("  (HELPDESK_AGENT_ENDPOINT appended to .env)")

    except Exception as e:
        print(f"\nDeployment failed: {e}")
    finally:
        pathlib.Path(zip_path).unlink(missing_ok=True)


def delete_agent():
    client = get_project_client()
    try:
        client.agents.delete_version(AGENT_NAME, version="*")
        print(f"Agent '{AGENT_NAME}' deleted.")
    except Exception as e:
        print(f"Delete failed: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    delete_agent() if args.delete else deploy()
```

### 5.10 `hosted_agent/orchestrator.py`

```python
"""
A2A Orchestrator — calls the deployed Helpdesk Hosted Agent via Responses protocol.

The Responses protocol (POST /responses) is the A2A transport in Foundry.
Each hosted agent's endpoint acts as an A2A peer.

Run:
    python orchestrator.py --issue "VPN not working" --email "bob@corp.com"
    python orchestrator.py --issue "Need new monitor" --email "alice@corp.com"

Tip: set PYTHONIOENCODING=utf-8 on Windows to avoid cp1252 errors in replies.
"""
import argparse
import os
import re

from dotenv import load_dotenv
from openai import OpenAI
from azure.identity import DefaultAzureCredential

load_dotenv()

AGENT_ENDPOINT  = os.environ.get("HELPDESK_AGENT_ENDPOINT", "http://localhost:8088")
FOUNDRY_API_KEY = os.environ.get("AZURE_AI_PROJECT_KEY", "")


def _make_openai_client() -> OpenAI:
    """
    Build an OpenAI SDK client pointed at the agent's Responses endpoint.

    Local:    base_url = http://localhost:8088
    Deployed: base_url = .../endpoint/protocols/openai   (Bearer + api-key auth)
    """
    base = AGENT_ENDPOINT.rstrip("/")
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


def call_helpdesk_agent(message: str, thread_id: str | None = None) -> tuple[str, str]:
    """
    Call the hosted helpdesk agent via A2A (Responses protocol).
    Returns (reply_text, response_id).
    Retries up to 5× on session_not_ready (container cold-start).
    """
    import time
    client = _make_openai_client()
    kwargs: dict = {"model": "", "input": message}
    if thread_id:
        kwargs["previous_response_id"] = thread_id

    for attempt in range(5):
        try:
            resp = client.responses.create(**kwargs)
            text = getattr(resp, "output_text", "") or _extract_text(getattr(resp, "output", []))
            rid  = getattr(resp, "id", "") or getattr(resp, "response_id", "")
            return text, rid
        except Exception as e:
            if "session_not_ready" in str(e) and attempt < 4:
                wait = 20 * (attempt + 1)
                print(f"  Container cold-starting — retrying in {wait}s (attempt {attempt+1}/5)...")
                time.sleep(wait)
            else:
                raise


def _extract_text(output: list) -> str:
    parts = []
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "\n".join(parts)


def _extract_ticket_id(text: str) -> str:
    match = re.search(r"TKT-[A-Z0-9]{6}", text)
    return match.group(0) if match else "TKT-UNKNOWN"


def run_helpdesk_session(user_issue: str, user_email: str):
    """
    Two-turn A2A session:
      Turn 1: user reports issue → agent creates ticket, logs, diagnoses
      Turn 2: orchestrator requests close + notification (same thread)
    """
    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  A2A HELPDESK SESSION")
    print(f"  Agent : {AGENT_ENDPOINT}")
    print(f"  User  : {user_email}")
    print(f"  Issue : {user_issue}")
    print(sep)

    print("\n[Turn 1] Sending issue to helpdesk agent...")
    reply1, resp_id = call_helpdesk_agent(f"Hi, I'm {user_email}. Issue: {user_issue}")
    print(f"\n  AGENT:\n  {reply1}\n")

    ticket_id = _extract_ticket_id(reply1)
    print(f"  Detected ticket: {ticket_id}")

    print("[Turn 2] Requesting resolution and notification (same thread)...")
    msg2 = (
        f"Please finalise ticket {ticket_id}: "
        "close it with the resolution and send a notification email to the user."
    )
    reply2, _ = call_helpdesk_agent(msg2, thread_id=resp_id)
    print(f"\n  AGENT:\n  {reply2}\n")

    print(sep)
    print("  A2A SESSION COMPLETE")
    print(sep)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue", default="My VPN keeps disconnecting.")
    parser.add_argument("--email", default="alice@corp.com")
    args = parser.parse_args()
    run_helpdesk_session(user_issue=args.issue, user_email=args.email)
```

---

## 6. Deploy and Run Order

### Step 1 — Deploy the MCP Function App

```python
# From the repo root, zip the mcp_server folder (exclude local_server.py and __pycache__)
import zipfile, pathlib

files = ["function_app.py", "host.json", "requirements.txt", "ticket_store.py"]
with zipfile.ZipFile("mcp_server.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for f in files:
        p = pathlib.Path("mcp_server") / f
        if p.exists():
            zf.write(p, f)
```

```bash
az functionapp deployment source config-zip \
  --resource-group rg-aifoundry-eus2 \
  --name <function-app-name> \
  --src mcp_server.zip
```

Set the storage connection string (see section 2.3), then verify:

```bash
curl -X POST "https://<function-app-name>.azurewebsites.net/api/mcp?code=<function-key>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
# Expected: data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",...}}
```

### Step 2 — Deploy the Hosted Agent

```bash
cd hosted_agent
python deploy.py
# Prints: Deployed successfully! Version: N  Endpoint: https://...
```

`deploy.py` auto-appends `HELPDESK_AGENT_ENDPOINT` to `.env`.

### Step 3 — Run the Orchestrator

```bash
# Windows — set encoding to avoid cp1252 errors on Unicode in agent replies
set PYTHONIOENCODING=utf-8
python hosted_agent/orchestrator.py --issue "My VPN keeps disconnecting" --email "alice@corp.com"
```

First run after a cold start: expect one `session_not_ready` retry (20 s wait). Subsequent runs hit a warm container and respond immediately.

---

## 7. Key Design Decisions and Gotchas

| # | Decision | Reason |
|---|---|---|
| 1 | **East US 2 only** | Azure AI Foundry Hosted Agents are region-gated. East US 2 is the primary supported region as of mid-2026. |
| 2 | **DefaultAzureCredential for deploy API** | `create_version_from_code` rejects API key auth — it requires an Entra ID Bearer token. The OpenAI inference endpoint accepts the API key. |
| 3 | **`azure-ai-agentserver-*` in requirements.txt** | `langchain-azure-ai` depends on `azure.ai.agentserver` which is split into 3 pre-release packages not auto-installed. Missing these causes an immediate `ModuleNotFoundError` and `session_not_ready`. |
| 4 | **Fast startup pattern** | `ResponsesHostServer` starts with a placeholder (no-tool) graph so `/readiness` returns 200 within ~3 s. MCP tools load in a background asyncio task and the graph is swapped. Without this, the container exceeds Foundry's session readiness timeout. |
| 5 | **SHA-256 of the zip file** | `code_zip_sha256` must be the hash of the zip file bytes, not of the individual source files. Computing it from individual file content causes a "Code part is empty or too small" error. |
| 6 | **Responses protocol URL format** | Deployed endpoint URL is `{endpoint}/agents/{name}/endpoint/protocols/openai/responses?api-version=v1`. The `?api-version=v1` query param is required. |
| 7 | **`responses-protocol-version: 1.0.0` header** | Must match the version declared in `ProtocolVersionRecord` in the deploy definition. Use `1.0.0` (not `v0.1.1`). |
| 8 | **MCP server in East US 2** | Co-locating the Function App in the same region as the hosted agent container avoids cross-region latency during the MCP `get_tools` call on container startup. |
| 9 | **MemorySaver is in-memory** | Multi-turn conversation state survives within a warm container session but is lost on cold start. For production, replace with `AsyncSqliteSaver` or `AsyncPostgresSaver` (LangGraph persistence backends). |
| 10 | **`send_notification` is a stub** | The function prints to stdout. Replace with Azure Communication Services Email or SendGrid for production. |
