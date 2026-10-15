"""``src.config_defaults`` のテスト。

- キーが無い → 既定値で動く
- 値はあるが不正 → エラーで止める （ メッセージにキー名と正しい形 ）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _write_config(tmp_path: Path, content: str) -> Path:
    """テスト用 ``config.ini`` を ``tmp_path`` に書く。"""
    path = tmp_path / "config.ini"
    path.write_text(content, encoding="utf-8")
    return path


def _load_app(config_path: Path) -> Any:
    """テスト用の config.ini を読んで ``AppConfig`` を返す。"""
    from src.config_defaults import load_from_config

    return load_from_config(config_path)


# ── キー欠落 → 既定値 ──────────────────────────────────────


def test_missing_files_section_uses_defaults(tmp_path: Path) -> None:
    """config.ini が空でも、 全てのキーが ``config_defaults`` の既定値で動く。"""
    path = _write_config(tmp_path, "")
    app = _load_app(path)

    assert app.files.master_xlsx_path == "./input/レポート管理表.xlsx"
    assert app.files.output_dir == "./output"
    assert app.files.describe_csv_path == "./data/項目定義.csv"
    assert app.files.column_fix_xlsx_path == "./input/列の直し.xlsx"
    assert app.files.xlsx_name == "SOQL調査.xlsx"

    assert app.survey.related_depth == 3
    assert app.survey.related_max == 40
    assert app.survey.row_limit_enabled is True
    assert app.survey.similar_column_threshold == 0.8

    assert "氏名" in app.pii.keywords
    assert "Contact" in app.pii.person_objects


def test_partial_files_overrides_only_given_keys(tmp_path: Path) -> None:
    """``[FILES]`` に一部のキーだけ書いた場合は、 書いた値は上書き、 残りは既定値。"""
    path = _write_config(
        tmp_path,
        "[FILES]\nOUTPUT_DIR = ./custom_output\n",
    )
    app = _load_app(path)
    # comken の Config が ``./`` 付きの相対パスを絶対パスに解決するため、 末尾で比較
    assert app.files.output_dir.endswith("custom_output")
    # 書いていないキーは既定値のまま
    assert app.files.master_xlsx_path.endswith("レポート管理表.xlsx")


# ── 値の検証 ──────────────────────────────────────


def test_invalid_int_value_raises_with_key_and_example(tmp_path: Path) -> None:
    """整数値でない ``RELATED_DEPTH`` はエラーメッセージにキー名と正しい形を出す。"""
    path = _write_config(
        tmp_path,
        "[SURVEY]\nRELATED_DEPTH = abc\n",
    )

    from src.config_defaults import ConfigValueError

    with pytest.raises(ConfigValueError) as exc_info:
        _load_app(path)
    assert "RELATED_DEPTH" in str(exc_info.value)
    assert "整数" in str(exc_info.value)


def test_int_value_out_of_range_raises(tmp_path: Path) -> None:
    """範囲外の整数はエラーメッセージで止める。"""
    path = _write_config(
        tmp_path,
        "[SURVEY]\nRELATED_MAX = 999\n",
    )

    from src.config_defaults import ConfigValueError

    with pytest.raises(ConfigValueError) as exc_info:
        _load_app(path)
    assert "RELATED_MAX" in str(exc_info.value)
    assert "範囲外" in str(exc_info.value)


def test_invalid_bool_value_raises(tmp_path: Path) -> None:
    """``ROW_LIMIT_CHECK`` が ○/× 以外なら止める。"""
    path = _write_config(
        tmp_path,
        "[SURVEY]\nROW_LIMIT_CHECK = maybe\n",
    )

    from src.config_defaults import ConfigValueError

    with pytest.raises(ConfigValueError) as exc_info:
        _load_app(path)
    assert "ROW_LIMIT_CHECK" in str(exc_info.value)


def test_pii_keywords_csv_parsing(tmp_path: Path) -> None:
    """``[PII] KEYWORDS`` のカンマ区切りがパースされる。"""
    path = _write_config(
        tmp_path,
        "[PII]\nKEYWORDS = foo, bar, baz\n",
    )
    app = _load_app(path)
    assert "foo" in app.pii.keywords
    assert "bar" in app.pii.keywords
    assert "baz" in app.pii.keywords


def test_pii_keywords_empty_uses_default(tmp_path: Path) -> None:
    """``KEYWORDS`` が空文字なら既定値。"""
    path = _write_config(
        tmp_path,
        "[PII]\nKEYWORDS =\n",
    )
    app = _load_app(path)
    # 既定値のキーワードがそのまま使われている
    from src.config_defaults import DEFAULT_PII_KEYWORDS

    assert app.pii.keywords == DEFAULT_PII_KEYWORDS


# ── 旧 config.ini 互換 ─────────────────────────────────


def test_old_config_without_survey_section_uses_defaults(tmp_path: Path) -> None:
    """``[SURVEY]`` / ``[PII]`` セクションが無い旧 config.ini でも、 既定値で動く。"""
    path = _write_config(
        tmp_path,
        "[FILES]\nMASTER_XLSX_PATH = ./input/master.xlsx\n",
    )
    app = _load_app(path)
    assert app.survey.related_depth == 3
    assert app.pii.person_objects == ("Contact", "Lead", "User", "Individual", "PersonAccount")
