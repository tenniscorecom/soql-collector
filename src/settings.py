"""config.ini から設定を読む。

必要なキーは ``[FILES] MASTER_XLSX_PATH`` （必須）と ``[FILES] OUTPUT_DIR` 、
``[LIMITS] RELATED_MAX`` 、``[SF] CREDENTIAL_PREFIX`` 。
``MASTER_XLSX_PATH`` が欠けていると ``run`` が動けないので、その時点でエラーを上げる。

``[OBJECTS]`` セクションは **読み取らない** （個別オブジェクトの取得は
やめた）。 旧 config.ini に ``[OBJECTS]`` が残っていてもエラーにせず、 無視する。

追加されたセクション:

- ``[CHECKS]`` … レポート単位の調査をどこまでやるか。 現時点では
  ``ROW_LIMIT`` （ ``○`` / ``×`` ） だけ
- ``[PII]`` … 個人情報（PII） の判定に使うキーワードと個人系オブジェクト
- ``[SIMILAR]`` … 似たレポート判定の閾値
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
# ``[LIMITS] RELATED_DEPTH`` の既定値。 1 = 今までの挙動 （ 参照先 1 段だけ ）。
# 関連オブジェクトの合計件数は ``RELATED_MAX`` で別個に打ち切る
DEFAULT_RELATED_DEPTH = 3

# ``[PII] KEYWORDS`` 未設定 / 空のときに使う既定の PII キーワード
DEFAULT_PII_KEYWORDS: tuple[str, ...] = (
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

# ``[PII] OBJECTS`` 未設定 / 空のときに使う既定の個人系オブジェクト
DEFAULT_PII_PERSON_OBJECTS: tuple[str, ...] = (
    "Contact",
    "Lead",
    "User",
    "Individual",
    "PersonAccount",
)

# ``[SIMILAR] COLUMN_SIMILARITY`` の既定値
DEFAULT_SIMILAR_COLUMN_SIMILARITY = 0.8

# ``[CHECKS] ROW_LIMIT`` の取り得る値
ROW_LIMIT_ENABLED = "○"
ROW_LIMIT_DISABLED = "×"
ROW_LIMIT_CHOICES: frozenset[str] = frozenset({ROW_LIMIT_ENABLED, ROW_LIMIT_DISABLED})


@dataclass(frozen=True)
class Settings:
    """config.ini から読んだ設定値。"""

    master_xlsx_path: Path
    output_dir: Path
    related_max: int
    related_depth: int
    credential_prefix: str
    # レポートごとに 2000 行チェックを実行するか （ True = 実行 ）
    row_limit: bool
    # PII 判定に使うキーワードと個人系オブジェクト
    pii_keywords: tuple[str, ...]
    pii_person_objects: tuple[str, ...]
    # 似たレポート判定で 「 近い 」 とみなす Jaccard の下限
    similar_column_similarity: float


def load_settings(project_root: Path | None = None) -> Settings:
    """config.ini を読み、 ``Settings`` を返す。

    Args:
        project_root: config.ini があるフォルダ。省略時は comken の
            ``project_dir()`` （実行スクリプトのフォルダ）。

    Raises:
        ConfigKeyNotFoundError: 必須キーが無い / 不正値。
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
    depth_value = _optional(
        parser,
        "LIMITS",
        "RELATED_DEPTH",
        ini_path,
        default=str(DEFAULT_RELATED_DEPTH),
    ).strip()
    try:
        related_depth = int(depth_value)
    except ValueError as exc:
        raise ConfigKeyNotFoundError(
            "LIMITS", "RELATED_DEPTH", _options(parser, "LIMITS"), ini_path
        ) from exc
    if related_depth < 1:
        raise ConfigKeyNotFoundError(
            "LIMITS", "RELATED_DEPTH", _options(parser, "LIMITS"), ini_path
        )
    credential_prefix = _optional(parser, "SF", "CREDENTIAL_PREFIX", ini_path, default="").strip()

    row_limit_value = _optional(
        parser, "CHECKS", "ROW_LIMIT", ini_path, default=ROW_LIMIT_ENABLED
    ).strip()
    if row_limit_value not in ROW_LIMIT_CHOICES:
        raise ConfigKeyNotFoundError("CHECKS", "ROW_LIMIT", sorted(ROW_LIMIT_CHOICES), ini_path)
    row_limit = row_limit_value == ROW_LIMIT_ENABLED

    pii_keywords = _parse_pii_keywords(parser, ini_path)
    pii_objects = _parse_pii_objects(parser, ini_path)
    similar_similarity = _parse_similar_similarity(parser, ini_path)

    return Settings(
        master_xlsx_path=_resolve(base_dir, master_value),
        output_dir=_resolve(base_dir, output_value),
        related_max=related_max,
        related_depth=related_depth,
        credential_prefix=credential_prefix,
        row_limit=row_limit,
        pii_keywords=pii_keywords,
        pii_person_objects=pii_objects,
        similar_column_similarity=similar_similarity,
    )


def _parse_pii_keywords(
    parser: configparser.ConfigParser,
    ini_path: Path,
) -> tuple[str, ...]:
    """``[PII] KEYWORDS`` を読む。 未設定 / 空なら既定値。"""
    raw = _optional(parser, "PII", "KEYWORDS", ini_path, default="").strip()
    if not raw:
        return DEFAULT_PII_KEYWORDS
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts if parts else DEFAULT_PII_KEYWORDS


def _parse_pii_objects(
    parser: configparser.ConfigParser,
    ini_path: Path,
) -> tuple[str, ...]:
    """``[PII] OBJECTS`` を読む。 未設定 / 空なら既定値。"""
    raw = _optional(parser, "PII", "OBJECTS", ini_path, default="").strip()
    if not raw:
        return DEFAULT_PII_PERSON_OBJECTS
    parts = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parts if parts else DEFAULT_PII_PERSON_OBJECTS


def _parse_similar_similarity(
    parser: configparser.ConfigParser,
    ini_path: Path,
) -> float:
    """``[SIMILAR] COLUMN_SIMILARITY`` を読む。 0.0 〜 1.0 の範囲。"""
    raw = _optional(
        parser,
        "SIMILAR",
        "COLUMN_SIMILARITY",
        ini_path,
        default=str(DEFAULT_SIMILAR_COLUMN_SIMILARITY),
    ).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigKeyNotFoundError(
            "SIMILAR", "COLUMN_SIMILARITY", _options(parser, "SIMILAR"), ini_path
        ) from exc
    if not 0.0 <= value <= 1.0:
        raise ConfigKeyNotFoundError(
            "SIMILAR", "COLUMN_SIMILARITY", _options(parser, "SIMILAR"), ini_path
        )
    return value


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
