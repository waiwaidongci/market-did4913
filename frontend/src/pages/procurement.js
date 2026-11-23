import {feedback} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {PLAN_STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date} from '../utils/format.js';

const money = cents => `¥${(cents / 100).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;

function catalogCsv(template) {
  const rows = [
    ['A01', '十月补档 南桥', '120', '南桥', '2026-10-26'],
    ['A01', '十月补档 南桥', '120', '南桥', '2026-10-27'],
    ['B02', '十月补档 新街', '80', '新街', '2026-10-28'],
  ];
  return [template.map(item => item.key).join(','), ...rows.map(row => row.join(','))].join('\n') + '\n';
}

function fieldRow(config, current) {
  const attrs = {name: config.key, required: config.required !== false, type: config.type,
    step: config.step, min: config.min, max: config.max, value: current ?? config.default};
  return el('label', {class: 'field'}, el('span', {}, config.label), el('input', attrs));
}

function replayRow(procurement, datasets, runs, seed) {
  const completed = runs.filter(run => run.status === 'complete');
  const runSelect = el('select', {name: 'run_id'},
    el('option', {value: ''}, '（不关联，手填窗口）'),
    completed.map(run => el('option', {value: run.id}, `${run.name} · ${run.created_at?.slice(0, 10) || ''}`)));
  const pilotInput = fieldRow({key: 'pilot_date', label: '原窗口启动日', type: 'date', required: false, default: '2026-11-09'});
  const windowInput = fieldRow({key: 'window_days', label: '每段天数', type: 'number', required: false, min: 1, max: 28, step: 1, default: 7});
  const datasetSelect = el('select', {name: 'dataset_id', required: true},
    datasets.map(item => el('option', {value: item.id}, `${item.name} · ${item.row_count} 行`)));
  runSelect.addEventListener('change', async () => {
    if (!runSelect.value) return;
    try {
      const run = await workspace.getRun(Number(runSelect.value));
      datasetSelect.value = String(run.dataset_id);
      pilotInput.querySelector('input').value = run.params?.pilot_date || '';
      windowInput.querySelector('input').value = run.params?.window_days || 7;
    } catch { /* 保留手填值 */ }
  });
  return el('fieldset', {class: 'replay-row', 'data-key': `r${seed}`},
    el('legend', {}, `复盘 ${seed + 1}`),
    fieldRow({key: 'name', label: '复盘名称', type: 'text', default: `复盘 ${seed + 1}`}),
    el('label', {class: 'field'}, el('span', {}, '关联已有差分运行（沿用原窗口）'), runSelect),
    el('label', {class: 'field'}, el('span', {}, '对应数据批次'), datasetSelect),
    pilotInput, windowInput,
    el('div', {class: 'replay-quota'},
      fieldRow({key: 'require_treated', label: `${procurement.groups[0]}家数`, type: 'number', min: 1, max: 50, step: 1, default: 3}),
      fieldRow({key: 'require_control', label: `${procurement.groups[1]}家数`, type: 'number', min: 1, max: 50, step: 1, default: 3})),
    el('p', {class: 'subtle'}, '三段：启动日前两段 + 启动日后一段，逐日齐全才算补齐一家。关联运行后以该运行的原窗口为准。'));
}

function collectReplays(container, procurement) {
  return [...container.querySelectorAll('.replay-row')].map(row => {
    const runValue = row.querySelector('[name="run_id"]').value;
    return {
      key: row.dataset.key,
      name: row.querySelector('[name="name"]').value.trim(),
      dataset_id: Number(row.querySelector('[name="dataset_id"]').value),
      pilot_date: row.querySelector('[name="pilot_date"]').value,
      window_days: Number(row.querySelector('[name="window_days"]').value),
      run_id: runValue === '' ? null : Number(runValue),
      required: {
        [procurement.groups[0]]: Number(row.querySelector('[name="require_treated"]').value),
        [procurement.groups[1]]: Number(row.querySelector('[name="require_control"]').value),
      },
    };
  });
}

function catalogForm(procurement, refresh, message) {
  const template = procurement.catalog_fields;
  const sample = button('下载目录示例', {class: 'quiet'});
  const blob = () => new Blob([catalogCsv(template)], {type: 'text/csv;charset=utf-8'});
  sample.addEventListener('click', () => {
    const url = URL.createObjectURL(blob());
    const anchor = el('a', {href: url, download: procurement.catalog_sample});
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  });
  const submit = button('导入档案目录', {type: 'submit', class: 'primary'});
  if (!session.canWrite) return panel('档案目录', p('只读角色可查看目录与方案。', 'subtle'));
  return panel('导入档案目录',
    p('每行是一个包可补的一个市场日；同一编号的价格与名称须一致。同名目录再次导入即生成新修订，旧方案随之失效。', 'subtle'),
    sample,
    el('form', {class: 'fields', onSubmit: handler(submit, message, async event => {
      const data = new FormData(event.target);
      const file = data.get('source');
      if (!file?.size) throw new Error('请选择档案目录 CSV');
      if (file.size > 4_000_000) throw new Error('档案目录最多 4 MB');
      await workspace.uploadCatalog({name: data.get('catalog_name'), filename: file.name, csv_text: await file.text()});
      await refresh();
    })},
      fieldRow({key: 'catalog_name', label: '目录名称', type: 'text', default: '十月档案目录'}),
      el('label', {class: 'field'}, el('span', {}, '档案目录 CSV'),
        el('input', {type: 'file', name: 'source', accept: '.csv,text/csv', required: true})),
      submit));
}

function replayCoverage(replay) {
  const rows = [];
  for (const group of replay.groups) {
    for (const market of group.markets) {
      rows.push({
        group: group.name,
        market: market.market_id,
        status: market.complete ? '已补齐' : `仍缺 ${market.missing.length} 天`,
        missing: market.missing.length ? market.missing.join('、') : '无',
        quota: `${group.complete}/${group.required}`,
      });
    }
  }
  return table([
    {key: 'group', label: '分组'},
    {key: 'market', label: '市场'},
    {key: 'status', label: '三段齐全'},
    {key: 'missing', label: '仍缺日期'},
    {key: 'quota', label: '组内已补齐/设定'},
  ], rows, {caption: `${replay.name} 市场补齐核对`, empty: '该复盘没有候选市场。'});
}

function planResult(job) {
  const result = job.result;
  if (!result) {
    return panel(job.name, pill(PLAN_STATUS[job.status], job.status), p(job.error || '后台正在计算，完成后会自动展示。'));
  }
  const days = result.packages.reduce((sum, item) => sum + item.days.length, 0);
  return el('div', {class: 'analysis'},
    el('div', {class: 'result-head'},
      p(job.name, 'eyeline'), h(2, `预算内补齐 ${result.completed} 份复盘`),
      p(`已选 ${result.packages.length} 个档案包，共 ${days} 个市场日，总价 ${money(result.cost)}；预算 ${money(result.budget)}。`, 'intro-copy'),
      el('div', {class: 'actions'}, pill(PLAN_STATUS[job.status], job.status),
        result.optimal ? '已按精确解选择' : '受计算时限影响，可能非最优')),
    metrics([
      {label: '补齐份数', value: result.completed, detail: `候选 ${result.candidate_total} 份`},
      {label: '总价（元）', value: result.cost / 100, detail: `预算 ${result.budget / 100} 元`},
      {label: '选中的包', value: result.packages.length, detail: `目录共 ${result.package_total} 个候选包`},
      {label: '市场日', value: days, detail: '同一包多份复盘共用只计一次'},
    ]),
    panel('选中的档案包', table([
      {key: 'code', label: '包编号'},
      {key: 'name', label: '包名称'},
      {label: '价格', render: row => money(row.price)},
      {label: '可补市场日', render: row => row.days.map(day => `${day[0]} ${day[1]}`).join('、')},
      {label: '市场日数', render: row => row.days.length},
    ], result.packages, {caption: '选中的档案包', empty: '预算内没有任何可用包。'})),
    ...result.replays.map(replay => panel(`复盘：${replay.name}`,
      p(`原窗口启动日 ${replay.pilot_date}，每段 ${replay.window_days} 天；` +
        Object.entries(replay.segments).map(([name, span]) => `${name} ${span[0]}–${span[1]}`).join('，'), 'subtle'),
      el('div', {class: 'actions'},
        pill(replay.satisfied ? '两组均达标·已补齐' : (replay.selected ? '已选但未达标' : '未纳入本次补齐'),
          replay.satisfied ? 'complete' : 'stale')),
      el('div', {class: 'quota-summary'}, replay.groups.map(group =>
        p(`${group.name}：已补齐 ${group.complete}/${group.required} 家` + (group.satisfied ? '（达标）' : '（不足）')))),
      replayCoverage(replay))));
}

function planDetail(job) {
  return panel('方案核对', planResult(job));
}

export async function procurementPage(route, refresh) {
  const {spec, datasets, catalogs, plans, runs} = workspace;
  const procurement = spec.procurement;
  const message = feedback();
  const id = route.id || plans[0]?.id;
  const job = id ? await workspace.getPlan(id).catch(() => null) : null;
  if (job && ['queued', 'running'].includes(job.status)) {
    const original = location.hash;
    const status = el('p', {role: 'status'}, '后台正在计算，完成后会自动展示。');
    poll(() => workspace.getPlan(id), item => !['queued', 'running'].includes(item.status),
      () => location.hash === original)
      .then(result => { if (result && status.isConnected && location.hash === original) refresh();
        else if (status.isConnected) status.textContent = '仍在执行，请点击刷新检查。'; })
      .catch(error => { if (status.isConnected) status.textContent = error.message; });
  }

  const replayContainer = el('div', {class: 'replay-list'});
  let seed = 0;
  const addRow = () => {
    if (replayContainer.children.length >= procurement.limits.replays) {
      message.textContent = `一次最多 ${procurement.limits.replays} 份复盘`;
      return;
    }
    replayContainer.append(replayRow(procurement, datasets, runs, seed++));
  };
  addRow();
  const addReplay = button('再加一份复盘', {class: 'quiet'});
  addReplay.addEventListener('click', addRow);

  const submit = button('计算取档方案', {type: 'submit', class: 'primary', 'data-testid': 'start-plan'});
  const fresh = catalogs.filter(item => item.fresh);
  const planForm = session.canWrite && (fresh.length && datasets.length
    ? el('form', {class: 'fields', 'data-testid': 'plan-form', onSubmit: handler(submit, message, async event => {
      event.preventDefault();
      const data = new FormData(event.target);
      const replays = collectReplays(replayContainer, procurement);
      if (replays.some(item => !item.name)) throw new Error('请为每份复盘填写名称');
      await workspace.startPlan({
        name: data.get('plan_name'),
        catalog_id: Number(data.get('catalog_id')),
        budget: Math.round(Number(data.get('budget')) * 100),
        replays,
      });
      await refresh();
    })},
      fieldRow({key: 'plan_name', label: '方案名称', type: 'text', default: '缺报补齐方案'}),
      el('label', {class: 'field'}, el('span', {}, '档案目录'),
        el('select', {name: 'catalog_id', required: true},
          fresh.map(item => el('option', {value: item.id}, `${item.name} · ${item.package_count} 包`)))),
      fieldRow({key: 'budget', label: '预算（元）', type: 'number', min: 0, step: 0.01, default: 1000}),
      el('div', {class: 'replay-head'}, h(3, '缺报复盘'), addReplay),
      replayContainer,
      el('div', {class: 'actions'}, submit))
    : empty(fresh.length ? '请先导入门岗数据批次。' : '请先导入档案目录。', '#inputs', '前往数据导入'));

  const catalogList = panel('档案目录修订', table([
    {key: 'name', label: '目录'},
    {key: 'package_count', label: '包数'},
    {key: 'day_count', label: '市场日'},
    {label: '修订状态', render: row => pill(row.fresh ? '当前可用' : '已失效', row.fresh ? 'complete' : 'stale')},
    {label: '导入时间', render: row => date(row.created_at)},
  ], catalogs, {caption: '档案目录修订', empty: '尚未导入档案目录。'}));

  const planList = panel('取档方案', table([
    {label: '方案', render: row => link(row.name, `#procurement/${row.id}`)},
    {label: '状态', render: row => pill(PLAN_STATUS[row.status], row.status)},
    {label: '补齐份数', render: row => row.result ? row.result.completed : '—'},
    {label: '总价', render: row => row.result ? money(row.result.cost) : '—'},
    {label: '预算', render: row => money(row.budget)},
    {label: '时间', render: row => date(row.created_at)},
  ], plans, {caption: '取档方案', empty: '还没有方案，导入目录后即可发起。'}));

  const detail = job ? planDetail(job) : (plans.length ? empty('选择上方方案查看选包与补齐情况。') : null);

  return el('div', {class: 'two-col'},
    el('div', {class: 'wide-panel'},
      panel(procurement.title,
        p('按包计费补齐缺报复盘：同一包可多份复盘共用、只计一次；预算内补齐份数最多，同数取总价最低。', 'intro-copy'),
        planForm, message),
      detail,
      planList),
    el('aside', {class: 'sidebar-panel'},
      catalogForm(procurement, refresh, message),
      panel('取档口径', el('ul', {class: 'method-list'}, procurement.rules.map(text => el('li', {}, text)))),
      catalogList));
}
