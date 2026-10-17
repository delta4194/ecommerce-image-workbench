/* Lovart 画布自动化：挂参考图 → 输入“/技能 + 提示词” → 发送 → 等出图 → 回传图片。
   这个脚本注入到页面主世界（MAIN world），结果通过 window.postMessage 交给后台。 */
globalThis.lovartRun = async function(job) {
  const kind = (job && job.kind) || '';
  const mode = (job && job.mode) || '';
  if (kind === 'lovart-probe' || mode === 'inspect' || mode === 'upload') {
    if (mode === 'inspect') return await globalThis.lovartInspect();
    if (mode === 'upload') return await globalThis.lovartUploadProbe({files: (job && job.files) || [], jobId: job && job.id});
    if (mode === 'open') return {ok: true, version: '2.5', project_url: location.href,
                                 note: '画布已打开：' + location.href};
    return await globalThis.lovartProbe();
  }
  return await globalThis.lovartStart({
    jobId: job && job.id,
    prompt: job && job.prompt,
    skill: job && job.skill,
    answer: job && job.answer,
    project: job && job.project,
    files: (job && job.files) || [],
    skip_upload: !!(job && job.skip_upload),
    timeout_sec: (job && job.timeout_sec) || 780,
  });
};

globalThis.lovartStart = async function(options) {
  const opts = options || {};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => { try { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; } catch (e) { return false; } };
  const textOf = el => (el && (el.innerText || el.textContent) || '').trim();
  const report = (payload) => window.postMessage({__lovart: 'result', jobId: opts.jobId, payload}, '*');
  const progress = (message) => window.postMessage({__lovart: 'progress', jobId: opts.jobId, message: String(message).slice(0, 200)}, '*');

  if (/\/login|\/signin|sign-in/i.test(location.pathname)) {
    throw Error('Lovart 需要登录：请在浏览器里登录后回到工作台点“继续”');
  }
  const bodyText = textOf(document.body).slice(-3000);
  if (!document.querySelector('textarea,[contenteditable="true"]') && /(登录|注册|Log in|Sign in|Sign up)/.test(bodyText)) {
    throw Error('Lovart 需要登录：请在浏览器里登录后回到工作台点“继续”');
  }

  const waitFor = async (find, ms, label) => {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      const node = find();
      if (node) return node;
      await sleep(500);
    }
    throw Error('没找到' + label + '（Lovart 页面结构可能变了）');
  };

  const composer = await waitFor(() => {
    const nodes = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible);
    if (!nodes.length) return null;
    return nodes.sort((a, b) => {
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      return (rb.width * rb.height) - (ra.width * ra.height);
    })[0];
  }, 90000, '输入框');

  const galleryImages = () => [...document.querySelectorAll('img')]
    .filter(img => img.naturalWidth >= 512 && img.naturalHeight >= 512)
    .filter(img => /^(https?:|blob:|data:)/.test(img.currentSrc || img.src || ''));
  const before = new Set(galleryImages().map(img => img.currentSrc || img.src));

  const toFile = (name, dataUrl) => {
    const parts = String(dataUrl).split(',');
    const mime = (parts[0].match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
    const binary = atob(parts[1] || '');
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new File([bytes], name || ('ref-' + Date.now() + '.jpg'), {type: mime});
  };

  const attachFiles = async (items) => {
    const files = items.map((item, index) => toFile(item.name || ('参考图' + (index + 1) + '.jpg'), item.dataUrl));
    const drop = async (element) => {
      if (!element) return;
      const transfer = new DataTransfer();
      files.forEach(file => transfer.items.add(file));
      for (const type of ['dragenter', 'dragover', 'drop']) {
        element.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: transfer}));
        await sleep(120);
      }
    };
    const attached = () => {
      // 附件缩略图通常出现在输入框上方，用“输入框上方的图片数量”粗判。
      const cbox = composer.getBoundingClientRect();
      return [...document.querySelectorAll('img,[class*="thumb"],[class*="attach"],[class*="upload"]')]
        .filter(el => {
          const r = el.getBoundingClientRect();
          return r.width >= 24 && r.height >= 24 && r.top > cbox.top - 260 && r.bottom <= cbox.bottom + 10;
        }).length;
    };
    const before = attached();
    // 拖放目标：输入框、它的上几层、body，以及“输入框中心和画布中心实际命中的元素”
    const targets = [];
    let node = composer;
    for (let level = 0; level < 5 && node; level++, node = node.parentElement) targets.push(node);
    const cbox = composer.getBoundingClientRect();
    for (const point of [[cbox.left + cbox.width / 2, cbox.top + cbox.height / 2],
                         [Math.round(window.innerWidth * 0.35), Math.round(window.innerHeight * 0.45)]]) {
      try { const hit = document.elementFromPoint(point[0], point[1]); if (hit) targets.push(hit); } catch (e) { }
    }
    targets.push(document.body, document.documentElement);
    for (const element of new Set(targets)) {
      await drop(element);
      await sleep(1800);
      if (attached() > before) return files.length;
    }
    // 粘贴事件（不少应用对“粘贴图片”和“拖放图片”是两套处理）
    for (const element of [composer, document.body]) {
      try {
        const transfer = new DataTransfer();
        files.forEach(file => transfer.items.add(file));
        const event = new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: transfer});
        element.dispatchEvent(event);
        await sleep(2000);
        if (attached() > before) return files.length;
      } catch (e) { }
    }
    // 页面里若存在 file input（有些版本点“上传”后才创建），也塞一次
    for (const input of [...document.querySelectorAll('input[type=file]')]) {
      const transfer = new DataTransfer();
      files.forEach(file => transfer.items.add(file));
      try { input.files = transfer.files; } catch (e) { continue; }
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      await sleep(2500);
      if (attached() > before) return files.length;
    }
    // 最后一条路：点开 Lovart 的上传按钮（会弹出系统文件窗口），
    // 由本机工作台把参考图路径填进去——这是目前唯一能自动挂图的通道。
    const triggers = [...document.querySelectorAll('button,[role="button"],li,[class*="upload"],[class*="Upload"]')]
      .filter(visible)
      .filter(el => /上传本地参考|上传文件|上传|Upload|本地文件/.test((el.innerText || '').trim()));
    for (const [index, trigger] of triggers.slice(0, 4).entries()) {
      trigger.click();
      await sleep(1200);
      window.postMessage({__lovart: 'progress', jobId: opts.jobId,
        message: 'PICKFILES 已点开上传按钮，正在把参考图填进系统文件窗口…'}, '*');
      const deadline = Date.now() + 45000;
      let got = 0;
      while (Date.now() < deadline) {
        await sleep(1500);
        got = attached();
        if (got > before) {
          steps.push({method: 'upload-button#' + index + ' ' + (trigger.innerText || '').trim().slice(0, 12),
                      before: before, after: got, delta: got - before});
          return files.length;
        }
      }
      steps.push({method: 'upload-button#' + index + ' ' + (trigger.innerText || '').trim().slice(0, 12),
                  before: before, after: attached(), delta: attached() - before,
                  note: '点了上传按钮但没等到附件出现在输入框上方'});
    }
    return attached() > before ? files.length : 0;
  };

  const setComposerText = async (value) => {
    composer.focus();
    if (composer.tagName === 'TEXTAREA' || composer.tagName === 'INPUT') {
      const proto = composer.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(composer, value);
      composer.dispatchEvent(new Event('input', {bubbles: true}));
    } else {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(composer);
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('insertText', false, value);
    }
    await sleep(500);
  };

  const clickSend = async () => {
    const buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible).filter(el => !el.disabled);
    const named = buttons.find(el => /send|发送|生成|generate|提交/i.test(
      (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '') + ' ' + textOf(el).slice(0, 16)));
    if (named) { named.click(); return 'aria:button'; }
    // 找不到名字，就按“离输入框右下角最近的那个无字小圆按钮”来找发送键（Lovart 的发送键没有 aria-label）
    const cbox = composer.getBoundingClientRect();
    const anchor = {x: cbox.right, y: cbox.bottom};
    const candidates = buttons.filter(el => {
      const r = el.getBoundingClientRect();
      const size = Math.max(r.width, r.height);
      return size >= 20 && size <= 80 && textOf(el).length <= 2
        && r.bottom > cbox.top && r.top < cbox.bottom + 80;
    }).map(el => {
      const r = el.getBoundingClientRect();
      const distance = Math.hypot(r.left + r.width / 2 - anchor.x, r.top + r.height / 2 - anchor.y);
      return {el, distance};
    }).filter(item => item.distance < 160).sort((a, b) => a.distance - b.distance);
    for (const item of candidates.slice(0, 3)) {
      item.el.click();
      await sleep(1500);
      if (!((composer.innerText || composer.value || '').trim().length > 20)) return 'position:button';
    }
    for (const target of [composer, document, document.body]) {
      for (const type of ['keydown', 'keypress', 'keyup']) {
        target.dispatchEvent(new KeyboardEvent(type, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
          bubbles: true, cancelable: true, composed: true}));
      }
    }
    return 'enter';
  };

  const agentQuestion = () => {
    const nodes = [...document.querySelectorAll('[class*="message"],[class*="chat"],[class*="assistant"]')]
      .map(node => textOf(node)).filter(value => value.length > 4 && value.length < 400);
    const last = nodes.slice(-3).find(value => /[?？]$|请选择|需要你|告诉我|which|would you like/i.test(value));
    return last || '';
  };

  const asDataUrl = async (url) => {
    try {
      const response = await fetch(url, {credentials: 'include'});
      const blob = await response.blob();
      return await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
    } catch (error) {
      return '';
    }
  };

  let uploaded = 0;
  if (opts.skip_upload) {
    progress('按你的要求跳过自动挂图，直接用已经挂好的参考图');
  } else if (opts.files && opts.files.length) {
    uploaded = await attachFiles(opts.files);
  }
  progress('已挂上 ' + uploaded + '/' + ((opts.files && opts.files.length) || 0) + ' 张参考图');
  if (!opts.skip_upload && (opts.files || []).length && !uploaded) {
    report({images: [], project_url: location.href, note: '参考图没能挂上去：Lovart 的上传控件没找到，'
      + '请在浏览器里手动把这批参考图拖进去，然后回工作台点“继续”'});
    return {ok: true, project_url: location.href, uploaded: 0, stuck: 'upload'};
  }

  const prompt = ((opts.skill ? '/' + String(opts.skill).replace(/^\//, '') + ' ' : '')
    + String(opts.prompt || '')).trim();
  await setComposerText(prompt);
  await sleep(400);
  const how = await clickSend();
  await sleep(2500);
  const stillThere = (composer.innerText || composer.value || '').trim().length > 20;
  progress('已发送提示词（' + how + '），等待出图…');
  if (stillThere) {
    report({images: [], project_url: location.href, note: '提示词没发出去（找到的发送方式：' + how + '）。'
      + '请在 Lovart 页面里按回车或点发送按钮发一次，然后回工作台点“继续”'});
    return {ok: true, project_url: location.href, uploaded, stuck: 'send'};
  }

  const projectUrl = location.href;
  // 观察任务放到页面里自己跑，不阻塞这次注入的返回（后台服务会被消息唤醒）。
  (async () => {
    const deadline = Date.now() + Math.max(120, Number(opts.timeout_sec) || 780) * 1000;
    const found = [];
    let lastNew = Date.now();
    while (Date.now() < deadline) {
      await sleep(3000);
      const fresh = galleryImages().filter(img => !before.has(img.currentSrc || img.src)
        && !found.includes(img.currentSrc || img.src));
      if (fresh.length) {
        fresh.forEach(img => found.push(img.currentSrc || img.src));
        lastNew = Date.now();
        progress('已出图 ' + found.length + ' 张，继续等它跑完…');
        continue;
      }
      if (found.length && Date.now() - lastNew > 20000) break;
      if (!found.length && Date.now() - lastNew > 75000) {
        const question = agentQuestion();
        if (question) {
          report({images: [], project_url: projectUrl, pending_question: question, note: 'Lovart 在等你回答'});
          return;
        }
      }
    }
    const images = [];
    for (const url of found.slice(0, 20)) {
      const dataUrl = await asDataUrl(url);
      if (dataUrl && dataUrl.length > 1000) images.push(dataUrl);
    }
    report({
      images,
      project_url: projectUrl,
      note: images.length ? '' : '画布上没抓到新图（可能还没生成完，或是视频/图层形式）',
      count: images.length,
    });
  })();

  return {ok: true, project_url: projectUrl, uploaded, how};
};

/* 只做体检：打开 Lovart 画布，确认登录状态和输入框在不在，不发任何生成请求（不消耗积分）。 */
globalThis.lovartProbe = async function() {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => { try { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; } catch (e) { return false; } };
  if (!/\/canvas/.test(location.pathname)) {
    return {ok: true, title: document.title, url: location.href, composer: false, logged_out: false,
            note: '当前标签页不是 Lovart 画布（' + location.pathname + '），请点“检查 Lovart”重开一次'};
  }
  const deadline = Date.now() + 60000;
  let composer = null;
  while (Date.now() < deadline) {
    composer = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible)[0] || null;
    if (composer) break;
    await sleep(800);
  }
  const body = (document.body && (document.body.innerText || '')) || '';
  const loggedOut = !composer && /(登录|注册|Log in|Sign in|Sign up)/.test(body.slice(0, 1500));
  const box = el => { const r = el.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const structure = {near_buttons: [], file_inputs: [], upload_buttons: []};
  let attachmentCount = 0;
  if (composer) {
    const cbox = box(composer);
    attachmentCount = [...document.querySelectorAll('img,[class*="thumb"],[class*="attach"],[class*="preview"]')]
      .filter(el => {
        const r = el.getBoundingClientRect();
        return r.width >= 24 && r.height >= 24 && r.top > cbox.top - 320 && r.bottom <= cbox.bottom + 20;
      }).length;
    structure.near_buttons = [...document.querySelectorAll('button,[role="button"],[class*="send"],[class*="Send"]')]
      .filter(visible)
      .filter(el => { const r = el.getBoundingClientRect(); return r.top > cbox.y - 80 && r.top < cbox.y + cbox.h + 160; })
      .map(el => ({tag: el.tagName.toLowerCase(), text: (el.innerText || '').trim().slice(0, 16),
                   aria: el.getAttribute('aria-label') || '', title: el.getAttribute('title') || '',
                   disabled: !!el.disabled, cls: String(el.className || '').slice(0, 48), box: box(el)}))
      .slice(0, 16);
    structure.file_inputs = [...document.querySelectorAll('input[type=file]')].map(el => ({
      accept: el.getAttribute('accept') || '', multiple: !!el.multiple, visible: visible(el),
      cls: String(el.className || '').slice(0, 40)}));
    structure.upload_buttons = [...document.querySelectorAll('button,[role="button"]')].filter(visible)
      .map(el => (el.innerText || '').trim()).filter(t => /上传|参考|Upload|添加/.test(t)).slice(0, 8);
  }
  return {
    ok: true,
    version: '2.5',
    title: document.title,
    url: location.href,
    composer: !!composer,
    attachments: attachmentCount,
    logged_out: loggedOut,
    skill_buttons: [...document.querySelectorAll('button')].map(b => (b.textContent || '').trim())
      .filter(t => /product-shoot|电商|技能|skill/i.test(t)).slice(0, 8),
    ...structure,
    note: composer ? 'Lovart 已就绪：已登录、输入框可用（助手 2.4）'
      : (loggedOut ? 'Lovart 需要登录：请在浏览器里登录后重试' : '没找到输入框（页面可能还没加载完或结构变了）'),
  };
};

/* 结构体检：把输入区附近的按钮、文件框、可拖放区域导出，用来对准选择器（不发送任何内容）。 */
globalThis.lovartUploadProbe = async function(options) {
  const opts = options || {};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => { try { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; } catch (e) { return false; } };
  const steps = [];
  const deadline = Date.now() + 45000;
  let composer = null;
  while (Date.now() < deadline) {
    composer = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible)
      .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height
                    - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || null;
    if (composer) break;
    await sleep(700);
  }
  if (!composer) return {ok: true, version: '2.5', note: '没找到输入框', steps};

  const toFile = (name, dataUrl) => {
    const parts = String(dataUrl).split(',');
    const mime = (parts[0].match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
    const binary = atob(parts[1] || '');
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new File([bytes], name || 'ref.jpg', {type: mime});
  };
  const files = (opts.files || []).map((item, index) => toFile(item.name || ('参考图' + (index + 1) + '.jpg'), item.dataUrl));
  const attachments = () => {
    const cbox = composer.getBoundingClientRect();
    return [...document.querySelectorAll('img,[class*="thumb"],[class*="attach"],[class*="upload"],[class*="preview"]')]
      .filter(el => {
        const r = el.getBoundingClientRect();
        return r.width >= 24 && r.height >= 24 && r.top > cbox.top - 320 && r.bottom <= cbox.bottom + 20;
      }).length;
  };
  const dropOn = async (element) => {
    if (!element) return;
    const transfer = new DataTransfer();
    files.forEach(file => transfer.items.add(file));
    for (const type of ['dragenter', 'dragover', 'drop']) {
      element.dispatchEvent(new DragEvent(type, {bubbles: true, cancelable: true, dataTransfer: transfer}));
      await sleep(150);
    }
  };
  const before = attachments();
  // 先把“上传”按钮记下来（合成拖放失败后界面可能已经切走）。
  const findTriggers = () => [...document.querySelectorAll('button,[role="button"],li,[class*="upload"],[class*="Upload"]')]
    .filter(visible)
    .filter(el => /上传本地参考|上传文件|上传|Upload|本地文件/.test((el.innerText || '').trim()))
    .slice(0, 4);
  const triggers = findTriggers();
  const triggerLabels = triggers.map(el => (el.innerText || '').trim().slice(0, 14));
  const targets = [];
  const labels = [];
  let node = composer;
  for (let level = 0; level < 5 && node; level++, node = node.parentElement) {
    targets.push(node);
    labels.push('ancestor' + level + ':' + (node.className || node.tagName).toString().slice(0, 40));
  }
  targets.push(document.body); labels.push('body');
  for (const [index, element] of targets.entries()) {
    const c0 = attachments();
    const inputs0 = document.querySelectorAll('input[type=file]').length;
    await dropOn(element);
    await sleep(2200);
    const c1 = attachments();
    const inputs1 = document.querySelectorAll('input[type=file]').length;
    steps.push({method: 'drop ' + labels[index], before: c0, after: c1, delta: c1 - c0,
                file_inputs: inputs0 + '→' + inputs1});
    if (c1 > before) return {ok: true, version: '2.5', winner: 'drop ' + labels[index], steps, note: '拖放挂图成功'};
  }
  // 粘贴
  for (const [name, element] of [['paste:composer', composer], ['paste:body', document.body]]) {
    const c0 = attachments();
    try {
      const transfer = new DataTransfer();
      files.forEach(file => transfer.items.add(file));
      element.dispatchEvent(new ClipboardEvent('paste', {bubbles: true, cancelable: true, clipboardData: transfer}));
    } catch (e) { steps.push({method: name, error: String(e).slice(0, 60)}); continue; }
    await sleep(2500);
    const c1 = attachments();
    steps.push({method: name, before: c0, after: c1, delta: c1 - c0});
    if (c1 > before) return {ok: true, version: '2.5', winner: name, steps, note: '粘贴挂图成功'};
  }
  // file input（有的版本点“上传”后才创建）
  for (const input of [...document.querySelectorAll('input[type=file]')]) {
    const transfer = new DataTransfer();
    files.forEach(file => transfer.items.add(file));
    try { input.files = transfer.files; } catch (e) { continue; }
    input.dispatchEvent(new Event('change', {bubbles: true}));
    await sleep(2500);
    const c1 = attachments();
    steps.push({method: 'file-input', before, after: c1, delta: c1 - before});
    if (c1 > before) return {ok: true, version: '2.5', winner: 'file-input', steps, note: 'file input 挂图成功'};
  }
  // 点开上传按钮 → 由工作台把路径填进系统文件窗口（目前唯一可能全自动的一条）
  for (const [index, trigger] of triggers.entries()) {
    const label = (trigger.innerText || '').trim().slice(0, 14);
    trigger.click();
    await sleep(1500);
    window.postMessage({__lovart: 'progress', jobId: opts.jobId,
      message: 'PICKFILES 已点开上传按钮（' + label + '），正在把参考图填进系统文件窗口…'}, '*');
    const deadline = Date.now() + 30000;
    while (Date.now() < deadline) {
      await sleep(1500);
      const c1 = attachments();
      if (c1 > before) {
        steps.push({method: 'upload-dialog#' + index + ' ' + label, before, after: c1, delta: c1 - before});
        return {ok: true, version: '2.5', winner: 'upload-dialog#' + index + ' ' + label, steps,
                note: '系统文件窗口方式挂图成功'};
      }
    }
    steps.push({method: 'upload-dialog#' + index + ' ' + label, before, after: attachments(),
                delta: attachments() - before, note: '点了上传按钮+填了文件窗口，但附件没出现'});
  }
  return {ok: true, version: '2.5', steps, winner: '', trigger_labels: triggerLabels,
          note: '自动挂图没成功；请手动拖一次，我把手动路径设成默认'};
};

globalThis.lovartInspect = async function() {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = el => { try { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; } catch (e) { return false; } };
  const box = el => { const r = el.getBoundingClientRect(); return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}; };
  const deadline = Date.now() + 45000;
  let composer = null;
  while (Date.now() < deadline) {
    composer = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(visible)
      .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height
                    - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || null;
    if (composer) break;
    await sleep(700);
  }
  if (!composer) {
    return {ok: true, composer: false, url: location.href, note: '没找到输入框'};
  }
  const cbox = box(composer);
  const near = [...document.querySelectorAll('button,[role="button"],a,[class*="send"],[class*="Send"]')]
    .filter(visible)
    .map(el => ({el, r: el.getBoundingClientRect()}))
    .filter(item => item.r.top > cbox.y - 60 && item.r.top < cbox.y + cbox.h + 160 && item.r.left > cbox.x - 80)
    .map(item => ({
      tag: item.el.tagName.toLowerCase(),
      text: (item.el.innerText || '').trim().slice(0, 18),
      aria: item.el.getAttribute('aria-label') || '',
      title: item.el.getAttribute('title') || '',
      cls: String(item.el.className || '').slice(0, 60),
      disabled: !!item.el.disabled,
      box: box(item.el),
    }));
  const inputs = [...document.querySelectorAll('input[type=file]')].map(el => ({
    accept: el.getAttribute('accept') || '', multiple: !!el.multiple, cls: String(el.className || '').slice(0, 50),
    visible: visible(el), box: visible(el) ? box(el) : null,
  }));
  const uploadButtons = [...document.querySelectorAll('button,[role="button"],[class*="upload"],[class*="Upload"]')]
    .filter(visible).map(el => (el.innerText || '').trim()).filter(t => /上传|参考|Upload|添加|图/.test(t)).slice(0, 10);
  return {
    ok: true, url: location.href, title: document.title,
    composer: {tag: composer.tagName.toLowerCase(), cls: String(composer.className || '').slice(0, 60), box: cbox},
    near_buttons: near.slice(0, 18),
    file_inputs: inputs,
    upload_buttons: uploadButtons,
    note: '结构体检完成（未发送任何内容）',
  };
};
