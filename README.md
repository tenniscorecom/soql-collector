# soql-collector

Salesforce レポートの Describe、状態、メモ、列マッピングを Excel と JSON に蓄積し、
再利用可能な SOQL ドラフトを組み立てる CLI ツールです。

## セットアップと実行

Python 3.11 以上で `pip install -e .` を実行し、`config.ini.example` を
`config.ini` にコピーします。comken の親を `PYTHONPATH` に追加してください。

```powershell
python -m soql_collector --help
python -m soql_collector import-master .\管理表.xlsx
python -m soql_collector collect --master
python -m soql_collector list
```

利用手順は [docs/使い方.md](docs/使い方.md)、内部仕様は
[docs/仕様書.md](docs/仕様書.md) を参照してください。
