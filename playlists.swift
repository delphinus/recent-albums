// Music のプレイリストを、フォルダの階層と曲数・合計時間付きで JSON の配列にして標準出力に返す。
//
//   swift playlists.swift
//
// Music.app のスクリプトからはプレイリストの親フォルダをまとめて取れず、1 本ずつ引くと 1,500 本で
// 2 分を超える。iTunesLibrary フレームワークなら全体を数秒で読める。読むのは Music.app がディスクに
// 書き出した内容なので、作ったばかりのプレイリストは少し遅れて出ることがある。
import Foundation
import iTunesLibrary

let lib = try ITLibrary(apiVersion: "1.1")
let byID = Dictionary(uniqueKeysWithValues: lib.allPlaylists.map { ($0.persistentID, $0) })

func folderPath(_ p: ITLibPlaylist) -> String {
    var names: [String] = []
    var parent = p.parentID.flatMap { byID[$0] }
    while let f = parent {
        names.insert(f.name, at: 0)
        parent = f.parentID.flatMap { byID[$0] }
    }
    return names.joined(separator: " / ")
}

// ライブラリ全体や「ミュージックビデオ」のような Music が用意するもの、フォルダ自体は除く
let rows: [[String: Any]] = lib.allPlaylists
    .filter { !$0.isPrimary && $0.distinguishedKind == .kindNone && $0.kind != .folder && $0.isVisible }
    .map { p in
        [
            "id": String(format: "%016llX", p.persistentID.uint64Value),
            "name": p.name,
            "folder": folderPath(p),
            "smart": p.kind == .smart,
            "tracks": p.items.count,
            "duration": p.items.reduce(0) { $0 + $1.totalTime } / 1000,
        ]
    }
let data = try JSONSerialization.data(withJSONObject: rows)
FileHandle.standardOutput.write(data)
