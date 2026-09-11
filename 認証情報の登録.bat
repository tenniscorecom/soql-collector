@echo off
rem 認証情報 (client_secret / パスワード / トークン) を Windows DPAPI で暗号化して保存する。
rem init で作ったプロジェクトなので comken への PATH は通っている。ここでは何も準備しない。

python -m comken cred gui
exit /b %ERRORLEVEL%
