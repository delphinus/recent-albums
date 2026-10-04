"""最近追加したアルバムを、追加日・再生回数付きで iPhone に見せる Web アプリ。

    python3 server.py [--port 8765] [--days 365] [--interval 3600]

一覧のアルバムをタップすると shortcuts:// で iPhone のショートカット「アルバムを再生」を
呼び、iPhone の Music アプリで再生させる。Web ページから Music アプリを直接操作する手段は
無いため、この形にしている。

アルバムは album 名だけでまとめる。1 枚の中で albumArtist が曲ごとに違うアルバムもあり、
albumArtist を鍵に含めるとそれが複数枚に割れる。

DJ mix かどうかは、プレイリスト「DJ Mix」に曲が入っているか、アルバム名で判定する。
ライブラリには DJ mix を示す情報が無く、グループやコメントの欄は既に別の用途で使われているため。
プレイリストとは曲の persistentID で照らし合わせ、一部の曲だけが入っていれば「一部が mix」とする。
その mix の部分だけを入れたプレイリスト「<アルバム名> Mix」があれば、それを流す手段も出す。

一覧は --interval ごとに裏で取り直してファイルに置き、要求にはそれをすぐ返す。Music.app への
問い合わせは 3 秒ほど掛かり、たまにしか開かない使い方では要求時に取るとほぼ毎回待たされる。

検索とプレイリストの一覧のために、ライブラリ全体のアルバムとプレイリストも同じ間隔で取り直しておく。

シリーズは、アルバム名の番号より前の部分が同じアルバムが SERIES_MIN 枚以上あるものとみなす。
ライブラリにはシリーズを示す情報が無く、"Otographic Arts 029" のように名前に番号を振る形がほとんどのため。
"""

import argparse
import json
import re
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path.home() / "Library/Caches/recent-albums"
ART_SIZE = 320  # px。一覧の 96pt x 3 倍 = 288px を少し上回る大きさ
PID_RE = re.compile(r"[0-9A-F]{16}")
# アルバム名に mix が単語として入っていれば DJ mix とみなす。ただしシングル・EP と、
# "(Extended Mix)" や "(Bonus Mix Edition)" のような曲や版の名前に入っているものは除く
DJ_MIX_RE = re.compile(r"(?i)\b(mega)?mix\b|\bmixed by\b")
NOT_DJ_MIX_RE = re.compile(
    r"(?i) - (single|ep)$|\((extended|original|radio|club|rave|[^)]*remix|[^)]*edition)[^)]*\)"
)

# シリーズの番号。"#45" "5th" "Vol. 3" の 3 などを拾い、"R4" や "Trance4nations" の 4 は拾わない
SERIES_NUM_RE = re.compile(r"(?<![\w.])#?(\d+)(?:st|nd|rd|th)?\b")
# 番号の前に付く "Volume" "Episode" や区切りの記号は、シリーズ名に含めない
SERIES_TAIL_RE = re.compile(
    r"(?i)[\s,:\-–#(\[]*\b(?:vol(?:ume)?|episode|ep|pt|part|chapter|no|disc|cd)\.?[\s,:\-–#(\[]*$|[\s,:\-–#(\[]+$"
)
SERIES_MIN = 3

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

_snapshots: dict[int, dict] = {}
_lock = threading.Lock()
_library: dict | None = None
_library_lock = threading.Lock()
SEARCH_LIMIT = 100
# 画面に入った画像がまとめて要求されるので、osascript が同時に何本も走らないよう絞る
_art_sem = threading.Semaphore(2)


def fetch_tracks(days: int) -> dict:
    """{"tracks": [...], "djMixIds": [...], "playlists": [...]} を返す。"""
    out = subprocess.run(
        ["osascript", "-l", "JavaScript", str(HERE / "recent.js"), str(days)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


def fetch_playlists() -> list[dict]:
    # 初回は swift のコンパイルが入るので長めに待つ
    out = subprocess.run(
        ["swift", str(HERE / "playlists.swift")],
        capture_output=True, text=True, timeout=180, check=True,
    )
    playlists = json.loads(out.stdout)
    playlists.sort(key=lambda p: (natural_key(p["folder"]), natural_key(p["name"])))
    return playlists


def natural_key(s: str) -> list:
    """数字を数として比べる並べ替えの鍵。"#11" "#16" "#112" の順になる。"""
    return [(0, int(x), "") if x.isdigit() else (1, 0, normalize(x)) for x in re.split(r"(\d+)", s) if x]


def dj_mix_kind(album: str, ts: list[dict], dj_mix_ids: set[str]) -> str | None:
    """"full" (DJ mix)、"part" (一部が mix)、None (DJ mix ではない) のどれかを返す。"""
    n = sum(t["persistentID"] in dj_mix_ids for t in ts)
    if n:
        return "full" if n == len(ts) else "part"
    return "full" if DJ_MIX_RE.search(album) and not NOT_DJ_MIX_RE.search(album) else None


def group_albums(tracks: list[dict], dj_mix_ids: set[str], playlists: set[str]) -> list[dict]:
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
        kind = dj_mix_kind(name, ts, dj_mix_ids)
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
            "djMix": kind,
            "mixPlaylist": f"{name} Mix" if kind == "part" and f"{name} Mix" in playlists else None,
        })
    albums.sort(key=lambda a: a["dateAdded"], reverse=True)
    return albums


def series_key(album: str) -> tuple[str, int] | None:
    """アルバム名からシリーズ名と番号を取り出す。番号が無ければ None。"""
    name = re.sub(r"(?i) - (single|ep)$", "", album)
    m = SERIES_NUM_RE.search(name)
    if not m or m.start() == 0:
        return None
    # "Asot 893 - A State of Trance Episode 893" のように略称の後ろに正式な名前が続くものは、後ろで数える
    rest = name[m.end():]
    if rest.startswith(" - ") and m.group(1) in rest and (k := series_key(rest[3:])):
        return k
    prefix = SERIES_TAIL_RE.sub("", name[:m.start()]).strip()
    return (prefix, int(m.group(1))) if len(prefix) >= 3 else None


def list_series() -> list[dict]:
    """ライブラリ全体のアルバムをシリーズにまとめる。シリーズは新しく追加したもの順、中は番号の昇順。"""
    groups: dict[str, list[tuple[int, dict]]] = {}
    names: dict[str, str] = {}
    # 新しいものから見るので、シリーズ名の表記は最も新しいアルバムのものになる
    for a in sorted(get_library()["albums"], key=lambda a: a["dateAdded"], reverse=True):
        if k := series_key(a["album"]):
            key = normalize(k[0])
            groups.setdefault(key, []).append((k[1], a))
            names.setdefault(key, k[0])
    series = []
    for key, items in groups.items():
        if len(items) < SERIES_MIN:
            continue
        series.append({
            "name": names[key],
            "dateAdded": items[0][1]["dateAdded"],
            # アイコンには新しい 4 枚を並べる
            "artPids": [a["artPid"] for _, a in items[:4]],
            "albums": [a for _, a in sorted(items, key=lambda x: (x[0], natural_key(x[1]["album"])))],
        })
    series.sort(key=lambda s: s["dateAdded"], reverse=True)
    return series


def snapshot_path(days: int) -> Path:
    return CACHE_DIR / f"albums-{days}.json"


def refresh_snapshot(days: int) -> dict:
    """Music.app から取り直し、メモリとファイルの両方に置く。"""
    with _lock:
        fetched = fetch_tracks(days)
        snap = {
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "albums": group_albums(fetched["tracks"], set(fetched["djMixIds"]), set(fetched["playlists"])),
        }
        _snapshots[days] = snap
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path(days).with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, ensure_ascii=False))
        tmp.replace(snapshot_path(days))
        return snap


def get_snapshot(days: int, force: bool) -> dict:
    """手元にある一覧を返す。メモリ → ファイルの順に探し、どちらにも無いときだけ取りに行く。"""
    if not force:
        if days in _snapshots:
            return _snapshots[days]
        try:
            _snapshots[days] = json.loads(snapshot_path(days).read_text())
            return _snapshots[days]
        except (OSError, ValueError):
            pass
    return refresh_snapshot(days)


def library_path() -> Path:
    return CACHE_DIR / "library.json"


def refresh_library() -> dict:
    """ライブラリ全体のアルバムとプレイリストを取り直し、メモリとファイルの両方に置く。"""
    global _library
    with _library_lock:
        fetched = fetch_tracks(0)
        lib = {
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "albums": group_albums(fetched["tracks"], set(fetched["djMixIds"]), set(fetched["playlists"])),
            "playlists": fetch_playlists(),
        }
        _library = lib
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = library_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(lib, ensure_ascii=False))
        tmp.replace(library_path())
        return lib


def get_library() -> dict:
    """手元にあるライブラリ全体の一覧を返す。get_snapshot と同じくメモリ → ファイル → 取得の順。"""
    global _library
    if _library is None:
        try:
            _library = json.loads(library_path().read_text())
        except (OSError, ValueError):
            return refresh_library()
    return _library


def normalize(s: str) -> str:
    # 全角・半角や大文字・小文字の違いを無視して比べる
    return unicodedata.normalize("NFKC", s).casefold()


def search(q: str) -> dict:
    """空白で区切った語をすべて含むアルバム (名前・アーティスト)、シリーズ (名前)、プレイリスト (名前・フォルダ) を返す。"""
    terms = normalize(q).split()
    lib = get_library()

    def hit(*fields: str) -> bool:
        s = normalize(" ".join(fields))
        return all(t in s for t in terms)

    return {
        "albums": [a for a in lib["albums"] if hit(a["album"], a["artist"])][:SEARCH_LIMIT],
        "series": [{k: v for k, v in s.items() if k != "albums"} | {"count": len(s["albums"])}
                   for s in list_series() if hit(s["name"])],
        "playlists": [p for p in lib["playlists"] if hit(p["name"], p["folder"])][:SEARCH_LIMIT],
    }


def music_running() -> bool:
    return subprocess.run(["pgrep", "-xq", "Music"]).returncode == 0


def refresher(days: int, interval: int):
    """一覧とアートワークを定期的に取り直す。

    Music.app が起動していない回は見送る。osascript は Music.app を起動してしまうので、
    ライブラリのバックアップのように Music.app を止めて行う作業とぶつかりうる。
    """
    while True:
        try:
            if music_running():
                snap = refresh_snapshot(days)
                for a in snap["albums"]:
                    get_artwork(a["artPid"])
                print(f"refreshed: {len(snap['albums'])} albums", flush=True)
                lib = refresh_library()
                print(f"library refreshed: {len(lib['albums'])} albums, {len(lib['playlists'])} playlists",
                      flush=True)
                for s in list_series():
                    for pid in s["artPids"]:
                        get_artwork(pid)
            else:
                print("refresh skipped: Music is not running", flush=True)
        except Exception as e:  # noqa: BLE001 - 1 回の失敗で定期取得を止めない
            print(f"refresh failed: {e}", flush=True)
        time.sleep(interval)


def get_artwork(pid: str) -> bytes | None:
    """縮小したアートワークを返す。無ければ None。結果はどちらもディスクに残す。"""
    # サイズをファイル名に含め、ART_SIZE を変えたら取り直す
    jpg, none = CACHE_DIR / f"{pid}-{ART_SIZE}.jpg", CACHE_DIR / f"{pid}.none"
    if jpg.exists():
        return jpg.read_bytes()
    if none.exists():
        return None
    with _art_sem:
        if jpg.exists():
            return jpg.read_bytes()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw = CACHE_DIR / f"{pid}.raw"
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
                body = json.dumps(get_snapshot(days, "refresh" in q), ensure_ascii=False)
                self._send(200, "application/json; charset=utf-8", body.encode())
            except subprocess.CalledProcessError as e:
                self._send(500, "text/plain; charset=utf-8", e.stderr.encode())
                return
            if "refresh" in q:
                # 検索用の一覧も取り直す。十数秒掛かるので、応答は待たせない
                threading.Thread(target=refresh_library, daemon=True).start()
        elif u.path in ("/api/search", "/api/playlists", "/api/series"):
            try:
                if u.path == "/api/search":
                    body = search(parse_qs(u.query).get("q", [""])[0])
                elif u.path == "/api/series":
                    body = {"fetchedAt": get_library()["fetchedAt"], "series": list_series()}
                else:
                    lib = get_library()
                    body = {"fetchedAt": lib["fetchedAt"], "playlists": lib["playlists"]}
                self._send(200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode())
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
    p.add_argument("--interval", type=int, default=3600, help="一覧を取り直す間隔 (秒)")
    a = p.parse_args()
    Handler.default_days = a.days
    threading.Thread(target=refresher, args=(a.days, a.interval), daemon=True).start()
    print(f"listening on :{a.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
