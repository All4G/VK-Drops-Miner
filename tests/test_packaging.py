"""Config template, data-dir logic for the exe."""
import os
import sys
import tempfile
from pathlib import Path

import main as m
import yaml


def test_default_config_yaml_matches_defaults_and_example_file():
    assert yaml.safe_load(m.DEFAULT_CONFIG_YAML) == m.DEFAULT_CONFIG
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    assert example.read_text(encoding="utf-8") == m.DEFAULT_CONFIG_YAML


def test_config_is_created_on_first_run_and_partial_files_are_merged():
    old = m.CONFIG_FILE
    try:
        m.CONFIG_FILE = Path(tempfile.mkdtemp()) / "config.yaml"
        assert m.load_config() == m.DEFAULT_CONFIG and m.CONFIG_FILE.exists()
        m.CONFIG_FILE.write_text("drops:\n  claim_delay_min: 7\n", encoding="utf-8")
        cfg = m.load_config()
        assert cfg["drops"]["claim_delay_min"] == 7 and cfg["drops"]["claim_delay_max"] == 5
    finally:
        m.CONFIG_FILE = old


def test_frozen_data_dir_is_next_to_exe():
    exe_dir = Path(tempfile.mkdtemp())
    old = (m.FROZEN, sys.executable)
    try:
        m.FROZEN, sys.executable = True, str(exe_dir / "VKDropsMiner.exe")
        assert m._resolve_data_dir() == exe_dir
    finally:
        m.FROZEN, sys.executable = old


def test_frozen_data_dir_falls_back_to_appdata_when_exe_dir_is_not_writable():
    old = (m.FROZEN, sys.executable, os.environ.get("APPDATA"))
    try:
        m.FROZEN, sys.executable = True, str(Path(tempfile.mkdtemp()) / "x" / "VKDropsMiner.exe")  # parent missing -> not writable
        os.environ["APPDATA"] = tempfile.mkdtemp()
        assert m._resolve_data_dir() == Path(os.environ["APPDATA"]) / "VKDropsMiner"
    finally:
        m.FROZEN, sys.executable = old[0], old[1]
        if old[2] is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old[2]
