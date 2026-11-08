"""月ごとによく聴いた曲のプレイリストを作る。Apple Music の月間ランキングを、自分のライブラリ全体で作る。

    python3 monthly_top.py monthly                      # 月初に LaunchAgent から呼ぶ (控え → 前月分を作る → Slack)
    python3 monthly_top.py snapshot [--date YYYY-MM-DD]  # 再生回数の控えだけ取る
    python3 monthly_top.py build YYYY-MM [--notify] [--dry-run]
    python3 monthly_top.py import-tsv <tracks.tsv> --date YYYY-MM-DD

Music.app は曲ごとの累計の再生回数しか持たず、いつ聴いたかの履歴は持たない。そこで月初に累計の
控え (persistentID → 回数) を取っておき、前月初めの控えとの差をその月の再生回数とみなす。

Apple の月間ランキングは Apple Music のカタログの曲しか数えないが、こちらはリッピングした曲や
購入した曲も同じように数える。逆に、ライブラリに入れていないカタログの曲は数えられない。

DJ mix の曲は入れない。通しで聴くもので、プレイリストに混ぜて流すものではないため。判定は
server.py と同じく、プレイリスト「DJ Mix」に入っているか、アルバム名に Mix が単語として入っているか。

控えは月の 1 日ちょうどに取れるとは限らない (Mac が寝ていれば起きたときに回る)。期間の両端には
1 日に最も近い控えを使い、どちらかが EDGE_DAYS 日より離れていれば、プレイリスト名に実際の期間を添える。
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from server import DJ_MIX_RE, NOT_DJ_MIX_RE

HERE = Path(__file__).resolve().parent
STATE_DIR = Path.home() / "Library/Application Support/recent-albums"
SNAP_DIR = STATE_DIR / "playcounts"
TOP_DIR = STATE_DIR / "top"
CONF_DIR = Path.home() / ".config/recent-albums"

FOLDER = "[! Monthly Top]"
LIMIT = 50  # 1 回だけ聴いた曲が 7 割を占めるので、100 曲にすると半分以上がそれで埋まる
MIN_PLAYS = 2
EDGE_DAYS = 3
SLACK_LINES = 10


def playlist_name(month: str, start: date, end: date) -> str:
    """"2026-10 Monthly Top"。期間が月とずれていれば "2026-08 Monthly Top (8/24〜)" のように添える。"""
    first = date.fromisoformat(month + "-01")
    nxt = next_month(first)
    note = ""
    if abs((start - first).days) > EDGE_DAYS or abs((end - nxt).days) > EDGE_DAYS:
        s = f"{start.month}/{start.day}" if abs((start - first).days) > EDGE_DAYS else ""
        # 控えは end の日の朝に取ったものなので、聴いた期間はその前日まで
        last = end - timedelta(days=1)
        e = f"{last.month}/{last.day}" if abs((end - nxt).days) > EDGE_DAYS else ""
        note = f" ({s}〜{e})"
    return f"{month} Monthly Top{note}"


def next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def ensure_music():
    """Music.app が起動していなければ起動し、Apple Event に答えるまで待つ。起動直後は答えない。"""
    if subprocess.run(["pgrep", "-xq", "Music"]).returncode != 0:
        subprocess.run(["open", "-g", "-a", "Music"], check=True)
    deadline = time.time() + 180
    while time.time() < deadline:
        r = subprocess.run(["osascript", "-e", 'tell application "Music" to get name of library playlist 1'],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            return
        time.sleep(5)
    sys.exit("Music.app が応答しない")


def fetch_library() -> dict:
    """recent.js でライブラリ全体の曲と「DJ Mix」の persistentID を取る。"""
    ensure_music()
    out = subprocess.run(["osascript", "-l", "JavaScript", str(HERE / "recent.js"), "0"],
                         capture_output=True, text=True, timeout=180, check=True)
    return json.loads(out.stdout)


def save_snapshot(d: date, counts: dict[str, int], source: str):
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"{d.isoformat()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"takenAt": datetime.now().isoformat(timespec="seconds"), "source": source,
                               "counts": counts}))
    tmp.replace(path)
    print(f"控え: {path.name} ({len(counts)} 曲, {source})", file=sys.stderr)


def take_snapshot(d: date, lib: dict | None = None) -> dict:
    lib = lib or fetch_library()
    save_snapshot(d, {t["persistentID"]: t.get("playedCount") or 0 for t in lib["tracks"]}, "Music.app")
    return lib


def import_tsv(path: Path, d: date):
    """music-relink の週次ダンプ (tracks-<日付>.tsv) から控えを作る。過去の月を作るときに使う。"""
    import csv
    counts = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            try:
                counts[r["persistentID"]] = int(r["playedCount"] or 0)
            except (KeyError, ValueError):
                pass
    save_snapshot(d, counts, path.name)


def snapshots() -> list[date]:
    return sorted(date.fromisoformat(p.stem) for p in SNAP_DIR.glob("*.json"))


def nearest(target: date, cands: list[date]) -> date | None:
    return min(cands, key=lambda d: (abs((d - target).days), d)) if cands else None


def load_counts(d: date) -> dict[str, int]:
    return json.loads((SNAP_DIR / f"{d.isoformat()}.json").read_text())["counts"]


def is_dj_mix(t: dict, dj_ids: set[str]) -> bool:
    album = t.get("album") or ""
    return t["persistentID"] in dj_ids or bool(DJ_MIX_RE.search(album) and not NOT_DJ_MIX_RE.search(album))


def rank(start: date, end: date, lib: dict) -> list[dict]:
    before, after = load_counts(start), load_counts(end)
    dj_ids = set(lib["djMixIds"])
    meta = {t["persistentID"]: t for t in lib["tracks"]}
    rows = []
    for pid, n in after.items():
        plays = n - before.get(pid, 0)
        t = meta.get(pid)
        # 期間の後に消した曲はプレイリストに入れられないので数えない
        if plays < MIN_PLAYS or not t or is_dj_mix(t, dj_ids):
            continue
        # 最後に再生した日が期間より前なら、期間中には聴いていない。それでも回数が増えて見えるのは、
        # 始まりの控えに居なかった曲が、昔の再生回数を持ったままライブラリに戻ってきた場合。
        # 控えから取り込んだ曲が iCloud 上の昔のエントリと結び付くと、2012 年の回数を引き継いで
        # 現れる (2026-08-25 に 20 曲以上)。追加日は取り込んだ日になるので、追加日では見分けられない
        if not t.get("playedDate") or datetime.fromisoformat(t["playedDate"]).astimezone().date() < start:
            continue
        rows.append({"persistentID": pid, "plays": plays, "name": t.get("name") or "",
                     "artist": t.get("artist") or "", "album": t.get("album") or "",
                     "playedDate": t.get("playedDate") or ""})
    # 同じ回数なら最近聴いたほうを上にする
    rows.sort(key=lambda r: r["playedDate"], reverse=True)
    rows.sort(key=lambda r: r["plays"], reverse=True)
    return rows[:LIMIT]


def make_playlist(name: str, pids: list[str]) -> dict:
    tmp = STATE_DIR / "pids.tmp.json"
    tmp.write_text(json.dumps(pids))
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", str(HERE / "make_playlist.js"), FOLDER, name, str(tmp)],
                             capture_output=True, text=True, timeout=600, check=True)
    finally:
        tmp.unlink(missing_ok=True)
    res = json.loads(out.stdout)
    if res["tracks"] != pids:
        sys.exit(f"プレイリストの中身が意図と違う: {len(res['tracks'])} 曲 / 期待 {len(pids)} 曲, "
                 f"見つからない曲 {res['missing']}")
    return res


def build(month: str, notify: bool, dry_run: bool, lib: dict | None = None) -> dict:
    first = date.fromisoformat(month + "-01")
    cands = snapshots()
    start, end = nearest(first, cands), nearest(next_month(first), cands)
    if not start or not end or start >= end:
        sys.exit(f"{month} の期間を挟む控えが無い (控え: {[d.isoformat() for d in cands]})")
    name = playlist_name(month, start, end)
    lib = lib or fetch_library()
    rows = rank(start, end, lib)
    print(f"{name}: {start} → {end}, {len(rows)} 曲", file=sys.stderr)
    for i, r in enumerate(rows, 1):
        print(f"  {i:2}. {r['plays']:3} 回  {r['name']} / {r['artist']}", file=sys.stderr)
    top = {"month": month, "name": name, "folder": FOLDER, "from": start.isoformat(), "to": end.isoformat(),
           "createdAt": datetime.now().isoformat(timespec="seconds"),
           "tracks": [{k: v for k, v in r.items() if k != "playedDate"} for r in rows]}
    if dry_run or not rows:
        return top
    make_playlist(name, [r["persistentID"] for r in rows])
    TOP_DIR.mkdir(parents=True, exist_ok=True)
    (TOP_DIR / f"{month}.json").write_text(json.dumps(top, ensure_ascii=False, indent=1))
    if notify:
        send_slack(top)
    return top


def read_conf(name: str) -> str:
    try:
        return (CONF_DIR / name).read_text().strip()
    except OSError:
        return ""


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_slack(top: dict):
    """Webhook (~/.config/recent-albums/slack-webhook) が無ければ何もしない。

    リンク先は ~/.config/recent-albums/public-url (例: https://music.example.com)。
    Slack は shortcuts:// をリンクにしないので、Web アプリの画面を経由して再生する。
    """
    hook = read_conf("slack-webhook")
    if not hook:
        print("Slack: webhook が無いので送らない", file=sys.stderr)
        return
    m = int(top["month"][5:])
    lines = [f":headphones: {m} 月によく聴いた曲のプレイリストを作りました: *{slack_escape(top['name'])}* "
             f"({len(top['tracks'])} 曲)"]
    if url := read_conf("public-url"):
        lines.append(f"<{url.rstrip('/')}/?top={top['month']}|一覧を開いて再生する>")
    for i, t in enumerate(top["tracks"][:SLACK_LINES], 1):
        lines.append(f"{i}. {slack_escape(t['name'])} — {slack_escape(t['artist'])} ({t['plays']} 回)")
    if len(top["tracks"]) > SLACK_LINES:
        lines.append(f"…ほか {len(top['tracks']) - SLACK_LINES} 曲")
    req = urllib.request.Request(hook, data=json.dumps({"text": "\n".join(lines)}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
        print(f"Slack: {body}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - 知らせられなくてもプレイリストは作れている
        print(f"Slack: 送れなかった: {e}", file=sys.stderr)


def monthly():
    """今日の控えを取り、前月分を作って知らせる。"""
    today = date.today()
    lib = take_snapshot(today)
    prev = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    build(prev, notify=True, dry_run=False, lib=lib)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("monthly")
    s = sub.add_parser("snapshot")
    s.add_argument("--date", type=date.fromisoformat, default=date.today())
    b = sub.add_parser("build")
    b.add_argument("month")
    b.add_argument("--notify", action="store_true")
    b.add_argument("--dry-run", action="store_true")
    t = sub.add_parser("import-tsv")
    t.add_argument("tsv", type=Path)
    t.add_argument("--date", type=date.fromisoformat, required=True)
    a = p.parse_args()
    if a.cmd == "monthly":
        monthly()
    elif a.cmd == "snapshot":
        take_snapshot(a.date)
    elif a.cmd == "build":
        build(a.month, a.notify, a.dry_run)
    else:
        import_tsv(a.tsv, a.date)


if __name__ == "__main__":
    main()
