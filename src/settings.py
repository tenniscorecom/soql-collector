"""config.ini から設定を読む。

必要なキーは ``[FILES] MASTER_XLSX_PATH`` （必須）と ``[FILES] OUTPUT_DIR`` 、
``[LIMITS] RELATED_MAX`` 、``[SF] CREDENTIAL_PREFIX`` 、
``[OBJECTS] NAMES`` （任意）・ ``[OBJECTS] ORG_ID`` （任意）。
``MASTER_XLSX_PATH`` が欠けていると ``run`` が動けないので、その時点でエラーを上げる。
"""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass
from pathlib import Path

from comken.core.files.ops import project_dir
from comken.exceptions import ConfigKeyNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = "./output"
DEFAULT_RELATED_MAX = 40


@dataclass(frozen=True)
class Settings:
    """config.ini から読んだ設定値。"""

    master_xlsx_path: Path
    output_dir: Path
    related_max: int
    credential_prefix: str
    objects_names: tuple[str, ...]
    objects_org_id: str | None


def load_settings(project_root: Path | None = None) -> Settings:
    """config.ini を読み、 ``Settings`` を返す。

    Args:
        project_root: config.ini があるフォルダ。省略時は comken の
            ``project_dir()`` （実行スクリプトのフォルダ）。

    Raises:
        ConfigKeyNotFoundError: ``[FILES] MASTER_XLSX_PATH`` が無い／空。
    """
    base_dir = (project_root or project_dir()).resolve()
    ini_path = base_dir / "config.ini"
    parser = configparser.ConfigParser()
    # デフォルトの ``optionxform = str.lower`` を抑止し、 ``MASTER_XLSX_PATH``
    # など大文字キー（プロジェクトの命名規約）をそのまま保持する
    parser.optionxform = str  # type: ignore[assignment]
    parser.read(ini_path, encoding="utf-8")

    master_value = _require(parser, "FILES", "MASTER_XLSX_PATH", ini_path).strip()
    if not master_value:
        raise ConfigKeyNotFoundError(
            "FILES", "MASTER_XLSX_PATH", _options(parser, "FILES"), ini_path
        )
    output_value = _optional(
        parser, "FILES", "OUTPUT_DIR", ini_path, default=DEFAULT_OUTPUT_DIR
    ).strip()
    related_value = _optional(
        parser,
        "LIMITS",
        "RELATED_MAX",
        ini_path,
        default=str(DEFAULT_RELATED_MAX),
    ).strip()
    try:
        related_max = int(related_value)
    except ValueError as exc:
        raise ConfigKeyNotFoundError(
            "LIMITS", "RELATED_MAX", _options(parser, "LIMITS"), ini_path
        ) from exc
    credential_prefix = _optional(parser, "SF", "CREDENTIAL_PREFIX", ini_path, default="").strip()
    objects_names = _parse_object_names(_optional(parser, "OBJECTS", "NAMES", ini_path, default=""))
    objects_org_id = _parse_object_org_id(
        _optional(parser, "OBJECTS", "ORG_ID", ini_path, default="")
    )
    return Settings(
        master_xlsx_path=_resolve(base_dir, master_value),
        output_dir=_resolve(base_dir, output_value),
        related_max=related_max,
        credential_prefix=credential_prefix,
        objects_names=objects_names,
        objects_org_id=objects_org_id,
    )


def _options(parser: configparser.ConfigParser, section: str) -> list[str]:
    """セクションのキー一覧を返す（無ければ空リスト）。"""
    if not parser.has_section(section):
        return []
    return list(parser.options(section))


def _require(parser: configparser.ConfigParser, section: str, name: str, ini_path: Path) -> str:
    """必須キー。無ければ ``ConfigKeyNotFoundError`` 。空文字も矛盾とする。"""
    if not parser.has_section(section) or not parser.has_option(section, name):
        raise ConfigKeyNotFoundError(section, name, _options(parser, section), ini_path)
    value = parser.get(section, name, fallback="")
    if value == "":
        raise ConfigKeyNotFoundError(section, name, _options(parser, section), ini_path)
    return value


def _optional(
    parser: configparser.ConfigParser,
    section: str,
    name: str,
    ini_path: Path,
    *,
    default: str,
) -> str:
    """任意キー。無ければ ``default`` を返す（セクションが無い場合も同じ）。"""
    if not parser.has_section(section) or not parser.has_option(section, name):
        return default
    value = parser.get(section, name, fallback="")
    return value if value else default


def _resolve(base_dir: Path, value: str) -> Path:
    """config.ini からの相対なら base_dir と連結し、絶対ならそのまま返す。"""
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _parse_object_names(raw: str) -> tuple[str, ...]:
    """``[OBJECTS] NAMES`` の値を ``tuple[str, ...]`` に直す。

    区切りはカンマ ``,`` と読点 ``、`` の両方を受け、 前後の空白を除き、
    空要素は無視し、 重複は 1 つにまとめる （順序は最初に出た順）。
    """
    if not raw:
        return ()
    tokens = [token.strip() for token in raw.replace("、", ",").split(",")]
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        result.append(token)
    return tuple(result)


def _parse_object_org_id(raw: str) -> str | None:
    """``[OBJECTS] ORG_ID`` の値を整える。前後の空白を除き、空なら ``None`` 。"""
    if not raw:
        return None
    stripped = raw.strip()
    return stripped or None
