"""
Azure Function App — MCP server for IT Helpdesk tools.

Implements the MCP "Streamable HTTP" transport:
  - POST /api/mcp  with Content-Type: application/json
  - Body: single JSON-RPC object  OR  array of JSON-RPC objects
  - Response: text/event-stream (SSE), one `data:` line per reply

Local dev:  python local_server.py
Deploy:     az functionapp deployment source config-zip ...
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
    """Process a single JSON-RPC message. Returns a response dict or None for notifications."""
    method = msg.get("method", "")
    msg_id = msg.get("id")           # None means it's a notification (no reply needed)
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
        return None  # notification — no reply

    if method == "tools/list":
        return ok({"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments", {})
        fn   = DISPATCH.get(name)
        if not fn:
            return err(-32601, f"Unknown tool: {name}")
        try:
            result = fn(**args)
            return ok({"content": [{"type": "text", "text": json.dumps(result)}]})
        except TypeError as e:
            return err(-32602, f"Bad arguments for {name}: {e}")

    return err(-32601, f"Unsupported method: {method}")


def _to_sse(responses: list[dict]) -> str:
    """Encode a list of JSON-RPC responses as an SSE body."""
    lines = []
    for r in responses:
        lines.append(f"data: {json.dumps(r)}\n\n")
    return "".join(lines)


@app.route(route="mcp", methods=["POST"])
def mcp_handler(req: func.HttpRequest) -> func.HttpResponse:
    try:
        raw = req.get_body().decode("utf-8")
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid JSON: {e}"}),
            status_code=400, mimetype="application/json",
        )

    # Batch (array) or single message
    messages = body if isinstance(body, list) else [body]

    responses = []
    for msg in messages:
        reply = _handle_one(msg)
        if reply is not None:
            responses.append(reply)

    if not responses:
        # All messages were notifications — return 202 with empty SSE
        return func.HttpResponse("", status_code=202, mimetype="text/event-stream")

    # Single non-batch request: can return plain JSON (simpler) OR SSE.
    # Azure AI Foundry MCP client requires SSE (Streamable HTTP transport).
    sse_body = _to_sse(responses)
    return func.HttpResponse(
        sse_body,
        status_code=200,
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
