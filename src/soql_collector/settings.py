"""設定ファイルを読み込む。"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_EXCEL_PATH = "data/soql_collector.xlsx"
DEFAULT_JSON_DIR = "data/describe_json"


@dataclass(frozen=True)
class Settings:
    excel_path: Path
    json_dir: Path
    master_xlsx_path: Optional[Path]
    csv_path: Optional[Path]
    credential_prefix: str


def load_settings(project_dir: Path | None = None) -> Settings:
    """config.ini を読み、未作成時は安全な既定値を返す。"""
    base_dir = (project_dir or Path.cwd()).resolve()
    parser = configparser.ConfigParser()
    parser.read(base_dir / "config.ini", encoding="utf-8")
    excel_value = parser.get("FILES", "EXCEL_PATH", fallback=DEFAULT_EXCEL_PATH)
    json_value = parser.get("FILES", "JSON_DIR", fallback=DEFAULT_JSON_DIR)
    master_value = parser.get("FILES", "MASTER_XLSX_PATH", fallback="")
    csv_value = parser.get("FILES", "CSV_PATH", fallback="")
    prefix = parser.get("SF", "CREDENTIAL_PREFIX", fallback="")
    return Settings(
        _resolve_path(base_dir, excel_value),
        _resolve_path(base_dir, json_value),
        _resolve_path(base_dir, master_value) if master_value else None,
        _resolve_path(base_dir, csv_value) if csv_value else None,
        prefix,
    )


def _resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base_dir / path
