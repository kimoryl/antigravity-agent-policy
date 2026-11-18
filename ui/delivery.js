(() => {
  const columns = [
    ['pending', '待处理', '#afbdd6'], ['in_progress', '进行中', '#94bdff'],
    ['pending_review', '待验收', '#f2cf8c'], ['accepted', '已验收', '#86dfbf'],
    ['needs_revision', '需修改', '#f4a795']
  ];
  const names = {pending_review: '待验收', accepted: '已验收', rejected: '退回修改', invalidated: '需重新提交'};
  const fileStatus = {ok: '源文件与提交版本一致', missing: '文件不存在', changed: '源文件已修改，需要提交新版', unavailable: '源文件无法读取'};
  const node = (tag, text, className) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; };
  const stamp = value => new Date(value * 1000).toLocaleString('zh-CN');
  let current = null, selected = null, busy = false, listSequence = 0, detailSequence = 0;
  function options(id, values, label) {
    const select = $(id), old = select.value;
    select.replaceChildren(new Option(label, ''), ...values.map(v => new Option(v.label, v.value)));
    if (values.some(v => v.value === old)) select.value = old;
  }
  async function load() {
    const sequence = ++listSequence;
    try {
      const query = new URLSearchParams({project_id: $('delivery-project').value, task_type: $('delivery-type').value});
      const data = await request(`api/deliveries?${query}`);
      if (sequence !== listSequence) return;
      options('delivery-project', data.projects.map(p => ({value: p.id, label: `${p.name} · ${p.path}`})), '全部项目');
      options('delivery-type', data.task_types.map(t => ({value: t, label: t})), '全部类型');
      $('delivery-count').textContent = `${data.tasks.length} 个任务`;
      const board = $('delivery-board'); board.replaceChildren();
      for (const [status, title, color] of columns) {
        const section = node('section', undefined, 'delivery-column'); section.style.setProperty('--column-color', color);
        const tasks = data.tasks.filter(t => t.delivery_status === status);
        const heading = node('h3', title); heading.append(node('span', tasks.length)); section.append(heading);
        for (const t of tasks) {
          const card = node('button', undefined, 'delivery-card');
          card.append(node('span', `${t.project_name} · ${t.task_type}`, 'muted'), node('h4', t.goal));
          card.append(node('p', `负责角色：${t.role}`, 'muted'), node('p', `当前阶段：${t.stage}`, 'muted'));
          const progress = node('progress'); progress.max = t.total_steps || 1; progress.value = t.done_steps; progress.setAttribute('aria-label', `步骤 ${t.done_steps}/${t.total_steps}`);
          card.append(node('span', `步骤 ${t.done_steps}/${t.total_steps}${t.delivery_version ? ` · 交付 v${t.delivery_version}` : ''}`, 'muted'), progress);
          card.append(node('p', t.summary, 'delivery-summary'), node('time', `更新于 ${stamp(t.updated_at)}`));
          card.onclick = () => open(t.task_id); section.append(card);
        }
        if (!tasks.length) section.append(node('p', '暂无任务', 'delivery-empty'));
        board.append(section);
      }
      message($('delivery-notice'), data.tasks.length ? '点击任务卡片查看产物、检查结果和验收要求。' : '当前筛选下没有任务。');
    } catch (error) { message($('delivery-notice'), `加载失败：${error.message}，可点击刷新重试。`, true); }
  }
  async function open(taskId) {
    const sequence = ++detailSequence;
    try {
      const detail = await request(`api/delivery?task_id=${encodeURIComponent(taskId)}`);
      if (sequence !== detailSequence) return;
      current = detail;
      selected = current.versions[0]?.version;
      $('delivery-comment').value = '';
      message($('delivery-review-result'), '');
      render();
      if (!$('delivery-dialog').open) $('delivery-dialog').showModal();
    } catch (error) { message($('delivery-notice'), error.message, true); }
  }
  function render() {
    const t = current.task;
    $('delivery-detail-title').textContent = t.goal;
    $('delivery-task-meta').textContent = `${t.project_root}\n任务 ID：${t.task_id}\n执行状态：${t.execution_status} · 负责角色：${t.role} · 步骤 ${t.done_steps}/${t.total_steps} · ${stamp(t.updated_at)}`;
    const version = current.versions.find(v => v.version === selected);
    $('delivery-version').replaceChildren(...current.versions.map(v => new Option(`v${v.version} · ${names[v.status]}${v.reopened_at ? ' · 已重开' : ''} · ${stamp(v.created_at)}`, String(v.version))));
    $('delivery-version').value = String(selected);
    $('delivery-version').disabled = busy || !version;
    const target = $('delivery-detail'); target.replaceChildren();
    if (version) {
      target.append(node('h3', `交付摘要 · v${version.version}`), node('p', version.summary));
      if (version.invalid_reason) target.append(node('p', version.invalid_reason, 'delivery-warning'));
      if (version.reopened_at) target.append(node('p', version.version < current.versions[0].version ? '此版本已归档，可切换到最新交付查看当前状态。' : '此版本已归档。任务已明确重开，等待 Codex 提交新版。', 'muted'));
      target.append(node('h3', '实际产物与检查报告'));
      for (const file of version.files) {
        const card = node('div', undefined, 'card');
        card.append(node('strong', `${file.kind === 'report' ? '检查报告 · ' : ''}${file.name}`), node('p', file.path, 'delivery-file-path'));
        const historical = version.version < current.versions[0].version;
        const sourceMessage = historical && file.source_status === 'changed' ? '源文件与此历史版本不同；下方链接保留当时提交的快照。' : fileStatus[file.source_status];
        card.append(node('p', sourceMessage, file.source_status === 'ok' || historical ? 'muted' : 'error'));
        if (file.snapshot_available) {
          const links = node('div', undefined, 'delivery-actions');
          for (const [label, download] of [['查看提交快照', false], ['下载该版本', true]]) {
            const link = node('a', label); link.href = api(`api/artifact?file_id=${encodeURIComponent(file.id)}${download ? '&download=1' : ''}`); link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
          }
          card.append(links);
        } else card.append(node('p', '提交时文件不存在，没有可查看或下载的快照。', 'error'));
        target.append(card);
      }
      target.append(node('h3', '检查结果'), node('pre', JSON.stringify(version.check_report, null, 2)));
    } else target.append(node('p', '尚未登记交付。Codex 完成执行并提交实际产物后，将进入待验收。', 'muted'));
    target.append(node('h3', '验收要求'));
    const requirements = node('ul');
    for (const criterion of version?.acceptance_criteria || current.acceptance_criteria) requirements.append(node('li', criterion));
    target.append(requirements);
    if (version) {
      target.append(node('h3', '验收与修改记录'));
      for (const review of version.reviews) target.append(node('p', `人工${names[review.decision]} · v${version.version} · ${stamp(review.created_at)}\n${review.comment || '已确认该版本满足验收要求。'}`, 'card'));
      for (const reopen of version.reopens) target.append(node('p', `明确重开 · ${stamp(reopen.created_at)}\n${reopen.reason}`, 'card'));
      if (!version.reviews.length && !version.reopens.length) target.append(node('p', '尚无人工验收记录。', 'muted'));
    }
    const latest = version && version.version === current.versions[0].version && !version.reopened_at;
    $('delivery-review').hidden = !(latest && ['pending_review', 'invalidated'].includes(version.status));
    $('delivery-accept').disabled = busy || version?.status !== 'pending_review';
    $('delivery-reject').disabled = busy;
  }
  async function review(decision) {
    if (busy) return;
    const comment = $('delivery-comment').value.trim();
    if (decision === 'rejected' && !comment) { message($('delivery-review-result'), '请填写退回修改意见。', true); $('delivery-comment').focus(); return; }
    busy = true; render();
    try {
      current = await request('api/delivery/review', {task_id: current.task.task_id, version: selected, decision, comment});
      message($('delivery-review-result'), decision === 'accepted' ? `已验收通过 v${selected}。` : `已退回 v${selected}，Codex 可读取意见并重开任务修改。`);
      $('delivery-comment').value = '';
      await load();
    } catch (error) { message($('delivery-review-result'), `${error.message}。请关闭详情并重新打开，确认最新版本。`, true); }
    finally { busy = false; render(); }
  }
  $('delivery-version').onchange = () => { selected = Number($('delivery-version').value); $('delivery-comment').value = ''; message($('delivery-review-result'), ''); render(); };
  $('delivery-close').onclick = () => $('delivery-dialog').close();
  $('delivery-accept').onclick = () => review('accepted');
  $('delivery-reject').onclick = () => review('rejected');
  $('delivery-refresh').onclick = load;
  $('delivery-project').onchange = load;
  $('delivery-type').onchange = load;
  if (location.protocol !== 'file:') { load(); setInterval(() => { if (!document.hidden && !busy) load(); }, 15000); }
})();
