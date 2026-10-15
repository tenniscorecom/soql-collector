"""config.ini のキーごとの既定値と軽い値検証。

comken の ``Config`` には **既定値の仕組みが無い** ので、 このプロジェクト側で
キーを読み、 値が不正ならエラーメッセージを出して止める。 値が無いときは
モジュール先頭の定数を既定値として採用する。

- キーが無い → モジュール先頭の定数を既定値として採用
- 値はあるが不正 （ 数値でない、 ○/× 以外、 範囲外 ） → メッセージを出して ``ConfigValueError``
- 値の正規化はここで行う （ ``○/×`` の bool 化、 ``int`` 化、 カンマ区切りの tuple 化 ）

新しいキーを足すときは ``_DEFAULTS`` の関連定数 / ``_parse_*`` /
``FilesConfig.load`` などの ``.load`` の 3 か所を追加する。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comken import config

logger = logging.getLogger(__name__)


# ── 既定値 ───────────────────────────────────────────────────────────────


# ``[FILES]`` で既定値を持つキー
DEFAULT_MASTER_XLSX_PATH = "./input/レポート管理表.xlsx"
DEFAULT_OUTPUT_DIR = "./output"
DEFAULT_DESCRIBE_CSV_PATH = "./data/項目定義.csv"
DEFAULT_COLUMN_FIX_XLSX_PATH = "./input/列の直し.xlsx"
DEFAULT_XLSX_NAME = "SOQL調査.xlsx"
DEFAULT_MARKDOWN_DIR = "SOQL資料"
DEFAULT_DIAGRAM_DIR = "関係図"

# ``[SURVEY]`` の既定値
DEFAULT_RELATED_DEPTH = 3
DEFAULT_RELATED_MAX = 40
DEFAULT_ROW_LIMIT_ENABLED = True
DEFAULT_SIMILAR_COLUMN_THRESHOLD = 0.8

# ``[PII]`` の既定値
DEFAULT_PII_KEYWORDS = (
    "氏名",
    "名前",
    "姓",
    "名",
    "フリガナ",
    "カナ",
    "メール",
    "email",
    "電話",
    "phone",
    "mobile",
    "fax",
    "住所",
    "street",
    "city",
    "state",
    "postal",
    "zip",
    "郵便",
    "生年月日",
    "birth",
    "性別",
    "gender",
    "マイナンバー",
    "個人番号",
    "口座",
    "免許",
    "パスポート",
    "年収",
    "給与",
)
DEFAULT_PII_PERSON_OBJECTS = ("Contact", "Lead", "User", "Individual", "PersonAccount")


# ── 値検証のエラー ────────────────────────────────────────────────────


class ConfigValueError(ValueError):
    """config.ini の値が不正なときのエラー。 止める側の責務。"""


# ── FilesConfig / SurveyConfig / PIIConfigExt ────────────────────────────


@dataclass(frozen=True)
class FilesConfig:
    """``[FILES]`` の読み取り結果。"""

    master_xlsx_path: str
    output_dir: str
    describe_csv_path: str
    column_fix_xlsx_path: str
    xlsx_name: str
    markdown_dir: str
    diagram_dir: str

    @classmethod
    def load(cls) -> FilesConfig:
        section = _safe_section("FILES")
        return cls(
            master_xlsx_path=_read_str(section, "MASTER_XLSX_PATH", DEFAULT_MASTER_XLSX_PATH),
            output_dir=_read_str(section, "OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
            describe_csv_path=_read_str(section, "DESCRIBE_CSV_PATH", DEFAULT_DESCRIBE_CSV_PATH),
            column_fix_xlsx_path=_read_path(
                section, "COLUMN_FIX_XLSX_PATH", DEFAULT_COLUMN_FIX_XLSX_PATH
            ),
            xlsx_name=_read_str(section, "XLSX_NAME", DEFAULT_XLSX_NAME),
            markdown_dir=_read_str(section, "MARKDOWN_DIR", DEFAULT_MARKDOWN_DIR),
            diagram_dir=_read_str(section, "DIAGRAM_DIR", DEFAULT_DIAGRAM_DIR),
        )


@dataclass(frozen=True)
class SurveyConfig:
    """``[SURVEY]`` の読み取り結果。"""

    related_depth: int
    related_max: int
    row_limit_enabled: bool
    similar_column_threshold: float

    @classmethod
    def load(cls) -> SurveyConfig:
        section = _safe_section("SURVEY")
        return cls(
            related_depth=_read_int(
                section, "RELATED_DEPTH", DEFAULT_RELATED_DEPTH, min_=1, max_=10
            ),
            related_max=_read_int(section, "RELATED_MAX", DEFAULT_RELATED_MAX, min_=1, max_=500),
            row_limit_enabled=_read_bool(section, "ROW_LIMIT_CHECK", DEFAULT_ROW_LIMIT_ENABLED),
            similar_column_threshold=_read_float(
                section,
                "SIMILAR_COLUMN_THRESHOLD",
                DEFAULT_SIMILAR_COLUMN_THRESHOLD,
                min_=0.0,
                max_=1.0,
            ),
        )


@dataclass(frozen=True)
class PIIConfigExt:
    """``[PII]`` の読み取り結果。"""

    keywords: tuple[str, ...]
    person_objects: tuple[str, ...]

    @classmethod
    def load(cls) -> PIIConfigExt:
        section = _safe_section("PII")
        return cls(
            keywords=_read_csv(section, "KEYWORDS", DEFAULT_PII_KEYWORDS),
            person_objects=_read_csv(section, "PERSON_OBJECTS", DEFAULT_PII_PERSON_OBJECTS),
        )


@dataclass(frozen=True)
class AppConfig:
    """アプリ全体の設定（ ``run()`` からまとめて 1 回読む ）。"""

    files: FilesConfig
    survey: SurveyConfig
    pii: PIIConfigExt

    @classmethod
    def load(cls) -> AppConfig:
        return cls(
            files=FilesConfig.load(),
            survey=SurveyConfig.load(),
            pii=PIIConfigExt.load(),
        )


def load_from_config(config_path: Path) -> AppConfig:
    """指定の ``config.ini`` を読んで ``AppConfig`` を返す。 テスト用。

    ``[SURVEY]`` / ``[PII]`` セクションが無いときは空の ``_Empty`` で埋めて
    既定値を使う。 値が無いだけで ``ConfigError`` にはしない
    """
    from comken.core.config import Config

    cfg = Config(config_path)
    return AppConfig(
        files=_files_from_cfg(_safe_attr(cfg, "FILES")),
        survey=_survey_from_cfg(_safe_attr(cfg, "SURVEY")),
        pii=_pii_from_cfg(_safe_attr(cfg, "PII")),
    )


def _safe_attr(cfg: Any, name: str) -> Any:
    """``cfg`` からセクションを安全に取り出す。 無いときは空の ``_Empty`` 。"""
    try:
        return getattr(cfg, name)
    except Exception:
        return _Empty()


def _files_from_cfg(section: Any) -> FilesConfig:
    return FilesConfig(
        master_xlsx_path=_read_str(section, "MASTER_XLSX_PATH", DEFAULT_MASTER_XLSX_PATH),
        output_dir=_read_str(section, "OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        describe_csv_path=_read_str(section, "DESCRIBE_CSV_PATH", DEFAULT_DESCRIBE_CSV_PATH),
        column_fix_xlsx_path=_read_path(
            section, "COLUMN_FIX_XLSX_PATH", DEFAULT_COLUMN_FIX_XLSX_PATH
        ),
        xlsx_name=_read_str(section, "XLSX_NAME", DEFAULT_XLSX_NAME),
        markdown_dir=_read_str(section, "MARKDOWN_DIR", DEFAULT_MARKDOWN_DIR),
        diagram_dir=_read_str(section, "DIAGRAM_DIR", DEFAULT_DIAGRAM_DIR),
    )


def _survey_from_cfg(section: Any) -> SurveyConfig:
    return SurveyConfig(
        related_depth=_read_int(section, "RELATED_DEPTH", DEFAULT_RELATED_DEPTH, min_=1, max_=10),
        related_max=_read_int(section, "RELATED_MAX", DEFAULT_RELATED_MAX, min_=1, max_=500),
        row_limit_enabled=_read_bool(section, "ROW_LIMIT_CHECK", DEFAULT_ROW_LIMIT_ENABLED),
        similar_column_threshold=_read_float(
            section,
            "SIMILAR_COLUMN_THRESHOLD",
            DEFAULT_SIMILAR_COLUMN_THRESHOLD,
            min_=0.0,
            max_=1.0,
        ),
    )


def _pii_from_cfg(section: Any) -> PIIConfigExt:
    return PIIConfigExt(
        keywords=_read_csv(section, "KEYWORDS", DEFAULT_PII_KEYWORDS),
        person_objects=_read_csv(section, "PERSON_OBJECTS", DEFAULT_PII_PERSON_OBJECTS),
    )


# ── 読み出しヘルパー ──────────────────────────────────────────────────


def _safe_section(name: str) -> Any:
    """``config.<NAME>`` を無かった空の ``Mapping`` として返す。

    旧 config.ini でセクションが無いときに ``__getattr__`` が
    ``_section_not_found_error`` を投げるので、 ここで受け止めて 「 空セクション 」
    扱いに変える。 例外を投げるのは **値が不正なとき** に限定する
    """
    try:
        return getattr(config, name)
    except (AttributeError, Exception):
        # comken の ``ConfigError`` は ``AttributeError`` を継承していないので
        # ``Exception`` でも受ける。 セクションが無いときは 「 空セクション 」 扱いに
        # 倒し、 値の検証をここで行う
        return _Empty()


class _Empty(Mapping[str, Any]):
    """セクションが無い／読めないときに ``_read_*`` に渡す空の mapping。"""

    def __getitem__(self, key: str) -> Any:
        raise ConfigValueError(
            f"config.ini のセクションが読めません。 キー {key} を取りに行くところで失敗しました。"
        )

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


def _section_name(section: Any) -> str:
    """エラーメッセージ用のセクション名を取り出す。"""
    if isinstance(section, _Empty):
        return "?"
    name = getattr(section, "section", None)
    if isinstance(name, str) and name:
        return name
    return "?"


def _read_str(section: Any, key: str, default: str) -> str:
    raw = _get_raw(section, key, default)
    if not isinstance(raw, str):
        # comken は一部を Path / int などに変換する。 ここでは str として扱う
        return str(raw)
    stripped = raw.strip()
    if not stripped:
        return default
    return stripped


def _read_path(section: Any, key: str, default: str) -> str:
    """``_read_str`` とほぼ同じだが、 既定値が返るとき ``_resolve_default_path``
    で絶対パスへ解決する。

    comken は ``config.ini`` に書かれたパス文字列を自動で ``Path`` に直し、
    ``str(raw)`` で絶対パスを返す。 キー未設定で既定値 （ 文字列 ） を返すとき
    は ``./input/列の直し.xlsx`` のように **config.ini からの相対** で書くのが
    流儀だが、 ``Path()`` に渡しただけではカレントワーキングディレクトリに
    解かれてしまう。 そこで ``_section_base_dir()`` で config.ini のある
    ディレクトリを基準に絶対パスへ直す
    """
    raw = _get_raw(section, key, default)
    if not isinstance(raw, str):
        return str(raw)
    stripped = raw.strip()
    base = _section_base_dir(section)
    if not stripped:
        return _resolve_default_path(default, base)
    return _resolve_default_path(stripped, base)


def _resolve_default_path(value: str, base: Path | None) -> str:
    """``value`` が相対パスのときは ``base`` を基準に絶対化する。

    ``Path`` に変換できる値のみを対象にし、 既に絶対パスならそのまま返す。
    ``base`` が ``None`` のときはそのまま返す （ テストで ``load_from_config``
    を使い、 section が空のときは base を知らない ）
    """
    text = value.strip()
    if not text:
        return text
    candidate = Path(text)
    if candidate.is_absolute():
        return str(candidate)
    if base is None:
        return text
    return str((Path(base) / candidate).resolve())


def _section_base_dir(section: Any) -> Path | None:
    """``section`` が ``_SectionNamespace`` のときに元ディレクトリを返す。

    comken の ``_SectionNamespace`` には ``_path`` （ 読んだ config.ini の
    ``Path`` ） を持つので、 その親ディレクトリを返す。 その他の ``section``
    （ ``_Empty`` や ``load_from_config`` のために ``cfg.FILES`` から取り出した
    ``_SectionNamespace`` ） では ``None``
    """
    path_obj = getattr(section, "_path", None)
    if isinstance(path_obj, Path):
        return path_obj.parent
    raw_path = getattr(section, "path", None)
    if isinstance(raw_path, (str, Path)) and raw_path:
        return Path(raw_path).parent
    return None


def _read_int(section: Any, key: str, default: int, *, min_: int, max_: int) -> int:
    raw = _get_raw(section, key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValueError(
            f"config.ini の [{_section_name(section)}] {key}={raw!r} は整数で書かれていません。"
            f" {min_} 〜 {max_} の範囲で指定してください。例: {key} = {default}"
        ) from exc
    if value < min_ or value > max_:
        raise ConfigValueError(
            f"config.ini の [{_section_name(section)}] {key}={value} は範囲外です。"
            f" {min_} 〜 {max_} の範囲で指定してください。"
        )
    return value


def _read_float(section: Any, key: str, default: float, *, min_: float, max_: float) -> float:
    raw = _get_raw(section, key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigValueError(
            f"config.ini の [{_section_name(section)}] {key}={raw!r} は小数で書かれていません。"
            f" {min_} 〜 {max_} の範囲で指定してください。例: {key} = {default}"
        ) from exc
    if value < min_ or value > max_:
        raise ConfigValueError(
            f"config.ini の [{_section_name(section)}] {key}={value} は範囲外です。"
            f" {min_} 〜 {max_} の範囲で指定してください。"
        )
    return value


def _read_bool(section: Any, key: str, default: bool) -> bool:
    raw = _get_raw(section, key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("○", "o", "yes", "true", "on", "1", "はい"):
            return True
        if text in ("×", "x", "no", "false", "off", "0", "いいえ"):
            return False
        raise ConfigValueError(
            f"config.ini の [{_section_name(section)}] {key}={raw!r} "
            f"は ○/× などで書かれていません。 例: {key} = ○ (ON) / × (OFF)"
        )
    raise ConfigValueError(
        f"config.ini の [{_section_name(section)}] {key}={raw!r} は ○/× で書かれていません。"
        f" 例: {key} = ○"
    )


def _read_csv(section: Any, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _get_raw(section, key, None)
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    text = str(raw)
    parts = [part.strip() for part in text.split(",")]
    return tuple(part for part in parts if part)


def _get_raw(section: Any, key: str, default: Any) -> Any:
    if isinstance(section, _Empty):
        return default
    try:
        return getattr(section, key)
    except AttributeError:
        return default
