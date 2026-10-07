"""src/run.py と main.py の薄い配線テスト（終了コードを捨てないことの確認）。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

# ── src.run.run() ─────────────────────────────────────────────────────────


def _patch_cli_main(monkeypatch: pytest.MonkeyPatch, fake_main: Any) -> Any:
    """``src.run`` が参照している ``cli_main`` を直接差し替える。

    なぜ: ``from src.cli import main as cli_main`` は import 時に解決される
    ので、 ``monkeypatch.setattr(src.cli, "main", ...)`` では ``src.run``
    側の ``cli_main`` が古くなる（ テスト順依存になる）。 ``src.run.cli_main``
    を直接差し替えれば、 テスト順に左右されずに済む。
    """
    from src import run as run_module

    monkeypatch.setattr(run_module, "cli_main", fake_main)
    return run_module


def test_run_passes_argv_to_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run(["list"])`` が ``src.cli.main`` の戻り値をそのまま返す。"""
    captured: dict = {}

    def fake_main(argv):
        captured["argv"] = list(argv)
        return 0

    run_module = _patch_cli_main(monkeypatch, fake_main)
    code = run_module.run(["list"])

    assert code == 0
    assert captured["argv"] == ["list"]


def test_run_uses_sys_argv_when_argv_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run(None)`` は ``sys.argv[1:]`` を ``cli.main`` に渡す。"""
    captured: dict = {}

    def fake_main(argv):
        captured["argv"] = list(argv)
        return 0

    monkeypatch.setattr(sys, "argv", ["main.py", "fetch", "1001", "--dry-run"])
    run_module = _patch_cli_main(monkeypatch, fake_main)

    code = run_module.run(None)

    assert code == 0
    assert captured["argv"] == ["fetch", "1001", "--dry-run"]


def test_run_empty_argv_runs_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run([])`` は対話メニュー（``cli.main`` が ``[]`` を受け取る）。

    なぜ: ``run()`` が ``argv`` を ``cli.main`` に渡さず、 ``cli.main()`` を
    引数なしで呼ぶ実装に戻ると、 ``cli.main`` の側で ``argv=sys.argv[1:]`` に
    なって ``argv=[]`` にならない（テストが落ちる）。
    """
    captured: dict = {}

    def fake_main(argv):
        captured["argv"] = list(argv)
        return 0

    run_module = _patch_cli_main(monkeypatch, fake_main)

    code = run_module.run([])

    assert code == 0
    assert captured["argv"] == []


def test_run_propagates_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` は ``cli.main`` の戻り値が 0 でなくてもそのまま返す。"""
    run_module = _patch_cli_main(monkeypatch, lambda _argv: 2)

    assert run_module.run(["fetch"]) == 2


# ── main.py ──────────────────────────────────────────────────────────────


def _reload_main(monkeypatch: pytest.MonkeyPatch) -> Any:
    """``main.py`` を再ロードする。テストごとに副作用をリセットする。

    pytest の sys.path 状態では ``from comken import comken_logger`` が
    「cannot import name ... (unknown location)」 で失敗する場合がある。
    ``F:\\dev\\comken`` を sys.path に直接入れて PathFinder で解決させると
    安定するので、 そのフォールバックを入れておく。
    """
    project_root = Path(__file__).resolve().parent.parent
    sys.modules.pop("main", None)
    # 一旦本物のパッケージとして読み込んでみる
    if not getattr(sys.modules.get("comken"), "__file__", None):
        for key in list(sys.modules):
            if key == "comken" or key.startswith("comken."):
                sys.modules.pop(key, None)
        comken_root = project_root.parent / "comken"
        monkeypatch.syspath_prepend(str(comken_root))
        import comken  # noqa: F401
    monkeypatch.syspath_prepend(str(project_root))
    return importlib.import_module("main")


def test_main_raises_systemexit_when_run_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` は ``run()`` が 1 を返したとき ``SystemExit(1)`` を送出する。

    なぜ: ``main()`` が ``run()`` の戻り値を捨てる実装に戻ると ``SystemExit``
    が出ないので、 RPA / ``実行.bat`` が失敗終了コードを受け取れなくなる。
    """
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 1)

    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1


def test_main_does_not_raise_when_run_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main()`` は ``run()`` が 0 を返したとき何も起きない。"""
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 0)

    # 何も送出されない
    main.main()


def test_main_propagates_exit_code_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` は ``run()`` が 2 を返したとき ``SystemExit(2)`` を送出する。"""
    main = _reload_main(monkeypatch)

    monkeypatch.setattr(main, "run", lambda: 2)

    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
