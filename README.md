# 工厂能耗证据采集与口径变更复核

合成工厂月度仪表、产量和公开折算因子演练记录。能源单位为kWh，产量为合格件数。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `meters.csv` contains 电表观测和计量点映射样本。 Columns are `reading`, `meter`, `line`, `period`, `kwh`, `version`.
- `production.csv` contains 产线合格产量及口径修订。 Columns are `row_id`, `line`, `period`, `good_units`, `scope`.
- `factors.csv` contains 引用的来源折算因子版本。 Columns are `factor`, `source`, `period`, `value`, `revision`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
