@echo off
chcp 65001 >nul
title Valo Dodger v7
cd /d "%~dp0"

:: -----------------------------------------------
::  Valo Dodger v7 — Windows 用ランチャー（GUI）
::  この .bat をダブルクリックするだけ
::
::  GUI の使い方:
::    1. 送りたい文章をコピーしておく（例: dodge pls）
::    2. 「▶ 起動」を押す
::    3. 攻めスタートを検出すると、その文章をチャットに
::       数秒おきに送り続けます（仮ピック切替はGUIでON）
::
::  モード・送信間隔・仮ピック・妨害時間などは GUI 上で変更できます
::  コンソールを出したくない場合は python を pythonw に変更
::  初回動作確認: python valo_dodger.py --dry-run --verbose
:: -----------------------------------------------

python valo_dodger.py --gui
pause
