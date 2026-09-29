// 追加日が過去 N 日以内の曲、プレイリスト「DJ Mix」に入っている曲の persistentID、
// プレイリストの名前の一覧を Music.app から取り出し、JSON で標準出力に返す。
//
//   osascript -l JavaScript recent.js <days>   (days が 0 ならライブラリ全体)
//
// whose で絞ってからフィールドごとに一括取得する (1 フィールド 1 イベント)。
// 曲ごとに取るより桁違いに速い。アルバムへのまとめは server.py 側で行う。
var DJ_MIX_PLAYLIST = "DJ Mix";
var FIELDS = ["persistentID", "name", "album", "albumArtist", "artist", "genre",
              "trackNumber", "discNumber", "duration", "dateAdded", "playedCount", "playedDate"];

function run(argv) {
  var days = Number(argv[0] || 365);
  var since = new Date(Date.now() - days * 86400e3);
  var music = Application("Music");
  var all = music.libraryPlaylists[0].tracks;
  var q = days > 0 ? all.whose({ dateAdded: { ">": since } }) : all;
  var cols = {};
  FIELDS.forEach(function (f) { cols[f] = q[f](); });
  var n = cols.persistentID.length, rows = [];
  for (var i = 0; i < n; i++) {
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
