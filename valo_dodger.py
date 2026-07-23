#!/usr/bin/env python3
"""
Valorant Auto-Dodger v5 — 安全策強化版
======================================

v4 からの変更:
- エージェント切替に人間らしいランダム遅延 (300〜900ms)
- チャット爆撃の頻度・内容に揺らぎ（bot判定対策）
- ポーリング間隔にジッター（機械的パターン回避）
- 起動時にリスク警告表示
- GLZ quit は非推奨明示

リスク評価:
  Vanguard 検知     : ほぼゼロ（メモリ/DLL操作なし）
  サーバーサイド検知 : 低〜中（GLZ APIの使用パターン次第）
  通報              : 中（griefing通報の蓄積に注意）
  GLZ quit         : 高（最も検知されやすい操作）

usage:
  python valo_dodger.py                    # 妨害のみ（デフォルト・安全）
  python valo_dodger.py --mode combo       # 妨害→最終ドッジ
  python valo_dodger.py --dry-run -v       # 動作確認
"""

import argparse
import base64
import json
import logging
import os
import platform
import random
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Union

LOG = logging.getLogger("valo-dodger")

# ---------------------------------------------------------------------------
# リスク警告
# ---------------------------------------------------------------------------

RISK_WARNING = """
  ╔══════════════════════════════════════════════════════╗
  ║  【重要】使用上のリスクについて                       ║
  ║                                                      ║
  ║  Vanguard 検知          : ほぼなし (メモリ操作なし)    ║
  ║  サーバーサイド検知      : 低〜中 (API通信パターン次第) ║
  ║  通報 (griefing/afk)    : 中 (味方から通報の可能性)   ║
  ║  GLZ quit (ドッジ)      : 高 (UI経由せずAPI直叩き)    ║
  ║                                                      ║
  ║  デフォルトの sabotage モードは                       ║
  ║  ゲーム内の通常操作の範囲に抑えていますが、            ║
  ║  使用は自己責任でお願いします。                        ║
  ╚══════════════════════════════════════════════════════╝
"""

# ---------------------------------------------------------------------------
# Paths (Windows)
# ---------------------------------------------------------------------------

def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", ""))


def lockfile_path() -> Path:
    return _localappdata() / "Riot Games" / "Riot Client" / "Config" / "lockfile"


def shooterlog_path() -> Path:
    return _localappdata() / "VALORANT" / "Saved" / "Logs" / "ShooterGame.log"


# ---------------------------------------------------------------------------
# Platform header
# ---------------------------------------------------------------------------

def _build_platform_b64() -> str:
    try:
        ver = platform.version()
    except Exception:
        ver = "10.0.19042.1.256.64bit"
    info = {"platformType": "PC", "platformOS": "Windows",
            "platformOSVersion": ver, "platformChipset": "Unknown"}
    return base64.b64encode(json.dumps(info).encode()).decode()


PLATFORM_B64 = _build_platform_b64()


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http(method, url, headers=None, body=None, timeout=8.0):
    LOG.debug("→ %s %s", method, url[:120])
    req = urllib.request.Request(url, data=body, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        use_ctx = url.startswith("https://127.0.0.1")
        with urllib.request.urlopen(req, timeout=timeout,
                                     context=_ssl_ctx() if use_ctx else None) as resp:
            raw = resp.read()
            LOG.debug("← HTTP %s (%d bytes)", resp.status, len(raw))
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        body_raw = e.read()
        LOG.debug("← HTTP %s (%d bytes)", e.code, len(body_raw))
        try:
            return e.code, json.loads(body_raw)
        except json.JSONDecodeError:
            return e.code, body_raw.decode(errors="replace")
    except urllib.error.URLError as e:
        raise ConnectionError(f"接続エラー {url}: {e.reason}") from e


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class ValoAPI:
    def __init__(self, port: int, password: str):
        self._local_base = f"https://127.0.0.1:{port}"
        raw = f"riot:{password}"
        self._basic = base64.b64encode(raw.encode()).decode()
        self._access_token = ""
        self._entitlements = ""
        self._puuid = ""
        self._region = ""
        self._shard = ""
        self._client_version = ""
        self._glz_base = ""

    def connect(self) -> None:
        self._refresh_tokens()
        self._fetch_region_info()

    def _refresh_tokens(self) -> None:
        status, data = _http(
            "GET", f"{self._local_base}/entitlements/v1/token",
            headers={"Authorization": f"Basic {self._basic}"},
        )
        if status != 200:
            raise RuntimeError(f"トークン取得失敗 HTTP {status}")
        self._access_token = data["accessToken"]
        self._entitlements = data.get("token", "")
        self._puuid = data.get("subject", "")
        if not self._puuid:
            raise RuntimeError("PUUID 取得不可")
        LOG.info("tokens: puuid=%s...", self._puuid[:12])

    def _fetch_region_info(self) -> None:
        region, shard, ver = (None, None, None)
        slog = shooterlog_path()
        if slog.exists():
            text = slog.read_text(encoding="utf-8", errors="replace")
            m = re.findall(r"https://glz-(.+?)-1\.(.+?)\.a\.pvp\.net", text)
            if m:
                region, shard = m[-1]
            m2 = re.search(r"CI server version:\s*(\S+)", text)
            if m2:
                ver = m2.group(1)
        if not region or not shard:
            try:
                status, data = _http(
                    "PUT",
                    "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant",
                    headers={"Authorization": f"Bearer {self._access_token}",
                             "Content-Type": "application/json"},
                    body=b"{}",
                )
                if status == 200 and isinstance(data, dict):
                    region = data.get("affinity", "ap")
                    shard = data.get("location", "jp")
            except Exception:
                pass
        region = region or "ap"
        shard = shard or "jp"
        if not ver:
            try:
                req = urllib.request.Request(
                    "https://valorant-api.com/v1/version",
                    headers={"User-Agent": ""})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    ver = json.loads(resp.read())["data"]["riotClientVersion"]
            except Exception:
                ver = "unknown"
        self._region = region
        self._shard = shard
        self._client_version = ver
        self._glz_base = f"https://glz-{region}-1.{shard}.a.pvp.net"
        LOG.info("GLZ=%s ver=%s", self._glz_base, ver)

    def _game_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Riot-Entitlements-JWT": self._entitlements,
            "X-Riot-ClientPlatform": PLATFORM_B64,
            "X-Riot-ClientVersion": self._client_version,
            "User-Agent": "",
        }

    def _local_headers(self) -> dict:
        return {"Authorization": f"Basic {self._basic}"}

    # -- GLZ --

    def glz_get(self, path: str) -> tuple[int, Union[dict, str]]:
        return _http("GET", f"{self._glz_base}{path}",
                     headers=self._game_headers())

    def glz_post(self, path: str, body: Optional[bytes] = None) -> tuple[int, Union[dict, str]]:
        return _http("POST", f"{self._glz_base}{path}",
                     headers=self._game_headers(), body=body)

    # -- Local API --

    def local_get(self, path: str) -> tuple[int, Union[dict, str]]:
        return _http("GET", f"{self._local_base}{path}",
                     headers=self._local_headers())

    def local_post(self, path: str, body: dict) -> tuple[int, Union[dict, str]]:
        return _http("POST", f"{self._local_base}{path}",
                     headers=self._local_headers(),
                     body=json.dumps(body).encode())

    # -- pregame --

    def get_pregame_player(self) -> Optional[dict]:
        status, data = self.glz_get(f"/pregame/v1/players/{self._puuid}")
        if status == 404:
            return None
        if status != 200:
            LOG.warning("pregame player HTTP %s", status)
            return None
        return data

    def get_pregame_match(self, match_id: str) -> Optional[dict]:
        status, data = self.glz_get(f"/pregame/v1/matches/{match_id}")
        return data if status == 200 else None

    def select_agent(self, match_id: str, agent_id: str) -> bool:
        status, _ = self.glz_post(
            f"/pregame/v1/matches/{match_id}/select/{agent_id}"
        )
        return status == 200

    def quit_pregame(self, match_id: str) -> bool:
        """⚠ 高リスク操作。API経由のドッジはRiotに検知される可能性がある。"""
        status, _ = self.glz_post(f"/pregame/v1/matches/{match_id}/quit")
        return status == 200

    def get_pregame_cid(self) -> Optional[str]:
        status, data = self.local_get("/chat/v6/conversations/ares-pregame")
        if status == 200 and isinstance(data, dict):
            return data.get("cid") or data.get("id")
        return None

    def send_chat(self, cid: str, message: str) -> bool:
        body = {"cid": cid, "message": message, "type": "groupchat"}
        status, _ = self.local_post(
            f"/chat/v6/conversations/{cid}/messages", body
        )
        return status == 200

    @property
    def puuid(self) -> str:
        return self._puuid

    @property
    def glz_base(self) -> str:
        return self._glz_base


def _trunc(x, n=300):
    s = json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)
    return s[:n] + ("..." if len(s) > n else "")


# ---------------------------------------------------------------------------
# Side detection
# ---------------------------------------------------------------------------

def detect_player_side(api: ValoAPI, player_data: dict) -> Optional[str]:
    match_id = player_data.get("MatchID") or player_data.get("MatchId")
    if not match_id:
        return None
    match_data = api.get_pregame_match(match_id)
    if not match_data:
        return None
    ally = match_data.get("AllyTeam", {})
    tid = ally.get("TeamID") or ally.get("TeamId")
    if not tid:
        for team in match_data.get("Teams", []):
            for p in team.get("Players", []):
                if p.get("Subject") == api.puuid:
                    tid = team.get("TeamID") or team.get("TeamId")
                    break
            if tid:
                break
    if not tid:
        return None
    side = "Defense" if tid == "Blue" else "Attack" if tid == "Red" else None
    if side:
        LOG.info("side: %s → %s", tid, side)
    return side


# ===================================================================
# Saboteur v5 — 安全策入り
# ===================================================================

# チャット爆撃用メッセージ（バリエーションでパターン回避）
_SPAM_POOL = [
    # ゼロ幅文字連打（メッセージとしてほぼ空だがデータ量あり）
    lambda: "\u2800" * random.randint(100, 250),
    lambda: "\u3164" * random.randint(100, 250),
    # ブロック文字
    lambda: "\u2588" * random.randint(80, 200),
    # 絵文字のランダムな組み合わせ
    lambda: "".join(random.choice("\U0001f4a9\U0001f525\U0001f480\U0001f389\U0001f3af\U0001f4a3\U0001f60e\U0001f921") for _ in range(random.randint(30, 60))),
    # 改行爆弾
    lambda: "\n" * random.randint(5, 15) + "." + "\n" * random.randint(5, 15),
    # 普通のメッセージ（たまに混ぜる）
    lambda: random.choice(["glhf", "nt", "ns", "lol", "mb", "srry"]),
]


class Saboteur:
    """味方クライアントに負荷をかける（自衛策あり）。

    - エージェント切替: 300〜900ms のランダム間隔（人間の操作速度に近い）
    - チャット爆撃: 内容・頻度に揺らぎ（bot判定パターン回避）
    - 時々数秒の沈黙（人間らしさの演出）
    """

    def __init__(self, api: ValoAPI, match_id: str, duration: float = 25.0):
        self._api = api
        self._match_id = match_id
        self._duration = duration
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active = False

    def start(self):
        if self._active:
            return
        self._active = True
        self._stop_event.clear()

        t1 = threading.Thread(target=self._agent_cycle, daemon=True)
        t1.start()
        self._threads.append(t1)

        t2 = threading.Thread(target=self._chat_flood, daemon=True)
        t2.start()
        self._threads.append(t2)

        LOG.info("sabotage v5 (humanized, %.1fs)", self._duration)

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        LOG.info("sabotage stopped")

    def _agent_cycle(self):
        agents = [
            "add6443a-41bd-e414-f6ad-e58d267f4e95",  # Jett
            "a3bfb853-43b2-7238-a4f1-ad90e9e46bcc",  # Reyna
            "569fdd95-4d10-43ab-ca70-79becc718b46",  # Sage
            "707eab51-4836-f488-046a-cda6bf494859",  # Phoenix
            "eb93336a-449b-9c1b-0a54-a891f7921d69",  # Raze
            "9f0d8ba9-4140-b941-57d3-a7ad57c6b417",  # Brimstone
            "f94c3b30-42be-e959-889c-5aa313dba261",  # Viper
            "117ed9e3-49f3-6512-3ccf-0cada7e3823b",  # Cypher
            "320b2a48-4d9b-a075-30f1-1f93a9b638fa",  # Sova
            "1e58de9e-4250-9012-b2ac-89ffe26b0f58",  # Killjoy
            "95b78ed7-4637-86d9-7e41-71ba8c293152",  # Skye
            "601dbbe7-43ce-be57-2a40-4abd24953621",  # Yoru
            "8e253930-4c05-31dd-1b6c-968525494517",  # Omen
        ]
        idx = 0
        while not self._stop_event.is_set():
            agent = agents[idx % len(agents)]
            idx += 1
            try:
                self._api.select_agent(self._match_id, agent)
            except Exception:
                pass
            # 人間らしいランダム遅延: 300〜900ms
            delay = random.uniform(0.3, 0.9)
            # 時々長めの間を入れる
            if random.random() < 0.15:
                delay = random.uniform(1.5, 3.0)
            self._stop_event.wait(delay)

    def _chat_flood(self):
        cid = self._api.get_pregame_cid()
        if not cid:
            LOG.warning("chat flood: cid 取得不可")
            return

        LOG.info("chat flood: cid=%s", cid)
        count = 0

        while not self._stop_event.is_set():
            # メッセージ生成（毎回変わる）
            msg = random.choice(_SPAM_POOL)()
            try:
                ok = self._api.send_chat(cid, msg)
                if ok:
                    count += 1
            except Exception:
                pass

            # 送信間隔に揺らぎ: 50〜200ms
            delay = random.uniform(0.05, 0.2)
            # 時々 1〜3 秒沈黙（人間らしさ）
            if random.random() < 0.1:
                delay = random.uniform(1.0, 3.0)
            self._stop_event.wait(delay)

        LOG.info("chat flood: %d messages", count)

    @property
    def duration(self) -> float:
        return self._duration


# ===================================================================
# Monitor
# ===================================================================

class DodgerMonitor:
    MODES = {
        "sabotage": "チャット爆撃＋エージェント切替（自分は抜けない／安全）",
        "combo": "妨害 → 最終ドッジ（最終手段にGLZ quitを使うため注意）",
        "dodge": "即ドッジ（⚠ 高リスク・非推奨）",
    }

    def __init__(self, api: ValoAPI, dodge_side: str = "Attack",
                 interval: float = 2.0, dry_run: bool = False,
                 mode: str = "sabotage", sabotage_duration: float = 25.0):
        self.api = api
        self.dodge_side = dodge_side.capitalize()
        self.interval = interval
        self.dry_run = dry_run
        self.mode = mode
        self.sabotage_duration = sabotage_duration
        self._running = True
        self._last_match_id: Optional[str] = None
        self._saboteur: Optional[Saboteur] = None
        self._tick_count = 0
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, frame):
        self._running = False
        self._cleanup()

    def _cleanup(self):
        if self._saboteur:
            self._saboteur.stop()
            self._saboteur = None

    def run(self):
        print("=" * 55)
        print("  Valo Dodger v5")
        print(f"  GLZ:  {self.api.glz_base}")
        print(f"  回避: {self.dodge_side} スタート")
        print(f"  モード: {self.MODES.get(self.mode, self.mode)}")
        if self.dry_run:
            print("  [DRY RUN]")
        print(f"  間隔: {self.interval}s（ジッターあり）")
        print("=" * 55)

        while self._running:
            try:
                self._tick()
            except ConnectionError:
                self._tick_count += 1
                if self._tick_count % 15 == 1:
                    LOG.debug("待機中 (tick=%d)", self._tick_count)
            except Exception:
                LOG.exception("tick error")
            # ジッター付きスリープ（機械的パターン回避）
            jitter = random.uniform(-0.3, 0.3)
            self._sleep(max(1.0, self.interval + jitter))

        self._cleanup()
        print("\n終了。")

    def _tick(self):
        self._tick_count += 1

        # トークンは30 tickごとにリフレッシュ（過剰なAPIコール回避）
        if self._tick_count % 30 == 0:
            try:
                self.api._refresh_tokens()
            except Exception:
                pass

        player = self.api.get_pregame_player()
        if player is None:
            self._last_match_id = None
            self._cleanup()
            return

        match_id = player.get("MatchID") or player.get("MatchId")
        if not match_id or match_id == self._last_match_id:
            return
        self._last_match_id = match_id

        side = detect_player_side(self.api, player)
        if side is None:
            print("\n⚠ サイド判定不可")
            return

        print(f"\n🎯 マッチ検出 | {match_id[:12]}... | サイド: **{side}**")

        if side != self.dodge_side:
            print(f"   ✅ {side} スタート → プレイ")
            return

        self._handle(match_id, side)

    def _handle(self, match_id, side):
        if self.mode == "dodge":
            self._do_dodge(match_id, side)
        elif self.mode == "sabotage":
            self._do_sabotage(match_id, side)
        else:
            self._do_combo(match_id, side)

    def _do_dodge(self, match_id, side):
        print(f"   ⚠⚠⚠ {side} → 即ドッジ（高リスク操作！）")
        if self.dry_run:
            print("   [DRY RUN] skip")
            return
        print("   ⚠ この操作はRiotに検知される可能性があります")
        ok = self.api.quit_pregame(match_id)
        print("   ✅ 成功" if ok else "   ❌ 失敗")

    def _do_sabotage(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → 妨害開始（人間らしいパターンで実行）")
        print(f"   🔥 チャット爆撃＋エージェント切替 ({self.sabotage_duration}s)...")

        self._saboteur = Saboteur(self.api, match_id, self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        while self._running and time.monotonic() < deadline:
            time.sleep(1.5)  # 監視も控えめに
            if self.api.get_pregame_player() is None:
                break

        self._cleanup()
        still_in = self.api.get_pregame_player()
        print("   🎉 誰かが抜けた！" if still_in is None else "   😐 誰も抜けず...")

    def _do_combo(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → コンボ（妨害 {self.sabotage_duration}s → 最終ドッジ）")

        self._saboteur = Saboteur(self.api, match_id, self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        dodged = False
        while self._running and time.monotonic() < deadline:
            time.sleep(1.5)
            if self.api.get_pregame_player() is None:
                dodged = True
                break

        self._cleanup()

        if dodged:
            print("   🎉 誰かが抜けた！")
        else:
            print("   ⚠⚠⚠ 最終手段: GLZ quit（高リスク）...")
            ok = self.api.quit_pregame(match_id)
            print("   ✅" if ok else "   ❌ 失敗")

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Valorant Auto-Dodger v5")
    parser.add_argument("--mode", choices=["sabotage", "combo", "dodge"],
                        default="sabotage",
                        help="sabotage=妨害のみ(安全), combo=妨害→最終ドッジ, dodge=即ドッジ(非推奨)")
    parser.add_argument("--dodge", choices=["attack", "defense"], default="attack")
    parser.add_argument("--sabotage-duration", type=float, default=25.0)
    parser.add_argument("--lockfile", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # リスク警告
    print(RISK_WARNING)

    lf = args.lockfile or lockfile_path()
    if not lf.exists():
        print(f"❌ lockfile なし: {lf}", file=sys.stderr)
        sys.exit(1)
    try:
        parts = lf.read_text().strip().split(":")
        port, password = int(parts[2]), parts[3]
    except Exception as e:
        print(f"❌ lockfile 読取失敗: {e}", file=sys.stderr)
        sys.exit(1)

    api = ValoAPI(port, password)
    try:
        api.connect()
    except Exception as e:
        print(f"❌ 接続失敗: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"✅ 接続OK | {api.glz_base}")

    monitor = DodgerMonitor(
        api=api,
        dodge_side="Attack" if args.dodge == "attack" else "Defense",
        interval=args.interval, dry_run=args.dry_run, mode=args.mode,
        sabotage_duration=args.sabotage_duration,
    )
    monitor.run()


if __name__ == "__main__":
    main()
