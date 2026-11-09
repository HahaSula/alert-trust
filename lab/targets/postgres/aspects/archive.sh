#!/bin/sh
# archive_command 的替身：/tmp/archive_fail 存在時失敗，否則假裝成功（不保存檔案）。
[ -f /tmp/archive_fail ] && exit 1
exit 0
