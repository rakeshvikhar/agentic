"""
Authentication helpers for azure-ai-projects v2.2.0.

Uses DefaultAzureCredential (Entra ID) for the AIProjectClient — required for
APIs like hosted agent deployment that don't accept API key auth.
Uses API key directly for the OpenAI client endpoint.
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
