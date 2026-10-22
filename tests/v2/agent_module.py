"""Loads agent/synchub_agent.py (a single file that lives outside the package) so tests can use it like a module."""
import importlib.util
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "agent" / "synchub_agent.py"
_spec = importlib.util.spec_from_file_location("synchub_agent", PATH)
agent = importlib.util.module_from_spec(_spec)
sys.modules["synchub_agent"] = agent
_spec.loader.exec_module(agent)
