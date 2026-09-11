from pathlib import Path

from soql_collector.settings import load_settings


def test_load_settings_with_master_and_csv_paths(tmp_path: Path) -> None:
    master_path = tmp_path / "master.xlsx"
    csv_path = tmp_path / "output.csv"
    (tmp_path / "config.ini").write_text(
        f"[FILES]\nMASTER_XLSX_PATH = {master_path}\nCSV_PATH = {csv_path}\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == master_path
    assert settings.csv_path == csv_path


def test_load_settings_without_optional_paths(tmp_path: Path) -> None:
    (tmp_path / "config.ini").write_text("[FILES]\n", encoding="utf-8")

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path is None
    assert settings.csv_path is None


def test_load_settings_relative_paths_resolved(tmp_path: Path) -> None:
    (tmp_path / "config.ini").write_text(
        "[FILES]\nMASTER_XLSX_PATH = ./input/master.xlsx\nCSV_PATH = ./output/soql_drafts.csv\n",
        encoding="utf-8",
    )

    settings = load_settings(tmp_path)

    assert settings.master_xlsx_path == tmp_path / "input" / "master.xlsx"
    assert settings.csv_path == tmp_path / "output" / "soql_drafts.csv"
    assert settings.master_xlsx_path.is_absolute()
    assert settings.csv_path.is_absolute()
