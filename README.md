# soql-collector

Salesforce のレポート describe（レポート本体 + 主オブジェクト + 関連オブジェクト）を
**管理番号ごと**にとって JSON に保存し、 SOQL を組み立てるための小さな CSV
（**対応表**・**項目表**）を作る CLI ツールです。

SOQL はツールが自動生成するのではなく、 **AI にチャットで組んでもらう**
ための材料（対応表・項目表）を用意するところまでをこのツールが担当します。
AI が出した SOQL は、本番で実行する前に人が確認してください。

## セットアップ

1. Python 3.11 以上を用意する
2. `config.ini.example` を `config.ini` にコピーし、 `[FILES] MASTER_XLSX_PATH`
   / `OUTPUT_DIR`、 `[LIMITS] RELATED_MAX`、 `[SF] CREDENTIAL_PREFIX` を
   必要に応じて書き換える（詳細は [docs/使い方.md](docs/使い方.md)）
3. comken の親（既定 `F:\dev`）を `PYTHONPATH` に含める。 `実行.bat` を
   使うと PYTHONPATH の設定を代行してくれる

## コマンド

| コマンド | 用途 |
|---|---|
| `python main.py fetch [管理番号 ...]` | 指定した管理番号の describe を取得して JSON に書く |
| `python main.py fetch --all` | 「有効」が ○ の管理番号を全部取る |
| `python main.py fetch [管理番号 ...] --dry-run` | 接続せず、対象だけ表示する |
| `python main.py list` | 管理表の一覧（管理番号・概要・有効・レポート ID）を表示する |
| `python main.py tables` | `OUTPUT_DIR` の JSON から対応表 CSV・項目表 CSV を作り直す |
| `python main.py` （引数なし） | 対話メニュー（ `1` 〜 `4` で `fetch` / `fetch --all` / `list` / `tables` ） |

`fetch` の実行が成功した ID については、 `fetch` の最後で自動的に
**対応表 CSV と項目表 CSV** が再生成される（ `--dry-run` では作らない）。

## 出力ファイル

`[FILES] OUTPUT_DIR`（既定 `./output`）に次の 4 種類が置かれます。

| ファイル | 中身 |
|---|---|
| `{管理番号}.json` | レポート describe・主オブジェクト・関連オブジェクト・列対応表・警告（**SOQL 組立に必要な部分だけ**） |
| `対応表_{管理番号}.csv` | その管理番号の、**レポート列 ⇔ フィールド** の対応（1 列 1 行） |
| `対応表.csv` | 全管理番号を連結した対応表（管理番号昇順） |
| `項目表.csv` | 全 JSON を集約した「オブジェクト × 項目」のフラット表 |

`{管理番号}.json` には describe の**原本**は書きません。 `reportMetadata` は
SOQL 組立に必要な 21 キーだけを**そのまま**残し、 `reportExtendedMetadata` は
3 キーだけを**そのまま**残します。 各オブジェクトは `name` / `label` /
`custom` / `fields` だけ、 各項目は `name` / `label` / `type` / `custom` /
`referenceTo` / `relationshipName` / `picklist` / `picklistTotal` の 8 キーだけ
が残ります。 `picklist` には `active` な値だけが最大 30 件入り、 `picklistTotal`
に active な選択肢の総数が入ります（ `picklistValues` / `childRelationships` /
`recordTypeInfos` などの重いキーは JSON に出ません）。 落としたキーの名前は
`report.droppedKeys` に出現順で残るので、 最初の実行で「 必要なものを
落としていないか 」 を確認できます。

CSV は UTF-8 BOM つき + CRLF。 Excel でそのまま開けます。
詳細は [docs/使い方.md](docs/使い方.md)・[docs/仕様書.md](docs/仕様書.md)
を参照してください。