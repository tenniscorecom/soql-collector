"""pytest の実行前に comken の場所を ``sys.path`` に足す。

優先順:

1. 環境変数 ``PYTHON_LIBRARY`` が指すフォルダ
2. このリポジトリの親にある ``../comken`` （共有リポジトリの配置）

どちらも無い場合は何もしない（``comken`` が ``pip install`` 済み、
もしくは PYTHONPATH 恒久設定済みとみなす）。
``pyproject.toml`` に ``F:/dev/comken`` を直書きする旧方式を、
実行する PC に依存しない形へ移すための仕掛け。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_paths() -> list[Path]:
    """``sys.path`` の先頭に足す候補を順に並べる。"""
    candidates: list[Path] = []
    env_value = os.environ.get("PYTHON_LIBRARY")
    if env_value:
        candidates.append(Path(env_value))

    # この conftest.py は tests/conftest.py。 リポジトリの親が隣接の comken。
    candidates.append(Path(__file__).resolve().parent.parent.parent / "comken")

    return candidates


def _ensure_comken_on_path() -> None:
    for path in _candidate_paths():
        if (path / "comken" / "__init__.py").is_file():
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
            return


_ensure_comken_on_path()
