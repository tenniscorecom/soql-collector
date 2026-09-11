@echo off
setlocal
rem このツールの起動用。RPA 基盤から呼ばれる入口でもある
rem (終了コードをそのまま返すので、pause は入れない)
rem comken の場所を変えたら、.vscode\settings.json も合わせて直す
set "PYTHON_LIBRARY=F:\dev"
pushd "%~dp0" || (echo [エラー] フォルダ未到達: %~dp0 & exit /b 1)
where python >nul 2>&1 || (echo [エラー] Python 未インストール & popd & exit /b 1)
rem 恒久登録済みの PYTHONPATH が通っていればそのまま使う
python -c "import comken" >nul 2>&1 && goto :run
set "PYTHONPATH=%PYTHON_LIBRARY%;%PYTHONPATH%"
if not exist "%PYTHON_LIBRARY%\comken\__init__.py" (
  echo [エラー] 共通ライブラリ comken 未到達: %PYTHON_LIBRARY%
  echo 共有サーバー / PYTHON_LIBRARY を確認してください（社内 PC は PYTHONPATH 配布済み）
  popd & exit /b 1
)
:run
python main.py
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" echo [失敗] 終了コード %EXIT_CODE%
endlocal & exit /b %EXIT_CODE%
