#!/usr/bin/env bash
# 山洪水位预警裁定服务 —— 端到端调用示例
# 用法：先启动服务（见 README），再执行 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
J='content-type: application/json'

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 登记监测站（上升阈值 10.0，解除阈值 8.0）"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"青石峡站","rise_threshold":10.0,"clear_threshold":8.0}' ; echo

step "2. 为监测站登记设备 DEV-001"
curl -s -X POST "$BASE/stations/1/devices" -H "$J" -d '{"device_no":"DEV-001"}'; echo

step "3. 设备补传读数（设备号 + 设备内序号 + 带时区观测时间 + 水位）"
for m in 00 05 10; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-001\",\"seq\":1$m,\"observed_at\":\"2026-10-01T08:$m:00+08:00\",\"water_level\":12.0}"; echo
done

step "4. 同号同序号同内容重试 → 返回原结果（deduplicated=true）"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":110,"observed_at":"2026-10-01T00:10:00Z","water_level":12.0}'; echo

step "5. 同号同序号异内容 → 409 冲突"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":110,"observed_at":"2026-10-01T08:10:00+08:00","water_level":13.0}'

step "6. 待发布裁定时间线（3 个读数达阈值、首尾 10 分钟 → 预警）"
curl -s "$BASE/stations/1/timeline"; echo

step "7. 发布：冻结公开结果"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"例行发布"}'; echo

step "8. 补传解除读数（2 条 ≤8.0，首尾 6 分钟 ≥5 → 待发布解除）"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":115,"observed_at":"2026-10-01T08:15:00+08:00","water_level":7.0}' > /dev/null
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":121,"observed_at":"2026-10-01T08:21:00+08:00","water_level":7.0}' > /dev/null
echo "待发布状态: $(curl -s "$BASE/stations/1/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
echo "公开状态（仍冻结为预警）: $(curl -s "$BASE/public/stations/1/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"

step "9. 审核员更正读数（带原因），裁定随之重算"
curl -s -X POST "$BASE/readings/1/correct" -H "$J" \
  -d '{"water_level":9.0,"reason":"比测后修正","auditor":"王审"}'; echo

step "10. 查询原值与历次修订"
curl -s "$BASE/readings/1/revisions"; echo

step "11. 重新发布 → 公开结果更新"
curl -s -X POST "$BASE/publish" > /dev/null
curl -s "$BASE/public/stations/1/timeline"; echo

step "12. 作废读数示例（读数 2，带原因）"
curl -s -X POST "$BASE/readings/2/void" -H "$J" \
  -d '{"reason":"传感器故障","auditor":"李审"}'; echo
