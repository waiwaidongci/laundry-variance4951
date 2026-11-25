# 洗衣工坊耗水差异透视

澄湾布草服务中心用水核算组。澄湾为 9 家酒店和 3 家护理机构洗涤布草。东港、白榆、龙桥三个工坊按生产日传回批次重量和洗程实耗，能源班组提供同批次计量窗口的独立水表读数。核算员用 Excel 合并计划表、洗程日志和水表记录，工艺主管复核差异后交给能源管理组。

导入 chengwan-wash-water-20261116-19.csv。每行一个工坊的一种布类洗涤批次，包含生产日、批号、计划和实洗重量、两个版本的定额、批次实耗及独立水表实耗。定额以 L/kg 填写，查不到适用版本时填「未知」。 给工艺主管提供按工坊和布类逐层核对的差异桥及可导出的批次证据。对账通过且复核批准后，能源管理组接收站内报告。

- 各工坊在 Excel 平均每批 L/kg，大小洗程对工坊指标的影响被算成一样。
- 执行定额、实洗重量和布类组合一起改变，设备班组难以判断超计划耗水从哪一步产生。
- 个别批次找不到定额，水表差一正一负又会抵消，能源组收到的表常要退回重查。

## 六个业务模块

1. 工坊用水概览
2. 洗涤批次台账
3. 用水透视配置
4. 布类与批次差异
5. 定额与水表复核
6. 耗水报告投递

## 工作剧本

1. 08:00 用水核算组查看 11 月 16–19 日的三工坊生产口径，确认四类布草按批号归集。
2. 08:15 导入 chengwan-wash-water-20261116-19.csv。48 批合计实洗 6,900 kg，批次实耗 63,106.3 L。
3. 08:35 以全部工坊和布类运行透视，水表累计容差设为 0 L。已匹配计划基准为 61,156.8 L，实耗多 1,949.5 L。
4. 09:10 筛选白榆工坊和厚绒浴巾并展开批次。四批共 549 kg、6,173.2 L，效率差为 244 L。
5. 09:30 核对批次 WX-19-22，实洗 153 kg，批次和水表各为 1,713.4 L。用副本将该批执行定额填为「未知」，验证 1,713.4 L 缺标实耗仍可追溯且无法批准。
6. 10:00 工艺主管批准原始台账的全量报告，核对分解残差与水表累计绝对差均为 0 L。
7. 10:15 导出工坊和布类差异 CSV，并将已批准报告安排在 14:00 送达能源管理组。

## 明日洗涤批次排工坊（第 7 个模块）

给次日洗涤批次安排工坊，导航「明日洗涤批次排工坊」（`#6`）。规则与可核对项：

- **整批不拆**：每个明日批次只分配给一个工坊，批次重量整批计入该坊限重。
- **同类实洗准入**：某种布只能去历史台账中存在该 `工坊 × 布类` 实洗记录的工坊。
- **加权单耗估算**：单耗 = 该坊该布历史 `sum(actual_liters) ÷ sum(actual_kg)`（不是各批 L/kg 的平均），本批估算用水 = 批次重量 × 该单耗。
- **最低耗水 / 最小增容**：在各坊限重下精确最小化全批估算耗水（PuLP/CBC 两阶段求解，不可用时回退到标准库确定性分支定界，并经穷举 oracle 校验）。排不下时先精确最小化 `Σ坊 max(0, 已排重量−限重)`，即至少要新增的容量，并给出加在哪些工坊、各加多少。
- **缺历史单列**：没有任何同类实洗记录（或历史总实洗重量为 0）的批次进入单独表格并写明原因，不参与优化。
- **超时不称最优**：结果含 `status`（`optimal` / `capacity_shortfall` / `timeout_unproven` / `all_missing_history`）、`proven_optimal`、`expansion_proven`、`timed_out`。超时只回报当前可行方案并在页面以警示条标明「最优性未证实，不得当作最低耗水/最小增容」；`time_limit_seconds: 0` 可确定性复现该状态。仅当日志出现 CBC `Result - Optimal solution found` 或分支定界完整穷举时才标记为已证实。
- **同版一致**：求解顺序与平局选择全部确定，结果带 `signature`；页面「同版重跑并核对去向」会再次提交同一输入快照并逐批比较工坊与估算用水。
- **逐批/逐坊核对**：页面给出每批去向、单耗、估算用水、历史总耗水/总实洗与候选工坊，以及每坊限重、已排、剩余容量、建议新增与该坊估算用水；两类表与缺历史表均可导出 CSV。

后端入口：`POST /scheduling/runs`（亦兼容 `POST /scheduling`、`POST /scheduling/run`），`GET /scheduling/runs[/{id}]`、`GET /scheduling/defaults`、`GET /scheduling/history-meta`、`GET /scheduling/runs/{id}/csv?kind=batches|shops|missing`。纯算法入口为 `backend/src/services/scheduling.py` 的 `schedule_workshops(history_rows, plan_batches, capacities, options)`，并已从 `services/domain.py` 导出。

## 算法与复核

由计划 kg × 计划定额起算，依次换成执行定额、实洗 kg、批次实耗，三步分别得到标准差、量与结构差、效率差。每层的加权实耗为总 L ÷ 总实洗 kg。工坊和布类可填全名筛选，星号表示全部。「展开批次」显示每个交叉项下的批号。

日期、重复批次、重量、用水或定额数值无效时请更正台账再导入。缺标实耗单列保留，当前范围有缺标、分解残差非零或累计批次水表绝对差超容差时，复核批准与分发受阻。 复核问题为「当前工坊与布类范围是否都已匹配两个版本的定额，三项差异是否加总到实耗，批次实耗与独立水表是否在容差内？」。每次分析绑定数据版本和参数，原始数据按规范化内容的 SHA-256 去重。日程针对已复核的固定结果版本，每日模式按初次投递时间每 24 小时重复。停机期间错过多次时只补一份到站内收件箱，下一次回到原时间点。

| 字段 | 含义 | 类型 |
|---|---|---|
| `work_date` | 生产日 | str |
| `shop` | 工坊 | str |
| `textile_class` | 布类 | str |
| `batch_id` | 洗涤批号 | str |
| `planned_kg` | 计划重量（kg） | float |
| `actual_kg` | 实际重量（kg） | float |
| `plan_standard` | 计划定额（L/kg 或未知） | str |
| `current_standard` | 执行定额（L/kg 或未知） | str |
| `actual_liters` | 批次实耗（L） | float |
| `meter_liters` | 独立水表实耗（L） | float |

## 启动

```sh
[ -f .env ] || cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

- Web 界面 `http://127.0.0.1:18300`
- API 文档 `http://127.0.0.1:19300/docs`
- 数据库端口 `17800`

| 账号 | 演示密码 | 工作权限 |
|---|---|---|
| analyst | analyst-demo-24 | 导入 CSV、运行分析、导出工作稿 |
| reviewer | reviewer-demo-24 | 复核、设置和停止投递、导出 |
| viewer | viewer-demo-24 | 浏览结果、导出已批准的报告、查收投递 |

原生 HTML/CSS/JavaScript 前端与 Nginx、FastAPI、Python worker、PostgreSQL 16 和 Redis 组成五服务 Compose。`backend/src/services/domain.py` 存放业务算法，`domain.json` 定义字段与验收口径，`frontend/src/pages` 为业务页面。更新任一侧源文件后，用 `docker compose up -d --build` 重建对应镜像。`backend/src/constants/enums.py` 和 `frontend/src/constants/enums.js` 定义角色及运行状态。`.env` 支持前端、API、数据库端口、演示 seed 开关和 JWT 密钥。首次部署到非演示环境时更换演示身份及 JWT/数据库凭据。

`docker compose down` 停止服务并保留数据卷。现场数据和演示结果可以在各自版本中并存。

项目源码采用 MIT 许可，见 `LICENSE`。
