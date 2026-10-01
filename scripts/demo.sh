#!/usr/bin/env bash
# 端到端调用示例：登记站/设备 -> 设备补传（含幂等重试与冲突）-> 裁定 ->
# 审核更正/作废 -> 发布冻结 -> 公开读取。
# 用法: BASE=http://127.0.0.1:8000 TOKEN=your-token bash scripts/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
TOKEN="${FLOOD_REVIEW_TOKEN:-${TOKEN:-}}"
AUTH=()
if [ -n "$TOKEN" ]; then AUTH=(-H "X-Reviewer-Token: $TOKEN"); fi

say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }
post() { curl -sS -X POST "$BASE$1" -H "Content-Type: application/json" "${@:2}"; }
get()  { curl -sS "$BASE$1" "${@:2}"; }

say "1) 健康检查"
get /health; echo

say "2) 登记监测站（上升阈值 5.00m / 解除阈值 4.00m）与设备"
post /api/stations "${AUTH[@]}" -d '{
  "code": "QS-001", "name": "青石沟1号站",
  "rise_threshold": "5.00", "clear_threshold": "4.00",
  "devices": [{"device_no": "DEV-QS-001", "name": "雷达水位计"}]
}'; echo

# 以当前时间为基准生成带时区（+08:00）的观测时间
iso() { date -u -d "$1 minutes" +"%Y-%m-%dT%H:%M:%S+00:00" 2>/dev/null \
        || date -u -v"${1}M" +"%Y-%m-%dT%H:%M:%S+00:00"; }

say "3) 设备补传：低-低，高位x3（跨度10分钟触发预警），低位x2（跨度5分钟解除）"
i=1
TS1=""
for item in "-55 3.2" "-50 3.5" "-45 3.8" "-40 5.4" "-35 5.8" "-30 6.2" "-25 3.9" "-20 3.7"; do
  set -- $item
  TS="$(iso "$1")"
  if [ -z "$TS1" ]; then TS1="$TS"; fi
  post /api/readings -d "{\"device_no\":\"DEV-QS-001\",\"seq\":$i,\"observed_at\":\"$TS\",\"water_level\":\"$2\"}" >/dev/null
  i=$((i+1))
done
echo "已补传 8 个读数"

say "4) 同号同序号同内容重试 -> 200 replayed=true（原样回放 seq=1）"
post /api/readings -d "{\"device_no\":\"DEV-QS-001\",\"seq\":1,\"observed_at\":\"$TS1\",\"water_level\":\"3.2\"}" | head -c 300; echo

say "5) 同号同序号异内容 -> 409 冲突"
post /api/readings -d "{\"device_no\":\"DEV-QS-001\",\"seq\":1,\"observed_at\":\"$TS1\",\"water_level\":\"9.99\"}" | head -c 300; echo

say "6) 查询待发布裁定时间线（预警->解除）"
get /api/stations/QS-001/timeline | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('state =', d['state'], '| events =', [(e['event'], e['trigger_observed_at']) for e in d['events']])
print('in_sync_with_public =', d['in_sync_with_public'])
"

say "7) 发布冻结 v1（补传/修订在重新发布前不影响公开结果）"
post /api/publish "${AUTH[@]}" -d '{"stations":["QS-001"]}' \
  | python3 -c "import json,sys;print('published version =', json.load(sys.stdin)['published'][0]['version'])"

say "8) 公开读取（无需令牌）"
get /public/stations
echo
get /public/stations/QS-001/timeline | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('public version =', d['version'], '| state =', d['state'])
"

say "9) 审核更正某读数（带原因），裁定自动重算（把首个高位读数改为低位 -> 预警消失）"
RID=$(get "/api/stations/QS-001/readings" | python3 -c "import json,sys;print(json.load(sys.stdin)['readings'][3]['id'])")
post "/api/readings/$RID/correct" "${AUTH[@]}" -H "X-Reviewer: lisi" \
  -d '{"water_level":"3.6","reason":"现场校测，原始值受漂浮物影响"}' >/dev/null
get "/api/readings/$RID/revisions" | python3 -m json.tool
echo "更正后待发布时间线："
get /api/stations/QS-001/timeline | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('state =', d['state'], '| events =', [e['event'] for e in d['events']], '| in_sync =', d['in_sync_with_public'])
"

say "10) 作废读数（带原因），原值与历次修订仍可查"
post "/api/readings/$RID/void" "${AUTH[@]}" -H "X-Reviewer: lisi" \
  -d '{"reason":"确认该时刻传感器故障"}' | python3 -c "import json,sys;print('status =', json.load(sys.stdin)['status'])"

say "演示完成。交互式文档: $BASE/docs"
