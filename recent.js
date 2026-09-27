// 追加日が過去 N 日以内の曲を Music.app から取り出し、JSON の配列で標準出力に返す。
//
//   osascript -l JavaScript recent.js <days>
//
// whose で絞ってからフィールドごとに一括取得する (1 フィールド 1 イベント)。
// 曲ごとに取るより桁違いに速い。アルバムへのまとめは server.py 側で行う。
var FIELDS = ["persistentID", "name", "album", "albumArtist", "artist", "genre",
              "trackNumber", "discNumber", "duration", "dateAdded", "playedCount", "playedDate"];

function run(argv) {
  var days = Number(argv[0] || 180);
  var since = new Date(Date.now() - days * 86400e3);
  var q = Application("Music").libraryPlaylists[0].tracks.whose({ dateAdded: { ">": since } });
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
  return JSON.stringify(rows);
}
