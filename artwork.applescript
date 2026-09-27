-- 指定した persistent ID の曲の 1 枚目のアートワークをファイルに書き出す。
--
--   osascript artwork.applescript <persistentID> <出力先>
--
-- 書き出したら画像の形式 (JPEG picture 等) を、アートワークが無ければ none を返す。
on run argv
  set pid to item 1 of argv
  set outPath to item 2 of argv
  tell application "Music"
    set t to first track of library playlist 1 whose persistent ID is pid
    if (count of artworks of t) is 0 then return "none"
    set a to artwork 1 of t
    set d to raw data of a
    set f to format of a
  end tell
  set fh to open for access (POSIX file outPath) with write permission
  set eof fh to 0
  write d to fh
  close access fh
  return f as text
end run
