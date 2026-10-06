"""Shared by the GPU-host services. The host is reachable from the internet, so every service
requires the same bearer token (GPU_API_KEY). vLLM enforces it itself via --api-key."""

import hmac
import os

from fastapi import Header, HTTPException


def require_key(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("GPU_API_KEY", "")
    if not expected:
        return  # local development only; serve_llm.sh and run_all.sh refuse to start without a key
    if not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="bad or missing API key")
