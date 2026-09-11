# エラー対応ガイド

エラーが出たら、**黒い画面（コンソール）の一番下あたりに出ている「エラー名」**をこの表から探してください。
エラー名は `SheetNotFoundError` のような英語の単語です。

> **このプロジェクトで出たエラーと対処を、下の表に追記してください。**

---

## まず試すこと（どのエラーでも共通）

1. 開いている Excel ファイルをすべて閉じて、もう一度実行する
2. それでもダメなら、このガイドでエラー名を探す
3. 表にない・解決しない場合は、**エラーの画面全体をスクリーンショット**して管理者に送る

| エラー名 | 意味 | 自分でできる対処 |
|---|---|---|
| `ModuleNotFoundError: No module named 'comken'` | 共有サーバーの comken を読み込めない | 共有サーバーに繋がっているか確認する。繋がっているなら `実行.bat` の `PYTHON_LIBRARY` の場所が正しいかを管理者に確認する |

---

## Excel のエラー

| エラー名 | 意味 | 自分でできる対処 |
|---|---|---|
| `SheetNotFoundError` | 指定した名前のシートがない | Excel を開いて、下のシート名（タブ）が変わっていないか確認する。変えた場合は元に戻す |
| `SheetAlreadyExistsError` | 同じ名前のシートが既にある | 別のシート名を指定するか、既存のシート名を変更する |
| `InvalidTableNameError` | Excel で使えないテーブル名を指定した | 空白・数字始まり・セル参照のような名前を避ける |
| `TableAlreadyExistsError` | 同じ名前のテーブルが既にある | 別のテーブル名を指定する |
| `TableNotFoundError` | 指定したテーブルがシートにない | エラーに表示された既存テーブル名を確認する |
| `ExcelFileNotFoundError` | Excel ファイルが見つからない | ファイルの置き場所と名前を確認する |
| `ExcelColumnNotFoundError` | Excel の列見出しが見つからない | Excel の1行目を確認する |
| `MacroError` | Excel のマクロが失敗した | Excel をすべて閉じて再実行する。続く場合は管理者へ |
| `EmptyHeaderCellError` | Excel の見出しに空欄がある | Excel の1行目の空欄を埋める |
| `ExcelHeadersTooFewError` | 指定した見出し数が列数より少ない | 管理者へ連絡する |

## Access のエラー

| エラー名 | 意味 | 対処 |
|---|---|---|
| `AccessFileNotFoundError` | Access ファイルが見つからない | ファイルの置き場所と名前を確認する |
| `AccessBackupError` | 元 DB を開く前のバックアップに失敗した | 保存先の空き容量・書き込み権限・元 DB の読み取り権限を確認する |
| `AccessLocalCopyError` | Access ファイルを一時フォルダへコピーできない | 使用状況・読み取り権限・空き容量を確認する |
| `AccessRoutineError` | Access マクロまたは VBA の実行に失敗した | 表示された名前と Access 側の内容を確認する |
| `AccessSourceNotFoundError` | テーブルまたはクエリが見つからない | エラーに表示された存在する名前を確認する |
| `FileFormatMismatchError` | 保存拡張子と形式が合わない | 管理者へ連絡する |
| `PermissionError` | ファイルが誰かに開かれている | 自分や他の人がそのファイルを開いていないか確認して閉じる |

---

## ファイルのエラー

| エラー名 | 意味 | 自分でできる対処 |
|---|---|---|
| `FileNotFoundError` | ファイルが見つからない | ファイルの置き場所と名前を確認する。「今日の日付のファイル」を探す処理なら、今日のファイルが作られているか確認する |
| `TimeoutError` | ダウンロードが終わらない | ネットワークの状態を確認して再実行する |
| `UnsupportedFileSuffixError` | 対応外の拡張子が指定された | CSV / Excel の対応する拡張子のファイルを指定する |
| `EncodingDetectionError` | CSV の文字コードを判定できない | CSV の保存形式を確認し、管理者へ連絡する |
| `CSVFileNotFoundError` | 読み込む CSV ファイルが存在しない | パスを確認する。新規出力は columns を指定して write / replace する |
| `CSVHeaderMissingError` | CSV に見出し行がない | 見出し行を追加するか、ヘッダーなし CSV なら columns を指定する |
| `CSVInvalidHeaderError` | CSV の見出しに空欄または重複がある | CSV の1行目にある空欄または重複した見出しを直す |
| `CSVRowLengthError` | CSV のデータ行の列数が見出し数と一致しない | 表示された行の区切り文字と値の数を確認する |
| `CSVColumnsRequiredError` | 空の新規 CSV に出力する列を決定できない | CSV(columns=[...]) または Table(columns, []) で列を指定する |
| `KeyColumnNotFoundError` | 比較に使うキー列が見つからない | Excel・CSV の列名を確認する |
| `InvalidColumnError` | 列の指定が正しくない（打ち間違いなど） | 列は番号（1, 2, …）か列記号（"A", "AA"）で指定する |
| `ConfigFileNotFoundError` | config.ini も config.ini.example も見当たらない | config.ini.example がプロジェクトにあるか確認する。無い場合は雛形（`comken/templates/新規プロジェクト/config.ini.example`）から取り直す |
| `ConfigCreatedFromExampleError` | config.ini が無かったので example から作った | 作られた config.ini の値を書き換えて、もう一度実行する |
| `ConfigLowerCaseNameError` | config.ini のセクション名・キー名に小文字がある | 表示された名前を大文字に書き換える（`[files]` → `[FILES]`） |
| `ConfigSectionNotFoundError` | config.ini の必要な節がない | 表示されたセクション名を config.ini に追加する |

---

## 分類（まとめて捕捉する用）

次の名前は、似たエラーをプログラム側でまとめて扱うための分類です。
これらの名前が単独で表示されることはありません。対処するときは、画面に表示された
具体的なエラー名を上の表から探してください。

| 分類名 | まとめるエラー |
|---|---|
| `ComkenError` | comken が出す固有エラー全体 |
| `ExcelError` | Excel に関するエラー |
| `AccessError` | Access に関するエラー |
| `CSVError` | CSV に関するエラー |
| `ColumnNotFoundError` | Excel・CSV・データ比較で列が見つからないエラー |
| `ConfigError` | config.ini に関するエラー |

---

## ブラウザ（Edge 自動操作）のエラー

| エラー名 | 意味 | 自分でできる対処 |
|---|---|---|
| `TimeoutException` | 画面の表示待ちで時間切れ | もう一度実行する。サイトが重いだけのことが多い。毎回出るなら画面が変わった可能性があるので管理者へ |
| `NoSuchElementException` | 画面の部品が見つからない | サイトの画面が変わった可能性が高い。管理者へ |
| `SessionNotCreatedException` | Edge とドライバーのバージョン不一致 | Windows Update で Edge が更新された直後に起きる。管理者へ（msedgedriver の更新が必要） |
| `WebDriverException` | ブラウザ操作の一般的なエラー | Edge のウィンドウをすべて閉じて再実行する |

---

## Outlook のエラー

| エラー名 | 意味 | 対処 |
|---|---|---|
| `OutlookError` | Outlook 関連エラーの分類 | 下の個別エラーを確認する |
| `ClassicOutlookNotAvailableError` | Classic Outlook を利用できない | Classic Outlook を使うか管理者に相談する |
| `OutlookFolderNotFoundError` | 指定したフォルダがない | エラーに表示された存在するフォルダ名を確認する |
| `OutlookAttachmentNotFoundError` | 添付ファイルがない | 表示されたファイルパスを確認する |

---

## プロジェクト固有のエラー

（プロジェクトごとにここへ追記する）

| エラー名・症状 | 意味 | 対処 |
|---|---|---|
| | | |

---

## それでも解決しないとき

以下をセットで管理者に送ってください。

1. エラー画面全体のスクリーンショット（黒い画面の文字が読める状態で）
2. 何をしようとしていたか（例: 「朝の売上レポート作成を実行した」）
3. いつから起きているか（例: 「昨日までは動いていた」）
