"""Pier agent adapter for the Kimi Code CLI."""

__all__ = ["KimiCode"]


def __getattr__(name: str):
    if name == "KimiCode":
        from pier_kimi.kimi_code import KimiCode

        return KimiCode
    raise AttributeError(name)
