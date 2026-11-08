// 曲の persistentID の並びから、フォルダの中に手動プレイリストを作る (同じ名前があれば作り直す)。
//
//   osascript -l JavaScript make_playlist.js <フォルダ名> <プレイリスト名> <pids.json>
//
// フォルダが無ければトップレベルに作る。作り直すのは、同じフォルダの中にある同じ名前のものだけ。
// 別のフォルダにある同じ名前のプレイリストには触れない。
//
// フォルダの参照は M.playlists を名前と class() で探す。M.folderPlaylists や
// <フォルダ>.playlists は 0 を返し、parent() を辿る方法では中身が空のフォルダを掴めない。
ObjC.import("Foundation");

function readFile(p) {
  return $.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null).js;
}

function findFolder(M, name) {
  var ps = M.playlists, names = ps.name(), classes = ps.class();
  for (var i = 0; i < names.length; i++) {
    if (names[i] === name && classes[i] === "folderPlaylist") return ps[i];
  }
  return null;
}

function run(argv) {
  var FOLDER = argv[0], NAME = argv[1], pids = JSON.parse(readFile(argv[2]));
  var M = Application("Music");

  var folder = findFolder(M, FOLDER);
  if (!folder) {
    M.make({ new: "folderPlaylist", withProperties: { name: FOLDER } });
    folder = findFolder(M, FOLDER);
    if (!folder) throw new Error("フォルダを作れませんでした: " + FOLDER);
  }
  var folderId = folder.persistentID();

  var same = M.userPlaylists.whose({ name: NAME })();
  for (var i = 0; i < same.length; i++) {
    var parentId = null;
    try { parentId = same[i].parent().persistentID(); } catch (e) {}
    if (parentId === folderId) M.delete(same[i]);
  }

  var pl = M.make({ new: "userPlaylist", at: folder, withProperties: { name: NAME } });
  var lib = M.libraryPlaylists[0], missing = [];
  pids.forEach(function (pid) {
    var t = lib.tracks.whose({ persistentID: pid })();
    if (t.length) M.duplicate(t[0], { to: pl });
    else missing.push(pid);
  });

  var got = pl.tracks().length ? pl.tracks.persistentID() : [];
  return JSON.stringify({ persistentID: pl.persistentID(), tracks: got, missing: missing });
}
