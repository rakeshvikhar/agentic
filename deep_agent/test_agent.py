"""
Local test for Deep Agent.

Usage:
    python test_agent.py                    # run default test suite
    python test_agent.py --live             # hit the running server on localhost:8089
    python test_agent.py --live --url http://localhost:8089
"""
import argparse
import asyncio
import json
import os
import sys

import httpx
from dotenv import load_dotenv

# Load .env so AZURE_OPENAI_API_KEY can be set here for direct tests
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TEST_CASES = [
    {
        "name": "Math calculation",
        "input": "What is 42 * 17 + sqrt(144)?",
    },
    {
        "name": "Current date",
        "input": "What is today's date and what day of the week is it?",
    },
    {
        "name": "Word count",
        "input": 'Count the words in: "The quick brown fox jumps over the lazy dog"',
    },
    {
        "name": "Multi-step reasoning",
        "input": (
            "I need to know: (1) what is 15% of 240, "
            "(2) today's date, and (3) how many words are in this sentence."
        ),
    },
]


async def test_direct():
    """Invoke the LangGraph agent directly (no HTTP server needed)."""
    # Must set key before importing main
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not api_key:
        print("ERROR: AZURE_OPENAI_API_KEY not set. Export it before running.")
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(__file__))
    from main import _make_graph   # noqa: PLC0415

    print("=== Deep Agent — Direct LangGraph Test ===\n")
    graph = _make_graph()

    for tc in TEST_CASES:
        print(f"[{tc['name']}]")
        print(f"  Q: {tc['input']}")
        config = {"configurable": {"thread_id": tc["name"]}}
        result = await graph.ainvoke({"messages": [("user", tc["input"])]}, config)
        answer = result["messages"][-1].content
        print(f"  A: {answer}\n")


async def test_live(base_url: str):
    """POST to the running server's /responses endpoint."""
    print(f"=== Deep Agent — Live HTTP Test ({base_url}) ===\n")
    async with httpx.AsyncClient(timeout=60) as client:
        for tc in TEST_CASES:
            print(f"[{tc['name']}]")
            print(f"  Q: {tc['input']}")
            resp = await client.post(
                f"{base_url}/responses",
                json={"input": tc["input"], "stream": False},
            )
            resp.raise_for_status()
            body = resp.json()
            # Responses protocol wraps in output array
            output = body.get("output") or body
            if isinstance(output, list):
                text = next(
                    (
                        item.get("content", [{}])[0].get("text", "")
                        for item in output
                        if item.get("type") == "message"
                    ),
                    json.dumps(output),
                )
            else:
                text = str(output)
            print(f"  A: {text}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Hit the running HTTP server")
    parser.add_argument("--url", default="http://localhost:8089")
    args = parser.parse_args()

    if args.live:
        asyncio.run(test_live(args.url))
    else:
        asyncio.run(test_direct())


if __name__ == "__main__":
    main()
