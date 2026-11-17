from __future__ import annotations

import json
import os
from typing import Any, Mapping


def configured_extra_headers() -> dict[str, str]:
    raw = os.environ.get("KIMI_EXTRA_HEADERS_JSON", "")
    if not raw:
        return {}
    values = json.loads(raw)
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise ValueError("KIMI_EXTRA_HEADERS_JSON must be a string mapping")
    return dict(values)


def merge_headers(existing: Mapping[str, str] | None, extra: Mapping[str, str]) -> dict[str, str]:
    return {**dict(existing or {}), **dict(extra)}


def install_extra_headers() -> None:
    extra = configured_extra_headers()
    if not extra:
        return

    import httpx
    import openai

    if not getattr(httpx, "_nvst_extra_headers_installed", False):
        original_httpx_client = httpx.Client

        class HeaderClient(original_httpx_client):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["headers"] = merge_headers(kwargs.get("headers"), extra)
                super().__init__(*args, **kwargs)

        httpx.Client = HeaderClient
        httpx._nvst_extra_headers_installed = True

    if not getattr(openai, "_nvst_extra_headers_installed", False):
        original_openai = openai.OpenAI
        original_async_openai = openai.AsyncOpenAI

        class HeaderOpenAI(original_openai):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["default_headers"] = merge_headers(kwargs.get("default_headers"), extra)
                super().__init__(*args, **kwargs)

        class HeaderAsyncOpenAI(original_async_openai):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs["default_headers"] = merge_headers(kwargs.get("default_headers"), extra)
                super().__init__(*args, **kwargs)

        openai.OpenAI = HeaderOpenAI
        openai.Client = HeaderOpenAI
        openai.AsyncOpenAI = HeaderAsyncOpenAI
        openai.AsyncClient = HeaderAsyncOpenAI
        openai._nvst_extra_headers_installed = True
