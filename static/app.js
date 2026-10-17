const STAGES = [
  ['queued', '排队'],
  ['competitor', '读竞品'],
  ['supplier', '读1688款式'],
  ['planning', '规划画面'],
  ['generating', '生成图片'],
  ['done', '完成'],
];
const MODELS = ['doubao-seedream-5-0-260128', 'doubao-seedream-4-5-251128', 'doubao-seedream-4-0-250828'];
let token = '';
let settings = {};
let promptTouched = false;
let controlsReady = false;
let lastSnapshot = '';
const $ = (id) => document.getElementById(id);

function toast(text, duration) {
  const node = $('toast');
  node.textContent = text;
  node.hidden = false;
  clearTimeout(node.timer);
  node.timer = setTimeout(() => { node.hidden = true; }, duration || 4600);
}

async function api(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Workbench-Token': token },
    body: JSON.stringify(body || {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '操作失败');
  return value;
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fillModelSelect() {
  const select = $('model');
  const options = [...new Set([settings.image_model, ...MODELS].filter(Boolean))];
  const keep = select.value || settings.image_model;
  select.innerHTML = options.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
  if (options.includes(keep)) select.value = keep;
}

function stepHtml(stage) {
  const index = STAGES.findIndex(([key]) => key === stage);
  return STAGES.slice(0, -1).map(([key, label], i) => {
    const done = stage === 'done' || index > i;
    const now = index === i && stage !== 'done';
    return `<span class="step ${done ? 'done' : ''} ${now ? 'now' : ''}">
      <span class="dot">${done ? '✓' : i + 1}</span>${label}</span>`;
  }).join('');
}

function taskHtml(task) {
  const badge = task.stage === 'done'
    ? '<span class="badge ok">已完成</span>'
    : (task.status === 'attention' ? '<span class="badge err">需要处理</span>' : '<span class="badge run">运行中</span>');
  const refs = [];
  task.supplier_images.forEach((image) => {
    refs.push(`<figure><img src="${image.url}" alt="1688基准图" loading="lazy">
      <figcaption><b>基准图</b> · 款式 ${esc(image.name)}<br>${esc(image.pixels)}</figcaption></figure>`);
  });
  (task.style_images || []).forEach((image, index) => {
    refs.push(`<figure><img src="${image.url}" alt="竞品参考图" loading="lazy">
      <figcaption><b>风格参考 ${index + 1}</b> · 竞品主副图<br>${esc(image.pixels)}</figcaption></figure>`);
  });
  const shots = task.cards.map((card, index) => `
    <div class="shot">
      <div class="frame">
        ${card.url ? `<img src="${card.url}" alt="${esc(card.title)}" loading="lazy">`
                   : `<div class="frame pending">${card.status === 'error' ? '生成失败，可重做' : '生成中…'}</div>`}
        <span class="idx">${String(index + 1).padStart(2, '0')}</span>
      </div>
      <div class="body">
        <div class="title">${esc(card.title)}</div>
        ${card.error ? `<div class="note">${esc(card.error)}</div>` : ''}
        <button data-redo="${card.id}" data-task="${task.id}">重做这张</button>
      </div>
    </div>`).join('');
  const bullets = (task.competitor_bullets || []).slice(0, 5).map((row) => `<li>${esc(row)}</li>`).join('');
  const refLine = task.style_total
    ? `<p class="kv"><b>竞品素材</b>：主副图 ${task.style_total} 张${task.gallery_total && task.gallery_total !== task.style_total ? `（页面公布 ${task.gallery_total} 张，重复的已合并）` : ''}，全部作为风格参考，另加标题与五点描述一起交给模型</p>`
    : '';
  const warnLine = (task.warnings || []).map((row) => `<p class="kv">提示：${esc(row)}</p>`).join('');
  return `
  <article class="task">
    <div class="task-head">
      <div>
        <div class="task-title">${esc(task.name || '未命名商品')}</div>
        <div class="task-meta">${esc([task.asin && 'ASIN ' + task.asin, task.erp && 'ERP ' + task.erp,
          task.identity && '拿货规格 ' + task.identity].filter(Boolean).join(' · '))}</div>
      </div>
      <div class="head-right">${badge}
        <button class="tiny danger" data-delete="${task.id}" title="删除这个作图任务">删除任务</button>
      </div>
    </div>
    <div class="stepper">${stepHtml(task.stage)}</div>
    <p class="msg ${task.error ? 'error' : ''}">${esc(task.error || task.message || '')}</p>
    ${task.competitor_title ? `<p class="kv"><b>竞品标题</b>：${esc(task.competitor_title)}</p>` : ''}
    ${refLine}
    ${bullets ? `<ul class="bullets">${bullets}</ul>` : ''}
    ${warnLine}
    ${refs.length ? `<div class="refs">${refs.join('')}</div>` : ''}
    ${shots ? `<div class="grid">${shots}</div>` : ''}
    <div class="task-actions">
      ${task.status === 'attention' ? `<button class="primary" data-resume="${task.id}">继续自动作图</button>` : ''}
      ${(task.stage && task.stage !== 'done' && task.status === 'automatic') ? `<button data-stop="${task.id}">停止</button>` : ''}
      ${task.source_url ? `<button data-open="${esc(task.source_url)}">打开1688页面</button>` : ''}
      ${task.reference_url ? `<button data-open="${esc(task.reference_url)}">打开竞品页面</button>` : ''}
      ${task.delivery ? `<button data-folder="${task.id}">打开成品文件夹</button>` : ''}
      ${task.cards.some((card) => card.url) ? `<button data-zip="${task.id}">下载图片包</button>` : ''}
      ${task.cards.length ? `<input class="revision" data-revision="${task.id}" placeholder="重做前可在这里写修改要求，例如：背景换成浅木色桌面">` : ''}
      <span class="folder-tag">保存为 <code>${esc(task.folder_name || task.name)}</code></span>
    </div>
  </article>`;
}

function render(state) {
  settings = state.settings || settings;
  const bridge = state.bridge || {};
  const pill = $('connection');
  const helperVersion = String(bridge.helper_version || '').trim();
  if (bridge.connected && helperVersion) {
    pill.textContent = '采集助手已连接 · v' + helperVersion;
    pill.className = 'pill ok';
  } else if (bridge.connected) {
    pill.textContent = '助手需重新加载（点“助手更新到最新版”）';
    pill.className = 'pill off';
  } else {
    pill.textContent = bridge.has_browser ? `${bridge.browser} 助手待机（开始作图时自动打开）` : '未检测到采集助手扩展';
    pill.className = 'pill' + (bridge.connected ? ' ok' : ' off');
  }
  $('apihint').textContent = settings.has_key
    ? '已配置火山方舟模型 ' + settings.image_model
    : '还没保存火山方舟 API Key：展开“更多设置”填一次即可';
  $('settingsNote').textContent = (settings.has_key
    ? '密钥已用 Windows 当前用户加密保存在本机，导出文件里不含密钥。'
    : '保存密钥后不用再填。')
    + (state.output_root ? ' 成品目录：' + state.output_root : '');
  if (document.activeElement !== $('modelId')) $('modelId').value = settings.image_model || '';
  const total = 1 + Number($('subCount').value || 6);
  $('chipCount').textContent = `${total}张图生成`;
  if (!controlsReady) {
    if (settings.auto_prompt) $('prompt').value = settings.auto_prompt;
    $('subCount').value = String(settings.sub_count || 6);
    $('mainImage').checked = String(settings.main_count) !== '0';
    $('styleCount').value = String(settings.style_count != null ? settings.style_count : 0);
    $('chipCount').textContent = `${1 + Number($('subCount').value || 6)}张图生成`;
    controlsReady = true;
  }
  const tasks = state.tasks || [];
  $('taskCount').textContent = tasks.length ? `共 ${tasks.length} 个商品` : '';
  const snapshot = JSON.stringify(tasks);
  const typing = document.activeElement instanceof HTMLElement
    && document.activeElement.classList.contains('revision');
  if ((snapshot !== lastSnapshot || !tasks.length) && !typing) {
    $('tasks').innerHTML = tasks.length ? tasks.map(taskHtml).join('') : emptyHtml();
    lastSnapshot = snapshot;
  }
}

function emptyHtml() {
  return `<div class="empty">
    <h3>还没有任务</h3>
    <p>把飞书那一行资料粘到上面的框里，点“开始作图”就行。</p>
    <div class="flow"><span>1 粘贴资料</span><span>2 自动采集</span><span>3 生成7张</span><span>4 本地成品文件夹</span></div>
  </div>`;
}

async function refresh() {
  const response = await fetch('/api/state');
  const state = await response.json();
  if (!token) { token = state.token; fillModelSelect(); }
  render(state);
}

async function start() {
  const text = $('text').value.trim();
  if (!text) { toast('先把商品资料粘贴进来'); return; }
  $('start').disabled = true;
  try {
    const result = await api('/api/auto/start', {
      text,
      model: $('model').value,
      prompt: $('prompt').value,
      sub_count: $('subCount').value,
      style_count: $('styleCount').value,
      main: $('mainImage').checked ? 1 : 0,
      paid_ack: true,
    });
    toast(`已提交 ${result.created.length} 个商品任务${result.reused.length ? `，${result.reused.length} 个已在生成中或已完成` : ''}`);
    $('text').value = '';
    refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    $('start').disabled = false;
  }
}

document.addEventListener('click', async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  try {
    if (target.dataset.resume) {
      target.disabled = true;
      await api('/api/auto/resume', { task: target.dataset.resume, paid_ack: true });
      toast('继续执行中');
      refresh();
    } else if (target.dataset.folder) {
      const result = await api('/api/auto/folder', { task: target.dataset.folder });
      toast('已打开 ' + result.path);
    } else if (target.dataset.redo) {
      const revision = document.querySelector(`[data-revision="${target.dataset.task}"]`);
      target.disabled = true;
      await api('/api/auto/redo', { task: target.dataset.task, card: target.dataset.redo, revision: revision ? revision.value : '' });
      toast('已提交重做');
      refresh();
    } else if (target.dataset.open) {
      await api('/api/auto/open', { url: target.dataset.open });
      toast('已在浏览器打开；登录或完成验证后回到这里点“继续自动作图”');
    } else if (target.dataset.zip) {
      window.location.href = '/api/export/' + target.dataset.zip;
    } else if (target.dataset.delete) {
      if (!confirm('删除这个作图任务？正在执行的任务会先停止；任务记录和中间素材会删除，成品文件夹里的图片保留。')) return;
      const result = await api('/api/auto/delete', { task: target.dataset.delete });
      toast('已删除任务：' + result.name);
      lastSnapshot = '';
      refresh();
    } else if (target.dataset.stop) {
      target.disabled = true;
      await api('/api/auto/cancel', { task: target.dataset.stop });
      toast('已请求停止，未开始的步骤会跳过');
      lastSnapshot = '';
      refresh();
    }
  } catch (error) {
    target.disabled = false;
    toast(error.message);
  }
});

$('start').addEventListener('click', start);
$('prompt').addEventListener('input', () => { promptTouched = true; });
$('subCount').addEventListener('change', () => {
  $('chipCount').textContent = `${1 + Number($('subCount').value || 6)}张图生成`;
});
$('topFolder').addEventListener('click', async () => {
  try {
    const result = await api('/api/auto/folder', {});
    toast('已打开 ' + result.path);
  } catch (error) { toast(error.message); }
});
$('openBrowser').addEventListener('click', async () => {
  try {
    const result = await api('/api/auto/browser', {});
    toast(result.opened ? '已打开浏览器，助手会在十几秒内连上' : '没有找到装了采集助手的浏览器');
  } catch (error) { toast(error.message); }
});
$('openExtension').addEventListener('click', async () => {
  try {
    const result = await api('/api/auto/extension', {});
    toast(result.opened ? '已打开扩展管理页：对“商品素材采集助手”点一次“重新加载”' : '没有找到装了采集助手的浏览器');
  } catch (error) { toast(error.message); }
});
$('probeModels').addEventListener('click', async () => {
  const button = $('probeModels');
  button.disabled = true;
  toast('正在检测…未开通的模型会立刻返回，最多生成1张小图', 6000);
  try {
    const result = await api('/api/models/probe', { paid_ack: true });
    if (result.working) {
      settings = { ...settings, image_model: result.working };
      fillModelSelect();
      $('modelId').value = result.working;
      toast('可用的模型：' + result.working + '，已经帮你选上');
    } else {
      const first = result.results[0];
      toast(first ? first.message : '没有检测到可用模型');
    }
    refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$('openFolder').addEventListener('click', async () => {
  try {
    const result = await api('/api/auto/folder', {});
    toast('已打开 ' + result.path);
  } catch (error) { toast(error.message); }
});
$('pickFolder').addEventListener('click', async () => {
  const button = $('pickFolder');
  button.disabled = true;
  toast('请在弹出的窗口里选择保存位置…', 8000);
  try {
    const result = await api('/api/pick-folder', {});
    if (!result.path) { toast('没有选择文件夹'); return; }
    await api('/api/settings', { output_root: result.path });
    toast('成品保存位置已改为 ' + result.path);
    refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});
$('saveSettings').addEventListener('click', async () => {
  try {
    settings = await api('/api/settings', {
      key: $('apiKey').value,
      image_model: $('modelId').value,
      auto_prompt: $('prompt').value,
      sub_count: $('subCount').value,
      style_count: $('styleCount').value,
      main_count: $('mainImage').checked ? 1 : 0,
    });
    $('apiKey').value = '';
    fillModelSelect();
    toast('设置已保存');
    refresh();
  } catch (error) { toast(error.message); }
});

if (location.protocol === 'file:') {
  $('fileWarning').hidden = false;
  $('start').disabled = true;
  $('connection').textContent = '未通过服务地址打开';
  $('connection').className = 'pill off';
} else {
  refresh().catch(() => {});
  setInterval(() => { refresh().catch(() => {}); }, 3000);
}
