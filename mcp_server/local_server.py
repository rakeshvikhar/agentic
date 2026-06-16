"""
Lightweight local HTTP server that mirrors function_app.py.
Implements MCP Streamable HTTP transport (SSE responses).

Usage:  python mcp_server/local_server.py
URL:    http://localhost:7071/api/mcp
"""
import json
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from ticket_store import (
    create_ticket, get_ticket_status, get_asset_info,
    assign_ticket, close_ticket, log_event,
    send_notification, list_all_tickets,
)

TOOL_DEFINITIONS = [
    {
        "name": "create_ticket",
        "description": "Create a new IT helpdesk ticket",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
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
        "description": "Send an email notification to a user",
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
        return None if msg_id is None else {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def err(code, message):
        return None if msg_id is None else {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

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
        fn   = DISPATCH.get(name)
        if not fn:
            return err(-32601, f"Unknown tool: {name}")
        try:
            result = fn(**args)
            return ok({"content": [{"type": "text", "text": json.dumps(result)}]})
        except TypeError as e:
            return err(-32602, f"Bad arguments for {name}: {e}")
    return err(-32601, f"Unsupported method: {method}")


class MCPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [MCP] {fmt % args}")

    def do_POST(self):
        if self.path not in ("/api/mcp", "/api/mcp/"):
            self._send_json({"error": "Not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        messages  = body if isinstance(body, list) else [body]
        responses = [r for msg in messages if (r := _handle_one(msg)) is not None]

        if not responses:
            self._send_sse("", 202)
            return

        sse = "".join(f"data: {json.dumps(r)}\n\n" for r in responses)
        self._send_sse(sse, 200)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, data: str, status: int = 200):
        body = data.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    host, port = "localhost", 7071
    server = HTTPServer((host, port), MCPHandler)
    print(f"MCP server running at http://{host}:{port}/api/mcp  (SSE transport)")
    print("Press Ctrl+C to stop.\n")
    server.serve_forever()
