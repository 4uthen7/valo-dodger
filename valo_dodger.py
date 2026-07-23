#!/usr/bin/env python3
"""
Valorant Auto-Dodger v3
=======================
v2 からの修正:
- 毎回トークンをリフレッシュ（有効期限切れ対策）
- region/shard 検出のフォールバック追加（riot-geo API）
- 動的プラットフォームヘッダー生成
- 全 HTTP レスポンスボディをログ出力（--verbose 時）
- User-Agent: '' ヘッダー追加
"""

import argparse
import base64
import json
import logging
import multiprocessing
import os
import platform
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
# Platform header (動的生成)
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
# Low-level HTTP
# ---------------------------------------------------------------------------

def _http(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    body: Optional[bytes] = None,
    timeout: float = 8.0,
) -> tuple[int, Union[dict, str, bytes]]:
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
            LOG.debug("← HTTP %s (%d bytes): %s",
                      resp.status, len(raw),
                      raw[:300].decode(errors="replace"))
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        body_raw = e.read()
        LOG.debug("← HTTP %s (%d bytes): %s",
                  e.code, len(body_raw),
                  body_raw[:500].decode(errors="replace"))
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
        """毎回トークンを再取得（有効期限切れ対策）。"""
        status, data = _http(
            "GET",
            f"{self._local_base}/entitlements/v1/token",
            headers={"Authorization": f"Basic {self._basic}"},
        )
        if status != 200:
            raise RuntimeError(f"トークン取得失敗 HTTP {status}: {_trunc(data)}")

        self._access_token = data["accessToken"]
        self._entitlements = data.get("token", "")
        self._puuid = data.get("subject", "")

        if not self._puuid or not self._entitlements:
            raise RuntimeError(f"PUUID/entitlements 取得不可: {_trunc(data)}")

        LOG.info("tokens refreshed: puuid=%s...", self._puuid[:12])

    def _fetch_region_info(self) -> None:
        """region/shard/version を取得。ShooterGame.log → riot-geo API の順に試行。"""
        region, shard, ver = (None, None, None)

        # 方法1: ShooterGame.log
        slog = shooterlog_path()
        if slog.exists():
            text = slog.read_text(encoding="utf-8", errors="replace")
            m = re.findall(r"https://glz-(.+?)-1\.(.+?)\.a\.pvp\.net", text)
            if m:
                region, shard = m[-1]  # 最後のマッチ（最新）
            m2 = re.search(r"CI server version:\s*(\S+)", text)
            if m2:
                ver = m2.group(1)

        # 方法2: riot-geo API
        if not region or not shard:
            LOG.info("ShooterGame.log から region 未検出 → riot-geo API を試行")
            try:
                geo_headers = {
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                }
                status, data = _http(
                    "PUT",
                    "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant",
                    headers=geo_headers,
                    body=b"{}",
                )
                if status == 200 and isinstance(data, dict):
                    region = data.get("affinity", "ap")
                    shard = data.get("location", "jp")
                    LOG.info("riot-geo: region=%s shard=%s", region, shard)
            except Exception as e:
                LOG.warning("riot-geo 失敗: %s", e)

        # 方法3: デフォルト（日本）
        if not region:
            region = "ap"
        if not shard:
            shard = "jp"

        # version fallback
        if not ver:
            LOG.info("version 未検出 → valorant-api.com")
            try:
                req = urllib.request.Request(
                    "https://valorant-api.com/v1/version",
                    headers={"User-Agent": ""},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    vdata = json.loads(resp.read())
                ver = vdata["data"]["riotClientVersion"]
            except Exception:
                ver = "unknown"

        self._region = region
        self._shard = shard
        self._client_version = ver
        self._glz_base = f"https://glz-{region}-1.{shard}.a.pvp.net"
        LOG.info("GLZ base=%s version=%s", self._glz_base, ver)

    # -- headers --

    def _game_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Riot-Entitlements-JWT": self._entitlements,
            "X-Riot-ClientPlatform": PLATFORM_B64,
            "X-Riot-ClientVersion": self._client_version,
            "User-Agent": "",
        }

    # -- GLZ requests (with token refresh) --

    def glz_get(self, path: str) -> tuple[int, Union[dict, str]]:
        self._refresh_tokens()
        url = f"{self._glz_base}{path}"
        return _http("GET", url, headers=self._game_headers())

    def glz_post(self, path: str) -> tuple[int, Union[dict, str]]:
        self._refresh_tokens()
        url = f"{self._glz_base}{path}"
        return _http("POST", url, headers=self._game_headers())

    # -- pregame --

    def get_pregame_player(self) -> Optional[dict]:
        path = f"/pregame/v1/players/{self._puuid}"
        status, data = self.glz_get(path)

        if status == 404:
            LOG.debug("pregame: not in match (404)")
            return None
        if status != 200:
            LOG.warning("pregame player HTTP %s: %s", status, _trunc(data))
            return None

        LOG.info("pregame: IN MATCH — %s", _trunc(data))
        return data

    def get_pregame_match(self, match_id: str) -> Optional[dict]:
        path = f"/pregame/v1/matches/{match_id}"
        status, data = self.glz_get(path)
        if status != 200:
            LOG.warning("pregame match HTTP %s: %s", status, _trunc(data))
            return None
        LOG.info("pregame match: %s", _trunc(data))
        return data

    def quit_pregame(self, match_id: str) -> bool:
        path = f"/pregame/v1/matches/{match_id}/quit"
        status, data = self.glz_post(path)
        if status == 200:
            LOG.info("DODGE SUCCESS")
            return True
        LOG.warning("dodge failed HTTP %s: %s", status, _trunc(data))
        return False

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
    match_id = player_data.get("MatchID") or player_data.get("MatchId")
    if not match_id:
        LOG.warning("player_data に MatchID なし: %s", _trunc(player_data))
        return None

    match_data = api.get_pregame_match(match_id)
    if not match_data:
        return None

    teams = match_data.get("Teams", [])
    if len(teams) < 2:
        LOG.warning("Teams < 2: %s", _trunc(match_data))
        return None

    player_team_id = player_data.get("TeamID") or player_data.get("TeamId")

    team_side_map: dict[str, str] = {}
    for team in teams:
        tid = team.get("TeamID") or team.get("TeamId")
        starting_side = (
            team.get("StartingSide")
            or team.get("Side")
            or team.get("InitialSide")
        )
        if starting_side:
            team_side_map[tid] = starting_side.capitalize()

    if player_team_id not in team_side_map:
        if player_team_id == "Red":
            team_side_map[player_team_id] = "Attack"
        elif player_team_id == "Blue":
            team_side_map[player_team_id] = "Defense"
        else:
            LOG.warning("TeamID '%s' 不明. teams=%s", player_team_id, _trunc(teams, 500))
            return None

    side = team_side_map.get(player_team_id)
    LOG.info("side: TeamID=%s → %s", player_team_id, side)
    return side


# ===================================================================
# Saboteur
# ===================================================================

def _cpu_burn_worker(stop_event: threading.Event):
    while not stop_event.is_set():
        _ = sum(i * 1.0001 for i in range(10000))


def _lower_valorant_priority():
    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            ["wmic", "process", "where",
             'name="VALORANT-Win64-Shipping.exe"',
             "call", "setpriority", "64"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _restore_valorant_priority():
    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            ["wmic", "process", "where",
             'name="VALORANT-Win64-Shipping.exe"',
             "call", "setpriority", "32"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _api_flood_worker(base_url: str, headers: dict, stop_event: threading.Event):
    endpoints = ["/chat/v4/presences", "/chat/v4/friendrequests"]
    idx = 0
    ctx = _ssl_ctx()
    while not stop_event.is_set():
        try:
            path = endpoints[idx % len(endpoints)]
            idx += 1
            req = urllib.request.Request(f"{base_url}{path}", method="GET")
            for k, v in headers.items():
                req.add_header(k, v)
            urllib.request.urlopen(req, timeout=1.0, context=ctx)
        except Exception:
            pass


class Saboteur:
    def __init__(self, api: ValoAPI, duration: float = 25.0,
                 cpu_workers: Optional[int] = None):
        self._api = api
        self._duration = duration
        self._cpu_workers = cpu_workers or max(1, multiprocessing.cpu_count())
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._active = False

    def start(self):
        if self._active:
            return
        self._active = True
        self._stop_event.clear()
        _lower_valorant_priority()
        for _ in range(self._cpu_workers):
            t = threading.Thread(target=_cpu_burn_worker,
                                 args=(self._stop_event,), daemon=True)
            t.start()
            self._threads.append(t)
        for _ in range(2):
            t = threading.Thread(
                target=_api_flood_worker,
                args=(self._api.glz_base, self._api._game_headers(), self._stop_event),
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        LOG.info("sabotage started (%d cpu, %.1fs)", self._cpu_workers, self._duration)

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()
        _restore_valorant_priority()
        LOG.info("sabotage stopped")

    @property
    def duration(self) -> float:
        return self._duration


# ===================================================================
# Monitor
# ===================================================================

class DodgerMonitor:
    MODES = {"dodge": "即ドッジ", "sabotage": "妨害のみ", "combo": "妨害→最終ドッジ"}

    def __init__(self, api: ValoAPI, dodge_side: str = "Attack",
                 interval: float = 2.0, dry_run: bool = False,
                 mode: str = "dodge", sabotage_duration: float = 25.0):
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
        self._cleanup_saboteur()

    def _cleanup_saboteur(self):
        if self._saboteur:
            self._saboteur.stop()
            self._saboteur = None

    def run(self):
        print("=" * 55)
        print("  Valo Dodger v3")
        print(f"  GLZ:  {self.api.glz_base}")
        print(f"  PUUID: {self.api.puuid[:16]}...")
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
                    LOG.debug("接続待機 (tick=%d)", self._tick_count)
            except Exception:
                LOG.exception("tick error")
            self._sleep(self.interval)

        self._cleanup_saboteur()
        print("\n終了。")

    def _tick(self):
        self._tick_count += 1
        player = self.api.get_pregame_player()
        if player is None:
            self._last_match_id = None
            self._cleanup_saboteur()
            return

        match_id = player.get("MatchID") or player.get("MatchId")
        if not match_id or match_id == self._last_match_id:
            return
        self._last_match_id = match_id

        side = detect_player_side(self.api, player)
        if side is None:
            print("\n⚠ サイド判定不可 → レスポンス確認:")
            print(f"   player: {_trunc(player, 500)}")
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
        print(f"   ❌ {side} → ドッジ...")
        ok = self.api.quit_pregame(match_id)
        print("   ✅ 成功" if ok else "   ❌ 失敗")

    def _do_sabotage(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → 妨害開始 ({self.sabotage_duration}s)")
        self._saboteur = Saboteur(self.api, self.sabotage_duration)
        self._saboteur.start()
        deadline = time.monotonic() + self.sabotage_duration
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)
        self._cleanup_saboteur()
        still_in = self.api.get_pregame_player()
        print("   🎉 誰かが抜けた！" if still_in is None else "   😐 誰も抜けず...")

    def _do_combo(self, match_id, side):
        if self.dry_run:
            print(f"   [DRY RUN] {side} → skip")
            return
        print(f"   💀 {side} → コンボ ({self.sabotage_duration}s)")
        self._saboteur = Saboteur(self.api, self.sabotage_duration)
        self._saboteur.start()
        deadline = time.monotonic() + self.sabotage_duration
        dodged = False
        while self._running and time.monotonic() < deadline:
            time.sleep(1.0)
            if self.api.get_pregame_player() is None:
                dodged = True
                break
        self._cleanup_saboteur()
        if dodged:
            print("   🎉 誰かが抜けた！")
        else:
            print("   ❌ 最終ドッジ...")
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
    parser = argparse.ArgumentParser(description="Valorant Auto-Dodger v3")
    parser.add_argument("--mode", choices=["dodge", "sabotage", "combo"],
                        default="dodge")
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

    # 1. lockfile
    lf = args.lockfile or lockfile_path()
    if not lf.exists():
        print(f"❌ lockfile が見つかりません: {lf}\nValorant 起動してますか？", file=sys.stderr)
        sys.exit(1)
    try:
        parts = lf.read_text().strip().split(":")
        port, password = int(parts[2]), parts[3]
    except Exception as e:
        print(f"❌ lockfile 読み取り失敗: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. connect
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
