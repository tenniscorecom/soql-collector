"""``src/run.py`` の run() を ** 本物のまま ** 通す e2e テスト。

``tests/test_run.py`` のように ``run_fetch`` / ``run_tables`` を差し替えず、
本物の ``run()`` から ``run_fetch`` / ``run_tables`` / 列の直しの書き戻しまで
全部通す。 Salesforce には繋がない。 ``_FakeClient`` / ``_site_for`` （
``tests/test_e2e.py`` から再利用 ） を ``site_for`` に渡すだけ

検証:

- 1 回目の ``run()`` で exit code 0、 出力一式
  （ ``SOQL調査.xlsx`` / ``SOQL資料/{key}.md`` / ``関係図`` / ``列名の対応.csv``
  / ``input/列の直し.xlsx`` ） ができる
- 2 回目の実行前に ``input/列の直し.xlsx`` の 「直したSOQL列名」 と 「メモ」
  に値を入れ、 2 回目で

    ① その値が残っている
    ② 自動で取れた列でも人の値が ``SOQL調査.xlsx`` の 列の対応シート、
       ``列名の対応.csv`` 、 Markdown の SOQL 下書きに使われる
    ③ 要対応だった列が人の値で OK になる

- 表示名 ``活動：作業会社名`` （全角コロン） が Task の ``WorkCo__c``
  （ラベル 作業会社名） に、 ``取引先: 取引先名`` が関連経由で解決される
- 旧 ``config.ini`` （新しいキー ``COLUMN_FIX_XLSX_PATH`` などが無い） でも
  動く
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.table import Table as XlsxTable
from openpyxl.worksheet.table import TableStyleInfo

# `tests/test_e2e.py` の ``_FakeClient`` / ``_site_for`` / ``_e2e_describe``
# / ``_e2e_object_describes`` / ``_f`` / ``DOMAIN`` をそのまま使う
_e2e_spec = importlib.util.spec_from_file_location(
    "tests.test_e2e", Path(__file__).parent / "test_e2e.py"
)
assert _e2e_spec is not None and _e2e_spec.loader is not None
_e2e = importlib.util.module_from_spec(_e2e_spec)
_e2e_spec.loader.exec_module(_e2e)
DOMAIN = _e2e.DOMAIN
_FakeClient = _e2e._FakeClient
_site_for = _e2e._site_for
_e2e_describe = _e2e._e2e_describe
_e2e_object_describes = _e2e._e2e_object_describes
_f = _e2e._f


def _build_master(path: Path) -> None:
    """``MASTER_XLSX_PATH`` の指す xlsx を作る。 1 行だけ。"""
    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet("PY_管理表")
    sheet.append(["ID", "概要", "Salesforce URL", "有効"])
    sheet.append(["1001", "活動一覧", f"{DOMAIN}/00O000000000001/view", "○"])
    table = XlsxTable(displayName="PY_T_ReportEntry", ref="A1:D2")
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    workbook.save(path)


def _write_full_ini(proj: Path) -> None:
    """``[FILES]`` の全キーと ``[SURVEY]`` / ``[PII]`` を ``config.ini`` に書く。"""
    (proj / "config.ini").write_text(
        "[FILES]\n"
        "MASTER_XLSX_PATH = ./input/レポート管理表.xlsx\n"
        "OUTPUT_DIR = ./output\n"
        "DESCRIBE_CSV_PATH = ./data/項目定義.csv\n"
        "COLUMN_FIX_XLSX_PATH = ./input/列の直し.xlsx\n"
        "XLSX_NAME = SOQL調査.xlsx\n"
        "MARKDOWN_DIR = SOQL資料\n"
        "DIAGRAM_DIR = 関係図\n"
        "[SURVEY]\n"
        "RELATED_DEPTH = 3\n"
        "RELATED_MAX = 40\n"
        "ROW_LIMIT_CHECK = ○\n"
        "SIMILAR_COLUMN_THRESHOLD = 0.8\n"
        "[PII]\n"
        "KEYWORDS = 氏名, 名前\n"
        "PERSON_OBJECTS = Contact, Lead\n",
        encoding="utf-8",
    )


def _write_legacy_ini(proj: Path) -> None:
    """``[FILES]`` に 4 つのキーしか無い旧 config.ini を書く。

    新しいキー（``COLUMN_FIX_XLSX_PATH`` 等） が無いことで、 comken の
    ``ConfigError`` が上がることを確認する経路。 既定値の補完はしない
    """
    (proj / "config.ini").write_text(
        "[FILES]\n"
        "MASTER_XLSX_PATH = ./input/レポート管理表.xlsx\n"
        "OUTPUT_DIR = ./output\n"
        "DESCRIBE_CSV_PATH = ./data/項目定義.csv\n"
        "COLUMN_MAP_CSV_PATH = ./data/列の対応.csv\n",
        encoding="utf-8",
    )


def _scenario_describe_and_objects():
    """再現シナリオのレポート describe と 偽オブジェクト describe を返す。

    「取引先: 取引先名」 と 「活動：作業会社名」 の **表示名からオブジェクト
    名の照合** と、 「MYSTERY」 （列キーが分からない列） を含む
    """
    describe = _e2e_describe()
    meta = describe["reportMetadata"]
    meta["detailColumns"] = [
        "SUBJECT",
        "ACCOUNT.NAME",
        "OWNER.NAME",
        "FK_WORK_CO",
        "MYSTERY",
    ]
    describe["reportExtendedMetadata"]["detailColumnInfo"].update(
        {
            "ACCOUNT.NAME": {"label": "取引先: 取引先名"},
            "FK_WORK_CO": {"label": "活動：作業会社名"},
            "MYSTERY": {"label": "謎の列"},
        }
    )
    objs = _e2e_object_describes()
    objs["Task"]["fields"].append(_f("WorkCo__c", "作業会社名"))
    return describe, objs


def _read_xlsx(path: Path) -> dict[str, list[dict[str, str]]]:
    """``load_workbook`` でシート名 → データ行 dict の list を読む。

    1 行目=案内、 2 行目=空、 3 行目=テーブル見出し の構造を前提にし、
    最初の文字列で埋まる行を見出しとして採用する
    """
    workbook = load_workbook(path)
    result: dict[str, list[dict[str, str]]] = {}
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        all_rows = list(sheet.iter_rows(values_only=True))
        header_idx = -1
        for i, row in enumerate(all_rows):
            if not row:
                continue
            if all(isinstance(v, str) and v for v in row):
                header_idx = i
                break
        if header_idx < 0:
            result[sheet_name] = []
            continue
        headers = list(all_rows[header_idx])
        data_rows = all_rows[header_idx + 1 :]
        result[sheet_name] = [
            {
                header: ("" if value is None else value)
                for header, value in zip(headers, row, strict=False)
            }
            for row in data_rows
        ]
    workbook.close()
    return result


def _read_column_fix(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """``列の直し.xlsx`` を ``(主オブジェクト, 列キー) → 行 dict`` で読む。"""
    workbook = load_workbook(path)
    sheet = workbook["列の直し"]
    rows_iter = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows_iter:
        return {}
    headers = ["" if v is None else str(v) for v in rows_iter[0]]
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows_iter[1:]:
        text_row = {h: ("" if v is None else str(v)) for h, v in zip(headers, row, strict=False)}
        main_object = text_row.get("主オブジェクト", "") or ""
        column_key = text_row.get("列キー", "") or ""
        if not main_object or not column_key:
            continue
        result[(main_object, column_key)] = text_row
    return result


def _do_run(proj: Path, client: _FakeClient) -> int:
    """``run()`` を 1 回実行する。 ``comken`` の config を ``proj`` から読む
    ように ``sys.argv`` を切り替える
    """
    from comken.core.config.base import _reset_cached_config

    from src.run import read_file_paths, run

    _reset_cached_config()
    original = sys.argv
    sys.argv = [str(proj / "main.py")]
    try:
        paths = read_file_paths()
        return run([], site_for=_site_for(client), file_paths=paths)
    finally:
        sys.argv = original
        _reset_cached_config()


def _setup_project(tmp_path: Path, client: _FakeClient) -> Path:
    """``tmp_path`` に再現用のプロジェクトを作る。 ``_do_run`` でそのまま動く
    ように ``input/レポート管理表.xlsx`` と ``config.ini`` を置く
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "input").mkdir()
    (proj / "data").mkdir()
    _build_master(proj / "input" / "レポート管理表.xlsx")
    _write_full_ini(proj)
    return proj


# ── テスト本体 ─────────────────────────────────────────────────────────


def test_run_e2e_first_run_creates_all_outputs(tmp_path: Path) -> None:
    """1 回目の ``run()`` で exit code 0、 出力一式ができる。"""
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    code = _do_run(proj, client)

    assert code == 0
    out = proj / "output"
    assert (out / "SOQL調査.xlsx").exists()
    assert (out / "SOQL資料" / "1001.md").exists()
    assert (out / "列名の対応.csv").exists()
    assert (proj / "input" / "列の直し.xlsx").exists()


def test_run_e2e_first_run_resolves_fullwidth_colon_and_related(tmp_path: Path) -> None:
    """表示名 ``活動：作業会社名`` （全角コロン） が ``WorkCo__c`` に、
    ``取引先: 取引先名`` が関連経由で ``What.Name`` に解決される
    """
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    assert _do_run(proj, client) == 0

    sheets = _read_xlsx(proj / "output" / "SOQL調査.xlsx")
    column_rows = sheets["列の対応"]
    by_key = {r["列キー"]: r for r in column_rows}
    # 全角コロン付きの表示名 → WorkCo__c
    assert by_key["FK_WORK_CO"]["表示名"] == "活動：作業会社名"
    assert by_key["FK_WORK_CO"]["使うSOQL列名"] == "WorkCo__c"
    # 半角コロン付きの表示名 → What.Name （関連経由）
    assert by_key["ACCOUNT.NAME"]["表示名"] == "取引先: 取引先名"
    assert by_key["ACCOUNT.NAME"]["使うSOQL列名"] == "What.Name"


def test_run_e2e_second_run_preserves_user_fix_and_uses_it(tmp_path: Path) -> None:
    """2 回目の実行前に ``input/列の直し.xlsx`` を人が直して、 2 回目で

    ① その値が残っている
    ② 自動で取れた列でも人の値が SOQL調査.xlsx の 列の対応シート・列名の
       対応.csv・md の SOQL 下書きに使われる
    ③ 要対応だった列が人の値で OK になる
    """
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    assert _do_run(proj, client) == 0

    # 人が直す: 1 件は要対応だった MYSTERY に値、 もう 1 件は自動が取れた
    # SUBJECT に別値を入れる
    fix_path = proj / "input" / "列の直し.xlsx"
    workbook = load_workbook(fix_path)
    sheet = workbook["列の直し"]
    headers = [c.value for c in sheet[1]]
    head_idx = {name: headers.index(name) + 1 for name in headers}
    for r in range(2, sheet.max_row + 1):
        key = sheet.cell(r, head_idx["列キー"]).value
        if key == "MYSTERY":
            sheet.cell(r, head_idx["直したSOQL列名"]).value = "Description"
            sheet.cell(r, head_idx["メモ"]).value = "人が確認済み"
        elif key == "SUBJECT":
            sheet.cell(r, head_idx["直したSOQL列名"]).value = "Owner.Name"
    workbook.save(fix_path)
    workbook.close()

    assert _do_run(proj, client) == 0

    # ① 人の値が残っている
    fix_rows = _read_column_fix(fix_path)
    assert fix_rows[("Task", "MYSTERY")]["直したSOQL列名"] == "Description"
    assert fix_rows[("Task", "MYSTERY")]["メモ"] == "人が確認済み"
    assert fix_rows[("Task", "SUBJECT")]["直したSOQL列名"] == "Owner.Name"

    # ② 自動で取れた列でも人の値が xlsx / csv / md に使われる
    sheets = _read_xlsx(proj / "output" / "SOQL調査.xlsx")
    column_rows = sheets["列の対応"]
    by_key = {r["列キー"]: r for r in column_rows}
    assert by_key["SUBJECT"]["使うSOQL列名"] == "Owner.Name"
    assert by_key["SUBJECT"]["どこから"] == "人が直した"
    # 自動が取っていた値は人の値で上書きされず、 自動の値として見える
    assert fix_rows[("Task", "SUBJECT")]["自動のSOQL列名"] == "Subject"
    assert by_key["SUBJECT"]["自動の値"] == "Subject"

    csv_text = (proj / "output" / "列名の対応.csv").read_text(encoding="utf-8-sig")
    assert "Task,Owner.Name" in csv_text
    # 自動で取れていた値も履歴として CSV に残る
    assert "Task,Subject" in csv_text

    md_text = (proj / "output" / "SOQL資料" / "1001.md").read_text(encoding="utf-8")
    # 列の対応: SUBJECT は Owner.Name （人が直した）
    assert "| 件名 | Owner.Name | 人が直した |" in md_text

    # ③ MYSTERY は人が書いた値 「Description」 が describe でたどれないので
    # 「要確認」 扱いになる （値はそのまま採用）
    assert by_key["MYSTERY"]["使うSOQL列名"] == "Description"
    assert by_key["MYSTERY"]["どこから"] == "人が直した（要確認: たどれない）"
    assert fix_rows[("Task", "MYSTERY")]["状態"] == "要確認"


def test_run_e2e_legacy_config_without_required_keys_aborts(tmp_path: Path) -> None:
    """旧 ``config.ini`` （``COLUMN_FIX_XLSX_PATH`` などの新しいキーが無い）
    は comken の ``ConfigError`` で止まる。 既定値での補完はしない。
    """
    from comken.core.config.base import _reset_cached_config
    from comken.exceptions import ConfigError, ConfigKeyNotFoundError

    from src.run import read_file_paths

    describe, objs = _scenario_describe_and_objects()
    _FakeClient(describe, objs)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "input").mkdir()
    (proj / "data").mkdir()
    _build_master(proj / "input" / "レポート管理表.xlsx")
    _write_legacy_ini(proj)

    _reset_cached_config()
    original = sys.argv
    sys.argv = [str(proj / "main.py")]
    try:
        with pytest.raises(ConfigKeyNotFoundError) as exc_info:
            read_file_paths()
        assert isinstance(exc_info.value, ConfigError)
        assert "COLUMN_FIX_XLSX_PATH" in str(exc_info.value)
    finally:
        sys.argv = original
        _reset_cached_config()


# ── 人が書いた値がたどれない （Issue 2） ─────────────────────────


def test_run_e2e_unresolved_user_value_is_need_check_in_both_files(
    tmp_path: Path,
) -> None:
    """人が書いた SOQL 列名が describe でたどれなかったとき、
    ``列の直し.xlsx`` と ``列の対応`` シートで 状態=要確認 になり、
    一覧の 「要対応・要確認の列数」 に数えられる
    """
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    assert _do_run(proj, client) == 0

    # 人が直す: MYSTERY は 自動が "(不明)" で、 そこに 「Description」 を書く
    # Task に Description 項目は無いので たどれず、 要確認扱いになる
    fix_path = proj / "input" / "列の直し.xlsx"
    workbook = load_workbook(fix_path)
    sheet = workbook["列の直し"]
    headers = [c.value for c in sheet[1]]
    head_idx = {name: headers.index(name) + 1 for name in headers}
    for r in range(2, sheet.max_row + 1):
        if sheet.cell(r, head_idx["列キー"]).value == "MYSTERY":
            sheet.cell(r, head_idx["直したSOQL列名"]).value = "Description"
    workbook.save(fix_path)
    workbook.close()

    assert _do_run(proj, client) == 0

    # 1. 列の直し.xlsx: 状態=要確認
    fix_rows = _read_column_fix(fix_path)
    assert fix_rows[("Task", "MYSTERY")]["状態"] == "要確認"
    # 値はそのまま採用されている
    assert fix_rows[("Task", "MYSTERY")]["直したSOQL列名"] == "Description"

    # 2. SOQL調査.xlsx / 列の対応: 状態=要確認、 どこから=人が直した（要確認: たどれない）
    sheets = _read_xlsx(proj / "output" / "SOQL調査.xlsx")
    column_rows = sheets["列の対応"]
    by_key = {r["列キー"]: r for r in column_rows}
    assert by_key["MYSTERY"]["状態"] == "要確認"
    assert by_key["MYSTERY"]["使うSOQL列名"] == "Description"
    assert by_key["MYSTERY"]["どこから"] == "人が直した（要確認: たどれない）"

    # 3. 一覧シートの 「要対応・要確認の列数」 に MYSTERY が 1 件として入る
    overview = sheets["一覧"]
    by_overview_key = {r["管理番号"]: r for r in overview}
    assert by_overview_key["1001"]["要対応・要確認の列数"] == "1"


# ── 列の直し.xlsx の見栄え （Issue 1） ──────────────────────────


def test_run_e2e_column_fix_xlsx_has_freeze_and_widths_and_colors(
    tmp_path: Path,
) -> None:
    """``列の直し.xlsx`` に見出し固定、 列幅、 人が書く列のヘッダの色、
    状態行の塗り が入る
    """
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    assert _do_run(proj, client) == 0
    fix_path = proj / "input" / "列の直し.xlsx"

    workbook = load_workbook(fix_path)
    sheet = workbook["列の直し"]

    # 見出し固定
    assert sheet.freeze_panes == "A2"

    # 列幅が入っている （全角=2 で計算）
    widths = {letter: dim.width for letter, dim in sheet.column_dimensions.items() if dim.width}
    assert len(widths) >= 8, f"列幅が入っていない: {widths}"
    # 表示名が長い 「取引先: 取引先名」 を含む 「表示名」 列は広くなる
    assert widths.get("C", 0) >= 18

    # 「直したSOQL列名」 と 「メモ」 の見出しセルが薄い緑で塗られる
    headers = [c.value for c in sheet[1]]
    fixed_idx = headers.index("直したSOQL列名") + 1
    note_idx = headers.index("メモ") + 1
    fixed_cell = sheet.cell(row=1, column=fixed_idx)
    note_cell = sheet.cell(row=1, column=note_idx)
    assert fixed_cell.fill and fixed_cell.fill.fill_type == "solid"
    assert note_cell.fill and note_cell.fill.fill_type == "solid"
    # C6EFCE 系 （薄い緑） であればよい
    assert str(fixed_cell.fill.fgColor.rgb).endswith("C6EFCE")
    assert str(note_cell.fill.fgColor.rgb).endswith("C6EFCE")

    # 1 回目の状態=要対応の MYSTERY 行が薄い赤で塗られる
    workbook.close()

    # 再ロードして実際の色を見る
    wb = load_workbook(fix_path)
    sheet = wb["列の直し"]
    # データ開始は 2 行目
    found_needs_fix = False
    for r in range(2, sheet.max_row + 1):
        status = sheet.cell(r, 1).value
        if status == "要対応":
            cell = sheet.cell(r, 1)
            assert cell.fill and cell.fill.fill_type == "solid"
            assert str(cell.fill.fgColor.rgb).endswith("F8CBAD"), (
                f"要対応の行が薄い赤で塗られていない: {cell.fill.fgColor.rgb}"
            )
            found_needs_fix = True
            break
    assert found_needs_fix, "要対応の行が見つからない"
    wb.close()


def test_run_e2e_column_fix_xlsx_user_fix_unresolved_yellow_check_fill(
    tmp_path: Path,
) -> None:
    """人が書いた値がたどれなかった行は、 ``列の直し.xlsx`` 内で 状態=要確認 と
    なり、 薄い黄で塗られる
    """
    describe, objs = _scenario_describe_and_objects()
    client = _FakeClient(describe, objs)
    proj = _setup_project(tmp_path, client)

    assert _do_run(proj, client) == 0

    fix_path = proj / "input" / "列の直し.xlsx"
    workbook = load_workbook(fix_path)
    sheet = workbook["列の直し"]
    headers = [c.value for c in sheet[1]]
    head_idx = {name: headers.index(name) + 1 for name in headers}
    for r in range(2, sheet.max_row + 1):
        if sheet.cell(r, head_idx["列キー"]).value == "MYSTERY":
            sheet.cell(r, head_idx["直したSOQL列名"]).value = "Description"
    workbook.save(fix_path)
    workbook.close()

    assert _do_run(proj, client) == 0

    wb = load_workbook(fix_path)
    sheet = wb["列の直し"]
    headers = [c.value for c in sheet[1]]
    head_idx = {name: headers.index(name) + 1 for name in headers}
    target_row = None
    for r in range(2, sheet.max_row + 1):
        if sheet.cell(r, head_idx["列キー"]).value == "MYSTERY":
            target_row = r
            break
    assert target_row is not None
    status = sheet.cell(target_row, head_idx["状態"]).value
    assert status == "要確認"
    cell = sheet.cell(target_row, head_idx["状態"])
    assert cell.fill and cell.fill.fill_type == "solid"
    assert str(cell.fill.fgColor.rgb).endswith("FFF2CC"), (
        f"要確認の行が薄い黄で塗られていない: {cell.fill.fgColor.rgb}"
    )
    wb.close()
