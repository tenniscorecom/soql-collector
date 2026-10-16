"""comken の内部状態をテスト間で初期化する（comken 本体にはテスト用の入口が無いため）。"""

from __future__ import annotations

from comken.core.config import base as config_base


def reset_cached_config() -> None:
    """``Config(path)`` のキャッシュとパス解決のキャッシュを破棄する。"""
    config_base._get_or_build_config.cache_clear()
    config_base._default_config_path.cache_clear()
    config_base._path_resolution_cache.clear()
