"""
Ticket and asset store.

When AZURE_STORAGE_CONNECTION_STRING is set (Azure Function App), data is
persisted in Azure Table Storage.  Otherwise falls back to in-memory dicts
(local dev / test).
"""
import os
import uuid
import json
import datetime

_USE_AZURE = bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))

# ── Azure Table Storage backend ────────────────────────────────────────────────
if _USE_AZURE:
    from azure.data.tables import TableServiceClient, UpdateMode

    _svc = TableServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    )
    # Ensure tables exist (idempotent)
    for _t in ("tickets", "events"):
        try:
            _svc.create_table(_t)
        except Exception:
            pass  # already exists

    _tickets_tbl = _svc.get_table_client("tickets")
    _events_tbl  = _svc.get_table_client("events")

    # Seed assets as well
    _assets_tbl = None
    try:
        _svc.create_table("assets")
    except Exception:
        pass
    _assets_tbl = _svc.get_table_client("assets")

    _SEED_ASSETS = {
        "A001": {"name": "Dell XPS Laptop",     "user": "alice@corp.com", "status": "in-use"},
        "A002": {"name": "USB-C Hub",            "user": "bob@corp.com",   "status": "in-use"},
        "A003": {"name": 'Monitor 27"',          "user": None,             "status": "available"},
        "A004": {"name": "Mechanical Keyboard",  "user": None,             "status": "available"},
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


# ── In-memory backend (local dev) ─────────────────────────────────────────────
else:
    _tickets: dict = {}
    _events:  dict = {}
    _assets: dict = {
        "A001": {"name": "Dell XPS Laptop",     "user": "alice@corp.com", "status": "in-use"},
        "A002": {"name": "USB-C Hub",            "user": "bob@corp.com",   "status": "in-use"},
        "A003": {"name": 'Monitor 27"',          "user": None,             "status": "available"},
        "A004": {"name": "Mechanical Keyboard",  "user": None,             "status": "available"},
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def create_ticket(title: str, description: str, priority: str) -> dict:
    tid = f"TKT-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "id":          tid,
        "title":       title,
        "description": description,
        "priority":    priority,
        "status":      "open",
        "created_at":  datetime.datetime.utcnow().isoformat(),
        "assigned_to": None,
        "resolution":  None,
    }
    if _USE_AZURE:
        _tickets_tbl.create_entity({
            "PartitionKey": "tickets",
            "RowKey":       tid,
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
            e["status"]      = "in-progress"
            _tickets_tbl.update_entity(e, mode=UpdateMode.REPLACE)
            return get_ticket_status(ticket_id)
        except Exception:
            return {"error": f"Ticket {ticket_id} not found"}
    if ticket_id not in _tickets:
        return {"error": f"Ticket {ticket_id} not found"}
    _tickets[ticket_id]["assigned_to"] = agent_name
    _tickets[ticket_id]["status"]      = "in-progress"
    return _tickets[ticket_id]


def close_ticket(ticket_id: str, resolution: str) -> dict:
    if _USE_AZURE:
        try:
            e = _tickets_tbl.get_entity("tickets", ticket_id)
            e["status"]     = "closed"
            e["resolution"] = resolution
            _tickets_tbl.update_entity(e, mode=UpdateMode.REPLACE)
            return get_ticket_status(ticket_id)
        except Exception:
            return {"error": f"Ticket {ticket_id} not found"}
    if ticket_id not in _tickets:
        return {"error": f"Ticket {ticket_id} not found"}
    _tickets[ticket_id]["status"]     = "closed"
    _tickets[ticket_id]["resolution"] = resolution
    return _tickets[ticket_id]


def log_event(ticket_id: str, event: str) -> dict:
    ts    = datetime.datetime.utcnow().isoformat()
    entry = {"timestamp": ts, "event": event}
    if _USE_AZURE:
        row_key = f"{ticket_id}_{ts.replace(':', '-')}"
        _events_tbl.create_entity({
            "PartitionKey": ticket_id,
            "RowKey":       row_key,
            "event":        event,
            "timestamp":    ts,
        })
    else:
        _events.setdefault(ticket_id, []).append(entry)
    return {"ticket_id": ticket_id, "logged": entry}


def send_notification(recipient: str, subject: str, message: str) -> dict:
    # Production: swap for Azure Communication Services / SendGrid
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
