# Valo Dodger

> Valorant のエージェント選択画面で攻め/守りスタートを自動判定し、**守りスタート以外ならドッジ or ロビー妨害**する常駐ツール

Valorant の local + GLZ API を使って pregame (agent select) 入室を検知し、Attack スタート（＝守りスタート以外）だった場合に自動で対処する Python スクリプト。

- **sabotage** — チャット送信 + エージェント切替で味方にドッジさせる（自分は抜けない → 自分にペナルティなし）
- **combo** — 妨害 → 誰も抜けなければ最終ドッジ
- **dodge** — 攻め検出で即ドッジ（⚠ ペナルティあり）

## ドッジペナルティの注意点（調査まとめ・2025-09時点）

**自分でドッジすると必ずペナルティが付く**（Riot公式ブログ・コミュニティ情報より）。

| 行為 | ペナルティ |
| --- | --- |
| 初回ドッジ | キュー制限（約数分） |
| 24時間以内の連続ドッジ | キュー制限が急増（数十分〜1時間超） |
| コンペティティブでドッジ | 隠しRR減 4〜12（反復で増加、UI非表示） |
| 過度なAFK/ドッジ | 1日ランク制限 → 継続悪質者はゲーム禁止 |
| 誰か**他のプレイヤー**がドッジ | **自分はペナルティなし**（マッチキャンセルでロビー復帰） |
| エージェント未ロックで時間切れ | 「ドッジ」扱いでペナルティ（AFK扱い） |

- パッチ 6.07（2023-04）で連続ドッジへの RR 減が導入、パッチ 11.05（2025-09）で頻繁なドッジャーへのペナルティ上昇が加速
- 行動レーティング方式: ドッジで評価が下がり、クリーンな試合を続けるとゆっくり回復
- **「ペナルティを受けずに流す」唯一の方法は、味方にドッジさせること**（sabotage モード）
- 本ツールは 24時間以内の自ドッジ回数を `valo_dodger_state.json` に記録し、`--max-dodges-per-day`（デフォルト2回）を超える自ドッジを自動的に見送る

## Features (v6)

- サイド判定: `AllyTeam.TeamID` で **Red=攻め / Blue=守り** を確定判定
- セッション状態判定 (pregame / ingame / menus) で「誰かが抜けた」と「試合開始」を正確に区別
- ドッジ後のキュー制限残り時間を party API（`RestrictedSeconds`）から表示
- ドッジ履歴の記録と24hウィンドウのエスカレーション警告
- `--dry-run --verbose` で全 API 通信のデバッグ表示
- region/shard 自動検出（ShooterGame.log + riot-geo API フォールバック）

## Quick Start

```bat
cd C:\path\to\valo-dodger
python valo_dodger.py --dry-run --verbose   # 動作確認
python valo_dodger.py                        # 妨害のみ（デフォルト・自分にペナなし）
python valo_dodger.py --mode combo           # 妨害→最終ドッジ
python valo_dodger.py --mode dodge           # 即ドッジ（ペナルティ注意）
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
                     pregame 検出 → サイド判定 (Blue=守り / Red=攻め)
                                         ↓
        Attack → sabotage: 味方にドッジさせる（自分にペナなし）
               → combo: 妨害 → 誰も抜けなければ最終ドッジ
               → dodge: POST /pregame/v1/matches/{id}/quit
                                         ↓
        ドッジ後: party API の RestrictedSeconds でキュー制限を表示
```

## Options

```
--mode {sabotage,combo,dodge}  モード選択 (default: sabotage)
--dodge {attack,defense}       ドッジ対象サイド (default: attack = 守り以外を流す)
--sabotage-duration SECONDS    妨害時間 (default: 30)
--max-dodges-per-day N         24h以内の自ドッジ上限 (default: 2)
--once                         1回処理したら終了
--status                       ドッジせず、アカウントのペナルティ/キュー/履歴を表示して終了
--interval SECONDS             ポーリング間隔 (default: 2)
--dry-run                      検出のみ・回避/妨害なし
--verbose, -v                  全 HTTP 通信ログ表示
--lockfile PATH                lockfile 明示指定
```

## References

- [Riot: VALORANT Systems Health Series - AFK and Queue Dodge Update](https://playvalorant.com/en-us/news/dev/valorant-systems-health-series-afk-and-queue-dodge-update/)
- [Riot: Behavior Update: AFKs & Dodges](https://playvalorant.com/en-us/news/game-updates/behavior-update-afks-dodges/)
- [Riot Support: Penalties and Bans FAQ](https://support.riotgames.com/en-us/valorant/penalties/penalties-and-bans-faq)
- [Esports.net: How Queue Dodge Penalty Works In VALORANT](https://www.esports.net/news/valorant/queue-dodge-penalty-valorant/)
- [techchrism/valorant-api-docs](https://github.com/techchrism/valorant-api-docs)
- [Imu-D-sama/Fast-Pick（サイド判定の実装参考）](https://github.com/Imu-D-sama/Fast-Pick)

## License

MIT © 2026 4uthen7
