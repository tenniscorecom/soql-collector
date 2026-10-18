"""人が Excel で開いて直す ``列の直し.xlsx`` の読み・書き・上書き適用。

``src/mapping.build_column_map`` が自動で取った列名より **人が Excel で書いた
「直したSOQL列名」 を常に優先する**。 ファイルが無いときは 「直したSOQL列名」
は空とみなして、 自動の結果をそのまま採用する。

ファイル形式:

- 1 シート、 1 つの Excel テーブル （構造化） 。 名前は ``ColumnFix``
- 列 （この順） : 状態 / 主オブジェクト / 表示名 / 列キー / 自動のSOQL列名 /
  候補 / 直したSOQL列名 / メモ / 使っているレポート / 更新日
- 並び: 状態 （要対応 → 要確認 → OK） → 主オブジェクト → 列キー
- 状態: ``OK`` （自動が取った・人が直した） / ``要対応`` （自動も人も取れず）
  / ``要確認`` （候補が複数、 人の値がたどれない）

見栄え: 見出し行は 1 行目で固定、 列幅は全角=2・半角=1 で整え （最小 8・最大 60） 、
人が書く 「直したSOQL列名」 と 「メモ」 の見出しセルは薄い緑、 状態=要対応の
行は薄い赤、 状態=要確認の行は薄い黄で塗る

実行のたびに:

- 新しい列キー: 行を足す
- 既存の列キー: 「状態 / 表示名 / 自動のSOQL列名 / 候補 / 使っているレポート /
  更新日」 を今回の値で書き換える
- 「直したSOQL列名」 と 「メモ」 は **絶対に書き換えない・消さない**
  （人の入力を尊重する）
- ファイルが ``PermissionError`` で書けない （Excel で開いている等） ときは
  警告だけ出して他の出力は作る。 読み込めない・見出しが違うときは例外で止める
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from comken.exceptions import ExcelError

from src import mapping, xlsx_style

if TYPE_CHECKING:
    from src.fetch import ReportRecord

logger = logging.getLogger(__name__)


# ── シート・列名定数 ─────────────────────────────────────────────────


SHEET_NAME = "列の直し"

# xlsx テーブルの表示名 （構造化参照に使われる名前）
TABLE_NAME = "ColumnFix"

# xlsx の列順
COLUMN_FIX_COLUMNS: tuple[str, ...] = (
    "状態",
    "主オブジェクト",
    "表示名",
    "列キー",
    "自動のSOQL列名",
    "候補",
    "直したSOQL列名",
    "メモ",
    "使っているレポート",
    "更新日",
)

# 状態列の値
STATUS_OK = "OK"
STATUS_NEEDS_FIX = "要対応"
STATUS_NEEDS_CHECK = "要確認"

# 表示順
STATUS_ORDER: dict[str, int] = {
    STATUS_NEEDS_FIX: 0,
    STATUS_NEEDS_CHECK: 1,
    STATUS_OK: 2,
}


# ── 行型 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ColumnFixRow:
    """``列の直し.xlsx`` の 1 行分。 人が編集するセル （直したSOQL列名 / メモ） は
    そのまま保持する。 自動が毎回更新するセル （状態 / 表示名 / 自動のSOQL列名 /
    候補 / 使っているレポート / 更新日） は ``to_observed`` で取り出す。
    """

    status: str
    main_object: str
    display_name: str
    column_key: str
    auto_soql_name: str
    candidates: str
    fixed_soql_name: str
    note: str
    used_in_reports: str
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "状態": self.status,
            "主オブジェクト": self.main_object,
            "表示名": self.display_name,
            "列キー": self.column_key,
            "自動のSOQL列名": self.auto_soql_name,
            "候補": self.candidates,
            "直したSOQL列名": self.fixed_soql_name,
            "メモ": self.note,
            "使っているレポート": self.used_in_reports,
            "更新日": self.updated_at,
        }

    @classmethod
    def from_dict(cls, row: dict[str, str]) -> ColumnFixRow:
        return cls(
            status=row.get("状態", ""),
            main_object=row.get("主オブジェクト", ""),
            display_name=row.get("表示名", ""),
            column_key=row.get("列キー", ""),
            auto_soql_name=row.get("自動のSOQL列名", ""),
            candidates=row.get("候補", ""),
            fixed_soql_name=row.get("直したSOQL列名", ""),
            note=row.get("メモ", ""),
            used_in_reports=row.get("使っているレポート", ""),
            updated_at=row.get("更新日", ""),
        )


# ── 既存ファイルの読み込み ─────────────────────────────────────────


def load_column_fix(
    path: Path,
) -> dict[tuple[str, str], ColumnFixRow]:
    """``path`` の ``列の直し.xlsx`` を読み ``(主オブジェクト小文字, 列キー) → 行`` を返す。

    - ファイルが存在しないときは空 dict を返す （新規扱い）
    - シート名が見出しと違う / 見出しが違うときは ``ExcelError`` を上げる。
      ファイルは変更しない
    - 主オブジェクトか列キーが空の行は読み飛ばす
    """
    if not path.exists():
        return {}
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {}
    workbook = load_workbook(path, read_only=True, data_only=True)
    if SHEET_NAME not in workbook.sheetnames:
        workbook.close()
        raise ExcelError(
            f"既存の {path.name} に『{SHEET_NAME}』シートがありません: {path}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )
    sheet = workbook[SHEET_NAME]
    rows = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not rows:
        return {}
    headers = ["" if v is None else str(v) for v in rows[0]]
    if list(headers) != list(COLUMN_FIX_COLUMNS):
        raise ExcelError(
            f"既存の {path.name} の見出しが想定と違います: {path}\n"
            f"期待: {list(COLUMN_FIX_COLUMNS)}\n"
            f"実際: {list(headers)}\n"
            "対処: ファイルを別名に退避してから再実行してください。"
        )
    result: dict[tuple[str, str], ColumnFixRow] = {}
    for row in rows[1:]:
        # 各セルを文字列に直す。 None は空文字
        str_row = {h: ("" if v is None else str(v)) for h, v in zip(headers, row, strict=False)}
        row_obj = ColumnFixRow.from_dict(str_row)
        if not row_obj.main_object or not row_obj.column_key:
            continue
        result[_normalize_key(row_obj.main_object, row_obj.column_key)] = row_obj
    return result


# ── 自動結果からの observed 行 ─────────────────────────────────────


@dataclass(frozen=True)
class ObservedAutoRow:
    """1 レポートの ``column_map`` の 1 行を、 自動が取れた／取れなかった 視点で
    持つ中間形式。 人の欄を埋める材料は渡さない。
    """

    main_object: str
    column_key: str
    display_name: str
    soql_name: str  # 自動で取れた値。 空のこともある
    candidates: str  # 「複数候補あり: ...」 の文字列。 無いときは空
    note: str  # 「列キーから」 / 「対応フィールドなし」 など
    owner_object: str = ""


# ── マージして書く ───────────────────────────────────────────────────


def merge_and_write(
    path: Path,
    existing: dict[tuple[str, str], ColumnFixRow],
    observed: list[ObservedAutoRow],
    today: date,
    *,
    used_in_reports: dict[tuple[str, str], str] | None = None,
) -> None:
    """``observed`` を ``existing`` にマージし、 xlsx を書き出す。

    ルール:

    - ``observed`` に無いキー → 触らない
    - ``observed`` に有るキーで 「直したSOQL列名」 が既に値 → **自動の値は
      「自動のSOQL列名」 列に書き、 人の値はそのまま残す。 状態は OK。**
    - ``observed`` に有るキーで 「直したSOQL列名」 が空かつ ``observed.soql_name``
      が空でない → 自動の値で 「自動のSOQL列名」 を埋め、 状態を OK
    - ``observed`` に有るキーで ``observed.soql_name`` が空 (取れず) かつ
      人の値も無い → 状態を要対応、 候補・備考をそのまま
    - 候補が複数 （``observed.candidates`` に 「複数候補あり」 を含む） ときは
      状態を要確認
    - 表示名は毎回 ``observed.display_name`` で書き換える
    - 「自動のSOQL列名」 「候補」 「状態」 「使っているレポート」 「更新日」 は
      今回用いた行で毎回書き換える
    - 「直したSOQL列名」 と 「メモ」 は **絶対に書き換えない・消さない**
    - 並び: 状態 → 主オブジェクト → 列キー
    - 「使っているレポート」 は ``used_in_reports`` （キー → ``|`` 区切りの管理番号）
      にあればその値、 無ければ既存の値のまま
    """
    used_in_reports = used_in_reports or {}
    iso_date = today.isoformat()
    new_rows = {
        _normalize_key(obs.main_object, obs.column_key): obs
        for obs in observed
        if obs.main_object and obs.column_key
    }

    # 全行を組み立て直す: 既存 + 新規/上書き分
    merged: dict[tuple[str, str], ColumnFixRow] = dict(existing)
    for key, obs in new_rows.items():
        existing_row = existing.get(key)
        # 状態・自動の値・候補・レポートは毎回書き換える
        auto_resolved = bool(obs.soql_name) and obs.soql_name != "(不明)"
        candidates_text = obs.candidates
        # 人の値があるか
        had_user_fix = bool(existing_row and existing_row.fixed_soql_name)
        # 人の値がたどれたかは、 apply_overrides が column_map の備考に書いた
        # 「人が直した（要確認: たどれない）」 印で判定する
        user_fix_unresolved = "人が直した（要確認: たどれない）" in obs.note
        # 状態: 人の値があり、たどれれば OK。 人の値があるがたどれなかった、 または
        # 候補が複数なら要確認。 自動が取ってれば OK。 人の値も自動の値も無いと要対応
        if had_user_fix and user_fix_unresolved:
            status = STATUS_NEEDS_CHECK
        elif had_user_fix:
            status = STATUS_OK
        elif candidates_text and "複数候補あり" in candidates_text:
            status = STATUS_NEEDS_CHECK
        elif auto_resolved:
            status = STATUS_OK
        else:
            status = STATUS_NEEDS_FIX
        # 「直したSOQL列名」 と 「メモ」 は人の値があればそのまま残す
        if existing_row:
            fixed_soql_name = existing_row.fixed_soql_name
            note = existing_row.note
        else:
            fixed_soql_name = ""
            note = ""
        merged[key] = ColumnFixRow(
            status=status,
            main_object=obs.main_object,
            display_name=obs.display_name,
            column_key=obs.column_key,
            auto_soql_name=obs.soql_name if obs.soql_name else "",
            candidates=candidates_text,
            fixed_soql_name=fixed_soql_name,
            note=note,
            used_in_reports=used_in_reports.get(
                key, existing_row.used_in_reports if existing_row else ""
            ),
            updated_at=iso_date,
        )

    # 並び: 状態 → 主オブジェクト → 列キー
    sorted_rows = sorted(
        merged.values(),
        key=lambda r: (
            STATUS_ORDER.get(r.status, 99),
            r.main_object.lower(),
            r.column_key,
        ),
    )
    _write_xlsx(path, sorted_rows)


def _write_xlsx(
    path: Path,
    rows: list[ColumnFixRow],
) -> None:
    """``列の直し.xlsx`` を書く。 PermissionError は呼び出し側で WARNING に縮退。

    見栄え:

    - 見出し行を 1 行目で固定
    - 列幅を全角=2・半角=1 で計算 （最小 8・最大 60・超は折り返し）
    - 「直したSOQL列名」 と 「メモ」 の見出しセルを薄い緑で塗り、
      人が書く列だと目で分かるようにする
    - 状態列が 要対応/要確認 の行は薄い赤/薄い黄で塗る （``SOQL調査.xlsx`` と同じ色）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # openpyxl で 1 シート・ 1 テーブル構造の xlsx を作る
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.table import Table as XlsxTable
    from openpyxl.worksheet.table import TableStyleInfo

    workbook = Workbook()
    active = workbook.active
    if active is not None:
        workbook.remove(active)
    sheet = workbook.create_sheet(SHEET_NAME)
    sheet.append(list(COLUMN_FIX_COLUMNS))
    row_dicts = [r.to_dict() for r in rows]
    for row in rows:
        values = [
            row.status,
            row.main_object,
            row.display_name,
            row.column_key,
            row.auto_soql_name,
            row.candidates,
            row.fixed_soql_name,
            row.note,
            row.used_in_reports,
            row.updated_at,
        ]
        sheet.append(values)
    end_row = max(len(rows) + 1, 1)
    end_col_letter = _get_column_letter(len(COLUMN_FIX_COLUMNS))
    table = XlsxTable(
        displayName=TABLE_NAME,
        ref=f"A1:{end_col_letter}{end_row}",
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)
    # 見出し行を固定
    sheet.freeze_panes = "A2"
    # 列幅を整える
    xlsx_style.adjust_column_widths(
        sheet,
        list(COLUMN_FIX_COLUMNS),
        row_dicts,
        start_row=1,
    )
    # 「直したSOQL列名」 と 「メモ」 の見出しセルを薄い緑で塗る
    user_header_fill = PatternFill(
        start_color=xlsx_style.FILL_USER_INPUT,
        end_color=xlsx_style.FILL_USER_INPUT,
        fill_type="solid",
    )
    for header_name in ("直したSOQL列名", "メモ"):
        if header_name not in COLUMN_FIX_COLUMNS:
            continue
        col_index = COLUMN_FIX_COLUMNS.index(header_name) + 1
        cell = sheet.cell(row=1, column=col_index)
        cell.fill = user_header_fill
        cell.font = Font(bold=True)
    # 状態列の値で塗り分け
    _apply_status_fills_to_fix_sheet(sheet, rows)
    workbook.save(path)


def _apply_status_fills_to_fix_sheet(
    sheet: Any,
    rows: list[ColumnFixRow],
) -> None:
    """``列の直し.xlsx`` のデータ行を、 状態で塗り分ける。

    ``tables._apply_status_fills`` と同じ配色 （要対応=薄い赤、 要確認=薄い黄）
    に揃え、 ``列の対応`` シートと色を一致させる
    """
    from openpyxl.styles import PatternFill

    fill_fix = PatternFill(
        start_color=xlsx_style.FILL_NEEDS_FIX,
        end_color=xlsx_style.FILL_NEEDS_FIX,
        fill_type="solid",
    )
    fill_check = PatternFill(
        start_color=xlsx_style.FILL_NEEDS_CHECK,
        end_color=xlsx_style.FILL_NEEDS_CHECK,
        fill_type="solid",
    )
    last_col = len(COLUMN_FIX_COLUMNS)
    for row_offset, row in enumerate(rows):
        if row.status == STATUS_NEEDS_FIX:
            fill = fill_fix
        elif row.status == STATUS_NEEDS_CHECK:
            fill = fill_check
        else:
            continue
        for col_index in range(1, last_col + 1):
            cell = sheet.cell(row=2 + row_offset, column=col_index)
            cell.fill = fill


def _get_column_letter(idx: int) -> str:
    """1-indexed の列番号を Excel の列文字（A, B, ...） にする。"""
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx)


# ── 古い CSV からの移行 ─────────────────────────────────────────────


# 旧 ``data/列の対応.csv`` 形式の行を 「直したSOQL列名」 にコピーするときの
# 必要な列名。 新形式との対応は次のとおり。
_OLD_CSV_COLUMNS: tuple[str, ...] = (
    "主オブジェクト",
    "列キー",
    "表示名",
    "SOQL列名",
    "メモ",
    "使っているレポート数",
    "更新日",
)


def migrate_legacy_csv(
    legacy_path: Path,
) -> dict[tuple[str, str], ColumnFixRow]:
    """旧 ``data/列の対応.csv`` から 「メモ=手入力 かつ SOQL列名あり」 の行だけを
    ``ColumnFixRow`` に変換して返す。 該当が無いときは空 dict 。
    """
    if not legacy_path.exists():
        return {}
    import csv

    try:
        with legacy_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            try:
                columns = next(reader)
            except StopIteration:
                return {}
            rows = [dict(zip(columns, line, strict=True)) for line in reader]
    except (OSError, UnicodeDecodeError):
        return {}

    if list(columns) != list(_OLD_CSV_COLUMNS):
        return {}

    result: dict[tuple[str, str], ColumnFixRow] = {}
    for row in rows:
        soql_name = row["SOQL列名"]
        note = row["メモ"]
        main_object = row["主オブジェクト"]
        column_key = row["列キー"]
        if not soql_name or note != "手入力" or not main_object or not column_key:
            continue
        result[_normalize_key(main_object, column_key)] = ColumnFixRow(
            status=STATUS_OK,
            main_object=main_object,
            display_name=row["表示名"],
            column_key=column_key,
            auto_soql_name="",
            candidates="",
            fixed_soql_name=soql_name,
            note=note,
            used_in_reports=row["使っているレポート数"],
            updated_at=row["更新日"],
        )
    return result


# ── column_map への上書き適用 ───────────────────────────────────────


def apply_overrides(
    column_map_rows: list[dict[str, str]],
    existing: dict[tuple[str, str], ColumnFixRow],
    main_object: str,
    objects: dict[str, dict[str, Any]],
    warnings: list[str],
    *,
    fetch_object: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[dict[str, str]]:
    """``column_map`` の各行に ``列の直し.xlsx`` の上書きを適用する。

    ルール:

    - **「直したSOQL列名」 に値があれば自動の結果より常に優先** （自動が
      取れた列でも、 人の値で上書きされる）
    - 値が無いとき、 自動が既に取れていればそのまま
    - 自動も取れず、 人の値も無ければそのまま
    - 人の値で上書きしたときは、 ``型`` を ``objects`` から ``Account.Name`` のような
      ドット記法でたどって引く。 引けなければ 「打ち間違い？」 として状態=要確認の
      ヒントを ``warnings`` に足す （値はそのまま使う）
    - たどれなかったときは備考に 「人が直した（要確認: たどれない）」 と付け、
      ``tables`` 側で要確認として数えられるようにする
    - 鍵は **``main_object``** （主オブジェクト） 。 同じ ``Opp.Account.Name``
      でも 「所属オブジェクト」 は ``Account`` と ``Opportunity`` で変わる一方、
      SOQL の起点は主オブジェクトなので
    """
    new_rows: list[dict[str, str]] = []
    for row in column_map_rows:
        column_key = row["列キー"]
        if not main_object or not column_key:
            new_rows.append(dict(row))
            continue
        override = existing.get(_normalize_key(main_object, column_key))
        if override is None or not override.fixed_soql_name:
            new_rows.append(dict(row))
            continue
        # 人の値で上書きする。 自動が取っていた値は 「自動の値」 に残す
        # （列の直し.xlsx の 「自動のSOQL列名」 と列の対応シートで見せるため）
        new_row = dict(row)
        new_row["自動の値"] = row["対応フィールドAPI名"]
        new_row["対応フィールドAPI名"] = override.fixed_soql_name
        # 型を describe から引く
        resolved_type = _walk_soql_path(
            main_object=main_object,
            soql_name=override.fixed_soql_name,
            objects=objects,
            fetch_object=fetch_object,
            column_key=column_key,
            warnings=warnings,
        )
        if resolved_type:
            new_row["型"] = resolved_type
            # 自動が取っていても人の値に置き換わるので、 「自動が取ってくれた」
            # 印は外し、 「人が直した」 ことを notes に残す
            new_row["備考"] = "直したSOQL列名（人が直した）"
        else:
            # たどれなかった。 値はそのまま採用するが、 要確認であることを備考で示す
            new_row["備考"] = "人が直した（要確認: たどれない）"
        new_rows.append(new_row)
    return new_rows


# ── 型を引くための経路の辿り ───────────────────────────────────────


_REFERENCE_TO_PATTERN = re.compile(r"\s+")


def _walk_soql_path(
    *,
    main_object: str,
    soql_name: str,
    objects: dict[str, dict[str, Any]],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
    column_key: str,
    warnings: list[str],
) -> str:
    """``soql_name`` （``Account.Owner.Name`` のようなドット記法） を
    ``main_object`` から 1 段ずつたどり、 最後の項目の ``型`` を返す。

    段数は 5 段まで。 6 段以上なら 「経路が長すぎます」 WARNING を出す。
    たどれなかったときの WARNING は 「どこで切れたか」 が分かる文にする。
    自動が取れた列を上書きする経路なので、 引けなかったときは 「打ち間違い？」 の
    ヒントだけ出し、 値はそのまま使う

    ``soql_name`` の先頭セグメントが ``main_object`` と同じならスキップする
    （人が ``Account.Name`` と書いても 主オブジェクト ``Account`` 起点で
    辿れるように）
    """
    parts = _split_path_parts(soql_name, main_object, column_key, warnings)
    if parts is None:
        return ""
    if not parts:
        return ""

    current_object = main_object
    for index, part in enumerate(parts):
        describe = objects.get(current_object)
        if describe is None:
            _warn_missing_describe(main_object, column_key, soql_name, current_object, warnings)
            return ""
        fields: list[dict[str, Any]] = describe.get("fields", [])

        matched = _find_matching_field(fields, part)
        if matched is None:
            _warn_field_not_found(
                main_object, column_key, soql_name, current_object, part, fields, warnings
            )
            return ""

        if index == len(parts) - 1:
            return matched.get("type") or ""

        next_object = _resolve_next_object(matched, objects, fetch_object)
        if next_object is None:
            _warn_target_unresolved(main_object, column_key, soql_name, matched, warnings)
            return ""
        current_object = next_object

    return ""


def _split_path_parts(
    soql_name: str,
    main_object: str,
    column_key: str,
    warnings: list[str],
) -> list[str] | None:
    """``soql_name`` を ``.`` で分割し、 ``main_object`` と同名の先頭セグメントを落とす。

    戻り値: 辿るべきセグメントのリスト。 ``None`` のときは ``warnings`` に
    経路が長すぎる旨を入れて探索を打ち切る。 空リストは 「辿る必要が無い」
    (先頭が ``main_object`` だけだった等) ことを示す。
    """
    if not soql_name or not main_object:
        return []
    parts = soql_name.split(".")
    if parts and parts[0].lower() == main_object.lower():
        parts = parts[1:]
    if not parts:
        return []
    if len(parts) > 5:
        warnings.append(
            f"列の直し.xlsx の直したSOQL列名をたどれません: "
            f"{main_object} / {column_key} / {soql_name}"
            f" — 経路が長すぎます（5 段まで）。打ち間違い？"
        )
        return None
    return parts


def _find_matching_field(
    fields: list[dict[str, Any]],
    part: str,
) -> dict[str, Any] | None:
    """``part`` に一致する項目を ``relationshipName`` 優先、 無ければ ``name`` で探す。"""
    matched = next((field for field in fields if field.get("relationshipName") == part), None)
    if matched is not None:
        return matched
    return next((field for field in fields if field.get("name") == part), None)


def _resolve_next_object(
    matched_field: dict[str, Any],
    objects: dict[str, dict[str, Any]],
    fetch_object: Callable[[str], dict[str, Any] | None] | None,
) -> str | None:
    """参照項目 ``matched_field`` の次段のオブジェクト名を解決する。"""
    candidates = _candidate_next_refs(matched_field, objects)
    for cand in candidates:
        if cand in objects:
            return cand
        if fetch_object is not None:
            fetched = fetch_object(cand)
            if fetched is not None:
                objects[cand] = fetched
                return cand
    return None


def _candidate_next_refs(
    matched_field: dict[str, Any],
    objects: dict[str, dict[str, Any]],
) -> list[str]:
    """次段候補の参照オブジェクト名 (重複なし) を並べる。"""
    reference_to: list[str] = matched_field.get("referenceTo") or []
    relationship = matched_field.get("relationshipName")
    is_polymorphic = len(reference_to) > 1
    candidates: list[str] = []
    if is_polymorphic:
        for ref in reference_to:
            if ref not in candidates:
                candidates.append(ref)
        return candidates
    # 非ポリモーフィック: ``relationshipName`` が既に手元にあればそれを優先
    if relationship and relationship in objects and not candidates:
        candidates.append(relationship)
    if not candidates:
        for ref in reference_to:
            if ref not in candidates:
                candidates.append(ref)
    return candidates


def _warn_missing_describe(
    main_object: str,
    column_key: str,
    soql_name: str,
    current_object: str,
    warnings: list[str],
) -> None:
    """``current_object`` の項目定義が無いときの WARNING を積む。"""
    warnings.append(
        f"列の直し.xlsx の直したSOQL列名をたどれません: "
        f"{main_object} / {column_key} / {soql_name}"
        f" — {current_object} の項目定義が無いためたどれません。打ち間違い？"
    )


def _warn_field_not_found(
    main_object: str,
    column_key: str,
    soql_name: str,
    current_object: str,
    part: str,
    fields: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """``part`` が ``current_object`` に見つからないときの WARNING を積む。"""
    candidates = mapping.candidate_relationship_names(fields)
    candidate_text = ", ".join(candidates) if candidates else "(なし)"
    warnings.append(
        f"列の直し.xlsx の直したSOQL列名をたどれません: "
        f"{main_object} / {column_key} / {soql_name}"
        f" — 「{part}」が {current_object} に見つかりません"
        f"（リレーション名の候補: {candidate_text}）。打ち間違い？"
    )


def _warn_target_unresolved(
    main_object: str,
    column_key: str,
    soql_name: str,
    matched_field: dict[str, Any],
    warnings: list[str],
) -> None:
    """参照先の項目定義を取得できないときの WARNING を積む。"""
    candidates = _candidate_next_refs(matched_field, {})
    target = candidates[0] if candidates else ""
    target_text = target if target else "(対象不明)"
    warnings.append(
        f"列の直し.xlsx の直したSOQL列名をたどれません: "
        f"{main_object} / {column_key} / {soql_name}"
        f" — 参照先 {target_text} の項目定義を取得できません。打ち間違い？"
    )


# ── 内部ヘルパー ─────────────────────────────────────────────────


def _normalize_key(main_object: str, column_key: str) -> tuple[str, str]:
    """比較用のキー。 主オブジェクトの大小文字無視。"""
    return (main_object.strip().lower(), column_key)


def merge_and_write_after_run(
    *,
    path: Path,
    existing: dict[tuple[str, str], ColumnFixRow],
    records: list[ReportRecord],
    today: date,
) -> None:
    """``run`` の最後に ``列の直し.xlsx`` をマージして書く薄いラッパー。

    ``records`` の ``column_map`` を ``ObservedAutoRow`` に直し、 どのレポートで
    使っているか（管理番号）と一緒に ``merge_and_write`` に渡す。
    """
    observed: list[ObservedAutoRow] = []
    report_keys: dict[tuple[str, str], set[str]] = {}
    for record in records:
        if not record.main_object:
            continue
        for row in record.column_map:
            note = row["備考"]
            observed.append(
                ObservedAutoRow(
                    main_object=record.main_object,
                    column_key=row["列キー"],
                    display_name=row["表示名"],
                    # 人の値で上書きした行は、 上書き前の自動の値を見せる
                    soql_name=row.get("自動の値", row["対応フィールドAPI名"]),
                    candidates=note if "複数候補あり" in note else "",
                    note=note,
                )
            )
            key = _normalize_key(record.main_object, row["列キー"])
            report_keys.setdefault(key, set()).add(record.key)

    try:
        merge_and_write(
            path=path,
            existing=existing,
            observed=observed,
            today=today,
            used_in_reports={key: "|".join(sorted(keys)) for key, keys in report_keys.items()},
        )
    except PermissionError as exc:
        logger.warning(
            "列の直し.xlsx を書けませんでした（Excel などで開いていないか確認）: %s",
            exc,
        )


def today_local() -> date:
    """「今日の日付」 をこの PC のローカル時間で返す。"""
    return datetime.now(tz=UTC).astimezone().date()
