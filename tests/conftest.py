"""Lets the tests import main.py on machines without a GUI toolkit (e.g. minimal CI)."""
import sys
import types

try:
    import customtkinter  # noqa: F401
except Exception:  # pragma: no cover
    stub = types.ModuleType("customtkinter")
    stub.CTk = object
    sys.modules["customtkinter"] = stub
