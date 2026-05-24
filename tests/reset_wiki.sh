#!/bin/sh
# 把 ./wiki 复位到 git 追踪的基线快照（tests/fixtures/wiki_baseline）。
# 用于性能优化前后的可复现对比：每次测量前先复位，确保起点一致。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
rm -rf "$ROOT/wiki"
cp -r "$ROOT/tests/fixtures/wiki_baseline" "$ROOT/wiki"
echo "wiki 已复位到基线："
find "$ROOT/wiki" -name '*.md' | sort
