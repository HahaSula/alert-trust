#!/bin/sh
# archive_command 的替身：/tmp/archive_fail 存在時失敗，否則假裝成功（不保存檔案）。
# 在容器裡 touch／rm 這個檔就能切換，不必改 archive_command（命令列 -c 的優先權高於 ALTER SYSTEM）。
[ -f /tmp/archive_fail ] && exit 1
exit 0
