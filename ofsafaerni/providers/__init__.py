"""Readers for explicitly selected local conversation stores and exports."""

from importlib import import_module

SUPPORTED = ("claude", "gemini", "grok")


def reader_for(provider: str):
    if provider not in SUPPORTED:
        raise ValueError(f"Unsupported history source: {provider}")
    module = import_module(f".{provider}", __name__)
    return getattr(module, f"iter_{provider}")
