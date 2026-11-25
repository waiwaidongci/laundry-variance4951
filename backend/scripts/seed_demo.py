import json
import time

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from src.constants.enums import Role
from src.models.database import engine
from src.models.tables import datasets, metadata, scheduling_runs, users
from src.services.data import PROFILE, convert, save_revision
from src.services.domain import sample_rows
from src.services.scheduling import schedule_workshops
from src.utils.security import protect


def seed():
    # 允许在未启动 API（尚未经过 lifespan create_all）时直接运行种子脚本。
    metadata.create_all(engine)
    for role in Role:
        try:
            with engine.begin() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    connection.execute(insert(users).values(username=role.value, role=role.value,
                                                            password=protect(f'{role.value}-demo-24')))
        except IntegrityError:
            with engine.connect() as connection:
                if not connection.scalar(select(users.c.username).where(users.c.username == role.value)):
                    raise
    records = [{field['key']: convert(row[field['key']], field) for field in PROFILE['columns']} for row in sample_rows()]
    revision, created = save_revision(PROFILE['filename'], records, 'seed')
    schedule_seed = seed_schedule_run(revision, records)
    with engine.connect() as connection:
        counts = {table.name: connection.scalar(select(func.count()).select_from(table)) for table in (users, datasets)}
    return {'revision': revision, 'created': created, 'schedule': schedule_seed,
            **counts, 'rows': len(records)}


def seed_schedule_run(dataset_id, records):
    """落一份与页面默认表单完全一致的已证实排坊方案，便于首次打开即核对。"""
    options = {'time_limit_seconds': 10, 'seeded': True}
    result = schedule_workshops(records, options=options)
    checksum = result['signature']
    now = time.time()
    request_snapshot = {'dataset_id': dataset_id, 'plan': None, 'plan_csv': None,
                        'capacities': None, 'options': options, 'name': '明日排坊默认方案'}
    with engine.begin() as connection:
        existing = connection.scalar(select(scheduling_runs.c.id).where(
            scheduling_runs.c.checksum == checksum).limit(1))
        if existing:
            return {'id': existing, 'created': False}
        key = connection.execute(insert(scheduling_runs).values(
            dataset_id=dataset_id, plan_name='明日排坊默认方案', checksum=checksum,
            request=request_snapshot, result=result, state='completed', owner='seed',
            created=now, finished=now)).inserted_primary_key[0]
    return {'id': key, 'created': True}


if __name__ == '__main__':
    print(json.dumps(seed(), ensure_ascii=False))
