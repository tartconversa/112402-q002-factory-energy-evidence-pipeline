# 工厂能耗证据采集与口径变更复核

合成工厂月度仪表、产量和公开折算因子演练记录。能源单位为kWh，产量为合格件数。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `meters.csv` contains 电表观测和计量点映射样本。 Columns are `reading`, `meter`, `line`, `period`, `kwh`, `version`.
- `production.csv` contains 产线合格产量及口径修订。 Columns are `row_id`, `line`, `period`, `good_units`, `scope`.
- `factors.csv` contains 引用的来源折算因子版本。 Columns are `factor`, `source`, `period`, `value`, `revision`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.

## 分析工具

`energy_ledger` 是一个只依赖 Python 标准库的采集与核对工具，数据保存在 SQLite
中。它把“原始观测”“人工确认”“月度草稿”和“已发布快照”分开保存，避免重算
悄悄改写已发布月份。

### 运行

```bash
python3 -m energy_ledger --db plant.db init

# 抓取（CSV、分页 JSON 接口、公开 HTML 表格均可）
python3 -m energy_ledger --db plant.db fetch meters \
    --origin meter-platform-202609 --path samples/meters.csv
python3 -m energy_ledger --db plant.db fetch factors \
    --origin public-factor-site --path samples/factors.csv

# 中断后续传；已提交的页不会重复抓取
python3 -m energy_ledger --db plant.db resume 12

# 计量人员登记换表前后关系
python3 -m energy_ledger --db plant.db mapping add E-OLD L-A \
    --from 2026-08-01 --to 2026-08-31
python3 -m energy_ledger --db plant.db mapping add E-NEW L-A \
    --from 2026-09-01 --to 2026-12-31 --replaces-meter E-OLD

# 生产人员确认产品、工艺版本和合格品统计范围
python3 -m energy_ledger --db plant.db scope add L-A \
    --from 2026-08-01 --to 2026-12-31 --product widget --process-version P1

# 存在多个因子来源时显式选择；--period 可只约束某月
python3 -m energy_ledger --db plant.db factor select --source public-A --factor-key ""

python3 -m energy_ledger --db plant.db compute 2026-09 --pretty
python3 -m energy_ledger --db plant.db publish 2026-09 --allow-partial --note "L-B 待核"
python3 -m energy_ledger --db plant.db impact 2026-09 --pretty
python3 -m energy_ledger --db plant.db trace 2026-09 L-A --pretty
```

### 接口来源与授权

`fetch` 通过 `--config`/`--config-json` 接收来源配置，支持四种适配器：

| kind | 用途 | 关键参数 |
| --- | --- | --- |
| `csv` | 本地导出文件，按页切分 | `path`, `page_size`, `source_columns` |
| `http-json` | 授权 JSON 接口，游标/页码分页 | `url`, `token_env`, `records_path`, `next_path` |
| `json-file` | 离线演练接口响应 | `path`, `records_path` |
| `html-table` | 公开因子页面 | `path_or_url`, `columns`, `table_index` |

授权令牌只通过环境变量读取（`token_env` 指定变量名）。配置里出现
`token`、`password`、`authorization` 等键会被拒绝，避免凭据随批次写入数据库。
非标准列名用 `source_columns`（命令行 `--map-column 来源列=标准字段`）映射，
例如把 `月份` 映射为 `period`。

### 核算规则

* 电量按计量点归集，只有“确认”状态的映射参与计算；`partial`（部分对应）
  和 `pending`（待核）都会阻断该产线，不会把电量分摊给看起来相近的产线。
* 换表当月要求新旧计量点的有效期连续覆盖整月，否则报告 `meter_mapping_gap`。
* 产量按来源行号取最高来源版本，同一月多条行号求和；`includes-rework`
  口径不能作为合格品分母，必须等生产人员修正或确认 `final`。
* 产量口径确认必须整月覆盖；月中切换口径会被标记为 `production_scope_partial_period`。
* 因子按 `source` + `factor_key` 识别系列，同一系列取整月有效的最高
  `revision`；不会借用其他期间的因子，也不会在最新修订缺值时回退到旧修订。

### 可发布条件与定版

一条产线当月只有在电量、产量、口径确认和因子都齐备时才产生指标并进入排名。
`publish` 默认要求所有产线可比较；确有部分产线待核时，用 `--allow-partial`
定版，报告会保留每条不可比产线的原因。

发布保存完整快照，包括当时的观测 ID、来源批次、映射、口径确认和因子修订，
并以 SHA-256 校验；任何后续重算都不会改写它。`impact` 比较最新快照与当前
重算，列出单位能耗、排名和园区汇总的变化，以及需要重新签发的产线范围；
`factor_revision_changes` 单独指出因子追溯修订的影响。

### 测试

```bash
python3 -m unittest discover -s tests -v
```

完整的采集、换表、返工口径、定版、追溯修订演练见
[`docs/walkthrough.md`](docs/walkthrough.md)。

