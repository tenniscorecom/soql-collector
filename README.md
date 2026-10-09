# soql-collector

Salesforce のレポート describe（レポート本体 + 主オブジェクト + 関連オブジェクト）を
**管理番号ごと**にとって JSON に保存し、 SOQL を組み立てるための小さな CSV
（**対応表**・**項目表**）を作るツールです。 関連オブジェクトは、
config.ini の `RELATED_DEPTH` 段数まで幅優先で掘ります（既定 3）。 1 段だけ
（ 今までの挙動 ） にしたいときは `RELATED_DEPTH = 1` にしてください。
（**対応表**・**項目表**）を作るツールです。

どのレポートを取るかは管理表の「有効」列が `○` のものすべて。 個別に取りたい
ときは、その行だけ `○` にして、ほかを `×` にします。 個別オブジェクトを別に
取りたいときは `config.ini` の `[OBJECTS] NAMES` に書きます。

SOQL はツールが自動生成するのではなく、 **AI にチャットで組んでもらう**
ための材料（対応表・項目表）を用意するところまでをこのツールが担当します。
AI が出した SOQL は、本番で実行する前に人が確認してください。

## セットアップ

1. Python 3.11 以上を用意する
2. `config.ini.example` を `config.ini` にコピーし、 `[FILES] MASTER_XLSX_PATH`
   / `OUTPUT_DIR`、 `[LIMITS] RELATED_MAX` / `[LIMITS] RELATED_DEPTH`、
   `[SF] CREDENTIAL_PREFIX`、 `[OBJECTS] NAMES` / `[OBJECTS] ORG_ID` を
   必要に応じて書き換える （詳細は [docs/使い方.md](docs/使い方.md)）
3. comken の親（既定 `F:\dev`）を `PYTHONPATH` に含める。 `実行.bat` を
   使うと PYTHONPATH の設定を代行してくれる

## 使い方

`実行.bat`（または `python main.py`）を **引数なしで** 実行する。 取られた
レポートは、 管理表の「有効」列が `○` のものすべて。 個別に取りたいときは、
その行だけ `○` にして、 ほかを `×` にする。

終了コードは `実行.bat` がそのまま返す。 RPA から呼ぶのも `実行.bat` を
そのまま起動すればよい）。

- `0` … 全件成功
- `1` … 失敗した ID またはオブジェクトが 1 件以上ある
- `2` … 引数が付いている / 管理表に「有効」が `○` の行が無い

**引数を付けるとエラーになる** （終了コード `2`）。 サブコマンドは無い。

## 出力ファイル

`[FILES] OUTPUT_DIR`（既定 `./output`）に次の 5 種類が置かれます。

| ファイル | 中身 |
|---|---|
| `{管理番号}.json` | レポート describe・主オブジェクト・関連オブジェクト・列対応表・警告（**SOQL 組立に必要な部分だけ**） |
| `objects/{Name}.json` | `[OBJECTS] NAMES` で取った個別オブジェクトの describe（ 取得日時・オブジェクト名・slim した `object` ） |
| `対応表_{管理番号}.csv` | その管理番号の、**レポート列 ⇔ フィールド** の対応（1 列 1 行） |
| `対応表.csv` | 全管理番号を連結した対応表（管理番号昇順） |
| `項目表.csv` | 全 JSON（ レポート＋個別オブジェクト ） を集約した「オブジェクト × 項目」のフラット表 |

`{管理番号}.json` には describe の **原本** は書きません。 `reportMetadata`
は SOQL 組立に必要な 21 キーだけを**そのまま**残し、 `reportExtendedMetadata`
は 3 キーだけを**そのまま**残します。 各オブジェクトは `name` / `label` /
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