#!/usr/bin/env python3
"""
Valorant Auto-Dodger v6 — ペナルティ対応版
==========================================

v6.2 からの変更:
- 新モード clip (デフォルト): 攻めスタート時に、クリップボードの文章を
  エージェントピック画面のチャットへ数秒おきに送り続ける（エージェント切替なし）
- 起動したら常駐し、攻めスタートのマッチを検出するたびに自動送信

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
  python valo_dodger.py                         # クリップボード送信 (デフォルト・エージェント放置)
  python valo_dodger.py --mode sabotage         # 妨害 (チャット+エージェント切替)
  python valo_dodger.py --mode combo            # 妨害→最終ドッジ (ペナルティ警告あり)
  python valo_dodger.py --mode dodge            # 即ドッジ (ペナルティ・RR減注意)
  python valo_dodger.py --status                # アカウント状態を表示して終了
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

# コンソールの文字コードに依存せずクラッシュしないようにする (bat は chcp 65001 推奨)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass
del _stream

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

    # -- pd (アカウント情報) --

    def get_penalties(self) -> Optional[dict]:
        """アカウントのペナルティ一覧 (restrictions/v3/penalties)。
        構造は非公開のため、呼び出し側で防御的にパースする。"""
        status, data = self.pd_get("/restrictions/v3/penalties")
        if status != 200 or not isinstance(data, dict):
            LOG.warning("penalties HTTP %s", status)
            return None
        return data

    def get_mmr(self) -> Optional[dict]:
        """現在のランク/RR と直近のレーティング変動。"""
        status, data = self.pd_get(f"/mmr/v1/players/{self._puuid}")
        if status != 200 or not isinstance(data, dict):
            return None
        return data

    def get_competitive_updates(self, limit: int = 10) -> list:
        """直近の競技アップデート。AFKPenalty フィールドでペナルティを検出できる。"""
        status, data = self.pd_get(
            f"/mmr/v1/players/{self._puuid}/competitiveupdates"
            f"?startIndex=0&endIndex={limit}"
            + "&queue=competitive"
        )
        if status != 200 or not isinstance(data, dict):
            return []
        matches = data.get("Matches") or []
        return [m for m in matches if isinstance(m, dict)]

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


# ---------------------------------------------------------------------------
# クリップボード送信 (clip モード)
# ---------------------------------------------------------------------------


def read_clipboard_text() -> Optional[str]:
    """Windows クリップボードのテキストを読む (標準ライブラリのみ / ctypes)。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
    except Exception:
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            size = int(kernel32.GlobalSize(h))
            raw = ctypes.string_at(p, size)
            return raw.decode("utf-16-le", errors="replace").rstrip("\x00").strip()
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


class ClipboardSpammer:
    """エージェント選択画面のチャットにクリップボードの文章を送り続ける。
    エージェント切替はしない（放置）。pregame が終わるまで無限に送る。"""

    def __init__(self, api: ValoAPI, interval: float = 5.0):
        self._api = api
        self._interval = interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._active = False
        self._sent = 0

    def start(self):
        if self._active:
            return
        self._active = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        LOG.info("clipboard spam started (interval=%.1fs)", self._interval)

    def stop(self):
        if not self._active:
            return
        self._active = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        LOG.info("clipboard spam stopped (%d sent)", self._sent)

    def _loop(self):
        warned_cid = False
        warned_clip = False
        while not self._stop_event.is_set():
            cid = self._api.get_pregame_cid()
            if not cid:
                if not warned_cid:
                    print("   ⚠ チャットに接続できません（再試行します）")
                    warned_cid = True
                self._stop_event.wait(2.0)
                continue
            text = read_clipboard_text()
            if not text:
                if not warned_clip:
                    print("   ⚠ クリップボードにテキストがありません")
                    print("      ※ 送信したい文章をコピーしておくと、その内容を自動送信します")
                    warned_clip = True
                self._stop_event.wait(self._interval)
                continue
            try:
                ok = self._api.send_chat(cid, text[:400])
                if ok:
                    self._sent += 1
                    if self._sent <= 3 or self._sent % 10 == 0:
                        print(f"   📋 {self._sent} 回目: {text[:30]}")
            except Exception:
                pass
            self._stop_event.wait(self._interval)


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
        "clip": "クリップボードの文章をチャットに送り続ける（エージェント放置・自分にペナなし）",
        "sabotage": "妨害のみ（味方にドッジさせる。自分にペナルティなし）",
        "combo": "妨害 → 最終ドッジ（ペナルティ警告あり）",
        "dodge": "即ドッジ（⚠ キュー制限 + コンペでは隠しRR減）",
    }

    def __init__(self, api: ValoAPI, dodge_side: str = "Attack",
                 interval: float = 2.0, dry_run: bool = False,
                 mode: str = "clip", sabotage_duration: float = 30.0,
                 chat_interval: float = 5.0,
                 max_dodges_per_day: int = 2, once: bool = False):
        self.api = api
        self.dodge_side = dodge_side.capitalize()
        self.interval = interval
        self.dry_run = dry_run
        self.mode = mode
        self.sabotage_duration = sabotage_duration
        self.chat_interval = chat_interval
        self.max_dodges_per_day = max_dodges_per_day
        self.once = once
        self.state = load_state()
        self._running = True
        self._last_match_id: Optional[str] = None
        self._saboteur: Optional[Saboteur] = None
        self._spammer: Optional[ClipboardSpammer] = None
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
        if self._spammer:
            self._spammer.stop()
            self._spammer = None

    def __init__(self, api: ValoAPI, dodge_side: str = "Attack",
                 interval: float = 2.0, dry_run: bool = False,
                 mode: str = "clip", sabotage_duration: float = 30.0,
                 chat_interval: float = 5.0,
                 max_dodges_per_day: int = 2, once: bool = False):
        self.api = api
        self.dodge_side = dodge_side.capitalize()
        self.interval = interval
        self.dry_run = dry_run
        self.mode = mode
        self.sabotage_duration = sabotage_duration
        self.chat_interval = chat_interval
        self.max_dodges_per_day = max_dodges_per_day
        self.once = once
        self.state = load_state()
        self._running = True
        self._last_match_id: Optional[str] = None
        self._saboteur: Optional[Saboteur] = None
        self._spammer: Optional[ClipboardSpammer] = None
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
        if self._spammer:
            self._spammer.stop()
            self._spammer = None

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
        if self.mode == "clip":
            return self._do_clip(ctx)
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

    # -- クリップボード送信 (エージェントは放置) --

    def _do_clip(self, ctx) -> bool:
        if self.dry_run:
            print(f"   [DRY RUN] {ctx['side']} → clip skip")
            return False
        print(f"   📋 {ctx['side']} → クリップボード送信開始（{self.chat_interval}秒間隔・エージェント放置）")
        print("      味方に抜けてもらうまで送り続けます。Ctrl+C で停止。")
        self._spammer = ClipboardSpammer(self.api, self.chat_interval)
        self._spammer.start()
        try:
            while self._running:
                time.sleep(1.5)
                if self.api.session_state() != "pregame":
                    break
        finally:
            self._spammer.stop()
            self._spammer = None
        state = self.api.session_state()
        if state == "menus":
            print("   🎉 ロビーが解散しました（誰かが抜けた or キャンセル）。次のマッチを監視します")
        elif state == "ingame":
            print("   ⚠ 試合が始まりました。送信を停止します")
        return True

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
# アカウントステータス表示 (--status)
# ---------------------------------------------------------------------------

_TIER_NAMES = {
    0: "Unranked",
    3: "Iron 1", 4: "Iron 2", 5: "Iron 3",
    6: "Bronze 1", 7: "Bronze 2", 8: "Bronze 3",
    9: "Silver 1", 10: "Silver 2", 11: "Silver 3",
    12: "Gold 1", 13: "Gold 2", 14: "Gold 3",
    15: "Platinum 1", 16: "Platinum 2", 17: "Platinum 3",
    18: "Diamond 1", 19: "Diamond 2", 20: "Diamond 3",
    21: "Ascendant 1", 22: "Ascendant 2", 23: "Ascendant 3",
    24: "Immortal 1", 25: "Immortal 2", 26: "Immortal 3",
    27: "Radiant",
}


def _tier_name(tier: int) -> str:
    return _TIER_NAMES.get(tier, f"Tier {tier}")


def _fmt_dt(ts) -> str:
    """epoch ms → ローカル時刻文字列。"""
    try:
        return time.strftime("%m/%d %H:%M", time.localtime(ts / 1000))
    except Exception:
        return str(ts)


def _fmt_restriction(sec: int) -> str:
    sec = int(sec or 0)
    if sec <= 0:
        return "なし"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}時間{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def print_status(api: ValoAPI) -> None:
    state = load_state()
    print("=" * 58)
    print("  Valo Dodger v6 — アカウントステータス")
    print(f"  GLZ:  {api.glz_base}")
    print("=" * 58)

    # ---- 1. キュー可否 ----
    print("\n【キュー可否】")
    try:
        info = api.get_party_restriction()
    except Exception:
        info = None
    if not info:
        print("  - 取得できませんでした (ゲーム/クライアント起動中か確認)")
    else:
        sec = int(info.get("restricted_seconds") or 0)
        if sec > 0:
            print(f"  ❌ キュー制限中: 残り {_fmt_restriction(sec)}")
        else:
            print("  ✅ 今すぐキュー可能")
        inelig = info.get("queue_ineligibilities") or []
        if inelig:
            print(f"  - キュー不可理由: {', '.join(map(str, inelig))}")
        eligible = info.get("eligible_queues") or []
        if eligible:
            print(f"  - 利用可能キュー: {', '.join(map(str, eligible))}")

    # ---- 2. ランク / RR ----
    print("\n【ランク / RR】")
    try:
        mmr = api.get_mmr()
    except Exception:
        mmr = None
    if not mmr:
        print("  - 取得できませんでした")
    else:
        best = None
        for qid, qs in (mmr.get("QueueSkills") or {}).items():
            if qid not in ("competitive",):
                continue
            for sid, s in (qs.get("SeasonalInfoBySeasonID") or {}).items():
                if best is None or s.get("Rank") >= best.get("Rank", 0):
                    best = s
        if best:
            tier = best.get("CompetitiveTier") or 0
            rr = best.get("RankedRating") or 0
            print(f"  - 現在: {_tier_name(tier)} ({rr} RR)")
            wins = best.get("NumberOfWins") or 0
            games = best.get("NumberOfGames") or 0
            print(f"  - 今シーズン: {wins}勝 {max(0, games - wins)}敗")
        latest = mmr.get("LatestCompetitiveUpdate")
        if latest:
            earned = latest.get("RankedRatingEarned") or 0
            afk = latest.get("AFKPenalty") or 0
            if afk:
                print(f"  - 直近変動: {earned:+d} RR (AFKペナルティ {afk})")
            else:
                print(f"  - 直近変動: {earned:+d} RR")

    # ---- 3. 直近の競技アップデート ----
    print("\n【直近の競技アップデート (AFK/ペナルティ検出)】")
    try:
        updates = api.get_competitive_updates(10)
    except Exception:
        updates = []
    if not updates:
        print("  - 履歴なし")
    else:
        for m in updates[:10]:
            earned = m.get("RankedRatingEarned") or 0
            afk = m.get("AFKPenalty") or 0
            flag = "  ⚠ AFK/ペナルティ" if afk else ""
            before = _tier_name(m.get("TierBeforeUpdate") or 0)
            after = _tier_name(m.get("TierAfterUpdate") or 0)
            print(f"  {_fmt_dt(m.get('MatchStartTime') or 0)} | {before} → {after} | {earned:+d} RR{flag}")

    # ---- 4. ペナルティ一覧 ----
    print("\n【アカウントのペナルティ (restrictions/v3/penalties)】")
    try:
        pen = api.get_penalties()
    except Exception:
        pen = None
    if not pen:
        print("  - 取得できませんでした (HTTPエラーか、エンドポイントが非対応の可能性)")
    else:
        plist = pen.get("Penalties") or []
        if not plist:
            print("  - ペナルティなし")
        else:
            for p in plist:
                if not isinstance(p, dict):
                    print(f"  - {p}")
                    continue
                ptype = p.get("Type") or p.get("type") or "?"
                secs = p.get("RestrictedSeconds") or p.get("restrictedSeconds") or 0
                start = p.get("StartDate") or p.get("RenderedStartDate") or ""
                print(f"  - {ptype} | 残り {_fmt_restriction(secs)} | {start}")
                unknown = {k: v for k, v in p.items()
                           if k not in ("Type", "type", "RestrictedSeconds",
                                        "restrictedSeconds", "StartDate",
                                        "RenderedStartDate")}
                if unknown:
                    LOG.debug("penalty fields: %s", json.dumps(unknown, ensure_ascii=False))

    # ---- 5. 自ドッジ履歴 ----
    dodges = state.get("dodges") or []
    now = time.time()
    n24 = len([t for t in dodges if now - t < DODGE_WINDOW_H * 3600])
    today = time.localtime()
    today0 = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    n_today = len([t for t in dodges if t >= today0])
    print("\n【自ドッジ履歴 (valo_dodger_state.json)】")
    print(f"  - 24h以内: {n24} 回 / 上限 2 回 (--max-dodges-per-day で変更)")
    print(f"  - 今日: {n_today} 回 / 累計: {len(dodges)} 回")
    if dodges:
        last = dodges[-1]
        ago = (now - last) / 3600
        if ago < 1:
            print(f"  - 最後のドッジ: {int(ago * 60)} 分前")
        else:
            print(f"  - 最後のドッジ: {ago:.1f} 時間前")
        recent = dodges[-5:]
        stamps = ", ".join(time.strftime("%m/%d %H:%M", time.localtime(t)) for t in recent)
        print(f"  - 直近5回: {stamps}")

    # ---- 6. RR減リスクの目安 ----
    print("\n【RR減リスクの目安】")
    recent_afk = any(m.get("AFKPenalty") for m in updates)
    if recent_afk:
        print("  ⚠ 直近の競技アップデートに AFK/ペナルティが検出されています")
        print("    → 次のドッジは RR 減 (4〜12) が適用される可能性が高い")
    elif n24 == 0:
        print("  - 24h以内ドッジなし: 初回ドッジはキュー制限のみの可能性が高い (RR減リスク低)")
    elif n24 == 1:
        print("  ⚠ 24h内 1 回目: 2 回目以降のドッジから RR 減が始まる可能性が高い")
    else:
        print(f"  ⚠⚠ 24h内 {n24} 回: RR 減 (4〜12) が適用されている可能性が高い")
        print("      今日は自ドッジを避け、妨害モードで味方に流すのを推奨")
    print("  ※ Riot は正確な閾値を公開していません。上記は公式発表 + コミュニティ情報に基づく目安です")
    print()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Valorant Auto-Dodger v6")
    parser.add_argument("--mode", choices=["clip", "sabotage", "combo", "dodge"],
                        default="clip",
                        help="clip=クリップボード送信(デフォルト), sabotage=妨害, combo=妨害→最終ドッジ, dodge=即ドッジ(ペナルティ注意)")
    parser.add_argument("--dodge", choices=["attack", "defense"], default="attack",
                        help="このサイドでスタートしたらドッジ対象にする (default: attack = 守り以外を流す)")
    parser.add_argument("--sabotage-duration", type=float, default=30.0,
                        help="妨害時間 (秒)。エージェント選択フェーズより短く (default: 30)")
    parser.add_argument("--chat-interval", type=float, default=5.0,
                        help="クリップボード送信の間隔 (秒) (default: 5)")
    parser.add_argument("--max-dodges-per-day", type=int, default=2,
                        help="24時間以内に自分でドッジする上限 (default: 2)。超過時は妨害のみ")
    parser.add_argument("--once", action="store_true",
                        help="1回処理したら終了")
    parser.add_argument("--status", action="store_true",
                        help="ドッジせず、アカウントのペナルティ/キュー/ドッジ履歴を表示して終了")
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

    if args.status:
        print_status(api)
        sys.exit(0)

    monitor = DodgerMonitor(
        api=api,
        dodge_side="Attack" if args.dodge == "attack" else "Defense",
        interval=args.interval, dry_run=args.dry_run, mode=args.mode,
        sabotage_duration=args.sabotage_duration,
        chat_interval=args.chat_interval,
        max_dodges_per_day=args.max_dodges_per_day,
        once=args.once,
    )
    monitor.run()


if __name__ == "__main__":
    main()
