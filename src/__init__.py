"""src/ — soql-collector の業務コード。

管理表の読み取り・config の解釈・describe 取得・CSV 生成を同じパッケージに
置いている。 ``main.py`` は ``src.run.run()`` を呼ぶだけにし、 業務の中身は
ここへ寄せる。

    from src.run import run

    run(["list"])
"""
