from __future__ import annotations

from .header_injection import install_extra_headers


def pytest_configure() -> None:
    install_extra_headers()
