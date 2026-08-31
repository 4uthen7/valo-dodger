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
::  使い方: 送りたい文章をコピーしておく（例: dodge pls）
::  攻めスタートを検出すると、その文章をチャットに
::  数秒おきに送り続けます（エージェントは放置）
::
::  モード変更は下の行を編集:
::    --mode clip     = クリップボード送信（デフォルト・推奨）
::    --mode sabotage = 妨害（チャット+エージェント切替）
::    --mode combo    = 妨害→最終ドッジ（ペナルティ警告あり）
::    --mode dodge    = 即ドッジ（⚠ キュー制限+RR減）
:: -----------------------------------------------

python valo_dodger.py --mode clip --chat-interval 5
pause
