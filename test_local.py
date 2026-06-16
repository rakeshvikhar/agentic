"""
Local integration test -- runs the MCP server in a background thread and
exercises every tool directly, without needing Azure AI Foundry credentials.

Usage:  python test_local.py
"""
import json
import sys
import threading
import time
import urllib.request
import urllib.error

MCP_URL = "http://localhost:7171/api/mcp"  # different port to avoid conflict


def call_mcp(method: str, params: dict | None = None) -> dict:
    body = {"method": method}
    if params:
        body["params"] = params
    raw = json.dumps(body).encode()
    req = urllib.request.Request(
        MCP_URL, data=raw, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def start_server():
    """Start local_server.py in a background thread on port 7171."""
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "mcp_server"))

    from http.server import HTTPServer
    import local_server as ls

    server = HTTPServer(("localhost", 7171), ls.MCPHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)  # let it bind
    return server


def run_tests():
    ok = passed = 0

    def check(label: str, result: dict, expected_key: str):
        nonlocal ok, passed
        ok += 1
        text = result.get("content", [{}])[0].get("text", "{}")
        data = json.loads(text)
        if expected_key in data or (isinstance(data, dict) and not data.get("error")):
            passed += 1
            print(f"  [PASS] {label}")
        else:
            print(f"  [FAIL] {label} -> {data}")
        return data

    print("\n-- tools/list --------------------------------------------------")
    tools = call_mcp("tools/list")
    names = [t["name"] for t in tools["tools"]]
    print(f"  Tools available: {names}")
    assert len(names) == 8, f"Expected 8 tools, got {len(names)}"
    print("  [PASS] tools/list returned 8 tools\n")

    print("-- create_ticket -----------------------------------------------")
    r = call_mcp("tools/call", {"name": "create_ticket", "arguments": {
        "title": "VPN keeps disconnecting",
        "description": "Every 20 minutes VPN drops and reconnecting takes 5 minutes.",
        "priority": "high",
    }})
    ticket = check("create_ticket", r, "id")
    ticket_id = ticket.get("id", "TKT-UNKNOWN")
    print(f"  Created ticket: {ticket_id}\n")

    print("-- get_ticket_status -------------------------------------------")
    r = call_mcp("tools/call", {"name": "get_ticket_status", "arguments": {"ticket_id": ticket_id}})
    check("get_ticket_status", r, "status")

    print("\n-- assign_ticket -----------------------------------------------")
    r = call_mcp("tools/call", {"name": "assign_ticket", "arguments": {
        "ticket_id": ticket_id, "agent_name": "Tech Support Team",
    }})
    check("assign_ticket", r, "assigned_to")

    print("\n-- log_event ---------------------------------------------------")
    r = call_mcp("tools/call", {"name": "log_event", "arguments": {
        "ticket_id": ticket_id, "event": "Checked VPN client logs - found timeout setting at 1200s.",
    }})
    check("log_event", r, "logged")

    print("\n-- get_asset_info ----------------------------------------------")
    for asset_id in ["A001", "A003", "A999"]:
        r = call_mcp("tools/call", {"name": "get_asset_info", "arguments": {"asset_id": asset_id}})
        text = json.loads(r["content"][0]["text"])
        status = text.get("status") or text.get("name") or text.get("error", "?")
        print(f"  {asset_id}: {status}")

    print("\n-- send_notification -------------------------------------------")
    r = call_mcp("tools/call", {"name": "send_notification", "arguments": {
        "recipient": "alice@corp.com",
        "subject":   f"Your ticket {ticket_id} has been resolved",
        "message":   "We increased the VPN keepalive interval to 3600s. Please reconnect and test.",
    }})
    check("send_notification", r, "status")

    print("\n-- close_ticket ------------------------------------------------")
    r = call_mcp("tools/call", {"name": "close_ticket", "arguments": {
        "ticket_id": ticket_id,
        "resolution": "Increased VPN keepalive interval from 1200s to 3600s in the client config.",
    }})
    check("close_ticket", r, "status")

    print("\n-- list_all_tickets --------------------------------------------")
    r = call_mcp("tools/call", {"name": "list_all_tickets", "arguments": {}})
    text = json.loads(r["content"][0]["text"])
    print(f"  Total tickets: {text['total']}")

    print(f"\n{'='*60}")
    print(f"  Results: {passed}/{ok} passed")
    return passed == ok


if __name__ == "__main__":
    import os
    os.chdir(os.path.join(os.path.dirname(__file__), "mcp_server"))

    print("Starting local MCP server on port 7171...")
    start_server()
    print("Server ready.\n")

    success = run_tests()
    sys.exit(0 if success else 1)
