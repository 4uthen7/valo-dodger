@echo off
chcp 65001 >nul
title Valo Dodger v2
cd /d "%~dp0"

:: -----------------------------------------------
::  Valo Dodger v2 — Windows 用ランチャー
::  この .bat をダブルクリックするだけ
::
::  初回は dry-run を推奨:
::    python valo_dodger.py --dry-run --verbose
::
::  モード変更は下の行を編集:
::    --mode dodge     = 即ドッジ
::    --mode sabotage  = 妨害のみ
::    --mode combo     = 妨害→最終ドッジ
:: -----------------------------------------------

python valo_dodger.py --mode combo --sabotage-duration 25
pause
