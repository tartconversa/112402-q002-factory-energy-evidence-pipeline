# 演练：园区月度能耗核算

以下命令全部使用合成样例，演示从采集到定版再到追溯修订的完整链路。
工作目录为仓库根目录，数据库写到临时路径。

```bash
DB=/tmp/plant-demo.db
rm -f "$DB"
```

## 1. 采集

电表导出按每页 2 行抓取，先只抓一页，模拟运行中断：

```bash
python3 -m energy_ledger --db "$DB" fetch meters \
    --origin meter-platform-2026Q3 --path samples/meters.csv --page-size 2 --pages 1
# {"batch_id": 1, "status": "open", "resume_page": 2, "processed_pages": 1, ...}

python3 -m energy_ledger --db "$DB" resume 1
```

产量与公开因子：

```bash
python3 -m energy_ledger --db "$DB" fetch production \
    --origin production-system --path samples/production.csv
python3 -m energy_ledger --db "$DB" fetch factors \
    --origin public-factor-site --path samples/factors.csv
```

## 2. 人工确认

```bash
# 8 月使用旧表 E-1，9 月起使用新表 E-2（换表）
python3 -m energy_ledger --db "$DB" mapping add E-1 L-A \
    --from 2026-08-01 --to 2026-08-31 --by meter-team
python3 -m energy_ledger --db "$DB" mapping add E-2 L-A \
    --from 2026-09-01 --to 2026-12-31 --by meter-team --replaces-meter E-1

# L-B 的新表计量关系待核
python3 -m energy_ledger --db "$DB" mapping add E-3 L-B \
    --from 2026-09-01 --to 2026-12-31 --status pending --by meter-team

python3 -m energy_ledger --db "$DB" scope add L-A \
    --from 2026-08-01 --to 2026-12-31 --product widget --process-version P1 --by prod
python3 -m energy_ledger --db "$DB" scope add L-B \
    --from 2026-09-01 --to 2026-12-31 --scope includes-rework --by prod
```

## 3. 计算与定版

```bash
python3 -m energy_ledger --db "$DB" compute 2026-09 --pretty
```

`L-A` 可比较，`L-B` 因为电量缺失、映射待核、产量含返工三项原因不可比较。
园区汇总只含 `L-A`。

```bash
# 默认拒绝部分发布
python3 -m energy_ledger --db "$DB" publish 2026-09
# {"ok": false, "error": "NotReadyError", ...}

# 显式部分发布，报告保留不可比原因
python3 -m energy_ledger --db "$DB" publish 2026-09 --allow-partial \
    --note "L-B 待计量与生产确认" --by analyst
```

## 4. 收到更正后复核

电表更正与因子追溯修订：

```bash
printf 'reading,meter,line,period,kwh,version\nR2,E-2,L-A,2026-09,9300,v3\n' > /tmp/meters-fix.csv
python3 -m energy_ledger --db "$DB" fetch meters --origin meter-correction --path /tmp/meters-fix.csv

printf 'factor,source,period,value,revision\nF4,public-A,2026-09,0.75,3\n' > /tmp/factors-fix.csv
python3 -m energy_ledger --db "$DB" fetch factors --origin factor-site --path /tmp/factors-fix.csv
```

```bash
python3 -m energy_ledger --db "$DB" impact 2026-09 --pretty
```

输出会给出：

* `line_changes`：`L-A` 单位能耗前后值；
* `ranking_changes`：排名变化；
* `park_summary`：园区汇总前后值；
* `factor_revision_changes`：因子从 r2 变为 r3 的归因；
* `reissue_scope.lines`：需要重新签发的产线。

已发布快照保持原值：

```bash
python3 -m energy_ledger --db "$DB" release show 2026-09 \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['payload']['draft']['lines'][0]['metric'])"
# 仍是发布时的 8700 kWh、因子 r2
```

## 5. 追溯

```bash
python3 -m energy_ledger --db "$DB" trace 2026-09 L-A --pretty
python3 -m energy_ledger --db "$DB" compare L-A --a 2026-08 --b 2026-09 --pretty
python3 -m energy_ledger --db "$DB" audit --limit 10
```
