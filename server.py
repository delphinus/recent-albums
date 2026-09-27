"""最近追加したアルバムを、追加日・再生回数付きで iPhone に見せる Web アプリ。

    python3 server.py [--port 8765] [--days 365]

一覧のアルバムをタップすると shortcuts:// で iPhone のショートカット「アルバムを再生」を
呼び、iPhone の Music アプリで再生させる。Web ページから Music アプリを直接操作する手段は
無いため、この形にしている。

アルバムは album 名だけでまとめる。1 枚の中で albumArtist が曲ごとに違うアルバムもあり、
albumArtist を鍵に含めるとそれが複数枚に割れる。
"""

import argparse
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CACHE_TTL = 120  # 秒。再生回数は iCloud 経由で遅れて届くので、これ以上短くしても意味が薄い

ART_DIR = Path.home() / "Library/Caches/recent-albums"
ART_SIZE = 320  # px。一覧の 96pt x 3 倍 = 288px を少し上回る大きさ
PID_RE = re.compile(r"[0-9A-F]{16}")

# Apple Music の日本のストアから入った曲は和名のジャンルが付く。英名に寄せて絞り込みを 1 つにする。
# ライブラリの表記はそのままで、表示だけを変える
GENRE_EN = {
    "ハードコア": "Hardcore",
    "トランス": "Trance",
    "ダンス": "Dance",
    "エレクトロニック": "Electronic",
    "テクノ": "Techno",
    "サウンドトラック": "Soundtrack",
    "アニメ": "Anime",
    "ゲーム": "Game",
    "ポップ": "Pop",
    "ロック": "Rock",
    "ハードロック": "Hard Rock",
    "オルタナティブ": "Alternative",
    "ブルース": "Blues",
    "ラップ": "Rap",
    "ヒップホップ": "Hip-Hop/Rap",
    "クラシック": "Classical",
    "イージーリスニング": "Easy Listening",
    "フュージョン": "Fusion",
    "オールディーズ": "Oldies",
    "ボーカル": "Vocal",
    "その他": "Other",
}

_cache: dict[int, tuple[float, list]] = {}
_lock = threading.Lock()
# 画面に入った画像がまとめて要求されるので、osascript が同時に何本も走らないよう絞る
_art_sem = threading.Semaphore(2)


def fetch_tracks(days: int) -> list[dict]:
    out = subprocess.run(
        ["osascript", "-l", "JavaScript", str(HERE / "recent.js"), str(days)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


def group_albums(tracks: list[dict]) -> list[dict]:
    by_album: dict[str, list[dict]] = {}
    for t in tracks:
        # 一括取得には出るが個別には引けない (-1728) 実体の無い項目がある。
        # 名前もアルバム名も数字だけで、duration が null になっている
        if t.get("album") and t.get("duration"):
            by_album.setdefault(t["album"], []).append(t)

    albums = []
    for name, ts in by_album.items():
        ts.sort(key=lambda t: (t.get("discNumber") or 0, t.get("trackNumber") or 0))
        played = [t["playedDate"] for t in ts if t.get("playedDate")]
        artists = list(dict.fromkeys(t.get("albumArtist") or t.get("artist") or "" for t in ts))
        albums.append({
            "album": name,
            "artPid": ts[0]["persistentID"],
            "artist": " / ".join(a for a in artists if a),
            "genre": GENRE_EN.get(ts[0].get("genre") or "", ts[0].get("genre") or ""),
            "tracks": len(ts),
            "duration": sum(t.get("duration") or 0 for t in ts),
            "dateAdded": max(t["dateAdded"] for t in ts),
            # 先頭トラックの回数 = 頭から聴き始めた回数、最終トラック = 最後まで聴いた回数
            "playsStarted": ts[0].get("playedCount") or 0,
            "playsFinished": ts[-1].get("playedCount") or 0,
            "lastPlayed": max(played) if played else None,
        })
    albums.sort(key=lambda a: a["dateAdded"], reverse=True)
    return albums


def get_albums(days: int, refresh: bool) -> list[dict]:
    with _lock:
        hit = _cache.get(days)
        if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        albums = group_albums(fetch_tracks(days))
        _cache[days] = (time.time(), albums)
        return albums


def get_artwork(pid: str) -> bytes | None:
    """縮小したアートワークを返す。無ければ None。結果はどちらもディスクに残す。"""
    # サイズをファイル名に含め、ART_SIZE を変えたら取り直す
    jpg, none = ART_DIR / f"{pid}-{ART_SIZE}.jpg", ART_DIR / f"{pid}.none"
    if jpg.exists():
        return jpg.read_bytes()
    if none.exists():
        return None
    with _art_sem:
        if jpg.exists():
            return jpg.read_bytes()
        ART_DIR.mkdir(parents=True, exist_ok=True)
        raw = ART_DIR / f"{pid}.raw"
        out = subprocess.run(
            ["osascript", str(HERE / "artwork.applescript"), pid, str(raw)],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0 or out.stdout.strip() == "none":
            none.touch()
            return None
        subprocess.run(
            ["sips", "-Z", str(ART_SIZE), "-s", "format", "jpeg", str(raw), "--out", str(jpg)],
            capture_output=True, timeout=30, check=True,
        )
        raw.unlink()
        return jpg.read_bytes()


class Handler(BaseHTTPRequestHandler):
    default_days = 365

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", (HERE / "index.html").read_bytes())
        elif u.path == "/api/albums":
            # keep_blank_values が無いと、値の無い ?refresh が捨てられる
            q = parse_qs(u.query, keep_blank_values=True)
            days = int(q.get("days", [self.default_days])[0])
            try:
                body = json.dumps(get_albums(days, "refresh" in q), ensure_ascii=False)
                self._send(200, "application/json; charset=utf-8", body.encode())
            except subprocess.CalledProcessError as e:
                self._send(500, "text/plain; charset=utf-8", e.stderr.encode())
        elif u.path.startswith("/art/") and PID_RE.fullmatch(u.path[5:]):
            img = get_artwork(u.path[5:])
            if img:
                self._send(200, "image/jpeg", img, cache="max-age=86400")
            else:
                self._send(404, "text/plain", b"no artwork")
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--days", type=int, default=365)
    a = p.parse_args()
    Handler.default_days = a.days
    print(f"listening on :{a.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
