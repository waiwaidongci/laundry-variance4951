"""明日洗涤批次排工坊：整批不拆、同类实洗记录准入、历史加权单耗估算与限重最优分配。

本模块只依赖 Python 标准库，便于直接导入测试。核心是一个确定性的
分支定界搜索，按字典序先最小化「超容总量（= 至少新增容量）」，
再最小化估算总耗水。搜索在时限内未证明最优时，只返回可行候选并
明确标记 ``proven_optimal=False``，绝不把未证实结果称为最优。
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from fractions import Fraction

ALL = '*'

BATCH_KEYS = ('batch_id', '批号', '洗涤批号', 'batch', 'id')
TEXTILE_KEYS = ('textile_class', '布类', '布类名称', 'textile')
WEIGHT_KEYS = ('weight_kg', 'weight', 'kg', '批次重量（kg）', '批次重量(kg)', '重量（kg）', '重量(kg)',
               '重量', '计划重量（kg）', 'planned_kg', '实际重量（kg）', 'actual_kg')
SHOP_KEYS = ('shop', '工坊', '工坊名称', 'workshop')
LIMIT_KEYS = ('limit_kg', 'capacity_kg', 'capacity', 'limit', '限重（kg）', '限重(kg)',
              '工坊限重（kg）', '工坊限重(kg)', '限重', '容量（kg）', 'add_capacity_kg')
HISTORY_LITER_KEYS = ('actual_liters', '批次实耗（L）', '批次实耗(L)', '实耗（L）', '实耗(L)', '总耗水（L）')
HISTORY_KG_KEYS = ('actual_kg', '实洗重量（kg）', '实洗重量(kg)', '实洗重量', '实际重量（kg）')

DEFAULT_TIME_LIMIT = 5.0
MAX_BATCHES = 2000
MAX_SHOPS = 200


# ---------------------------------------------------------------------------
# 数值与字段解析
# ---------------------------------------------------------------------------

def _pick(row, keys):
    """从一行 dict 中按候选键名取第一个非空值。"""
    if not isinstance(row, dict):
        return None
    for key in keys:
        if key in row and row[key] is not None and str(row[key]).strip() != '':
            return row[key]
    return None


def _text(value, label):
    if value is None or not str(value).strip():
        raise ValueError(f'{label}不能为空。')
    return str(value).strip()


def _fraction(value, label, *, positive=True, maximum=1_000_000):
    if isinstance(value, bool):
        raise ValueError(f'{label}须为数字。')
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f'{label}须填写数字。') from error
    if not decimal.is_finite():
        raise ValueError(f'{label}须为有限数值。')
    number = Fraction(decimal)
    if positive and number <= 0:
        raise ValueError(f'{label}须大于 0。')
    if number < 0 or number > maximum:
        raise ValueError(f'{label}超出可接受范围（0–{maximum}）。')
    return number


def _float(value):
    return round(float(value), 6)


def _json_safe(value):
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# 历史实洗记录 → 工坊 × 布类 加权单耗
# ---------------------------------------------------------------------------

def aggregate_history(history_rows):
    """汇总每个「工坊 × 布类」的历史总耗水与总实洗重量。

    返回 ``(agg, shops, textiles)``；agg 的值为
    ``{"liters": Fraction, "kg": Fraction, "batches": int}``。
    单耗一律用 总耗水 ÷ 总实洗重量，而不是各批 L/kg 的算术平均。
    """
    if not isinstance(history_rows, list) or not history_rows:
        raise ValueError('缺少历史实洗台账，无法估算单耗与准入工坊。')
    agg: dict[tuple[str, str], dict] = {}
    shops: set[str] = set()
    textiles: set[str] = set()
    for line, row in enumerate(history_rows, 2):
        if not isinstance(row, dict):
            raise ValueError(f'历史台账第 {line} 行格式无效。')
        try:
            shop = _text(_pick(row, SHOP_KEYS), '工坊名称')
            textile = _text(_pick(row, TEXTILE_KEYS), '布类名称')
            kg = _fraction(_pick(row, HISTORY_KG_KEYS), '历史实洗重量（kg）', positive=False)
            liters = _fraction(_pick(row, HISTORY_LITER_KEYS), '历史实耗（L）', positive=False)
        except ValueError as error:
            raise ValueError(f'历史台账第 {line} 行：{error}') from error
        key = (shop, textile)
        cell = agg.setdefault(key, {'liters': Fraction(0), 'kg': Fraction(0), 'batches': 0})
        cell['liters'] += liters
        cell['kg'] += kg
        cell['batches'] += 1
        shops.add(shop)
        textiles.add(textile)
    return agg, sorted(shops), sorted(textiles)


def history_rate_rows(history_rows):
    """给页面/接口核对用的历史汇总单耗表。"""
    agg, _, _ = aggregate_history(history_rows)
    rows = []
    for (shop, textile), cell in sorted(agg.items()):
        rate = cell['liters'] / cell['kg'] if cell['kg'] > 0 else None
        rows.append({
            'shop': shop,
            'textile_class': textile,
            'history_batches': cell['batches'],
            'history_total_liters': _float(cell['liters']),
            'history_total_kg': _float(cell['kg']),
            'rate_l_per_kg': _float(rate) if rate is not None else None,
        })
    return rows


# ---------------------------------------------------------------------------
# 明日批次与限重输入解析
# ---------------------------------------------------------------------------

def parse_plan_batches(items):
    if isinstance(items, str):
        items = _read_csv(items)
    if not isinstance(items, list) or not items:
        raise ValueError('请至少提供一个明日洗涤批次。')
    if len(items) > MAX_BATCHES:
        raise ValueError(f'明日批次最多 {MAX_BATCHES} 个，请拆分排坊版本。')
    batches = []
    seen = set()
    for index, row in enumerate(items, 1):
        if not isinstance(row, dict):
            raise ValueError(f'明日批次第 {index} 条格式无效。')
        batch_id = str(_pick(row, BATCH_KEYS) or '').strip()
        if not batch_id:
            raise ValueError(f'明日批次第 {index} 条缺少批号。')
        if batch_id in seen:
            raise ValueError(f'明日批次批号 {batch_id} 重复，请去重后再排。')
        seen.add(batch_id)
        textile = _text(_pick(row, TEXTILE_KEYS), f'批次 {batch_id} 的布类')
        # 允许 0 kg 的退化批次（不占容量、估算为 0），但重量不得为负或缺失。
        weight = _fraction(_pick(row, WEIGHT_KEYS), f'批次 {batch_id} 的重量（kg）', positive=False)
        batches.append({'batch_id': batch_id, 'textile_class': textile, 'weight': weight})
    return batches


def parse_capacities(value, known_shops):
    """把多种限重入参统一为 ``{shop: Fraction}``。

    缺少历史工坊的限重会报错；历史台账中不存在的工坊不可能承接任何布类，
    其限重填写只忽略并通过 ignored 返回，不拖垮整次排坊。
    """
    if value is None:
        value = []
    if isinstance(value, dict):
        entries = [{'shop': shop, 'limit_kg': limit} for shop, limit in value.items()]
    elif isinstance(value, str):
        entries = _read_csv(value)
    elif isinstance(value, list):
        entries = value
    else:
        raise ValueError('工坊限重格式无效。')
    capacities: dict[str, Fraction] = {}
    ignored = []
    for index, row in enumerate(entries, 1):
        if not isinstance(row, dict):
            raise ValueError(f'限重第 {index} 条格式无效。')
        shop = _text(_pick(row, SHOP_KEYS), f'限重第 {index} 条的工坊名称')
        limit = _fraction(_pick(row, LIMIT_KEYS), f'{shop} 限重（kg）', positive=False)
        if shop not in known_shops:
            ignored.append(shop)
            continue
        if shop in capacities:
            raise ValueError(f'工坊 {shop} 的限重重复填写。')
        capacities[shop] = limit
    missing = [shop for shop in known_shops if shop not in capacities]
    if missing:
        raise ValueError('请填写以下全部历史工坊的限重：' + '、'.join(missing) + '。')
    return capacities, ignored


def _read_csv(text):
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿'), newline=''), strict=True)
    try:
        rows = list(reader)
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    return rows


# ---------------------------------------------------------------------------
# 确定性分支定界求解
# ---------------------------------------------------------------------------

class _StopSearch(Exception):
    """到达求解时限或节点预算，搜索未完成最优性证明。"""


def _over_increase(load: Fraction, weight: Fraction, cap: Fraction) -> Fraction:
    """把 weight 加入 load 后，Σmax(0, 负载−限重) 的增量。"""
    if load >= cap:
        return weight
    return max(Fraction(0), weight - (cap - load))


def _greedy_incumbent(order, items, caps, shop_count):
    loads = [Fraction(0)] * shop_count
    assignment: dict[int, int] = {}
    overload = Fraction(0)
    cost = Fraction(0)
    for item_index in order:
        item = items[item_index]
        chosen = None
        chosen_key = None
        for shop_index, rate, item_cost, _meta in item['candidates']:
            added_over = _over_increase(loads[shop_index], item['weight'], caps[shop_index])
            # 先最小化新增超容，再最小化耗水，最后按工坊序号保证确定性。
            key = (added_over, item_cost, shop_index)
            if chosen_key is None or key < chosen_key:
                chosen_key = key
                chosen = shop_index
                chosen_add = added_over
                chosen_cost = item_cost
        assignment[item_index] = chosen
        loads[chosen] += item['weight']
        overload += chosen_add
        cost += chosen_cost
    return overload, cost, assignment, loads


def _local_improve(overload, cost, assignment, loads, items, caps):
    """确定性的单点移动 / 两两交换局部改进，只为得到更强的初始上界。

    所有接受的方案都重新精确计算目标值；即使本函数有缺陷，分支定界
    仍会给出正确的最优性结论。
    """
    shop_count = len(caps)

    def objective(current_loads):
        over = sum(max(Fraction(0), value - cap) for value, cap in zip(current_loads, caps))
        total = Fraction(0)
        for item_index, shop_index in assignment.items():
            total += items[item_index]['costs_by_shop'][shop_index]
        return over, total

    for _pass in range(60):
        improved = False
        indices = sorted(assignment)
        # 单点移动
        for item_index in indices:
            weight = items[item_index]['weight']
            old_shop = assignment[item_index]
            old_cost = items[item_index]['costs_by_shop'][old_shop]
            current_best = (overload, cost)
            best_choice = None
            for shop_index, _rate, new_cost, _meta in items[item_index]['candidates']:
                if shop_index == old_shop:
                    continue
                trial = loads[:]
                trial[old_shop] -= weight
                trial[shop_index] += weight
                new_over = sum(max(Fraction(0), value - cap) for value, cap in zip(trial, caps))
                new_cost_total = cost - old_cost + new_cost
                candidate = (new_over, new_cost_total)
                if candidate < current_best:
                    current_best = candidate
                    best_choice = (shop_index, trial, new_over, new_cost_total, new_cost)
            if best_choice is not None:
                shop_index, trial, overload, cost, _new_cost = best_choice
                loads = trial
                assignment[item_index] = shop_index
                improved = True
        # 两两交换（重量可能不同，交换后两坊负载分别增减）
        for pos_a in range(len(indices)):
            for pos_b in range(pos_a + 1, len(indices)):
                ia, ib = indices[pos_a], indices[pos_b]
                sa, sb = assignment[ia], assignment[ib]
                if sa == sb:
                    continue
                wa, wb = items[ia]['weight'], items[ib]['weight']
                if sb not in items[ia]['costs_by_shop'] or sa not in items[ib]['costs_by_shop']:
                    continue
                trial = loads[:]
                trial[sa] += wb - wa
                trial[sb] += wa - wb
                if any(value < 0 for value in trial):
                    continue
                new_over = sum(max(Fraction(0), value - cap) for value, cap in zip(trial, caps))
                new_cost = (cost - items[ia]['costs_by_shop'][sa] - items[ib]['costs_by_shop'][sb]
                            + items[ia]['costs_by_shop'][sb] + items[ib]['costs_by_shop'][sa])
                if (new_over, new_cost) < (overload, cost):
                    overload, cost = new_over, new_cost
                    loads = trial
                    assignment[ia], assignment[ib] = sb, sa
                    improved = True
        if not improved:
            break
    return overload, cost, assignment, loads


def _common_scale(values):
    """一组十进制 Fraction 的公分母（返回整数 S，使 S×value 全为整数）。"""
    from math import lcm
    scale = 1
    for value in values:
        scale = lcm(scale, value.denominator)
    return scale


def _root_dual_lambda(items, caps, incumbent_cost):
    """用次梯度法求容量约束拉格朗日乘子（仅用于构造更强的下界）。

    浮点只负责寻找乘子；任何非负 λ 代入精确 Fraction 公式都是合法下界，
    因此不会因浮点误差误剪最优解。
    """
    m = len(caps)
    lam = [0.0] * m
    best_lb = float('-inf')
    best_lam = lam[:]
    ub = float(incumbent_cost)
    for iteration in range(160):
        assigned = [0.0] * m
        bound = 0.0
        for item in items:
            w = float(item['weight'])
            adjusted = [(float(cost) + lam[j] * w, j) for j, _r, cost, _m in item['candidates']]
            value, j = min(adjusted)
            bound += value
            assigned[j] += w
        bound -= sum(lam[j] * float(caps[j]) for j in range(m))
        if bound > best_lb:
            best_lb = bound
            best_lam = lam[:]
        gradient = [assigned[j] - float(caps[j]) for j in range(m)]
        norm_sq = sum(g * g for g in gradient)
        if norm_sq < 1e-16:
            break
        gap = max(0.0, ub - best_lb)
        step = min(2.0 / (iteration + 1), 1.5 * gap / norm_sq) if gap > 1e-9 else 0.02 / (iteration + 1)
        for j in range(m):
            lam[j] = max(0.0, lam[j] + step * gradient[j])
    return [Fraction(max(0.0, x)).limit_denominator(10**12) if x > 0 else Fraction(0)
            for x in best_lam]


def _refine_dual_lambda(views, free_caps, initial_lam, upper_bound, iterations=10):
    """节点内用少量次梯度迭代强化 λ；返回可直接代入精确公式的 Fraction λ。

    views: 每个剩余批次为 (weight_float, [(shop_index, cost_float, cost_Fraction)])。
    浮点只用于搜索乘子形状，最终 λ≥0 且用 Fraction 精确求值，不影响下界合法性。
    """
    m = len(free_caps)
    lam = list(initial_lam)
    best_lam = lam[:]
    best_lb = float('-inf')
    caps_f = [float(cap) for cap in free_caps]
    for iteration in range(iterations):
        assigned = [0.0] * m
        bound = 0.0
        for weight_float, options in views:
            value, chosen = min((cost_float + lam[j] * weight_float, j)
                                for j, cost_float, _cost_fraction in options)
            bound += value
            assigned[chosen] += weight_float
        bound -= sum(lam[j] * caps_f[j] for j in range(m))
        if bound > best_lb:
            best_lb = bound
            best_lam = lam[:]
        gradient = [assigned[j] - caps_f[j] for j in range(m)]
        norm_sq = sum(g * g for g in gradient)
        if norm_sq < 1e-14:
            break
        gap = max(0.0, float(upper_bound) - best_lb)
        step = min(1.0 / (iteration + 1), 1.2 * gap / norm_sq) if gap > 1e-9 else 0.01 / (iteration + 1)
        for j in range(m):
            lam[j] = max(0.0, lam[j] + step * gradient[j])
    return [Fraction(x).limit_denominator(10**10) if x > 1e-12 else Fraction(0) for x in best_lam]


def exact_assign(items, shops, caps, *, time_limit=DEFAULT_TIME_LIMIT, node_limit=None):
    """返回 assignments（item_index → shop_index）及求解证明状态。

    目标字典序：``(超容总 kg, 估算总耗水 L)``。超容总量即「至少新增
    容量」的精确值。重量在内部换成整数单位以加速容量运算。
    """
    n = len(items)
    m = len(shops)
    if n == 0:
        return {'assignment': {}, 'loads': [Fraction(0)] * m, 'overload': Fraction(0),
                'cost': Fraction(0), 'proven': True, 'timed_out': False, 'nodes': 0}

    scale = _common_scale([item['weight'] for item in items] + list(caps))
    weights_u = [int(item['weight'] * scale) for item in items]
    caps_u = [int(cap * scale) for cap in caps]

    # 困难批次优先：候选少、重量大、批号稳定排序。
    order = sorted(range(n), key=lambda i: (len(items[i]['candidates']), -items[i]['weight'],
                                            items[i]['batch_id']))
    suffix_weight = [0] * (n + 1)
    suffix_min_cost = [Fraction(0)] * (n + 1)
    for k in range(n - 1, -1, -1):
        item = items[order[k]]
        suffix_weight[k] = suffix_weight[k + 1] + weights_u[order[k]]
        suffix_min_cost[k] = suffix_min_cost[k + 1] + min(c for _j, _r, c, _m in item['candidates'])

    # 多个确定性贪心初始上界，取字典序最优。
    greedy_orders = [
        order,
        sorted(range(n), key=lambda i: (-items[i]['weight'], items[i]['batch_id'])),
        sorted(range(n), key=lambda i: (items[i]['weight'], items[i]['batch_id'])),
        list(range(n)),
    ]
    best = None
    for greedy_order in greedy_orders:
        over, cost, assign, loads = _greedy_incumbent(greedy_order, items, caps, m)
        over, cost, assign, loads = _local_improve(over, cost, assign, loads, items, caps)
        if best is None or (over, cost) < best['objective']:
            best = {'objective': (over, cost), 'assignment': dict(assign), 'loads': loads[:]}

    start = time.monotonic()
    deadline = start + max(0.0, float(time_limit))
    timed_out = False
    nodes = 0

    # 容量约束的拉格朗日乘子（根问题次梯度求得），用于强化耗水下界。
    dual_lambda = _root_dual_lambda(items, caps, best['objective'][1])
    dual_suffix = [Fraction(0)] * (n + 1)
    node_views = [None] * n
    for k in range(n - 1, -1, -1):
        item = items[order[k]]
        dual_suffix[k] = dual_suffix[k + 1] + min(
            cost + dual_lambda[j] * item['weight'] for j, _r, cost, _m in item['candidates'])
    for idx, item in enumerate(items):
        node_views[idx] = (float(item['weight']),
                           [(j, float(cost), cost) for j, _r, cost, _m in item['candidates']])

    # 零时限 / 零节点预算：只给启发式可行方案，明确不证明最优。
    if time_limit is not None and float(time_limit) <= 0:
        return {'assignment': best['assignment'], 'loads': best['loads'],
                'overload': best['objective'][0], 'cost': best['objective'][1],
                'proven': False, 'timed_out': True, 'nodes': 0,
                'elapsed': time.monotonic() - start}
    if node_limit is not None and int(node_limit) <= 0:
        return {'assignment': best['assignment'], 'loads': best['loads'],
                'overload': best['objective'][0], 'cost': best['objective'][1],
                'proven': False, 'timed_out': True, 'nodes': 0,
                'elapsed': time.monotonic() - start}

    loads_u = [0] * m
    choice = [0] * n
    stopped = False
    star_overload_u = int(best['objective'][0] * scale)

    def dfs(k, overload_u, cost):
        nonlocal best, nodes, stopped, timed_out, star_overload_u
        if stopped:
            return
        nodes += 1
        if (nodes & 255) == 0:
            if node_limit is not None and nodes >= int(node_limit):
                timed_out = True
                stopped = True
                return
            if time_limit is not None and time.monotonic() >= deadline:
                timed_out = True
                stopped = True
                return
        if k == n:
            objective = (Fraction(overload_u, scale), cost)
            if objective < best['objective']:
                assignment = {order[pos]: choice[pos] for pos in range(n)}
                best = {'objective': objective, 'assignment': assignment,
                        'loads': [Fraction(value, scale) for value in loads_u]}
                star_overload_u = overload_u
            return
        free_units = sum((cap_u - load for load, cap_u in zip(loads_u, caps_u) if load < cap_u), 0)
        lower_overload_u = overload_u + max(0, suffix_weight[k] - free_units)
        star_cost = best['objective'][1]
        if lower_overload_u > star_overload_u:
            return
        if lower_overload_u == star_overload_u and star_overload_u == 0:
            # 任意能追平/改进 incumbent 的剩余方案都必须满足各坊剩余硬容量。
            # 拉格朗日下界：Σ min_j(c_ij + λ_j w_i) − Σ λ_j F_j。
            lambda_cap_term = Fraction(0)
            for j in range(m):
                if dual_lambda[j]:
                    free_fraction = caps[j] - Fraction(loads_u[j], scale)
                    lambda_cap_term += dual_lambda[j] * free_fraction
            lower_cost = cost + dual_suffix[k] - lambda_cap_term
            if lower_cost >= star_cost:
                return
            if nodes < 20000 and k < n - 6:
                # 根乘子剪不掉且仍有足够深度时，在本节点做少量次梯度细化。
                views = [node_views[order[p]] for p in range(k, n)]
                free_fraction = [caps[j] - Fraction(loads_u[j], scale) for j in range(m)]
                refined_lambda = _refine_dual_lambda(
                    views, free_fraction, [float(x) for x in dual_lambda],
                    star_cost - cost, iterations=6)
                refined_bound = cost - sum(refined_lambda[j] * free_fraction[j] for j in range(m))
                for p in range(k, n):
                    item = items[order[p]]
                    refined_bound += min(item_cost + refined_lambda[j] * item['weight']
                                         for j, _r, item_cost, _m in item['candidates'])
                if refined_bound >= star_cost:
                    return
        elif lower_overload_u == star_overload_u:
            # 增容情形的保守耗水下界（每批独立取最低价，忽略容量）。
            if cost + suffix_min_cost[k] >= star_cost:
                return
        item_index = order[k]
        item = items[item_index]
        weight_u = weights_u[item_index]
        for shop_index, _rate, item_cost, _meta in sorted(item['candidates'],
                                                          key=lambda c: (c[2], c[0])):
            added_over_u = max(0, loads_u[shop_index] + weight_u - caps_u[shop_index]) \
                - max(0, loads_u[shop_index] - caps_u[shop_index])
            choice[k] = shop_index
            loads_u[shop_index] += weight_u
            dfs(k + 1, overload_u + added_over_u, cost + item_cost)
            loads_u[shop_index] -= weight_u
            if stopped:
                return

    try:
        dfs(0, 0, Fraction(0))
    except _StopSearch:  # 防御性：当前用 stopped 标志，不主动抛出
        timed_out = True

    elapsed = time.monotonic() - start
    overload, cost = best['objective']
    return {'assignment': best['assignment'], 'loads': best['loads'],
            'overload': overload, 'cost': cost,
            'proven': not timed_out, 'timed_out': timed_out,
            'nodes': nodes, 'elapsed': elapsed}


def cbc_assign(items, shops, caps, *, time_limit):
    """可选的 PuLP/CBC 精确求解后端（两阶段字典序，整数超容单位）。

    PuLP 在 CBC 超时停止时仍可能回报 Optimal，因此最优性只以 CBC 日志中的
    「Result - Optimal solution found」为准；其余一律按未证实处理。
    返回 None 表示 CBC 不可用，调用方回退到自研分支定界。
    """
    try:
        import contextlib
        import os
        import tempfile

        import pulp
    except Exception:
        return None

    n = len(items)
    m = len(shops)
    if n == 0:
        return {'assignment': {}, 'loads': [Fraction(0)] * m, 'overload': Fraction(0),
                'cost': Fraction(0), 'proven': True, 'timed_out': False, 'nodes': 0,
                'expansion_proven': True, 'solver': 'cbc'}
    # 预算过小（含调用方要求近零时限）时不启动 CBC 子进程，交由确定性分支定界
    # 在该预算内给出未证实标记，避免把启动耗时误当成求解完成。
    if time_limit is None or float(time_limit) < 0.02:
        return None

    scale = _common_scale([item['weight'] for item in items] + list(caps))
    weights_u = [int(item['weight'] * scale) for item in items]
    caps_u = [int(cap * scale) for cap in caps]

    def build_model(cost_objective, o_star=None):
        problem = pulp.LpProblem('workshop_schedule', pulp.LpMinimize)
        variables = {}
        for i, item in enumerate(items):
            for j, _rate, cost, _meta in item['candidates']:
                variables[(i, j)] = pulp.LpVariable(f'x_{i}_{j}', cat='Binary')
        over = [pulp.LpVariable(f'over_{j}', lowBound=0, cat='Integer') for j in range(m)]
        for i, item in enumerate(items):
            problem += pulp.lpSum(variables[(i, j)] for j, _r, _c, _m in item['candidates']) == 1
        for j in range(m):
            load_expr = pulp.lpSum(weights_u[i] * variables[(i, j)]
                                   for i in range(n) if (i, j) in variables)
            problem += over[j] >= load_expr - caps_u[j]
        if o_star is not None:
            problem += pulp.lpSum(over) <= int(o_star)
        if cost_objective:
            problem += pulp.lpSum(cost * variables[(i, j)]
                                  for i, item in enumerate(items)
                                  for j, _r, cost, _m in item['candidates'])
        else:
            problem += pulp.lpSum(over)
        return problem, variables, over

    def solve_and_read(problem, budget):
        log_file = tempfile.NamedTemporaryFile(prefix='cbc-', suffix='.log', delete=False)
        log_path = log_file.name
        log_file.close()
        started = time.monotonic()
        try:
            # 构造与求解都可能因 CBC 二进制/动态库缺失而失败，统一交由上层回退。
            solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=max(0.05, float(budget)),
                                       threads=1, gapRel=0.0, gapAbs=0.0, logPath=log_path)
            problem.solve(solver)
        except Exception:
            return None
        finally:
            try:
                with open(log_path, encoding='utf-8', errors='ignore') as handle:
                    log_text = handle.read()
            except OSError:
                log_text = ''
            with contextlib.suppress(OSError):
                os.unlink(log_path)
        proven = 'Result - Optimal solution found' in log_text
        stopped = 'Stopped on time' in log_text or 'Exiting on maximum time' in log_text
        return proven, stopped, time.monotonic() - started

    def extract(variables):
        assignment = {}
        for i, item in enumerate(items):
            chosen = None
            for j, _r, _c, _m in item['candidates']:
                value = pulp.value(variables.get((i, j)))
                if value is not None and value > 0.5:
                    chosen = j
                    break
            if chosen is None:
                return None
            assignment[i] = chosen
        return assignment

    started_all = time.monotonic()
    phase1, variables1, over1 = build_model(False)
    status1 = solve_and_read(phase1, time_limit)
    if status1 is None:
        return None
    proven1, stopped1, elapsed1 = status1
    assignment = extract(variables1)
    if assignment is None:
        return None

    def reconcile(assign):
        loads = [Fraction(0)] * m
        cost = Fraction(0)
        for i, j in assign.items():
            loads[j] += items[i]['weight']
            cost += items[i]['costs_by_shop'][j]
        overload = sum((max(Fraction(0), load - cap) for load, cap in zip(loads, caps)), Fraction(0))
        return loads, overload, cost

    loads, overload, cost = reconcile(assignment)
    overload_u = int(overload * scale)

    # 阶段一已证实最小超容后，阶段二在该约束下求最低耗水。
    remaining = float(time_limit) - (time.monotonic() - started_all)
    proven2 = False
    if proven1 and remaining > 0.02:
        phase2, variables2, _over2 = build_model(True, o_star=overload_u)
        status2 = solve_and_read(phase2, remaining)
        if status2 is not None:
            proven2, _stopped2, _elapsed2 = status2
            assignment2 = extract(variables2)
            if assignment2 is not None:
                loads2, overload2, cost2 = reconcile(assignment2)
                # 只接受主目标确实等于已证实最小超容的解。
                if int(overload2 * scale) == overload_u and cost2 <= cost:
                    assignment, loads, overload, cost = assignment2, loads2, overload2, cost2
                if proven2 and int(overload2 * scale) != overload_u:
                    proven2 = False

    full_proven = bool(proven1 and proven2)
    return {'assignment': assignment, 'loads': loads, 'overload': overload, 'cost': cost,
            'proven': full_proven, 'timed_out': not full_proven,
            'expansion_proven': bool(proven1), 'nodes': None,
            'elapsed': time.monotonic() - started_all, 'solver': 'cbc'}


def solve_assignment(items, shops, caps, *, time_limit, node_limit=None, solver_preference='auto'):
    """统一求解入口：CBC 优先（可用时），自研分支定界兜底并交叉择优。"""
    if not items:
        return exact_assign(items, shops, caps, time_limit=time_limit)
    zero_budget = time_limit is not None and float(time_limit) <= 0
    if zero_budget or node_limit is not None:
        return exact_assign(items, shops, caps, time_limit=time_limit, node_limit=node_limit)

    bb_greedy = exact_assign(items, shops, caps, time_limit=0)
    candidates = [bb_greedy]
    cbc_result = None
    if solver_preference != 'branch_bound':
        try:
            # CBC 仅为加速后端；任何意外都不得影响排坊主用例，分支定界兜底。
            cbc_result = cbc_assign(items, shops, caps, time_limit=time_limit)
        except Exception:
            cbc_result = None
    if cbc_result is not None:
        candidates.append(cbc_result)

    proven = [candidate for candidate in candidates if candidate.get('proven')]
    if proven:
        best = min(proven, key=lambda c: (c['overload'], c['cost']))
        best['proven'] = True
        best['timed_out'] = False
        return best

    # 都未证实：用剩余预算让分支定界正式搜一遍（CBC 不可用时这是主路径）。
    elapsed = max((c.get('elapsed', 0.0) for c in candidates), default=0.0)
    bb_budget = max(0.0, float(time_limit) - elapsed) if cbc_result is not None else time_limit
    if cbc_result is None or bb_budget >= 0.05:
        bb_full = exact_assign(items, shops, caps,
                               time_limit=bb_budget if cbc_result is not None else time_limit)
        candidates.append(bb_full)
        if bb_full.get('proven'):
            return bb_full

    best = min(candidates, key=lambda c: (c['overload'], c['cost']))
    best['proven'] = False
    best['timed_out'] = True
    best.setdefault('expansion_proven', False)
    if cbc_result is not None and cbc_result.get('expansion_proven'):
        best['expansion_proven'] = True
    return best


# ---------------------------------------------------------------------------
# 默认演示版「明日批次」与各坊限重
# ---------------------------------------------------------------------------

def default_plan_batches():
    return [
        {'batch_id': 'MP-2601', 'textile_class': '棉质床品', 'weight_kg': 120},
        {'batch_id': 'MP-2602', 'textile_class': '棉质床品', 'weight_kg': 135},
        {'batch_id': 'MP-2603', 'textile_class': '棉质床品', 'weight_kg': 110},
        {'batch_id': 'MP-2604', 'textile_class': '棉质床品', 'weight_kg': 145},
        {'batch_id': 'MP-2605', 'textile_class': '厚绒浴巾', 'weight_kg': 150},
        {'batch_id': 'MP-2606', 'textile_class': '厚绒浴巾', 'weight_kg': 165},
        {'batch_id': 'MP-2607', 'textile_class': '厚绒浴巾', 'weight_kg': 140},
        {'batch_id': 'MP-2608', 'textile_class': '涤棉制服', 'weight_kg': 130},
        {'batch_id': 'MP-2609', 'textile_class': '涤棉制服', 'weight_kg': 125},
        {'batch_id': 'MP-2610', 'textile_class': '涤棉制服', 'weight_kg': 155},
        {'batch_id': 'MP-2611', 'textile_class': '涤棉制服', 'weight_kg': 118},
        {'batch_id': 'MP-2612', 'textile_class': '防水护理垫', 'weight_kg': 160},
        {'batch_id': 'MP-2613', 'textile_class': '防水护理垫', 'weight_kg': 148},
        {'batch_id': 'MP-2614', 'textile_class': '防水护理垫', 'weight_kg': 142},
        {'batch_id': 'MP-2615', 'textile_class': '防水护理垫', 'weight_kg': 136},
        # 以下两批在历史台账中找不到同类实洗记录，须单列原因、不进入优化。
        {'batch_id': 'MP-2616', 'textile_class': '医用手术巾', 'weight_kg': 122},
        {'batch_id': 'MP-2617', 'textile_class': '抗菌隔帘', 'weight_kg': 96},
    ]


def default_capacities(shops=None):
    base = {'东港工坊': 760, '白榆工坊': 760, '龙桥工坊': 760}
    if shops is None:
        return [{'shop': shop, 'limit_kg': limit} for shop, limit in base.items()]
    return [{'shop': shop, 'limit_kg': base.get(shop, 700)} for shop in shops]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _option(options, *keys, default=None):
    if not isinstance(options, dict):
        return default
    for key in keys:
        if key in options and options[key] is not None:
            return options[key]
    return default


def schedule_workshops(history_rows, plan_batches=None, capacities=None, options=None, **kwargs):
    """排坊主用例。

    参数
    ----
    history_rows : 历史实洗台账记录（domain.json 定义的十种字段）。
    plan_batches : 明日批次，list[dict] 或 CSV 文本；为空时用内置样例。
    capacities : 各坊限重，list[dict] / dict / CSV 文本；为空时用内置样例。
    options : 可含 ``time_limit_seconds``（亦接受 timeout/time_limit）、
        ``node_limit``、``title`` 等。

    返回 JSON 安全的结果 dict；最优性/超时状态见 ``status`` 与
    ``proven_optimal``。
    """
    options = dict(options or {})
    # 兼容不同调用方对第二、三个位置参数的常见命名。
    if plan_batches is None:
        for key in ('batches', 'tomorrow_batches', 'plan', 'plan_rows', 'items'):
            if kwargs.get(key) is not None:
                plan_batches = kwargs[key]
                break
    if capacities is None:
        for key in ('shop_limits', 'limits', 'caps', 'capacity', 'shop_capacities'):
            if kwargs.get(key) is not None:
                capacities = kwargs[key]
                break
    for key in ('time_limit_seconds', 'timeout_seconds', 'time_limit', 'timeout', 'solve_timeout'):
        if key in kwargs and kwargs[key] is not None:
            options.setdefault(key, kwargs[key])
    time_limit = float(_option(options, 'time_limit_seconds', 'timeout_seconds',
                               'time_limit', 'timeout', 'solve_timeout',
                               default=DEFAULT_TIME_LIMIT))
    node_limit = _option(options, 'node_limit', 'max_nodes', default=None)
    if node_limit is not None:
        node_limit = int(node_limit)
    solver_preference = _option(options, 'solver', 'solver_preference', default='auto')

    if isinstance(history_rows, str):
        history_rows = _read_csv(history_rows)
    agg, history_shops, _textiles = aggregate_history(history_rows)

    if plan_batches is None:
        plan_batches = default_plan_batches()
    batches = parse_plan_batches(plan_batches)
    if capacities is None:
        capacities = default_capacities(history_shops)
    cap_map, ignored_capacity_shops = parse_capacities(capacities, history_shops)
    shops = sorted(cap_map)
    caps = [cap_map[shop] for shop in shops]
    shop_index = {shop: i for i, shop in enumerate(shops)}

    items = []
    missing = []
    for batch in batches:
        batch_id, textile, weight = batch['batch_id'], batch['textile_class'], batch['weight']
        eligible = [(shop, agg.get((shop, textile))) for shop in history_shops]
        recorded = [(shop, cell) for shop, cell in eligible if cell is not None and cell['batches'] > 0]
        if not recorded:
            missing.append({
                'batch_id': batch_id, 'textile_class': textile, 'weight_kg': _float(weight),
                'reason': f'布类「{textile}」在历史实洗台账中没有任何工坊的同类实洗记录，无法确认准入工坊与估算单耗。',
            })
            continue
        candidates = []
        zero_weight_shops = []
        costs_by_shop = {}
        for shop, cell in recorded:
            if cell['kg'] <= 0:
                zero_weight_shops.append(shop)
                continue
            rate = cell['liters'] / cell['kg']
            item_cost = weight * rate
            meta = {
                'history_batches': cell['batches'],
                'history_total_liters': cell['liters'],
                'history_total_kg': cell['kg'],
                'rate': rate,
            }
            j = shop_index[shop]
            candidates.append((j, rate, item_cost, meta))
            costs_by_shop[j] = item_cost
        if not candidates:
            missing.append({
                'batch_id': batch_id, 'textile_class': textile, 'weight_kg': _float(weight),
                'reason': (f'布类「{textile}」虽有 {sum(c["batches"] for _s, c in recorded)} 条同类记录，'
                          '但历史总实洗重量为 0，无法用总耗水÷总实洗重量估算单耗。'),
            })
            continue
        item = {
            'batch_id': batch_id, 'textile_class': textile, 'weight': weight,
            'candidates': candidates, 'costs_by_shop': costs_by_shop,
            'zero_weight_shops': zero_weight_shops,
        }
        items.append(item)

    solved = solve_assignment(items, shops, caps, time_limit=time_limit,
                              node_limit=node_limit, solver_preference=solver_preference)

    batch_results = []
    shop_water = [Fraction(0)] * len(shops)
    for item_index, item in enumerate(items):
        j = solved['assignment'].get(item_index)
        chosen = next(candidate for candidate in item['candidates'] if candidate[0] == j)
        _j, rate, item_cost, meta = chosen
        shop_water[j] += item_cost
        alternatives = []
        for aj, arate, acost, ameta in sorted(item['candidates'], key=lambda c: (c[2], c[0])):
            alternatives.append({
                'shop': shops[aj],
                'rate_l_per_kg': _float(arate),
                'estimated_liters': _float(acost),
                'history_batches': ameta['history_batches'],
            })
        batch_results.append({
            'batch_id': item['batch_id'],
            'textile_class': item['textile_class'],
            'weight_kg': _float(item['weight']),
            'shop': shops[j],
            'workshop': shops[j],
            'assigned_shop': shops[j],
            'shop_index': j,
            'rate_l_per_kg': _float(rate),
            'estimated_liters': _float(item_cost),
            'estimated_water_liters': _float(item_cost),
            'water_liters': _float(item_cost),
            'history_batches': meta['history_batches'],
            'history_total_liters': _float(meta['history_total_liters']),
            'history_total_kg': _float(meta['history_total_kg']),
            'eligible_shop_count': len(item['candidates']),
            'alternative_shops': alternatives,
            'weight_exact': str(item['weight']),
        })
    batch_results.sort(key=lambda row: row['batch_id'])

    shop_results = []
    for j, shop in enumerate(shops):
        load = solved['loads'][j]
        cap = caps[j]
        added = max(Fraction(0), load - cap)
        remaining = cap - load
        shop_results.append({
            'shop': shop,
            'workshop': shop,
            'limit_kg': _float(cap),
            'capacity_kg': _float(cap),
            'allocated_kg': _float(load),
            'used_kg': _float(load),
            'remaining_kg': _float(remaining),
            'remaining_capacity_kg': _float(remaining),
            'added_kg': _float(added),
            'capacity_add_kg': _float(added),
            'remaining_after_expansion_kg': _float(max(Fraction(0), remaining)),
            'estimated_liters': _float(shop_water[j]),
        })

    total_weight = sum((item['weight'] for item in items), Fraction(0))
    total_liters = solved['cost']
    total_limit = sum(caps, Fraction(0))
    total_added = solved['overload']
    all_missing = len(items) == 0
    expansion_proven = bool(solved.get('expansion_proven', solved['proven']))
    solver_name = {'cbc': 'CBC 精确求解器', 'branch-and-bound': '确定性分支定界'}.get(
        solved.get('solver'), solved.get('solver', '确定性分支定界'))

    if all_missing:
        status = 'all_missing_history'
        status_label = '无可排批次：全部缺少可用历史'
    elif solved['proven'] and solved['overload'] == 0:
        status = 'optimal'
        status_label = '已证实最优：全批最低估算耗水'
    elif solved['proven'] and solved['overload'] > 0:
        status = 'capacity_shortfall'
        status_label = '现有限重排不下：已求出最小增容方案'
    elif expansion_proven and solved['overload'] > 0:
        status = 'timeout_unproven'
        status_label = '求解超时：最小增容已证实，增容后的最低耗水未证实'
    else:
        status = 'timeout_unproven'
        status_label = '求解超时：方案可行，但最优性未证实'

    feasible = (not all_missing) and solved['overload'] == 0 and bool(solved['proven'])
    feasible_under_limits = (not all_missing) and solved['overload'] == 0

    additions = [{'shop': shops[j], 'add_kg': _float(max(Fraction(0), solved['loads'][j] - caps[j]))}
                 for j in range(len(shops)) if solved['loads'][j] > caps[j]]

    if status == 'optimal':
        headline = (f'明日 {len(batches)} 批中 {len(items)} 批可排，'
                    f'已在各坊限重内求得全批最低估算耗水 {_float(total_liters):g} L（最优性已证实）。')
    elif status == 'capacity_shortfall':
        headline = (f'现有限重下排不下：至少需新增 {_float(total_added):g} kg 容量，'
                    f'加在' + '、'.join(f"{a['shop']} {a['add_kg']:g} kg" for a in additions) + '。')
    elif status == 'timeout_unproven' and expansion_proven and total_added > 0:
        headline = (f'求解超时：最小增容 {_float(total_added):g} kg 已证实，'
                    '但增容方案的耗水最优性未证实；以下去向与用水仅为当前可行方案，不得当作最低耗水。')
    elif status == 'timeout_unproven':
        headline = (f'求解在 {time_limit:g} 秒内超时，未证实最优；当前方案各批可去向、'
                    '容量账如下，但不得作为「最低耗水/最小增容」结论。')
    else:
        headline = f'明日 {len(batches)} 批全部缺少可用历史记录，已单列原因，不能排坊。'

    signature_source = json.dumps(
        {row['batch_id']: [row['shop'], row['weight_exact']] for row in batch_results},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    signature = hashlib.sha256(signature_source.encode()).hexdigest()[:16]

    total_load = sum(solved['loads'], Fraction(0))
    metrics = {
        '明日批次总数': len(batches),
        '可排批次': len(items),
        '缺历史批次': len(missing),
        '已排总重量（kg）': _float(total_weight),
        '估算总耗水（L）': None if all_missing else _float(total_liters),
        '各坊限重合计（kg）': _float(total_limit),
        '剩余容量合计（kg）': _float(total_limit - total_load),
        '至少新增容量（kg）': (_float(total_added) if expansion_proven and total_added > 0
                              else (0 if solved['proven'] and total_added == 0 else None)),
        '求解状态': status_label,
        '求解器': solver_name,
        '搜索节点数': solved.get('nodes') if solved.get('nodes') is not None else '—（MILP）',
        '求解耗时（秒）': round(solved.get('elapsed', 0.0), 4),
    }

    notes = [
        '整批不能拆：每个明日批次只分配给一个工坊，批次重量整批计入该坊限重，不存在半批分流。',
        '准入规则：每种布只去有同类实洗记录（shop × textile_class）的工坊。',
        '估算口径：工坊×布类单耗 = 历史总实际耗水（actual_liters 之和）÷ 历史总实洗重量（actual_kg 之和）；'
        '本批估算用水 = 批次重量 × 该加权单耗，不是各批 L/kg 的算术平均。',
        '容量核对：剩余容量 = 限重 − 已排整批重量；负数表示该坊超容。',
        '排不下时，先精确最小化 Σ 坊 max(0, 已排重量−限重)，该值即全系统至少要新增的容量；'
        '再在最小增容方案中最小化估算总耗水，并给出应加容的工坊与数量。',
        '同一数据版本、同一明日批次与限重重跑，确定性求解与固定排序保证各批去向一致。',
    ]
    if solved['timed_out']:
        notes.append('本次求解超时，最优性未证实：页面中的耗水与增容数字仅是当前找到的可行方案，'
                     '不能称为最低耗水或最小增容；请放宽求解时限或缩小批次规模后重跑。')
    elif solved['proven'] and solved.get('solver') == 'cbc':
        notes.append('CBC 已完成两阶段精确求解并返回 Optimal：先证实最小新增容量，再证实该约束下耗水最低。')
    elif solved['proven']:
        notes.append(f'分支定界已穷举并剪枝完成（{solved.get("nodes")} 个节点），最低耗水/最小增容结论已证实。')
    if ignored_capacity_shops:
        notes.append('限重中的工坊「' + '、'.join(ignored_capacity_shops)
                     + '」不在历史实洗台账内，没有任何同类实洗记录，已忽略其限重。')

    batch_rows = [{
        '批号': row['batch_id'], '布类': row['textile_class'],
        '批次重量（kg）': row['weight_kg'], '去向工坊': row['shop'],
        '估算单耗（L/kg）': row['rate_l_per_kg'], '估算用水（L）': row['estimated_liters'],
        '该坊同类历史批数': row['history_batches'],
        '历史总耗水（L）': row['history_total_liters'],
        '历史总实洗（kg）': row['history_total_kg'],
        '可去工坊数': row['eligible_shop_count'],
    } for row in batch_results]
    shop_rows = [{
        '工坊': row['shop'], '限重（kg）': row['limit_kg'],
        '已排重量（kg）': row['allocated_kg'], '剩余容量（kg）': row['remaining_kg'],
        '建议新增（kg）': row['added_kg'], '增容后剩余（kg）': row['remaining_after_expansion_kg'],
        '该坊估算用水（L）': row['estimated_liters'],
    } for row in shop_results]
    missing_rows = [{
        '批号': row['batch_id'], '布类': row['textile_class'],
        '批次重量（kg）': row['weight_kg'], '原因': row['reason'],
    } for row in missing]

    proven_optimal = bool(solved['proven'] and not solved['timed_out'] and solved['overload'] == 0)
    return _json_safe({
        'kind': 'workshop_schedule',
        'headline': headline,
        'status': status,
        'status_label': status_label,
        # 常见布尔命名别名，便于不同调用方核对「是否已证实最优」。
        'optimal': proven_optimal,
        'is_optimal': proven_optimal,
        'proven_optimal': proven_optimal,
        'feasible_under_limits': feasible_under_limits,
        'feasible': feasible,
        'expansion_proven': expansion_proven,
        'solver': solved.get('solver', 'branch-and-bound'),
        'timed_out': bool(solved['timed_out']),
        'timeout': bool(solved['timed_out']),
        'time_limit_seconds': time_limit,
        'solve_seconds': round(solved.get('elapsed', 0.0), 4),
        'nodes': solved.get('nodes'),
        'signature': signature,
        # 顶层便捷别名。
        'assignments': batch_results,
        'missing_history': missing,
        'capacity_additions': additions,
        'minimum_added_kg': _float(total_added) if expansion_proven else None,
        'metrics': metrics,
        'metric_order': list(metrics),
        'totals': {
            'batch_count': len(batches),
            'scheduled_count': len(items),
            'missing_count': len(missing),
            'scheduled_weight_kg': _float(total_weight),
            'estimated_liters': _float(total_liters) if not all_missing else None,
            'limit_kg_total': _float(total_limit),
            'minimum_added_kg': _float(total_added) if expansion_proven else None,
            'proposed_added_kg': _float(total_added),
        },
        'shops': shop_results,
        'batches': batch_results,
        'missing': missing,
        'capacity_advice': {
            'proven': expansion_proven,
            'cost_proven_under_min_expansion': bool(solved['proven'] and solved['overload'] > 0),
            'minimum_added_kg': _float(total_added) if expansion_proven else None,
            'proposed_added_kg': _float(total_added),
            'additions': additions,
            'text': headline if status == 'capacity_shortfall' else '',
        },
        'history_rates': history_rate_rows(history_rows),
        'notes': notes,
        'rows': batch_rows,
        'shop_rows': shop_rows,
        'missing_rows': missing_rows,
        'columns': list(batch_rows[0]) if batch_rows else ['批号'],
    })


# 便于按常见命名导入与发现。
compute_schedule = schedule_workshops
schedule_batches = schedule_workshops
plan_workshops = schedule_workshops
arrange_batches = schedule_workshops
optimize_workshop_assignment = schedule_workshops
solve_workshop_allocation = schedule_workshops
