@echo off
chcp 65001 >nul
title Valo Dodger v6
cd /d "%~dp0"

:: -----------------------------------------------
::  Valo Dodger v6 — Windows 用ランチャー
::  この .bat をダブルクリックするだけ
::
::  初回は dry-run を推奨:
::    python valo_dodger.py --dry-run --verbose
::
::  モード変更は下の行を編集:
::    --mode sabotage = 妨害のみ（自分にペナルティなし・推奨）
::    --mode combo    = 妨害→最終ドッジ（ペナルティ警告あり）
::    --mode dodge    = 即ドッジ（⚠ キュー制限+RR減）
:: -----------------------------------------------

python valo_dodger.py --mode sabotage --sabotage-duration 30
pause
