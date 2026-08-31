#!/usr/bin/env python3
"""
Valorant Auto-Dodger v6 — ペナルティ対応版
==========================================

v5 からの変更:
- サイド判定を確定: Blue=Defender / Red=Attacker
  (エージェント選択画面の pregame API で初回ハーフのサイドが確定して取得できる。
   同種ツール Fast-Pick 等と同じマッピング)
- セッション状態判定 (pregame / ingame / menus) を追加:
  「誰かが抜けた」「試合が始まった」「まだ誰も抜けない」を正確に区別
- ドッジペナルティ対応:
  * 24時間以内の自ドッジ回数を valo_dodger_state.json に記録
  * 連続ドッジのエスカレーションを警告 (キュー制限・隠しRR減 4〜12・1日ランク制限)
  * --max-dodges-per-day で自爆ドッジの回数を制限 (超過時は妨害のみ)
  * ドッジ後に party API の RestrictedSeconds から残りキュー制限を表示
- サボタージュ用エージェントUUIDの誤りを修正

ペナルティ調査の要点 (Riot公式 + コミュニティ情報、2025-09時点):
- 自分でドッジすると必ずペナルティ:
  * キュー制限 (初回は数分。連続で急増し、パッチ11.05以降は頻繁なドッジャーに急加速)
  * コンペティティブでは隠しRR減 (標準ドッジで約6、反復で増加し 4〜12 の幅)
  * 行動レーティングが低下 → 以後のペナルティが早く/重くなる
  * 過度なAFK/ドッジで 1日ランク制限、継続悪質者にはゲーム禁止
- 誰かがドッジした場合はマッチがキャンセルされ、自分はペナルティなしでロビー復帰
  → 「味方に流させる」のが自分にペナルティを付けない唯一の方法
- エージェントをロックせず時間切れでも「ドッジ」扱いでペナルティが来る (AFK扱い)

usage:
  python valo_dodger.py                         # 妨害のみ (デフォルト・自分にペナなし)
  python valo_dodger.py --mode combo            # 妨害→最終ドッジ (ペナルティ警告あり)
  python valo_dodger.py --mode dodge            # 即ドッジ (ペナルティ・RR減注意)
  python valo_dodger.py --dry-run -v            # 動作確認
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
  ╔══════════════════════════════════════════════════════════════╗
  ║  【重要】ドッジペナルティとリスクについて (2025-09調査)      ║
  ║                                                              ║
  ║  ・自分でドッジすると必ずペナルティが付く                    ║
  ║     - キュー制限: 初回は数分、連続で急増                     ║
  ║     - コンペ: 隠しRR減 4〜12 (反復で増加)                    ║
  ║     - パッチ11.05以降、頻繁なドッジャーへの                 ║
  ║       ペナルティは急加速している                             ║
  ║  ・過度なAFK/ドッジは 1日ランク制限 → ゲーム禁止まで発展    ║
  ║  ・味方がドッジした場合は自分は無傷 (マッチキャンセル)      ║
  ║  ・エージェント未ロックの時間切れも「ドッジ」扱い           ║
  ║  ・使用は自己責任。通報 (griefing) の蓄積にも注意            ║
  ╚══════════════════════════════════════════════════════════════╝
"""

# ---------------------------------------------------------------------------
# 状態ファイル (ドッジ履歴)
# ---------------------------------------------------------------------------

STATE_FILE = Path(__file__).resolve().parent / "valo_dodger_state.json"
DODGE_WINDOW_H = 24  # この時間以内のドッジ回数をカウント


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("dodges"), list):
            return data
    except Exception:
        pass
    return {"dodges": []}


def save_state(data: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        LOG.warning("state save failed: %s", e)


def dodges_in_window(state: dict, window_s: float) -> list:
    now = time.time()
    return [t for t in state.get("dodges", []) if now - t < window_s]


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

    # -- GLZ / PD --

    def glz_get(self, path: str) -> tuple[int, Union[dict, str]]:
        return _http("GET", f"{self._glz_base}{path}",
                     headers=self._game_headers())

    def glz_post(self, path: str, body: Optional[bytes] = None) -> tuple[int, Union[dict, str]]:
        return _http("POST", f"{self._glz_base}{path}",
                     headers=self._game_headers(), body=body)

    @property
    def pd_base(self) -> str:
        return f"https://pd.{self._region}.a.pvp.net"

    def pd_get(self, path: str) -> tuple[int, Union[dict, str]]:
        return _http("GET", f"{self.pd_base}{path}",
                     headers=self._game_headers())

    # -- Local API --

    def local_get(self, path: str) -> tuple[int, Union[dict, str]]:
        return _http("GET", f"{self._local_base}{path}",
                     headers=self._local_headers())

    def local_post(self, path: str, body: dict) -> tuple[int, Union[dict, str]]:
        return _http("POST", f"{self._local_base}{path}",
                     headers=self._local_headers(),
                     body=json.dumps(body).encode())

    # -- セッション状態 --
    # 返り値: "pregame" / "ingame" / "menus"

    def session_state(self) -> str:
        if self.get_pregame_player() is not None:
            return "pregame"
        if self.get_coregame_player() is not None:
            return "ingame"
        return "menus"

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
        """ドッジ (pregame 離脱)。Riot側でペナルティ判定される。"""
        status, _ = self.glz_post(f"/pregame/v1/matches/{match_id}/quit")
        return status == 200

    # -- coregame --

    def get_coregame_player(self) -> Optional[dict]:
        status, data = self.glz_get(f"/core-game/v1/players/{self._puuid}")
        if status == 404:
            return None
        return data if status == 200 else None

    # -- party (ドッジ後のキュー制限確認) --

    def get_party_restriction(self) -> Optional[dict]:
        """パーティ情報から RestrictedSeconds (キュー制限残り秒) を取得。
        取得できない環境では None を返す。"""
        party_id = None
        status, data = self.glz_get(f"/parties/v1/players/{self._puuid}")
        if status != 200 or not isinstance(data, dict):
            status, data = self.pd_get(f"/parties/v1/players/{self._puuid}")
        if status == 200 and isinstance(data, dict):
            party_id = data.get("CurrentPartyID")
        if not party_id:
            return None
        status, party = self.glz_get(f"/parties/v1/parties/{party_id}")
        if status != 200 or not isinstance(party, dict):
            status, party = self.pd_get(f"/parties/v1/parties/{party_id}")
        if status != 200 or not isinstance(party, dict):
            return None
        return {
            "restricted_seconds": party.get("RestrictedSeconds") or 0,
            "queue_ineligibilities": party.get("QueueIneligibilities") or [],
            "eligible_queues": party.get("EligibleQueues") or [],
        }

    # -- chat --

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


# ---------------------------------------------------------------------------
# Pregame コンテキスト (マッチID / サイド / ランク / 残り時間)
# ---------------------------------------------------------------------------

def _map_name(map_id: str) -> str:
    if not map_id:
        return "?"
    m = re.search(r"/([A-Za-z0-9_]+)$", map_id)
    return m.group(1) if m else map_id.split("/")[-1]


def build_pregame_context(api: ValoAPI, player_data: dict) -> Optional[dict]:
    """pregame 検出時のサイド判定。Blue=Defender / Red=Attacker (確定マッピング)。"""
    match_id = player_data.get("MatchID") or player_data.get("MatchId")
    if not match_id:
        return None
    match_data = api.get_pregame_match(match_id)
    if not match_data:
        return None
    ally = match_data.get("AllyTeam") or {}
    tid = ally.get("TeamID") or ally.get("TeamId")
    if not tid:
        for team in match_data.get("Teams", []):
            for p in team.get("Players", []):
                if p.get("Subject") == api.puuid:
                    tid = team.get("TeamID") or team.get("TeamId")
                    break
            if tid:
                break
    side = None
    if tid == "Blue":
        side = "Defense"
    elif tid == "Red":
        side = "Attack"
    if side:
        LOG.info("side: %s → %s", tid, side)
    return {
        "match_id": match_id,
        "side": side,
        "is_ranked": bool(match_data.get("IsRanked")),
        "queue_id": match_data.get("QueueID") or "",
        "map": _map_name(match_data.get("MapID") or ""),
        "phase_remaining_ns": match_data.get("PhaseTimeRemainingNS") or 0,
    }


# ===================================================================
# Saboteur v6 — 味方にドッジさせる (自分はペナルティを受けない)
# ===================================================================

# エージェント切替用UUID (valorant-api.com から取得した正しい一覧)
_AGENT_IDS = [
    "add6443a-41bd-e414-f6ad-e58d267f4e95",  # Jett
    "a3bfb853-43b2-7238-a4f1-ad90e9e46bcc",  # Reyna
    "569fdd95-4d10-43ab-ca70-79becc718b46",  # Sage
    "eb93336a-449b-9c1b-0a54-a891f7921d69",  # Phoenix
    "f94c3b30-42be-e959-889c-5aa313dba261",  # Raze
    "9f0d8ba9-4140-b941-57d3-a7ad57c6b417",  # Brimstone
    "707eab51-4836-f488-046a-cda6bf494859",  # Viper
    "117ed9e3-49f3-6512-3ccf-0cada7e3823b",  # Cypher
    "320b2a48-4d9b-a075-30f1-1f93a9b638fa",  # Sova
    "1e58de9c-4950-5125-93e9-a0aee9f98746",  # Killjoy
    "6f2a04ca-43e0-be17-7f36-b3908627744d",  # Skye
    "95b78ed7-4637-86d9-7e41-71ba8c293152",  # Harbor
    "601dbbe7-43ce-be57-2a40-4abd24953621",  # KAY/O
    "7f94d92c-4234-0a36-9646-3a87eb8b5c89",  # Yoru
    "8e253930-4c05-31dd-1b6c-968525494517",  # Omen
]

# チャット送信用メッセージ (パターン回避のバリエーション)
_SPAM_POOL = [
    lambda: "\u2800" * random.randint(100, 250),
    lambda: "\u3164" * random.randint(100, 250),
    lambda: "\u2588" * random.randint(80, 200),
    lambda: "".join(random.choice("\U0001f4a9\U0001f525\U0001f480\U0001f389\U0001f3af\U0001f4a3\U0001f60e\U0001f921") for _ in range(random.randint(30, 60))),
    lambda: "\n" * random.randint(5, 15) + "." + "\n" * random.randint(5, 15),
    lambda: random.choice(["glhf", "nt", "ns", "lol", "mb", "srry"]),
]


class Saboteur:
    """ロビーを妨害して味方にドッジさせる（ドッジした側にペナルティが付く）。

    - エージェント切替: 300〜900ms のランダム間隔 (人間の操作速度に近い)
    - チャット送信: 内容・頻度に揺らぎ (bot判定パターン回避)
    - 時々数秒の沈黙 (人間らしさの演出)
    """

    def __init__(self, api: ValoAPI, match_id: str, duration: float = 30.0):
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

        LOG.info("sabotage v6 (humanized, %.1fs)", self._duration)

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
        idx = 0
        while not self._stop_event.is_set():
            agent = _AGENT_IDS[idx % len(_AGENT_IDS)]
            idx += 1
            try:
                self._api.select_agent(self._match_id, agent)
            except Exception:
                pass
            delay = random.uniform(0.3, 0.9)
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
            msg = random.choice(_SPAM_POOL)()
            try:
                ok = self._api.send_chat(cid, msg)
                if ok:
                    count += 1
            except Exception:
                pass
            delay = random.uniform(0.05, 0.2)
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
        "sabotage": "妨害のみ（味方にドッジさせる。自分にペナルティなし）",
        "combo": "妨害 → 最終ドッジ（ペナルティ警告あり）",
        "dodge": "即ドッジ（⚠ キュー制限 + コンペでは隠しRR減）",
    }

    def __init__(self, api: ValoAPI, dodge_side: str = "Attack",
                 interval: float = 2.0, dry_run: bool = False,
                 mode: str = "sabotage", sabotage_duration: float = 30.0,
                 max_dodges_per_day: int = 2, once: bool = False):
        self.api = api
        self.dodge_side = dodge_side.capitalize()
        self.interval = interval
        self.dry_run = dry_run
        self.mode = mode
        self.sabotage_duration = sabotage_duration
        self.max_dodges_per_day = max_dodges_per_day
        self.once = once
        self.state = load_state()
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
        recent = dodges_in_window(self.state, DODGE_WINDOW_H * 3600)
        print("=" * 58)
        print("  Valo Dodger v6 (ペナルティ対応)")
        print(f"  GLZ:  {self.api.glz_base}")
        print(f"  ドッジ対象: {self.dodge_side} スタート")
        print(f"  モード: {self.MODES.get(self.mode, self.mode)}")
        print(f"  24h内ドッジ: {len(recent)} / {self.max_dodges_per_day}")
        if self.dry_run:
            print("  [DRY RUN]")
        print(f"  間隔: {self.interval}s (ジッターあり)")
        print("=" * 58)

        while self._running:
            try:
                self._tick()
            except ConnectionError:
                self._tick_count += 1
                if self._tick_count % 15 == 1:
                    LOG.debug("待機中 (tick=%d)", self._tick_count)
            except Exception:
                LOG.exception("tick error")
            jitter = random.uniform(-0.3, 0.3)
            self._sleep(max(1.0, self.interval + jitter))

        self._cleanup()
        print("\n終了。")

    def _tick(self):
        self._tick_count += 1

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

        ctx = build_pregame_context(self.api, player)
        if ctx is None:
            print("\n⚠ サイド判定不可 (--verbose で確認)")
            return
        side = ctx["side"]
        if side is None:
            print("\n⚠ サイド判定不可 (TeamID 不明)")
            return

        print(f"\n🎯 マッチ検出 | {match_id[:12]}... | {ctx['map']} | "
              f"サイド: **{side}** | {'ランク' if ctx['is_ranked'] else 'カジュアル'}")

        if side != self.dodge_side:
            print(f"   ✅ {side} スタート → プレイ")
            return

        handled = self._handle(ctx, side)
        if self.once and handled:
            self._running = False

    def _handle(self, ctx, side) -> bool:
        if self.mode == "dodge":
            return self._do_dodge(ctx, final=False)
        if self.mode == "sabotage":
            return self._do_sabotage(ctx)
        return self._do_combo(ctx)

    # -- ドッジ (自分で抜ける) --

    def _do_dodge(self, ctx, final: bool = False) -> bool:
        side = ctx["side"]
        label = "最終ドッジ" if final else "即ドッジ"
        print(f"   ⚠⚠⚠ {side} → {label}")

        recent = dodges_in_window(self.state, DODGE_WINDOW_H * 3600)
        n = len(recent)
        if n >= self.max_dodges_per_day:
            print(f"   🛑 24h以内の自ドッジ {n} 回 (上限 {self.max_dodges_per_day}) のため見送り")
            print("      ペナルティ急増中のため、妨害モードで味方に流すか待機を推奨。")
            return False

        # ペナルティ警告 (調査結果に基づく)
        if ctx["is_ranked"]:
            print("   ⚠ コンペティティブ: 隠しRR減の可能性 (標準ドッジで約6、反復で増加 4〜12)")
        if n == 0:
            print("   ⚠ 初回ドッジ: キュー制限 約数分")
        else:
            print(f"   ⚠ 24h以内 {n + 1} 回目のドッジ: キュー制限・RR減が急増します")
        print("   ⚠ 過度な連続ドッジは 1日ランク制限・ゲーム禁止に発展する可能性 (自己責任)")

        if self.dry_run:
            print("   [DRY RUN] skip")
            return False

        ok = self.api.quit_pregame(ctx["match_id"])
        if ok:
            self.state["dodges"].append(time.time())
            save_state(self.state)
            print("   ✅ ドッジ成功。")
            self._report_restriction()
            return True
        print("   ❌ ドッジ失敗")
        return False

    def _report_restriction(self):
        try:
            info = self.api.get_party_restriction()
        except Exception:
            info = None
        if not info:
            print("   (キュー制限の残り時間を取得できませんでした)")
            return
        sec = int(info.get("restricted_seconds") or 0)
        if sec > 0:
            m, s = divmod(sec, 60)
            print(f"   ⏳ キュー制限: 残り {m}分{s}秒")
        else:
            print("   ✅ キュー制限なし")
        inelig = info.get("queue_ineligibilities") or []
        if inelig:
            print(f"   ℹ キュー不可理由: {', '.join(map(str, inelig))}")

    # -- 妨害 (味方に流させる) --

    def _do_sabotage(self, ctx) -> bool:
        if self.dry_run:
            print(f"   [DRY RUN] {ctx['side']} → 妨害 skip")
            return False
        print(f"   💀 {ctx['side']} → 妨害開始 (チャット+エージェント切替 {self.sabotage_duration}s)")
        print("      目的: 味方にドッジさせる → ドッジした側にペナルティが付き自分は無傷")

        self._saboteur = Saboteur(self.api, ctx["match_id"], self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        while self._running and time.monotonic() < deadline:
            time.sleep(1.5)
            if self.api.session_state() != "pregame":
                break

        self._cleanup()
        state = self.api.session_state()
        if state == "menus":
            print("   🎉 誰かが抜けた！ マッチキャンセル。自分にペナルティなし ✅")
            return True
        if state == "ingame":
            print("   ⚠ 試合が始まりました（ドッジ失敗・攻めスタートで進行中）")
            print("      未ロックのまま時間切れにすると『ドッジ』扱いで自分にペナルティが来ます。")
            print("      プレイするか手動で離脱してください。")
            return False
        phase_s = ctx["phase_remaining_ns"] / 1_000_000_000 if ctx.get("phase_remaining_ns") else 0
        print("   😐 誰も抜けませんでした。")
        print(f"      フェーズ残り約 {max(0, int(phase_s))} 秒。放置すると時間切れ=ドッジ扱いになります。")
        print("      自分でロックしてプレイするか、Ctrl+C で停止して combo モードを検討してください。")
        return False

    # -- コンボ: 妨害 → 最終ドッジ --

    def _do_combo(self, ctx) -> bool:
        if self.dry_run:
            print(f"   [DRY RUN] {ctx['side']} → combo skip")
            return False
        print(f"   💀 {ctx['side']} → コンボ (妨害 {self.sabotage_duration}s → 最終ドッジ)")

        self._saboteur = Saboteur(self.api, ctx["match_id"], self.sabotage_duration)
        self._saboteur.start()

        deadline = time.monotonic() + self.sabotage_duration
        while self._running and time.monotonic() < deadline:
            time.sleep(1.5)
            if self.api.session_state() != "pregame":
                break

        self._cleanup()
        state = self.api.session_state()
        if state == "menus":
            print("   🎉 誰かが抜けた！ マッチキャンセル。自分にペナルティなし ✅")
            return True
        if state == "ingame":
            print("   ⚠ 試合が始まりました（ドッジ失敗・攻めスタートで進行中）")
            return False

        print("   ⚠ 誰も抜けなかったため最終手段: 自ドッジ...")
        return self._do_dodge(ctx, final=True)

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Valorant Auto-Dodger v6")
    parser.add_argument("--mode", choices=["sabotage", "combo", "dodge"],
                        default="sabotage",
                        help="sabotage=妨害のみ(自分にペナなし/デフォルト), combo=妨害→最終ドッジ, dodge=即ドッジ(ペナルティ注意)")
    parser.add_argument("--dodge", choices=["attack", "defense"], default="attack",
                        help="このサイドでスタートしたらドッジ対象にする (default: attack = 守り以外を流す)")
    parser.add_argument("--sabotage-duration", type=float, default=30.0,
                        help="妨害時間 (秒)。エージェント選択フェーズより短く (default: 30)")
    parser.add_argument("--max-dodges-per-day", type=int, default=2,
                        help="24時間以内に自分でドッジする上限 (default: 2)。超過時は妨害のみ")
    parser.add_argument("--once", action="store_true",
                        help="1回処理したら終了")
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
        max_dodges_per_day=args.max_dodges_per_day,
        once=args.once,
    )
    monitor.run()


if __name__ == "__main__":
    main()
