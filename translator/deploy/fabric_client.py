"""Deploy a `.SemanticModel` PBIP folder to a Microsoft Fabric workspace.

Uses the Fabric REST `Items - Create/Update Item` endpoint and the
``InMemoryItemDefinition`` content with each file base64-encoded.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Iterable

import requests
from azure.identity import DefaultAzureCredential


_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_SCOPE = "https://api.fabric.microsoft.com/.default"


def _token() -> str:
    cred = DefaultAzureCredential()
    return cred.get_token(_SCOPE).token


def _walk_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _to_parts(root: Path) -> list[dict]:
    parts: list[dict] = []
    for p in _walk_files(root):
        rel = p.relative_to(root).as_posix()
        payload = base64.b64encode(p.read_bytes()).decode("ascii")
        parts.append({"path": rel, "payload": payload, "payloadType": "InlineBase64"})
    return parts


def deploy_semantic_model(model_dir: Path, workspace_id: str, display_name: str, *, timeout_s: int = 600) -> dict:
    """Create or update a SemanticModel item in a Fabric workspace.

    Returns the final item descriptor.
    """
    headers = {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}

    # 1. Look up existing item by display name
    list_url = f"{_FABRIC_BASE}/workspaces/{workspace_id}/items?type=SemanticModel"
    resp = requests.get(list_url, headers=headers, timeout=60)
    resp.raise_for_status()
    existing = next(
        (i for i in resp.json().get("value", []) if i.get("displayName") == display_name),
        None,
    )

    body = {
        "displayName": display_name,
        "type": "SemanticModel",
        "definition": {"parts": _to_parts(model_dir)},
    }

    if existing:
        item_id = existing["id"]
        url = f"{_FABRIC_BASE}/workspaces/{workspace_id}/items/{item_id}/updateDefinition"
        resp = requests.post(url, headers=headers, json={"definition": body["definition"]}, timeout=120)
    else:
        url = f"{_FABRIC_BASE}/workspaces/{workspace_id}/items"
        resp = requests.post(url, headers=headers, json=body, timeout=120)

    if resp.status_code == 202:
        op_url = resp.headers.get("Location")
        return _poll(op_url, headers, timeout_s)
    resp.raise_for_status()
    return resp.json() if resp.content else {"status": "ok"}


def _poll(op_url: str, headers: dict, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(op_url, headers=headers, timeout=30)
        r.raise_for_status()
        body = r.json()
        if body.get("status") in {"Succeeded", "Failed"}:
            return body
        time.sleep(2)
    raise TimeoutError(f"Fabric LRO did not complete within {timeout_s}s: {op_url}")
