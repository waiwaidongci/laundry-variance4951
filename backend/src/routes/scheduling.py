"""明日批次排坊的 HTTP 接口：默认样例、历史单耗、排坊运行与导出。"""

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import insert, select

from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, scheduling_runs
from src.services.domain import sample_rows
from src.services.scheduling import (
    DEFAULT_TIME_LIMIT,
    default_capacities,
    default_plan_batches,
    history_rate_rows,
    schedule_workshops,
)
from src.utils.exports import write_csv
from src.utils.security import signed_in

router = APIRouter()


class ScheduleBody(BaseModel):
    dataset_id: int | None = Field(default=None, gt=0)
    history: list[dict[str, Any]] | None = None
    plan: Any = None
    plan_csv: str | None = None
    capacities: Any = None
    options: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(default=None, max_length=180)
    # 允许把求解时限放在请求顶层，兼容不同调用习惯。
    time_limit_seconds: float | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=0)
    timeout: float | None = Field(default=None, ge=0)


def _csv_response(data, name):
    from urllib.parse import quote
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(name)})


def _resolve_history(body: ScheduleBody):
    """返回 (history_rows, dataset_id, dataset_checksum, source_label)。"""
    if body.history is not None:
        if not body.history:
            raise ValueError('直接传入的历史台账不能为空。')
        return body.history, body.dataset_id, None, 'inline_history'
    dataset_id = body.dataset_id
    with engine.connect() as connection:
        if dataset_id is None:
            row = connection.execute(select(datasets).order_by(datasets.c.id.desc()).limit(1)).mappings().one_or_none()
        else:
            row = fetch_one(connection, datasets, dataset_id)
            if row is None:
                raise HTTPException(404, '历史数据版本不存在。')
    if row is None:
        # 全新未 seed 的环境也能打开页面核对口径：使用与 seed 相同的内置样例。
        return sample_rows(), None, None, 'built_in_sample'
    return row['records'], row['id'], row['checksum'], 'dataset'


@router.get('/scheduling/defaults')
def scheduling_defaults(account=Depends(signed_in)):
    batches = default_plan_batches()
    shops = sorted({row['shop'] for row in sample_rows()})
    capacities = default_capacities(shops)
    plan_columns = ['batch_id', 'textile_class', 'weight_kg']
    plan_csv = write_csv(batches, plan_columns).decode('utf-8-sig')
    cap_csv = write_csv(capacities, ['shop', 'limit_kg']).decode('utf-8-sig')
    return {
        'plan_name': '明日洗涤批次计划（样例）',
        'batches': batches,
        'capacities': capacities,
        'plan_csv': plan_csv,
        'capacities_csv': cap_csv,
        'time_limit_seconds': DEFAULT_TIME_LIMIT,
    }


@router.get('/scheduling/history-meta')
def history_meta(dataset_id: int | None = None, account=Depends(signed_in)):
    body = ScheduleBody(dataset_id=dataset_id)
    rows, resolved_id, checksum, source = _resolve_history(body)
    rates = history_rate_rows(rows)
    shops = sorted({row['shop'] for row in rows if isinstance(row, dict) and row.get('shop')})
    return {'dataset_id': resolved_id, 'source': source, 'checksum': checksum,
            'shops': shops, 'rates': rates}


def _run_summary(row):
    result = row['result'] or {}
    return {
        'id': row['id'],
        'dataset_id': row['dataset_id'],
        'plan_name': row['plan_name'],
        'state': row['state'],
        'owner': row['owner'],
        'created': row['created'],
        'status': result.get('status'),
        'status_label': result.get('status_label'),
        'headline': result.get('headline'),
        'signature': result.get('signature'),
        'proven_optimal': result.get('proven_optimal'),
        'timed_out': result.get('timed_out'),
        'totals': result.get('totals'),
    }


@router.get('/scheduling/runs')
def list_schedule_runs(account=Depends(signed_in)):
    with engine.connect() as connection:
        rows = connection.execute(select(scheduling_runs).order_by(
            scheduling_runs.c.id.desc()).limit(50)).mappings().all()
    return [_run_summary(dict(row)) for row in rows]


@router.get('/scheduling/runs/{key}')
def get_schedule_run(key: int, account=Depends(signed_in)):
    with engine.connect() as connection:
        row = fetch_one(connection, scheduling_runs, key)
    if row is None:
        raise HTTPException(404, '排坊版本不存在。')
    return dict(row)


@router.post('/scheduling/runs', status_code=201)
@router.post('/scheduling/run', status_code=201)
@router.post('/scheduling', status_code=201)
def create_schedule_run(body: ScheduleBody, account=Depends(signed_in)):
    history, dataset_id, dataset_checksum, source = _resolve_history(body)
    plan = body.plan_csv if (body.plan is None and body.plan_csv) else body.plan
    options = dict(body.options or {})
    for key, value in (('time_limit_seconds', body.time_limit_seconds),
                       ('timeout_seconds', body.timeout_seconds),
                       ('timeout', body.timeout)):
        if value is not None:
            options.setdefault(key, value)

    # 先计算，ValueError 由全局处理器转成 422 中文提示。
    result = schedule_workshops(history, plan_batches=plan,
                                capacities=body.capacities, options=options)
    result['history_source'] = source
    if dataset_checksum:
        result['history_checksum'] = dataset_checksum

    request_snapshot = {
        'dataset_id': dataset_id,
        'plan': body.plan,
        'plan_csv': body.plan_csv,
        'capacities': body.capacities,
        'options': options,
        'name': body.name,
    }
    checksum = hashlib.sha256(json.dumps(
        {'request': request_snapshot, 'history_checksum': dataset_checksum},
        ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':')).encode()
    ).hexdigest()

    now = time.time()
    with engine.begin() as connection:
        key = connection.execute(insert(scheduling_runs).values(
            dataset_id=dataset_id, plan_name=(body.name or '明日排坊方案')[:180],
            checksum=checksum, request=request_snapshot, result=result,
            state='completed', owner=account['username'], created=now, finished=time.time())
        ).inserted_primary_key[0]
        add_event(connection, account['username'], 'schedule_run', key, now)
    return {'id': key, **result}


@router.get('/scheduling/runs/{key}/csv')
def schedule_run_csv(key: int, kind: str = 'batches', account=Depends(signed_in)):
    with engine.connect() as connection:
        row = fetch_one(connection, scheduling_runs, key)
    if row is None:
        raise HTTPException(404, '排坊版本不存在。')
    result = row['result'] or {}
    empty_headers = {
        'shops': ['工坊', '限重（kg）', '已排重量（kg）', '剩余容量（kg）',
                  '建议新增（kg）', '增容后剩余（kg）', '该坊估算用水（L）'],
        'missing': ['批号', '布类', '批次重量（kg）', '原因'],
        'batches': list((result.get('columns') or ['批号'])),
    }
    if kind == 'shops':
        rows, suffix = result.get('shop_rows', []), '工坊容量'
    elif kind == 'missing':
        rows, suffix = result.get('missing_rows', []), '缺历史批次'
    else:
        kind = 'batches'
        rows, suffix = result.get('rows', []), '逐批去向'
    columns = list(rows[0]) if rows else empty_headers[kind]
    return _csv_response(write_csv(rows, columns), f"明日排坊-{suffix}-{key}.csv")
