import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {display,esc, time} from '../utils/html.js';

const batchColumns = [
  {key: 'batch_id', label: '批号'},
  {key: 'textile_class', label: '布类'},
  {key: 'weight_kg', label: '批次重量（kg）'},
  {key: 'shop', label: '去向工坊'},
  {key: 'rate_l_per_kg', label: '估算单耗（L/kg）'},
  {key: 'estimated_liters', label: '本批估算用水（L）'},
  {key: 'history_batches', label: '该坊同类实洗批数'},
  {key: 'history_total_liters', label: '历史总耗水（L）'},
  {key: 'history_total_kg', label: '历史总实洗重量（kg）'},
  {key: 'eligible_shop_count', label: '可去工坊数'},
];

const shopColumns = [
  {key: 'shop', label: '工坊'},
  {key: 'limit_kg', label: '限重（kg）'},
  {key: 'allocated_kg', label: '已排整批重量（kg）'},
  {key: 'remaining_kg', label: '剩余容量（kg）'},
  {key: 'added_kg', label: '建议新增容量（kg）'},
  {key: 'remaining_after_expansion_kg', label: '增容后剩余（kg）'},
  {key: 'estimated_liters', label: '该坊估算用水（L）'},
];

const missingColumns = [
  {key: 'batch_id', label: '批号'},
  {key: 'textile_class', label: '布类'},
  {key: 'weight_kg', label: '批次重量（kg）'},
  {key: 'reason', label: '无法排坊的原因'},
];

const rateColumns = [
  {key: 'shop', label: '工坊'},
  {key: 'textile_class', label: '布类'},
  {key: 'history_batches', label: '实洗批数'},
  {key: 'history_total_kg', label: '历史总实洗（kg）'},
  {key: 'history_total_liters', label: '历史总耗水（L）'},
  {key: 'rate_l_per_kg', label: '加权单耗（L/kg）'},
];

function chooseScheduleRun(runs, selected) {
  return `<label>排坊版本<select id="pick-schedule">${runs.map(row => `<option value="${row.id}" ${selected === row.id ? 'selected' : ''}>#${row.id} · ${esc(row.status_label || row.status || '')} · ${time(row.created)}</option>`).join('')}</select></label>`;
}

function statusBanner(result) {
  if (result.status === 'optimal') {
    return `<p class="notice schedule-banner" data-schedule-status="optimal" role="status"><strong>${esc(result.status_label)}</strong><br>${esc(result.headline)}</p>`;
  }
  if (result.status === 'capacity_shortfall') {
    const additions = result.capacity_advice?.additions || [];
    return `<p class="notice schedule-banner" data-schedule-status="capacity_shortfall" role="warning"><strong>${esc(result.status_label)}</strong><br>${esc(result.headline)}<br><span class="hint">增容明细：${additions.map(a => `${esc(a.shop)} +${a.add_kg} kg`).join('；')}</span></p>`;
  }
  if (result.status === 'timeout_unproven') {
    return `<p class="error schedule-banner" data-schedule-status="timeout" role="alert"><strong>⚠ ${esc(result.status_label)}</strong><br>${esc(result.headline)}</p>`;
  }
  return `<p class="notice schedule-banner" data-schedule-status="all_missing" role="status"><strong>${esc(result.status_label)}</strong><br>${esc(result.headline)}</p>`;
}

export function scheduleResult(run) {
  const result = run.result;
  if (!result) return `<p class="error">该版本没有排坊结果。</p>`;
  const metrics = result.metric_order?.length ? result.metric_order : Object.keys(result.metrics || {});
  const metricValue = key => {
    const value = result.metrics?.[key];
    if (value === null || value === undefined) return '未证实';
    return typeof value === 'number' ? display(value) : value;
  };
  return `<div class="reading schedule-result">
    ${statusBanner(result)}
    <dl class="metrics">${metrics.map(key => `<div><dt>${esc(key)}</dt><dd>${esc(metricValue(key))}</dd></div>`).join('')}</dl>

    <div class="schedule-actions">
      <button type="button" class="secondary" id="sch-rerun" data-id="${run.id}">同版重跑并核对去向</button>
      <button type="button" class="secondary" data-download="batches" data-id="${run.id}">下载逐批去向 CSV</button>
      <button type="button" class="secondary" data-download="shops" data-id="${run.id}">下载工坊容量 CSV</button>
      <button type="button" class="secondary" data-download="missing" data-id="${run.id}">下载缺历史批次 CSV</button>
      <p id="sch-consistency" role="status" aria-live="polite"></p>
      <p id="sch-download-error" class="error" role="alert"></p>
    </div>

    <h3>各工坊限重与剩余容量</h3>
    <p class="hint">剩余容量 = 限重 − 已排整批重量；为负表示按当前方案超容，需按「建议新增容量」扩限。</p>
    <div id="sch-shop-table" data-testid="shop-capacity-table">${table(result.shops || [], shopColumns)}</div>

    <h3>逐批核对去向和估算用水</h3>
    <p class="hint">每批整批只去一个工坊；估算用水 = 批次重量 ×（该坊该布历史总耗水 ÷ 历史总实洗重量）。</p>
    <div id="sch-batch-table" data-testid="batch-assignment-table">${table(result.batches || [], batchColumns)}</div>

    <h3>缺历史批次（单列原因，不参与排坊）</h3>
    <div id="sch-missing-table" data-testid="missing-history-table">${table(result.missing || [], missingColumns)}</div>

    <details class="rate-details"><summary>核对「工坊 × 布类」历史总耗水 / 总实洗与加权单耗</summary>${table(result.history_rates || [], rateColumns)}</details>

    <h3>口径与限制</h3>
    <ul>${(result.notes || []).map(note => `<li>${esc(note)}</li>`).join('')}</ul>
  </div>`;
}

function capacityInputs(capacities) {
  return `<div class="cap-grid" id="sch-cap-inputs">${(capacities || []).map(item => `
    <label>${esc(item.shop)} 限重（kg）
      <input class="sch-cap-limit" data-shop="${esc(item.shop)}" type="number" min="0" step="any" value="${esc(item.limit_kg)}" required>
    </label>`).join('')}</div>`;
}

export async function scheduling() {
  const [datasets, runs, defaults] = await Promise.all([
    api('/datasets'),
    api('/scheduling/runs').catch(() => []),
    api('/scheduling/defaults'),
  ]);
  const chosenId = state.runId && runs.some(row => row.id === state.runId) ? state.runId : runs[0]?.id;
  const run = chosenId ? await api(`/scheduling/runs/${chosenId}`) : null;
  const selectedDataset = run?.dataset_id || datasets[0]?.id || '';

  const form = `<section>
    <h2>排坊条件</h2>
    <p>选择历史实洗台账版本（用水估算与同类记录准入的唯一依据），确认或粘贴明日批次 CSV（列：batch_id,textile_class,weight_kg），并填写各坊明日限重。原则：整批不能拆。</p>
    <form id="sch-form">
      <label>历史台账版本
        <select name="dataset_id" id="sch-dataset">
          <option value="">内置样例历史（不落数据版本）</option>
          ${datasets.map(row => `<option value="${row.id}" ${String(row.id) === String(selectedDataset) ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}
        </select>
      </label>
      <div class="sch-grid">
        <label>明日批次 CSV（批号,布类,重量 kg）
          <textarea name="plan_csv" id="sch-plan" rows="9">${esc(defaults.plan_csv)}</textarea>
        </label>
        <div>
          <p class="hint">各坊限重（切换历史版本后按其中工坊重建）</p>
          ${capacityInputs(defaults.capacities)}
          <label>求解时限（秒；填 0 只给可行方案并明确不证实最优）
            <input name="time_limit_seconds" id="sch-timeout" type="number" min="0" step="0.5" value="${esc(defaults.time_limit_seconds)}">
          </label>
          <div class="schedule-form-buttons">
            <button type="submit" id="sch-submit">生成排坊方案</button>
            <button type="button" class="secondary" id="sch-shortfall-demo">填入紧张限重示例（各 300 kg，演示增容建议）</button>
          </div>
        </div>
      </div>
      ${errorSlot}
    </form>
  </section>`;

  const history = runs.length ? `<section>${chooseScheduleRun(runs, run?.id)}<p class="hint">排坊版本保存输入快照与结果；打开任一版本可逐批复核。</p></section>` : '';
  const resultSection = run ? `<section>${scheduleResult(run)}</section>`
    : `<section>${empty('提交排坊条件后，这里给出逐批去向、估算用水与工坊剩余容量。')}</section>`;
  return `${form}${history}${resultSection}`;
}

function collectCapacities(root) {
  return [...root.querySelectorAll('.sch-cap-limit')].map(input => ({
    shop: input.dataset.shop,
    limit_kg: Number(input.value),
  }));
}

function assignmentMap(result) {
  return Object.fromEntries((result?.batches || []).map(row => [row.batch_id, {
    shop: row.shop, liters: row.estimated_liters, weight: row.weight_kg,
  }]));
}

export function bindScheduling(root, refresh) {
  bindForm(root, '#sch-form', async form => {
    const datasetValue = form.elements.dataset_id.value;
    const payload = {
      dataset_id: datasetValue ? Number(datasetValue) : null,
      plan_csv: form.elements.plan_csv.value,
      capacities: collectCapacities(root),
      options: {time_limit_seconds: Number(form.elements.time_limit_seconds.value)},
      name: '明日排坊方案',
    };
    const created = await api('/scheduling/runs', payload);
    state.scheduleJustCreated = created.id;
    location.hash = `6/${created.id}`;
  });

  root.querySelector('#pick-schedule')?.addEventListener('change', event => {
    location.hash = `6/${event.target.value}`;
  });

  root.querySelector('#sch-dataset')?.addEventListener('change', async event => {
    const datasetId = event.target.value;
    try {
      const meta = await api(`/scheduling/history-meta${datasetId ? `?dataset_id=${datasetId}` : ''}`);
      const defaults = await api('/scheduling/defaults');
      const limits = Object.fromEntries(defaults.capacities.map(item => [item.shop, item.limit_kg]));
      const container = root.querySelector('#sch-cap-inputs');
      if (container) {
        container.innerHTML = meta.shops.map(shop => `
          <label>${esc(shop)} 限重（kg）
            <input class="sch-cap-limit" data-shop="${esc(shop)}" type="number" min="0" step="any" value="${limits[shop] ?? 700}" required>
          </label>`).join('');
      }
    } catch (error) {
      const slot = root.querySelector('#sch-form [data-error]');
      if (slot) slot.textContent = error.message;
    }
  });

  root.querySelector('#sch-shortfall-demo')?.addEventListener('click', () => {
    root.querySelectorAll('.sch-cap-limit').forEach(input => { input.value = '300'; });
    root.querySelector('#sch-timeout').value = '10';
    root.querySelector('#sch-form')?.requestSubmit();
  });

  root.querySelector('#sch-rerun')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const slot = root.querySelector('#sch-consistency');
    button.disabled = true;
    try {
      const run = await api(`/scheduling/runs/${button.dataset.id}`);
      const payload = run.request || {};
      const again = await api('/scheduling/runs', {
        dataset_id: payload.dataset_id ?? run.dataset_id,
        plan: payload.plan ?? null,
        plan_csv: payload.plan_csv ?? null,
        capacities: payload.capacities ?? null,
        options: {...(payload.options || {}), rerun: true},
        name: payload.name || '同版重跑',
      });
      const before = assignmentMap(run.result);
      const after = assignmentMap(again);
      const ids = new Set([...Object.keys(before), ...Object.keys(after)]);
      const differences = [...ids].filter(id => before[id]?.shop !== after[id]?.shop
        || Math.abs((before[id]?.liters ?? 0) - (after[id]?.liters ?? 0)) > 1e-6);
      if (!differences.length && run.result?.signature === again.signature) {
        slot.className = 'notice';
        slot.setAttribute('role', 'status');
        slot.textContent = `同版重跑一致：${ids.size} 个可排批次的去向与估算用水完全相同（签名 ${again.signature}）。`;
      } else if (!differences.length) {
        slot.className = 'notice';
        slot.textContent = `同版重跑一致：${ids.size} 批去向与估算用水相同（结果签名 ${again.signature}）。`;
      } else {
        slot.className = 'error';
        slot.setAttribute('role', 'alert');
        slot.textContent = `重跑后有 ${differences.length} 批去向或用水不一致：${differences.slice(0, 5).join('、')}。`;
      }
    } catch (error) {
      slot.className = 'error';
      slot.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  root.querySelectorAll('[data-download]').forEach(button => {
    button.addEventListener('click', async () => {
      const errorSlotElement = root.querySelector('#sch-download-error');
      if (errorSlotElement) errorSlotElement.textContent = '';
      try {
        await download(`/scheduling/runs/${button.dataset.id}/csv?kind=${button.dataset.download}`,
          `明日排坊-${button.dataset.download}-${button.dataset.id}.csv`);
      } catch (error) {
        if (errorSlotElement) errorSlotElement.textContent = error.message;
      }
    });
  });
}
