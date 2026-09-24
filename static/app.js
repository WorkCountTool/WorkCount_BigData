const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { items: [], summary: {}, filter: 'all', query: '', employeeId: '', employeeName: '', semester: '2025-2026-2', syncing: false };
const kindNames = { theory: '理论课程', training: '集中实训', internship: '岗位实习', thesis: '毕业论文/设计', defense: '答辩', social: '社会实践', manual: '手工核定' };
const number = value => Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 4 });
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error?.message || `请求失败 (${response.status})`);
  }
  if (response.status === 204) return {};
  return response.json();
}

function notify(message, error = false) {
  const node = $('#notice'); node.textContent = message; node.hidden = false; node.classList.toggle('error', error);
  clearTimeout(notify.timer); notify.timer = setTimeout(() => node.hidden = true, 4500);
}

function showLoginProgress(percent, message) {
  const value = Math.max(2, Math.min(100, Number(percent || 2)));
  $('#loginProgress').hidden = false;
  $('#loginProgressPercent').textContent = `${value}%`;
  $('#loginProgressBar').style.width = `${value}%`;
  $('#loginProgressMessage').textContent = message || '正在验证青果账号';
}

function showSyncProgress(percent, message) {
  state.syncing = percent < 100;
  $('#syncProgress').hidden = false;
  $('#syncProgressPercent').textContent = `${percent}%`;
  $('#syncProgressBar').style.width = `${percent}%`;
  $('#syncProgressMessage').textContent = message || '正在读取青果平台数据';
  $('#exportButton').classList.toggle('disabled', state.syncing);
  $('#budgetExportButton').classList.toggle('disabled', state.syncing);
  $('#personalExportButton').classList.toggle('disabled', state.syncing);
}

function renderSyncPlaceholder() {
  ['totalWorkload','totalHours','theoryWorkload','practiceWorkload','theoryHours','practiceHours','recordCount','legendTheory','legendPractice'].forEach(id => $(`#${id}`).textContent = '--');
  $('#ranking').innerHTML = '<div class="empty"><strong>青果数据更新中</strong><span>完成后自动显示工作量排名。</span></div>';
  $('#recentBody').innerHTML = '';
  $('#recordsBody').innerHTML = '';
}

function renderNotLoadedPlaceholder() {
  renderSyncPlaceholder();
  $('#ranking').innerHTML = '<div class="empty"><strong>该学期尚未读取</strong><span>点击“查询并计算”后实时从青果获取。</span></div>';
}

async function load() {
  try {
    const params = new URLSearchParams();
    if (state.employeeId) params.set('employee_id', state.employeeId);
    if (state.employeeName) params.set('employee_name', state.employeeName);
    if (state.semester) params.set('semester', state.semester);
    const suffix = params.toString() ? `?${params}` : '';
    const data = await api(`/api/items${suffix}`); state.items = data.items; state.summary = data.summary; state.syncing = data.syncing;
    $('#exportButton').href = `/api/export.xlsx${suffix}`;
    $('#budgetExportButton').href = `/api/export-budget.xlsx${suffix}`;
    $('#personalExportButton').href = `/api/export-personal-hours.xlsx${suffix}`;
    $('#activeTeacher').textContent = state.employeeId || state.employeeName ? `当前：${state.employeeName || '未指定姓名'} · ${state.employeeId || '未指定工号'}` : '当前显示全部教师';
    if (data.syncing && !data.items.length) renderSyncPlaceholder(); else if (!data.loaded) renderNotLoadedPlaceholder(); else render();
  } catch (error) { notify(error.message, true); }
  finally { $('#loading').hidden = true; $('.view[data-view="overview"]').hidden = false; }
}

async function loadSemesters() {
  const params = new URLSearchParams();
  if (state.employeeId) params.set('employee_id', state.employeeId);
  if (state.employeeName) params.set('employee_name', state.employeeName);
  const data = await api(`/api/semesters?${params}`);
  if (!data.semesters.length) throw new Error('未读取到可用学期，请退出后重新登录');
  const select = $('#semester'); const selected = state.semester;
  select.innerHTML = data.semesters.map(term => `<option value="${term.value}" ${term.value === selected ? 'selected' : ''}>${escapeText(term.label)}${term.loaded ? ` · 本次会话已读取` : ' · 按需读取'}</option>`).join('');
  if (!data.semesters.some(term => term.value === selected)) state.semester = data.semesters[0]?.value || selected;
}

function tag(item) { return `<span class="tag ${item.kind === 'theory' ? '' : 'practice'}">${kindNames[item.kind]}</span>`; }
function escapeText(value) { const node = document.createElement('span'); node.textContent = value ?? ''; return node.innerHTML; }

function render() {
  const s = state.summary;
  const selectedSemester = $('#semester').selectedOptions[0]?.textContent?.split(' · ')[0] || state.semester;
  $('#activeSemester').textContent = selectedSemester;
  $('#totalWorkload').textContent = number(s.total_workload); $('#totalHours').textContent = number(s.total_hours);
  $('#theoryWorkload').textContent = number(s.theory_workload); $('#practiceWorkload').textContent = number(s.practice_workload);
  $('#theoryHours').textContent = `${number(s.theory_hours)} 学时`; $('#practiceHours').textContent = `${number(s.practice_hours)} 学时`;
  $('#recordCount').textContent = `${s.item_count} 条核算记录`; $('#legendTheory').textContent = number(s.theory_workload); $('#legendPractice').textContent = number(s.practice_workload);
  const pct = s.total_workload ? s.theory_workload / s.total_workload * 100 : 0;
  $('#donutPercent').textContent = `${pct.toFixed(1)}%`; $('#donut').style.background = `conic-gradient(var(--green) 0 ${pct}%, #d8dfdc ${pct}% 100%)`;
  const top = [...state.items].sort((a,b) => b.workload - a.workload).slice(0, 5);
  $('#ranking').innerHTML = top.map((x,i) => `<div class="rank-row"><span>${String(i+1).padStart(2,'0')}</span><div class="rank-name"><strong>${escapeText(x.course)}</strong><small>${escapeText(x.class_name || '未指定班级')}</small></div><strong>${number(x.workload)}</strong></div>`).join('');
  $('#recentBody').innerHTML = state.items.slice(0,6).map(x => `<tr><td>${escapeText(x.course)}<br><small class="muted">${escapeText(x.employee_name)} · ${escapeText(x.employee_id)}</small></td><td>${escapeText(x.class_name)}</td><td>${tag(x)}</td><td class="num">${number(x.display_hours)}</td><td class="num"><strong>${number(x.workload)}</strong></td></tr>`).join('');
  renderRecords();
}

function renderRecords() {
  const q = state.query.toLowerCase();
  const items = state.items.filter(x => (state.filter === 'all' || (state.filter === 'theory' ? x.kind === 'theory' : x.kind !== 'theory')) && [x.course,x.class_name,x.course_code].join(' ').toLowerCase().includes(q));
  $('#recordsBody').innerHTML = items.map(x => `<tr><td>${escapeText(x.course)}<br><small class="muted">${escapeText(x.employee_name)} · ${escapeText(x.employee_id)} · ${escapeText(x.course_code)}</small></td><td>${escapeText(x.class_name)}</td><td>${tag(x)}</td><td class="num">${number(x.student_count)}</td><td class="num">${number(x.display_hours)}</td><td class="num"><strong>${number(x.workload)}</strong></td><td>${escapeText(x.calculation)}</td></tr>`).join('');
  $('#emptyState').hidden = items.length > 0;
}

function switchView(view) {
  $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.view === view));
  $$('.view').forEach(x => x.hidden = x.dataset.view !== view);
  const titles = { overview:'工作量总览', records:'明细记录', rules:'核算规则', sync:'数据同步' }; $('#pageTitle').textContent = titles[view];
  $('#addButton').hidden = !['overview','records'].includes(view);
}

function toggleFields() { const theory = $('#itemForm [name="kind"]').value === 'theory'; $('#theoryFields').hidden = !theory; $('#practiceFields').hidden = theory; }
function openDialog(item = null) {
  const form = $('#itemForm'); form.reset(); form.id.value = item?.id || ''; $('#dialogTitle').textContent = item ? '编辑记录' : '新增记录';
  if (!item) { form.employee_id.value = state.employeeId; form.employee_name.value = state.employeeName; form.semester.value = state.semester; }
  if (item) Object.entries(item).forEach(([key,value]) => { if (form.elements[key]) form.elements[key].value = value ?? ''; });
  toggleFields(); $('#itemDialog').showModal();
}
function closeDialog() { $('#itemDialog').close(); }

async function saveItem(event) {
  event.preventDefault(); const form = event.currentTarget; const data = Object.fromEntries(new FormData(form)); const id = data.id; delete data.id;
  try { await api(id ? `/api/items/${id}` : '/api/items', { method:id?'PUT':'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) }); closeDialog(); await load(); notify(id ? '记录已更新并重新计算' : '记录已新增并完成计算'); }
  catch (error) { notify(error.message, true); }
}

async function deleteItem(id) {
  if (!confirm('确定删除这条工作量记录吗？')) return;
  try { await api(`/api/items/${id}`, {method:'DELETE'}); await load(); notify('记录已删除'); } catch(error) { notify(error.message, true); }
}

async function syncPlatform(event) {
  event.preventDefault();
  const button = $('#platformSync'); const semester = $('#semester').value;
  button.disabled = true; button.textContent = '正在启动读取…';
  $('#platformState').textContent = `正在读取 ${semester}，可返回总览查看进度。`;
  try {
    await api('/api/platform/sync', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:state.employeeId, semester})});
    state.semester=semester; showSyncProgress(3,`正在重新读取 ${semester}`); monitorBackgroundSync(); switchView('overview');
  } catch (error) {
    $('#platformState').textContent = error.message; notify(error.message, true);
  } finally {
    button.disabled = false; button.textContent = '重新读取当前选择学期';
  }
}

function showApp(user) {
  state.employeeId = user.employee_id; state.employeeName = user.employee_name;
  $('#employeeId').value = state.employeeId; $('#employeeName').value = state.employeeName;
  $('#employeeId').readOnly = true; $('#employeeName').readOnly = true;
  $('#loginScreen').hidden = true; $('#appShell').hidden = false;
}

async function login(event) {
  event.preventDefault(); const form = event.currentTarget; const button = $('#loginButton'); const status = $('#loginStatus');
  const data = Object.fromEntries(new FormData(form)); button.disabled = true; button.textContent = '正在验证账号…';
  status.classList.remove('error'); status.textContent = '登录过程中会实时显示当前步骤。'; showLoginProgress(2, '正在启动登录任务');
  try {
    const started = await api('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    form.password.value = '';
    let result; let loginPolls = 0;
    while (true) {
      if (loginPolls) await delay(loginPolls < 20 ? 500 : 1000);
      result = await api(`/api/login-status?job=${encodeURIComponent(started.job_id)}`);
      if (result.status === 'done') break;
      loginPolls += 1;
      if (loginPolls > 125) throw new Error('登录等待超时，请确认 Windows 已安装最新版 Chrome 或 Edge，并能访问青果平台');
      showLoginProgress(result.percent, result.message);
      status.textContent = `${result.message || '正在验证青果账号'} · 已等待 ${result.elapsed} 秒`;
    }
    showLoginProgress(100, '登录成功，学期列表已就绪');
    form.password.value = ''; showApp(result.user); await loadSemesters(); state.semester = $('#semester').value; await load();
    notify('登录成功，请选择需要读取的具体学期');
  } catch (error) { $('#loginProgress').hidden = true; status.textContent = error.message; status.classList.add('error'); }
  finally { form.password.value = ''; button.disabled = false; button.textContent = '登录并选择学期'; }
}

async function monitorBackgroundSync() {
  while (true) {
    await delay(1500);
    try {
      const sync = await api('/api/sync-status');
      showSyncProgress(Number(sync.percent || 0), sync.message);
      if (sync.syncing) continue;
      if (sync.error) { notify(`青果数据更新失败：${sync.error}`, true); return; }
      $('#syncProgress').hidden = true; state.syncing = false;
      if (sync.result) {
        state.employeeId = sync.result.employee_id; state.employeeName = sync.result.employee_name;
        $('#employeeId').value = state.employeeId; $('#employeeName').value = state.employeeName;
        await loadSemesters(); state.semester = $('#semester').value; await load();
        notify(`该学期更新完成：${sync.result.items} 条工作量记录`);
      }
      return;
    } catch (error) { notify(error.message, true); return; }
  }
}

async function logout() {
  try { await api('/api/logout', {method:'POST'}); } catch (_) {}
  state.items=[]; state.employeeId=''; state.employeeName=''; $('#appShell').hidden=true; $('#loginScreen').hidden=false;
  $('#loginProgress').hidden=true;
  $('#loginStatus').textContent='已退出。可使用另一位教师的青果账号登录。'; $('#loginStatus').classList.remove('error');
}

async function bootstrap() {
  try {
    const session = await api('/api/session'); showApp(session.user); await loadSemesters(); state.semester=$('#semester').value; await load();
    if (session.syncing) { showSyncProgress(session.sync_percent || 5, session.sync_message); monitorBackgroundSync(); }
  } catch (_) { $('#loginScreen').hidden=false; $('#appShell').hidden=true; }
}

async function loadSelectedSemester() {
  const semester = $('#semester').value;
  if (!semester) { notify('请选择学期', true); return; }
  state.semester = semester;
  $('#activeSemester').textContent = $('#semester').selectedOptions[0]?.textContent?.split(' · ')[0] || semester;
  try {
    const result = await api('/api/platform/load-semester', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({semester})});
    if (result.status === 'ready') { await load(); notify('该学期已在当前会话中读取'); return; }
    showSyncProgress(3, `正在准备读取 ${$('#semester').selectedOptions[0]?.textContent || semester}`);
    renderSyncPlaceholder(); monitorBackgroundSync();
  } catch (error) { notify(error.message, true); }
}

async function downloadWorkbook(anchor, fallbackName) {
  try {
    const response = await fetch(anchor.href);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error?.message || '该学期没有可导出的数据');
    }
    const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a');
    link.href = url; link.download = fallbackName; document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  } catch (error) { notify(error.message, true); }
}

document.addEventListener('click', event => {
  const nav = event.target.closest('[data-view]'); if (nav?.classList.contains('nav-item')) switchView(nav.dataset.view);
  const go = event.target.closest('[data-goto]'); if (go) switchView(go.dataset.goto);
  const edit = event.target.closest('[data-edit]'); if (edit) openDialog(state.items.find(x => x.id === Number(edit.dataset.edit)));
  const del = event.target.closest('[data-delete]'); if (del) deleteItem(del.dataset.delete);
  const filter = event.target.closest('[data-kind]'); if (filter) { $$('#kindFilter button').forEach(x => x.classList.remove('active')); filter.classList.add('active'); state.filter=filter.dataset.kind; renderRecords(); }
});
$('#addButton').addEventListener('click', () => openDialog()); $('#closeDialog').addEventListener('click', closeDialog); $('#cancelDialog').addEventListener('click', closeDialog);
$('#itemForm').addEventListener('submit', saveItem); $('#itemForm [name="kind"]').addEventListener('change', toggleFields);
$('#platformForm').addEventListener('submit', syncPlatform);
$('#loginForm').addEventListener('submit', login); $('#logoutButton').addEventListener('click', logout);
$('#searchInput').addEventListener('input', event => { state.query=event.target.value; renderRecords(); });
$('#exportButton').addEventListener('click', event => { event.preventDefault(); downloadWorkbook(event.currentTarget, `${state.semester}-决算工作量表.xlsx`); });
$('#budgetExportButton').addEventListener('click', event => { event.preventDefault(); downloadWorkbook(event.currentTarget, `${state.semester}-工作量预算汇总表.xlsx`); });
$('#personalExportButton').addEventListener('click', event => { event.preventDefault(); downloadWorkbook(event.currentTarget, `${state.semester}-教师个人学时统计表.xlsx`); });
$('#teacherSearch').addEventListener('click', loadSelectedSemester);
$('#clearTeacher').addEventListener('click', async () => { state.employeeId=''; state.employeeName=''; $('#employeeId').value=''; $('#employeeName').value=''; await loadSemesters(); await load(); });
['employeeId','employeeName'].forEach(id => $(`#${id}`).addEventListener('keydown', event => { if (event.key === 'Enter') $('#teacherSearch').click(); }));
bootstrap();
