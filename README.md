# Valo Dodger

> Valorant のエージェント選択画面で攻め/守りスタートを自動判定し、攻めならドッジ or ロビー妨害する常駐ツール

Valorant's local + GLZ API を使って pregame (agent select) 入室を検知し、Attack スタートだった場合に自動でドッジする Python スクリプト。sabotage モードでは CPU 飽和 + API flood でロビーを重くし、味方にドッジさせることも可能。

## Features

- **dodge** — 攻め検出で即 API ドッジ
- **sabotage** — CPU 全コア飽和 + API flood + Valorant プロセス優先度低下でロビー妨害（自分は抜けない）
- **combo** — 妨害 → 誰も抜けなければ最終ドッジ
- `--dry-run --verbose` で全 API 通信のデバッグ表示
- region/shard 自動検出（ShooterGame.log + riot-geo API フォールバック）
- 毎 API コールでトークンリフレッシュ（403/1010 対策済み）

## Quick Start

```bat
cd C:\path\to\valo
python valo_dodger.py --dry-run --verbose   # 動作確認
python valo_dodger.py                        # 本番（即ドッジ）
python valo_dodger.py --mode combo           # 妨害→最終ドッジ
```

`valo_dodger.bat` をダブルクリックでも起動可。

## Requirements

- **Windows**（Valorant が動いているマシン）※macOS では Valorant が動作しない
- Python 3.9+（標準ライブラリのみ、外部依存ゼロ）

## How It Works

```
lockfile → /entitlements/v1/token (local) → accessToken + PUUID
ShooterGame.log or riot-geo API → region + shard + client_version
                                         ↓
  https://glz-{region}-1.{shard}.a.pvp.net/pregame/v1/players/{puuid}
                                         ↓ (2秒ポーリング)
                     pregame 検出 → サイド判定
                                         ↓
        Attack → POST /pregame/v1/matches/{id}/quit (dodge)
              → CPU飽和 + API flood + 優先度低下 (sabotage)
```

## Options

```
--mode {dodge,sabotage,combo}  モード選択
--dodge {attack,defense}       回避サイド (default: attack)
--sabotage-duration SECONDS    妨害時間 (default: 25)
--interval SECONDS             ポーリング間隔 (default: 2)
--dry-run                      検出のみ・回避/妨害なし
--verbose, -v                  全 HTTP 通信ログ表示
--lockfile PATH                lockfile 明示指定
```

## References

- [techchrism/valorant-api-docs](https://github.com/techchrism/valorant-api-docs)
- [AjaxFNC-YT/Valorant-Thing](https://github.com/AjaxFNC-YT/Valorant-Thing)

## License

MIT © 2026 4uthen7
