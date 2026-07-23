#!/usr/bin/env python3
"""
Valorant Auto-Dodger v4
=======================
v3 からの主な変更:
- 妨害方法を「味方に届く」ものに全面刷新
  - プリゲームチャット爆撃（ローカルAPI経由＝安全）
  - エージェント連続切り替え（GLZ select = 通常操作）
  - CPU飽和/プロセス優先度操作は削除（自分にしか効かないため）
- GLZ quit（ドッジ）はデフォルトで無効化。comboモード時のみ最終手段として使用
- banリスク最小化のため GLZ API 呼び出し頻度を抑制

モード:
  sabotage = チャット爆撃 + エージェント切替で味方を追い出す（自分は抜けない）
  combo    = 妨害 → 誰も抜けなければ最終手段で自分がドッジ
  dodge    = 即ドッジ（非推奨）
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
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Union

LOG = logging.getLogger("valo-dodger")

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
    info = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": ver,
        "platformChipset": "Unknown",
    }
    return base64.b64encode(json.dumps(info).encode()).decode()


PLATFORM_B64 = _build_platform_b64()

# ---------------------------------------------------------------------------
# SSL
# ---------------------------------------------------------------------------

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

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
        self._access_token: str = ""
        self._entitlements: str = ""
        self._puuid: str = ""
        self._region: str = ""
        self._shard: str = ""
        self._client_version: str = ""
        self._glz_base: str = ""

    # -- connect --

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
            LOG.info("ShooterGame.log 未検出 → riot-geo")
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
        if not region:
            region = "ap"
        if not shard:
            shard = "jp"
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

    # -- headers --

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

    # -- GLZ (pregame) --

    def glz_get(self, path: str) -> tuple[int, Union[dict, str]]:
        url = f"{self._glz_base}{path}"
        return _http("GET", url, headers=self._game_headers())

    def glz_post(self, path: str, body: Optional[bytes] = None) -> tuple[int, Union[dict, str]]:
        url = f"{self._glz_base}{path}"
        return _http("POST", url, headers=self._game_headers(), body=body)

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
        path = f"/pregame/v1/players/{self._puuid}"
        status, data = self.glz_get(path)
        if status == 404:
            return None
        if status != 200:
            LOG.warning("pregame player HTTP %s", status)
            return None
        LOG.info("pregame: IN MATCH")
        return data

    def get_pregame_match(self, match_id: str) -> Optional[dict]:
        status, data = self.glz_get(f"/pregame/v1/matches/{match_id}")
        if status != 200:
            LOG.warning("pregame match HTTP %s", status)
            return None
        return data

    def select_agent(self, match_id: str, agent_id: str) -> bool:
        path = f"/pregame/v1/matches/{match_id}/select/{agent_id}"
        status, _ = self.glz_post(path)
        return status == 200

    def quit_pregame(self, match_id: str) -> bool:
        path = f"/pregame/v1/matches/{match_id}/quit"
        status, _ = self.glz_post(path)
        if status == 200:
            LOG.info("DODGE SUCCESS")
            return True
        LOG.warning("dodge failed HTTP %s", status)
        return False

    def get_agents(self) -> list[str]:
        """使用可能なエージェントのUUIDリストを取得。"""
        status, data = self.glz_get(
            f"/pregame/v1/matches/{self._last_match_id}/loadouts"
        )
        # フォールバック：既知のエージェントID
        return [
            "add6443a-41bd-e414-f6ad-e58d267f4e95",  # Jett
            "a3bfb853-43b2-7238-a4f1-ad90e9e46bcc",  # Reyna
            "569fdd95-4d10-43ab-ca70-79becc718b46",  # Sage
            "707eab51-4836-f488-046a-cda6bf494859",  # Phoenix
            "eb93336a-449b-9c1b-0a54-a891f7921d69",  # Raze
            "9f0d8ba9-4140-b941-57d3-a7ad57c6b417",  # Brimstone
            "f94c3b30-42be-e959-889c-5aa313dba261",  # Viper
            "1dbf2edd-4729-0984-3115-daa5eed44993",  # Breach
            "117ed9e3-49f3-6512-3ccf-0cada7e3823b",  # Cypher
            "320b2a48-4d9b-a075-30f1-1f93a9b638fa",  # Sova
            "1e58de9e-4250-9012-b2ac-89ffe26b0f58",  # Killjoy
            "95b78ed7-4637-86d9-7e41-71ba8c293152",  # Skye
            "601dbbe7-43ce-be57-2a40-4abd24953621",  # Yoru
            "8e253930-4c05-31dd-1b6c-968525494517",  # Omen
            "cc8b64c8-4b25-4a69-6e47-8e75bfc8e1e7",  # Gekko
        ]

    # -- pregame chat spam --

    def get_pregame_cid(self) -> Optional[str]:
        """ares-pregame の会話IDを取得。"""
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
    if isinstance(x, dict):
        s = json.dumps(x, ensure_ascii=False)
    else:
        s = str(x)
    return s[:n] + ("..." if len(s) > n else "")


# ---------------------------------------------------------------------------
# Side detection
# ---------------------------------------------------------------------------

def detect_player_side(api: ValoAPI, player_data: dict) -> Optional[str]:
    """AllyTeam.TeamID からサイド判定。Blue=Defense, Red=Attack。"""
    match_id = player_data.get("MatchID") or player_data.get("MatchId")
    if not match_id:
        LOG.warning("MatchID なし")
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
        LOG.warning("TeamID 取得不可")
        return None

    side = "Defense" if tid == "Blue" else "Attack" if tid == "Red" else None
    if side:
        LOG.info("side: %s → %s", tid, side)
    return side


# ===================================================================
# Saboteur v4 — 味方に届く妨害
# ===================================================================

# チャット爆撃用メッセージ（連投でラグを誘発）
_SPAM_MESSAGES = [
    "\u2800" * 200,           # 空白文字200連打（ゼロ幅）
    "\u3164" * 200,           # ハングルフィラー
    "\U0001f4a9" * 50,        # 💩 x50
    "\U0001f389" * 50,        # 🎉 x50
    "\U0001f525" * 50,        # 🔥 x50
    "\U0001f480" * 50,        # 💀 x50
    "\u2588" * 200,           # █ ブロック文字 x200
    "\n" * 10 + " " + "\n" * 10,  # 改行爆弾
]


class Saboteur:
    """味方のクライアントに負荷をかける妨害エンジン。

    自分への影響を最小限にしつつ、以下の方法で味方を追い出す:
    1. プリゲームチャット爆撃（ローカルAPI経由＝banリスクほぼゼロ）
    2. エージェント高速切り替え（通常操作の範囲内）
    """

    def __init__(self, api: ValoAPI, match_id: str,
                 duration: float = 25.0, chat_rps: float = 10.0):
        self._api = api
        self._match_id = match_id
        self._duration = duration
        self._chat_rps = chat_rps  # 1秒あたりのチャット送信数
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active = False

    def start(self):
        if self._active:
            return
        self._active = True
        self._stop_event.clear()

        # スレッド1: チャット爆撃
        t1 = threading.Thread(target=self._chat_flood, daemon=True)
        t1.start()
        self._threads.append(t1)

        # スレッド2: エージェント高速切替
        t2 = threading.Thread(target=self._agent_cycle, daemon=True)
        t2.start()
        self._threads.append(t2)

        LOG.info("sabotage v4 started (chat+agent, %.1fs)", self._duration)

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        LOG.info("sabotage stopped")

    def _chat_flood(self):
        """プリゲームチャットに高速でメッセージを送りつける。"""
        cid = self._api.get_pregame_cid()
        if not cid:
            LOG.warning("chat flood: cid 取得不可")
            return

        LOG.info("chat flood: cid=%s rps=%.0f", cid, self._chat_rps)
        interval = 1.0 / max(self._chat_rps, 1.0)
        count = 0

        while not self._stop_event.is_set():
            msg = random.choice(_SPAM_MESSAGES)
            try:
                ok = self._api.send_chat(cid, msg)
                if ok:
                    count += 1
            except Exception:
                pass
            time.sleep(interval)

        LOG.info("chat flood: %d messages sent", count)

    def _agent_cycle(self):
        """エージェントを高速で切り替え続ける。"""
        agents = self._api.get_agents()
        if not agents:
            LOG.warning("agent cycle: エージェントリスト取得不可")
            return

        interval = 0.15  # 150ms 間隔で切り替え
        idx = 0
        while not self._stop_event.is_set():
            agent = agents[idx % len(agents)]
            idx += 1
            try:
                self._api.select_agent(self._match_id, agent)
            except Exception:
                pass
            time.sleep(interval)

    @property
    def duration(self) -> float:
        return self._duration


# ===================================================================
# Monitor
# ===================================================================

class DodgerMonitor:
    MODES = {
        "dodge": "即ドッジ（非推奨）",
        "sabotage": "チャット爆撃＋エージェント切替（自分は抜けない）",
        "combo": "妨害 → 最終ドッジ",
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
        print("  Valo Dodger v4")
        print(f"  GLZ:  {self.api.glz_base}")
        print(f"  回避: {self.dodge_side} スタート")
        print(f"  モード: {self.MODES.get(self.mode, self.mode)}")
        if self.dry_run:
            print("  [DRY RUN]")
        print(f"  間隔: {self.interval}s")
        print("=" * 55)

        while self._running:
            try:
                self._tick()
            except ConnectionError:
                self._tick_count += 1
                if self._tick_count == 1 or self._tick_count % 15 == 0:
                    LOG.debug("待機中 (tick=%d)", self._tick_count)
            except Exception:
                LOG.exception("tick error")
            self._sleep(self.interval)

        self._cleanup()
        print("\n終了。")

    def _tick(self):
        self._tick_count += 1
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
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   ⚠ {side} → ドッジ（非推奨。ペナルティ/検知リスクあり）")
        ok = self.api.quit_pregame(match_id)
        print("   ✅ 成功" if ok else "   ❌ 失敗")

    def _do_sabotage(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → チャット爆撃＋エージェント切替開始")
        print(f"   🔥 味方クライアントに負荷送信中 ({self.sabotage_duration}s)...")
        print(f"   👀 誰かが抜けるのを待ちます")

        self._saboteur = Saboteur(self.api, match_id, self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        while self._running and time.monotonic() < deadline:
            time.sleep(1.0)
            if self.api.get_pregame_player() is None:
                break

        self._cleanup()
        still_in = self.api.get_pregame_player()
        print("   🎉 誰かが抜けた！" if still_in is None else "   😐 誰も抜けず...")

    def _do_combo(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → コンボ（妨害 → 最終ドッジ）")
        print(f"   🔥 チャット爆撃＋エージェント切替 ({self.sabotage_duration}s)...")

        self._saboteur = Saboteur(self.api, match_id, self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        dodged = False
        while self._running and time.monotonic() < deadline:
            time.sleep(1.0)
            if self.api.get_pregame_player() is None:
                dodged = True
                break

        self._cleanup()

        if dodged:
            print("   🎉 誰かが抜けた！")
        else:
            print("   ⚠ 最終手段: ドッジします（ペナルティ/検知リスクあり）...")
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
    parser = argparse.ArgumentParser(description="Valorant Auto-Dodger v4")
    parser.add_argument("--mode", choices=["dodge", "sabotage", "combo"],
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
