from pathlib import Path

import pytest
from comken.exceptions import ConfigKeyNotFoundError

from src.settings import load_settings


def _write_ini(tmp_path: Path, content: str) -> None:
    (tmp_path / "config.ini").write_text(content, encoding="utf-8")


def test_load_settings_with_master(tmp_path: Path) -> None:
    master_path = tmp_path / "master.xlsx"
    _write_ini(tmp_path, f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n")

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == master_path
    assert settings.output_dir == tmp_path / "output"
    assert settings.related_max == 40
    # ``RELATED_DEPTH`` が無いときは既定の 3
    assert settings.related_depth == 3
    assert settings.credential_prefix == ""


def test_load_settings_related_depth_explicit(tmp_path: Path) -> None:
    """``[LIMITS] RELATED_DEPTH`` が書かれていればその値。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[LIMITS]\nRELATED_DEPTH = 5\n",
    )

    settings = load_settings(tmp_path)

    assert settings.related_depth == 5


def test_load_settings_related_depth_invalid_raises(tmp_path: Path) -> None:
    """``RELATED_DEPTH`` が数字以外 / 1 未満なら ``ConfigKeyNotFoundError``。"""
    import pytest

    master_path = tmp_path / "master.xlsx"

    # 数字以外
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[LIMITS]\nRELATED_DEPTH = abc\n",
    )
    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)

    # 1 未満
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[LIMITS]\nRELATED_DEPTH = 0\n",
    )
    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)


def test_load_settings_relative_paths_resolved(tmp_path: Path) -> None:
    _write_ini(
        tmp_path,
        "[FILES]\n"
        "MASTER_XLSX_PATH = ./input/master.xlsx\n"
        "OUTPUT_DIR = ./output\n"
        "[LIMITS]\nRELATED_MAX = 12\nRELATED_DEPTH = 2\n"
        "[SF]\nCREDENTIAL_PREFIX = dev\n",
    )

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == tmp_path / "input" / "master.xlsx"
    assert settings.output_dir == tmp_path / "output"
    assert settings.related_max == 12
    assert settings.related_depth == 2
    assert settings.credential_prefix == "dev"


def test_load_settings_without_master_path_raises(tmp_path: Path) -> None:
    _write_ini(tmp_path, "[FILES]\n")

    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)


def test_load_settings_missing_config_file_raises(tmp_path: Path) -> None:
    # config.ini が無い場合も MASTER_XLSX_PATH 不足と同じエラー
    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)


# ── [OBJECTS] セクションは無視する ──────────────────────────────────────


def test_load_settings_ignores_objects_section(tmp_path: Path) -> None:
    """``[OBJECTS]`` セクションが config.ini に残っていても無視する
    （個別オブジェクトの取得は廃止）。
    """
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n"
        "[OBJECTS]\nNAMES = Account, Contact\nORG_ID = 1001\n",
    )

    # エラーにならず読み込める。 古い ``[OBJECTS]`` の値も ``Settings`` には
    # 載らない （個別オブジェクト関連のフィールド自体がないため ） 。
    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == master_path
    # 関連フィールドが存在しないことを確認
    assert not hasattr(settings, "objects_names")
    assert not hasattr(settings, "objects_org_id")
