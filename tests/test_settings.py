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
    assert settings.credential_prefix == ""
    # ``[OBJECTS]`` セクションが無いときは両方とも既定値
    assert settings.objects_names == ()
    assert settings.objects_org_id is None


def test_load_settings_relative_paths_resolved(tmp_path: Path) -> None:
    _write_ini(
        tmp_path,
        "[FILES]\n"
        "MASTER_XLSX_PATH = ./input/master.xlsx\n"
        "OUTPUT_DIR = ./output\n"
        "[LIMITS]\nRELATED_MAX = 12\n"
        "[SF]\nCREDENTIAL_PREFIX = dev\n",
    )

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == tmp_path / "input" / "master.xlsx"
    assert settings.output_dir == tmp_path / "output"
    assert settings.related_max == 12
    assert settings.credential_prefix == "dev"


def test_load_settings_without_master_path_raises(tmp_path: Path) -> None:
    _write_ini(tmp_path, "[FILES]\n")

    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)


def test_load_settings_missing_config_file_raises(tmp_path: Path) -> None:
    # config.ini が無い場合も MASTER_XLSX_PATH 不足と同じエラー
    with pytest.raises(ConfigKeyNotFoundError):
        load_settings(tmp_path)


# ── [OBJECTS] セクション ─────────────────────────────────────────────────


def test_load_settings_objects_names_comma(tmp_path: Path) -> None:
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n"
        "[OBJECTS]\nNAMES = Account, Contact, Custom__c\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_names == ("Account", "Contact", "Custom__c")
    assert settings.objects_org_id is None


def test_load_settings_objects_names_jp_comma(tmp_path: Path) -> None:
    """読点 ``、`` 区切りも受け付ける。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[OBJECTS]\nNAMES = Account、Contact\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_names == ("Account", "Contact")


def test_load_settings_objects_names_trims_whitespace(tmp_path: Path) -> None:
    """前後の空白は除く。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[OBJECTS]\nNAMES =  Account ,   Contact\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_names == ("Account", "Contact")


def test_load_settings_objects_names_dedup(tmp_path: Path) -> None:
    """重複は 1 つにまとめる。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n"
        "[OBJECTS]\nNAMES = Account, Contact, Account, Contact\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_names == ("Account", "Contact")


def test_load_settings_objects_names_empty_ignored(tmp_path: Path) -> None:
    """空要素は無視。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[OBJECTS]\nNAMES = Account, , Contact,\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_names == ("Account", "Contact")


def test_load_settings_objects_org_id_present(tmp_path: Path) -> None:
    """``ORG_ID`` が書かれているときは ``str`` で持つ。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[OBJECTS]\nNAMES = Account\nORG_ID = 1001\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_org_id == "1001"


def test_load_settings_objects_org_id_missing(tmp_path: Path) -> None:
    """``ORG_ID`` が無いときは ``None`` 。"""
    master_path = tmp_path / "master.xlsx"
    _write_ini(
        tmp_path,
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\n[OBJECTS]\nNAMES = Account\n",
    )

    settings = load_settings(tmp_path)

    assert settings.objects_org_id is None
