"""
Deploy the Helpdesk MCP Function App to Azure.

Uses Kudu zipdeploy with ENABLE_ORYX_BUILD=true so Azure installs
Python packages via remote build (no pre-bundling required).

Run:
    python deploy.py           # deploy / update
    python deploy.py --check   # test the endpoint without deploying
"""
import argparse
import os
import pathlib
import sys
import tempfile
import time
import zipfile

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

APP_NAME       = "helpdesk-mcp-tools-eus2"
RESOURCE_GROUP = "rg-aifoundry-eus2"
FUNCTION_KEY   = os.environ.get("MCP_SERVER_KEY", "")
AGENT_DIR      = pathlib.Path(__file__).parent

# Files to include in the deployment zip
SOURCE_FILES = ["function_app.py", "requirements.txt", "host.json", "ticket_store.py"]

# App settings to ensure are present (Oryx build settings + storage)
REQUIRED_SETTINGS = {
    "FUNCTIONS_WORKER_RUNTIME":                 "python",
    "FUNCTIONS_EXTENSION_VERSION":              "~4",
    "ENABLE_ORYX_BUILD":                        "true",
    "SCM_DO_BUILD_DURING_DEPLOYMENT":           "true",
}


def _get_token() -> str:
    import subprocess
    r = subprocess.run(
        ["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True
    )
    token = r.stdout.strip()
    if not token:
        raise RuntimeError("az account get-access-token failed — run `az login` first")
    return token


def _get_storage_conn() -> str:
    """Read AzureWebJobsStorage from the live app settings."""
    import subprocess, json
    r = subprocess.run(
        ["az", "functionapp", "config", "appsettings", "list",
         "--name", APP_NAME, "--resource-group", RESOURCE_GROUP, "-o", "json"],
        capture_output=True, text=True
    )
    settings = json.loads(r.stdout)
    for s in settings:
        if s["name"] == "AzureWebJobsStorage":
            return s["value"]
    return ""


def create_zip() -> str:
    """Zip source files; return path to the temp zip."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in SOURCE_FILES:
            fpath = AGENT_DIR / fname
            if fpath.exists():
                zf.write(fpath, fname)
                print(f"  + {fname}  ({fpath.stat().st_size / 1024:.1f} KB)")
            else:
                print(f"  ! WARNING: {fname} not found, skipping")
    size = pathlib.Path(tmp.name).stat().st_size
    print(f"  -> zip total: {size / 1024:.1f} KB")
    return tmp.name


def ensure_build_settings():
    """Make sure ENABLE_ORYX_BUILD and SCM_DO_BUILD_DURING_DEPLOYMENT are set."""
    import subprocess, json

    r = subprocess.run(
        ["az", "functionapp", "config", "appsettings", "list",
         "--name", APP_NAME, "--resource-group", RESOURCE_GROUP, "-o", "json"],
        capture_output=True, text=True
    )
    current = {s["name"]: s["value"] for s in json.loads(r.stdout)}

    to_set = {k: v for k, v in REQUIRED_SETTINGS.items() if current.get(k) != v}
    if not to_set:
        print("  Build settings already correct.")
        return

    # Also ensure AZURE_STORAGE_CONNECTION_STRING is set for ticket persistence
    if "AZURE_STORAGE_CONNECTION_STRING" not in current:
        storage_conn = _get_storage_conn()
        if storage_conn:
            to_set["AZURE_STORAGE_CONNECTION_STRING"] = storage_conn

    settings_args = [f"{k}={v}" for k, v in to_set.items()]
    subprocess.run(
        ["az", "functionapp", "config", "appsettings", "set",
         "--name", APP_NAME, "--resource-group", RESOURCE_GROUP,
         "--settings"] + settings_args + ["--output", "none"],
        check=True
    )
    print(f"  Updated settings: {list(to_set.keys())}")


def deploy():
    import urllib.request

    print(f"Deploying '{APP_NAME}' to Azure Functions...\n")

    print("Step 1: Ensuring build settings...")
    ensure_build_settings()

    print("\nStep 2: Packing source files:")
    zip_path = create_zip()

    print("\nStep 3: Uploading via Kudu zipdeploy (Oryx will install packages)...")
    token = _get_token()
    url = f"https://{APP_NAME}.scm.azurewebsites.net/api/zipdeploy?isAsync=true"

    with open(zip_path, "rb") as f:
        zip_data = f.read()

    req = urllib.request.Request(url, data=zip_data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/zip")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(f"  HTTP {resp.status} — deployment queued")
    except Exception as e:
        # 202 Accepted raises an error in urlopen — that's success
        if "202" in str(e) or "Accepted" in str(e):
            print("  HTTP 202 — deployment queued (async)")
        else:
            print(f"  Deploy error: {e}")
            pathlib.Path(zip_path).unlink(missing_ok=True)
            return

    pathlib.Path(zip_path).unlink(missing_ok=True)

    print("\nStep 4: Waiting for Oryx build to complete (~30-60s)...")
    time.sleep(15)
    _wait_for_deployment(token)

    print("\nStep 5: Testing endpoint...")
    check_endpoint()


def _wait_for_deployment(token: str, max_wait: int = 120):
    import urllib.request, json
    url = f"https://{APP_NAME}.scm.azurewebsites.net/api/deployments"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")

    for _ in range(max_wait // 5):
        time.sleep(5)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                deploys = json.loads(resp.read())
                if deploys:
                    latest = deploys[0]
                    status = latest.get("status", 0)
                    # Status: 3=Failed, 4=Success
                    if status == 4:
                        print("  Build complete: SUCCESS")
                        return
                    elif status == 3:
                        print("  Build FAILED — check Kudu logs")
                        return
                    else:
                        print(f"  Build in progress (status={status})...")
        except Exception:
            pass
    print("  Timed out waiting for build")


def check_endpoint():
    import urllib.request, json
    url = f"https://{APP_NAME}.azurewebsites.net/api/mcp"
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "deploy-check", "version": "1.0"},
        }
    }).encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if FUNCTION_KEY:
        req.add_header("x-functions-key", FUNCTION_KEY)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode()
            if '"protocolVersion"' in text:
                print("  MCP endpoint OK — server responded to initialize")
            else:
                print(f"  MCP endpoint responded but unexpected body: {text[:200]}")
    except Exception as e:
        print(f"  MCP endpoint check failed: {e}")
        print("  (The app may still be cold-starting — retry in 30s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Test endpoint only, no deploy")
    args = parser.parse_args()

    if args.check:
        check_endpoint()
    else:
        deploy()
