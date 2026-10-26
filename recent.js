// 追加日が過去 N 日以内の曲を含むアルバムの全曲、プレイリスト「DJ Mix」に入っている曲の persistentID、
// プレイリストの名前の一覧を Music.app から取り出し、JSON で標準出力に返す。
//
//   osascript -l JavaScript recent.js <days>   (days が 0 ならライブラリ全体)
//
// 期間で絞るのはアルバムを選ぶところまで。古いアルバムに 1 曲だけ足したとき、その 1 曲だけの
// アルバムとして数えないよう、選んだアルバムは期間外に追加した曲も含めて返す。
// whose で曲を絞っても期間外の曲は引けないので、ライブラリ全体を取ってから絞る (全体でも 3 秒ほど)。
//
// フィールドごとに一括取得する (1 フィールド 1 イベント)。
// 曲ごとに取るより桁違いに速い。アルバムへのまとめは server.py 側で行う。
var DJ_MIX_PLAYLIST = "DJ Mix";
var FIELDS = ["persistentID", "name", "album", "albumArtist", "artist", "genre",
              "trackNumber", "discNumber", "duration", "dateAdded", "playedCount", "playedDate"];

function run(argv) {
  var days = Number(argv[0] || 365);
  var since = new Date(Date.now() - days * 86400e3);
  var music = Application("Music");
  var q = music.libraryPlaylists[0].tracks;
  var cols = {};
  FIELDS.forEach(function (f) { cols[f] = q[f](); });
  var n = cols.persistentID.length, rows = [];
  var recent = {};
  if (days > 0) {
    for (var i = 0; i < n; i++) {
      if (cols.album[i] && cols.dateAdded[i] > since) recent[cols.album[i]] = true;
    }
  }
  for (var i = 0; i < n; i++) {
    if (days > 0 && !recent[cols.album[i]]) continue;
    var o = {};
    FIELDS.forEach(function (f) {
      var v = cols[f][i];
      if (v instanceof Date) v = isNaN(v.getTime()) ? null : v.toISOString();
      o[f] = v;
    });
    rows.push(o);
  }
  // プレイリストが無ければ空
  var mix = music.userPlaylists.whose({ name: DJ_MIX_PLAYLIST })();
  return JSON.stringify({
    tracks: rows,
    djMixIds: mix.length ? mix[0].tracks.persistentID() : [],
    playlists: music.userPlaylists.name(),
  });
}
