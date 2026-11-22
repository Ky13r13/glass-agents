'use strict';
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="agentctl-token"]').content;
let state = null, selected = null, filter = 'all', currentView = 'agents', tab = 'activity';
let busy = false, toastTimer, connectionsKey = '', resultsKey = '', renderedFeed = '', workflowKey = '';
const feeds = new Map();
const activeStates = new Set(['preparing', 'running', 'stopping']);
const finishedStates = new Set(['completed', 'failed', 'cancelled', 'blocked', 'scope_violation', 'interrupted']);

function node(tag, cls, text) {
  const result = document.createElement(tag);
  if (cls) result.className = cls;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}
function button(text, cls, handler) {
  const result = node('button', cls, text);
  result.type = 'button';
  result.addEventListener('click', handler);
  return result;
}
function badge(status) { return node('span', 'status ' + status, status.replaceAll('_', ' ')); }
function toast(text) {
  $('toast').textContent = text; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 6500);
}
async function api(path, body) {
  const options = {headers: {'X-Agentctl-Token': token}};
  if (body !== undefined) {
    options.method = 'POST'; options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed');
  return result;
}
function jobById(id) { return state && state.jobs.find(j => j.id === id); }
function elapsed(job) {
  if (!job.started) return 'Queued';
  const seconds = Math.max(0, Math.floor((Date.parse(job.finished || new Date().toISOString()) - Date.parse(job.started)) / 1000));
  return seconds >= 60 ? Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's' : seconds + 's';
}
function timeLabel(value) {
  return new Date(value).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function setView(view) {
  currentView = view;
  for (const item of document.querySelectorAll('.view')) item.hidden = item.id !== 'view-' + view;
  for (const item of document.querySelectorAll('[data-view]')) item.classList.toggle('active', item.dataset.view === view);
  $('breadcrumb').textContent = {agents:'Agent workspace', connections:'Connections', history:'Run history'}[view];
  if (state) { renderConnections(true); renderHistory(); }
}
function selectJob(id) {
  selected = id; resultsKey = ''; renderedFeed = '';
  renderList(); renderDetail(); loadEvents();
}
function infoRow(parent, name, value) {
  const row = node('div', 'info-row');
  row.append(node('span', '', name), node('strong', '', value || 'Not reported'));
  parent.append(row);
}
function renderList() {
  const list = $('agent-list');
  const jobs = state.jobs.filter(j => filter === 'all' || (filter === 'active' && activeStates.has(j.status)) ||
    (filter === 'queued' && j.status === 'queued') || (filter === 'finished' && finishedStates.has(j.status)));
  list.replaceChildren();
  if (!jobs.length) {
    list.append(node('div', 'empty-list', state.jobs.length ? 'No agents in this view.' : 'A clear workspace.\nStart with one focused task.'));
    return;
  }
  for (const job of jobs) {
    const card = node('button', 'agent-card' + (selected === job.id ? ' selected' : ''));
    card.type = 'button'; card.dataset.job = job.id;
    card.setAttribute('aria-pressed', String(selected === job.id));
    const top = node('div', 'agent-card-top'), label = node('div', 'agent-label');
    label.append(node('span', 'agent-avatar ' + job.backend, job.backend === 'opencode' ? 'O' : job.backend === 'local' ? 'L' : 'C'), node('strong', '', job.name));
    top.append(label, badge(job.status));
    card.append(top, node('p', '', job.goal));
    const meta = node('div', 'agent-meta');
    meta.append(node('span', 'backend-tag', job.backend.toUpperCase()), node('span', '', '·'),
      node('span', '', job.permission === 'read-only' ? 'Read-only' : 'Exclusive'), node('span', 'time', elapsed(job)));
    card.append(meta);
    card.addEventListener('click', () => selectJob(job.id));
    list.append(card);
  }
}
function renderDetail() {
  const job = jobById(selected);
  $('selection-empty').hidden = !!job; $('selection-detail').hidden = !job;
  if (!job) return;
  const running = activeStates.has(job.status);
  const canContinue = finishedStates.has(job.status);
  $('detail-name').textContent = job.name;
  $('detail-avatar').textContent = job.backend === 'opencode' ? 'O' : job.backend === 'local' ? 'L' : 'C';
  $('detail-avatar').className = 'agent-avatar ' + job.backend;
  $('detail-subtitle').textContent = job.backend.toUpperCase() + '  /  ' + (job.model || 'CLI default') + '  ·  ' + elapsed(job);
  $('detail-status').replaceWith(Object.assign(badge(job.status), {id:'detail-status'}));
  $('stop-agent').hidden = !!job.external || (!running && job.status !== 'queued');
  $('repeat-agent').hidden = !canContinue;
  const route = $('route-strip'); route.replaceChildren();
  const provider = (job.connection || {}).provider || 'CLI configuration';
  for (const [index, text] of [job.name, job.backend + ' CLI', provider].entries()) {
    if (index) route.append(node('span', 'arrow', '→'));
    route.append(node('span', 'route-node', text));
  }
  const liveMessage = job.backend === 'claude-sdk' && running && !!job.run_id;
  $('followup-text').disabled = !liveMessage && (!canContinue || !!job.external);
  $('followup-send').disabled = $('followup-text').disabled;
  $('followup-label').textContent = liveMessage ? 'MESSAGE A MANAGED AGENT' : 'NEXT INSTRUCTION';
  $('message-recipient').hidden = !liveMessage;
  if (liveMessage) {
    const recipient = $('message-recipient'), names = (job.run || {}).agent_names || [];
    if (recipient.dataset.names !== JSON.stringify(names)) {
      const previous = recipient.value;recipient.replaceChildren();
      for (const name of names) { const option = node('option','',name);option.value=name;recipient.append(option); }
      if (names.includes(previous)) recipient.value=previous;
      recipient.dataset.names=JSON.stringify(names);
    }
  }
  $('followup-hint').textContent = liveMessage ? 'Delivered on the recipient’s next message-tool read or invocation. This does not interrupt a model response.' :
    job.external ? 'Started from the CLI. Use its terminal to stop or resume; Run again creates a fresh dashboard task.' :
    canContinue ? 'Starts a fresh task with the previous reported result and current source.' :
    'Follow-ups become available when this task finishes. You can stop a task to redirect it.';
  $('stream-label').textContent = running ? 'Listening for CLI events' : job.status === 'queued' ? 'Waiting for a worker slot' : 'Saved CLI activity';
  renderFeed(); renderTeam(job); renderJobConnections(job); renderResults(job);
}
function renderTeam(job) {
  const root = $('tab-team'); root.replaceChildren();
  const block = node('div','info-block'), run = job.run || {};
  block.append(node('h3','','MANAGED AGENT CALLS'));
  if (run.loop_outcome) infoRow(block,'Workflow outcome',run.loop_outcome + ' · iteration ' + run.loop_iteration);
  const calls = Object.values(run.agent_activity || {});
  if (!calls.length) block.append(node('p','info-caption',
    job.backend === 'claude-sdk' ? 'Named agents appear here when the feedback workflow starts.' : 'This task is a single CLI run. Choose a Managed feedback workflow to use configured peers and automatic review cycles.'));
  for (const call of calls) {
    const card = node('div','managed-agent');
    const head = node('div','managed-agent-heading');
    head.append(node('strong','',call.name),badge(call.status));
    card.append(head);
    infoRow(card,'Runtime',call.backend);
    infoRow(card,'Model',call.model || 'Runtime default');
    infoRow(card,'Delegated by',call.parent || 'Workflow controller');
    if (call.task) card.append(node('p','info-caption',call.task));
    if (call.error) card.append(node('p','error-text',call.error));
    block.append(card);
  }
  block.append(node('p','info-caption','Agents exchange bounded task results and project-local messages. Native CLI peers use their own configured tools and permissions.'));
  root.append(block);
}
function renderFeed() {
  const job = jobById(selected); if (!job) return;
  const cache = feeds.get(job.id) || {events:[]};
  const key = job.id + ':' + cache.offset + ':' + job.status + ':' + (cache.diagnostic || '');
  if (renderedFeed === key) return;
  renderedFeed = key;
  const feed = $('activity-feed'), previousScroll = feed.scrollTop;
  feed.replaceChildren();
  if (!cache.events.length) {
    feed.append(node('div', 'feed-empty', job.error || job.waiting_reason ||
      (job.status === 'queued' ? 'Task queued. Context will be prepared when it can start.' : 'Waiting for the CLI to report activity…')));
  }
  for (const [index, event] of cache.events.slice(-350).entries()) {
    const item = node('article', 'event ' + event.kind);
    const title = node('div', 'event-title');
    title.append(node('span', '', event.label), node('span', '', event.status || String(index + 1).padStart(2, '0')));
    item.append(title);
    if (event.text) item.append(node('div', 'event-text', event.text));
    if (event.detail) {
      const details = node('details');
      details.append(node('summary', '', 'View tool output'), node('pre', '', event.detail));
      item.append(details);
    }
    feed.append(item);
  }
  if (job.error && cache.events.length) {
    const error = node('article', 'event error'); error.append(node('div', 'event-title', 'Task status'), node('div', 'event-text', job.error)); feed.append(error);
  }
  if (cache.diagnostic) {
    const diagnostic = node('article', 'event diagnostic');
    const details = node('details');
    details.append(node('summary', '', 'CLI diagnostics (stderr)'), node('pre', '', cache.diagnostic));
    diagnostic.append(details); feed.append(diagnostic);
  }
  feed.scrollTop = $('autoscroll').checked ? feed.scrollHeight : previousScroll;
}
function renderJobConnections(job) {
  const root = $('tab-connections'); root.replaceChildren();
  const block = node('div', 'info-block'), run = job.run || {}, info = job.connection || {};
  block.append(node('h3', '', 'EXECUTION ROUTE'));
  infoRow(block, 'Working directory', state.root);
  infoRow(block, 'CLI executable', run.argv ? run.argv[0] : state.settings[job.backend + '_bin'] || job.backend);
  infoRow(block, 'Process ID', activeStates.has(job.status) ? run.pid || 'Starting' : run.pid ? run.pid + ' (finished)' : 'Not started');
  infoRow(block, 'Provider', info.provider);
  infoRow(block, 'Model', info.model);
  infoRow(block, 'Configured endpoint', info.endpoint || 'Resolved inside the CLI; not reported');
  infoRow(block, 'Access', job.backend === 'opencode' ? 'OpenCode permissions · exclusive scheduling' : job.permission);
  infoRow(block, 'Native session', run.thread_id);
  if (run.argv) infoRow(block, 'Launch arguments', JSON.stringify(run.argv, null, 2));
  block.append(node('p', 'info-caption', info.source || 'Configured route; not a network trace.'));
  block.append(node('h3', '', 'OBSERVED NETWORK CONNECTIONS'));
  if ((job.sockets || []).length) {
    for (const socket of job.sockets) block.append(node('div', 'socket-row', socket.address + ':' + socket.port + '  ·  ' + socket.state));
  } else block.append(node('p', 'info-caption', activeStates.has(job.status) ? 'No established TCP connections visible in this sample.' : 'Network samples are shown while the managed process is active.'));
  block.append(node('p', 'info-caption', state.socket_observation));
  block.append(node('h3', '', 'CONFIGURED MCP INTEGRATIONS'));
  if ((info.integrations || []).length) {
    for (const entry of info.integrations) infoRow(block, entry.name, (entry.endpoint || entry.executable || 'CLI-managed') + '\n' + entry.state);
  } else block.append(node('p', 'info-caption', 'No integrations found in readable configuration. Additional runtime settings may apply.'));
  root.append(block);
}
function renderResults(job) {
  const run = job.run || {};
  const key = job.id + JSON.stringify([job.status, run.answer_excerpt, run.checks, run.changes, run.usage, run.loop_outcome, state.verifying]);
  if (resultsKey === key) return;
  resultsKey = key;
  const root = $('tab-results'); root.replaceChildren();
  const block = node('div', 'info-block');
  block.append(node('h3', '', 'REPORTED RESULT'));
  if (run.loop_outcome) infoRow(block,'Feedback outcome',run.loop_outcome + ' · iteration ' + run.loop_iteration);
  block.append(node('div', 'answer-preview', run.answer_excerpt || job.error || 'The final answer will appear when this task finishes.'));
  block.append(node('p', 'info-caption', 'A completed CLI turn records execution. Verification evidence is recorded separately below.'));
  block.append(node('h3', '', 'RUN ARTIFACTS'));
  const artifacts = node('div', 'artifacts-grid');
  const fileNames = ['PACKET.md', 'answer.md', 'stderr.log', 'manifest.json', 'launch.json', 'events.jsonl'];
  if (job.backend === 'claude-sdk') fileNames.push('loop/state.json','loop/messages.jsonl');
  for (const file of fileNames) {
    const b = button(file, 'artifact-button', () => openArtifact(job.id, file)); b.disabled = !job.run_id; artifacts.append(b);
  }
  block.append(artifacts);
  if (run.manifest && run.manifest.estimated_total_known_tokens) infoRow(block, 'Initial context', '~' + run.manifest.estimated_total_known_tokens.toLocaleString() + ' estimated tokens; runtime overhead is additional');
  infoRow(block, 'Reported usage', run.usage && Object.keys(run.usage).length ? JSON.stringify(run.usage, null, 2) : 'Not reported by the provider');
  if (run.reported_cost_usd !== null && run.reported_cost_usd !== undefined) infoRow(block, 'Reported cost', '$' + run.reported_cost_usd.toFixed(5));
  if (run.changes) {
    const paths = ['added','changed','removed'].flatMap(k => (run.changes[k] || []).map(p => k + ': ' + p));
    infoRow(block, 'Observed changes', paths.join('\n') || 'No scanned source changes');
  }
  block.append(node('h3', '', 'VERIFICATION'));
  if ((run.checks || []).length) {
    for (const check of run.checks) block.append(node('div', 'check-result', (check.exit_code === 0 && check.contract_passed !== false ? '✓' : '×') + ' Exit ' + check.exit_code + ' · ' + check.argv.join(' ') + (check.contract_passed === false ? ' · tool result contract failed' : '') + (check.source_changed_during_check ? ' · source changed during check' : '') + '\n' + check.finished));
    block.append(node('p', 'info-caption', 'These are historical check results. Source changes after a check can make its evidence stale; agentctl status RUN_ID checks freshness.'));
  } else block.append(node('p', 'info-caption', 'No explicit verification has been recorded.'));
  if (finishedStates.has(job.status) && job.run_id) {
    const form = node('form', 'check-form'), input = node('input');
    input.placeholder = 'e.g. python3 -m unittest discover -s tests'; input.required = true; input.setAttribute('aria-label', 'Verification command');
    const submit = node('button', 'button secondary small', state.verifying ? 'Checking…' : 'Run check'); submit.disabled = state.verifying;
    form.append(input, submit);
    form.addEventListener('submit', async e => { e.preventDefault(); try { await api('/api/verify', {id:job.id, command:input.value}); toast('Verification started.'); await refresh(); } catch(err) { toast(err.message); } });
    block.append(form, node('p', 'info-caption', 'Runs your command in this project using an argument array. Shell operators are not interpreted. The task queue must be idle.'));
  }
  root.append(block);
}
async function openArtifact(id, name) {
  try {
    const result = await api('/api/artifact?id=' + encodeURIComponent(id) + '&name=' + encodeURIComponent(name));
    showText(name, result.text);
  } catch(error) { toast(error.message); }
}
function showText(title, text) {
  $('artifact-title').textContent = title; $('artifact-text').textContent = text;
  if (!$('artifact-dialog').open) $('artifact-dialog').showModal();
}
function renderConnections(force=false) {
  if (currentView !== 'connections') return;
  const key = JSON.stringify([state.settings, state.backends, state.demo]);
  if (!force && key === connectionsKey) return;
  connectionsKey = key;
  const root = $('connections-content'); root.replaceChildren();
  const grid = node('div', 'connection-layout');
  for (const backend of state.backends) {
    const card = node('form', 'panel connection-card'), head = node('div', 'connection-head');
    head.append(node('h2', '', backend.id === 'codex' ? 'Codex' : 'OpenCode'), badge(backend.resolved ? 'detected' : 'not found'));
    card.append(head, node('p', '', backend.id === 'codex' ? 'Launches your installed Codex CLI. Select a read-only or workspace-write sandbox for each task.' :
      'Launches your installed OpenCode CLI. Existing tool permissions apply. Runs exclusively; tool permissions are not an OS sandbox.'));
    const label = node('label', 'field', 'Executable path'), input = node('input');
    input.value = state.settings[backend.id + '_bin']; input.required = true; input.disabled = state.demo;
    label.append(input); card.append(label);
    const output = node('div', 'probe-output'); output.hidden = true;
    const actions = node('div', 'connection-buttons');
    const save = node('button', 'button primary small', 'Save connection'); save.disabled = state.demo;
    const probe = button('Check CLI', 'button secondary small', async () => {
      probe.disabled = true;
      try {
        const result = await api('/api/probe', {backend:backend.id}); output.hidden = false;
        output.textContent = result.message + '\n' + (result.note || '');
      } catch(error) { toast(error.message); }
      finally { probe.disabled = false; }
    });
    actions.append(save, probe); card.append(actions, output);
    card.addEventListener('submit', async e => {
      e.preventDefault();
      try { await api('/api/settings', {[backend.id + '_bin']:input.value}); connectionsKey = ''; await refresh(); toast('Connection saved.'); }
      catch(error) { toast(error.message); }
    });
    const info = backend.connection || {};
    infoRow(card, 'Resolved binary', backend.resolved || 'Not on PATH');
    infoRow(card, 'Default provider', info.provider);
    infoRow(card, 'Default model', info.model);
    infoRow(card, 'Configured endpoint', info.endpoint || 'Resolved inside CLI');
    grid.append(card);
  }
  const help = node('div', 'panel info-block connection-help');
  if (state.managed) {
    const managed = node('div','panel info-block connection-help');
    managed.append(node('h3','','MANAGED FEEDBACK RUNTIME'));
    infoRow(managed,'Claude SDK',state.managed.sdk_installed ? 'Installed in this Python environment' : 'Optional dependency not installed');
    infoRow(managed,'Agent definitions',state.managed.configured ? '.agentctl/agents.json' : 'Run agentctl agents init in this project');
    infoRow(managed,'Workflows',(state.managed.workflows || []).join(', ') || 'None configured');
    managed.append(node('p','info-caption','Each project defines named agents, peer relationships, command schemas, checks and iteration limits. Install the optional runtime from the source folder with python3 -m pip install ".[agents]".'));
    root.append(grid, managed);
  } else root.append(grid);
  help.append(node('h3', '', 'AUTHENTICATION & WORKSPACE'),
    node('p', 'info-caption', 'Log in using the CLI in your terminal before launching tasks. agentctl reuses that authentication and does not collect API keys. Check CLI verifies the executable version only.'),
    node('p', 'info-caption', 'This dashboard controls jobs it launches. Existing terminal sessions remain in their original terminals. Follow-ups start fresh bounded tasks using the previous result as context.'));
  infoRow(help, 'Project directory', state.root);
  infoRow(help, 'Parallel readers', state.max_workers);
  help.append(node('p', 'info-caption', 'To work on another project, launch agentctl -C /path/to/project dashboard. Linux is supported; on Windows run the app and CLIs inside WSL.'));
  root.append(help);
}
function renderHistory() {
  if (currentView !== 'history') return;
  const root = $('history-content'); root.replaceChildren();
  const rows = [...state.jobs.filter(j => finishedStates.has(j.status)).map(j => ({...j, dashboard_id:j.id})), ...state.history];
  rows.sort((a,b) => b.created.localeCompare(a.created));
  if (!rows.length) { root.append(node('div','empty-list','Completed runs will appear here.')); return; }
  const table = node('table','history-table'), head = node('thead'), hr = node('tr');
  for (const text of ['Task', 'Status', 'Runtime', 'Started']) hr.append(node('th','',text));
  head.append(hr); table.append(head); const body = node('tbody');
  for (const row of rows) {
    const tr = node('tr');
    const cell = node('td'); cell.append(button(row.name || row.goal, 'text-button', () => {
      if (row.dashboard_id) { setView('agents'); selectJob(row.dashboard_id); }
      else openArtifact(row.id, 'answer.md');
    }));
    const status = node('td'); status.append(badge(row.status));
    tr.append(cell,status,node('td','mono',row.backend),node('td','',new Date(row.created).toLocaleString()));
    body.append(tr);
  }
  table.append(body); root.append(table);
}
async function loadEvents() {
  const job = jobById(selected); if (!job || !job.run_id) return;
  const id = job.id, cache = feeds.get(id) || {offset:0,events:[]};
  if (cache.loading || (cache.done && finishedStates.has(job.status))) return;
  cache.loading = true; feeds.set(id,cache);
  try {
    const result = await api('/api/events?id=' + encodeURIComponent(id) + '&offset=' + cache.offset);
    cache.offset = result.offset; cache.events.push(...result.events);
    cache.diagnostic = result.diagnostic;
    if (cache.events.length > 1000) cache.events = cache.events.slice(-1000);
    if (!result.events.length && finishedStates.has(job.status)) cache.done = true;
    if (selected === id) renderFeed();
  } catch(error) { /* A queued task may not have written events yet. The next poll retries. */ }
  finally { cache.loading = false; }
}
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    state = await api('/api/state');
    $('server-state').textContent = 'Dashboard connected'; $('server-dot').style.background = '';
    $('project-name').textContent = state.project; $('project-info').title = state.root;
    $('demo-banner').hidden = !state.demo;
    const count = state.jobs.length;
    $('agent-count').textContent = count; $('list-count').textContent = count;
    const active = state.jobs.filter(j => activeStates.has(j.status)).length;
    $('stat-active').textContent = active; $('stat-queued').textContent = state.jobs.filter(j => j.status === 'queued').length;
    $('stat-complete').textContent = state.jobs.filter(j => j.status === 'completed').length + state.history.filter(j => j.status === 'completed').length;
    $('worker-cap').textContent = state.verifying ? 'Verification in progress' : Math.max(0,state.max_workers-active) + ' reader slots available';
    const available = state.backends.filter(b => b.resolved).length;
    $('stat-connections').replaceChildren(document.createTextNode(String(available)), node('span','','/2'));
    if (!selected && count) selected = state.jobs.find(j => activeStates.has(j.status))?.id || state.jobs[0].id;
    const workflow = $('launch-workflow');
    const nextWorkflowKey = JSON.stringify([state.workflows,state.managed && state.managed.workflows]);
    if (workflowKey !== nextWorkflowKey && !$('launch-dialog').open) {
      const previous = workflow.value;
      workflow.replaceChildren();
      const single = node('option','','Single agent');single.value='';workflow.append(single);
      for (const name of (state.managed && state.managed.workflows) || []) {
        const option = node('option','','Managed feedback · ' + name);option.value='managed:' + name;workflow.append(option);
      }
      for (const item of state.workflows) {
        const option = node('option','',item.name + ' · ' + item.agents + ' agents'); option.value = item.name; workflow.append(option);
      }
      workflow.value = previous; workflowKey = nextWorkflowKey;
    }
    renderList(); renderDetail(); renderConnections(); renderHistory(); await loadEvents();
  } catch(error) {
    $('server-state').textContent = 'Dashboard disconnected'; $('server-dot').style.background = '#bd8b79';
    if (!state) toast(error.message);
  } finally { busy = false; }
}
function accessOptions() {
  const backend = $('launch-backend').value, select = $('launch-permission');
  select.replaceChildren();
  const options = backend === 'claude-sdk' ? [['workspace-write','Managed scopes · exclusive']] : backend === 'opencode' ? [['workspace-write','CLI-managed · exclusive']] :
    backend === 'local' || backend === 'codex-local' ? [['read-only','Read-only']] :
    [['read-only','Read-only sandbox'], ['workspace-write','Workspace write · exclusive']];
  for (const [value,text] of options) { const option = node('option','',text); option.value = value; select.append(option); }
  $('launch-note').textContent = backend === 'claude-sdk' ?
    'Runs the configured worker → checks → reviewer loop. Revisions and peer delegation are bounded by .agentctl/agents.json. Initialize definitions using agentctl agents init.' :
    backend === 'opencode' ?
    'Uses existing OpenCode tool permissions. No permission bypass flags are added. OpenCode tasks run exclusively in this project.' :
    'Context is prepared when the task starts. Existing CLI authentication is reused. Writers run exclusively.';
}
function openLaunch(job) {
  $('launch-form').reset(); $('launch-name').disabled = false;
  $('launch-backend').disabled = false;
  $('launch-submit').textContent = 'Launch agent ↗';
  if (job) {
    $('launch-name').value = job.name; $('launch-goal').value = job.goal;
    $('launch-backend').value = job.backend; $('launch-model').value = job.model || '';
    $('launch-includes').value = job.includes.join('\n');
    if (job.backend === 'claude-sdk') $('launch-workflow').value = 'managed:' + (job.agent_workflow || 'default');
  }
  accessOptions();
  if (job) $('launch-permission').value = job.permission;
  $('launch-dialog').showModal(); setTimeout(() => $('launch-goal').focus(), 20);
}
for (const item of document.querySelectorAll('[data-view]')) item.addEventListener('click', () => setView(item.dataset.view));
for (const item of document.querySelectorAll('[data-filter]')) item.addEventListener('click', () => {
  filter = item.dataset.filter; for (const b of document.querySelectorAll('[data-filter]')) b.classList.toggle('active', b === item); renderList();
});
for (const item of document.querySelectorAll('[data-tab]')) item.addEventListener('click', () => {
  tab = item.dataset.tab;
  for (const b of document.querySelectorAll('[data-tab]')) { b.classList.toggle('active',b === item); b.setAttribute('aria-selected',String(b === item)); }
  for (const name of ['activity','team','connections','results']) $('tab-' + name).hidden = name !== tab;
});
for (const item of document.querySelectorAll('[data-close]')) item.addEventListener('click', () => $(item.dataset.close).close());
$('new-agent').addEventListener('click', () => openLaunch());
$('empty-launch').addEventListener('click', () => openLaunch());
$('repeat-agent').addEventListener('click', () => openLaunch(jobById(selected)));
$('refresh').addEventListener('click', refresh);
$('show-all').addEventListener('click', () => document.querySelector('[data-filter="all"]').click());
$('launch-backend').addEventListener('change',accessOptions);
$('launch-workflow').addEventListener('change', () => {
  const value = $('launch-workflow').value, managed = value.startsWith('managed:');
  $('launch-name').disabled = !!value && !managed;
  $('launch-backend').disabled = managed;
  if (managed) $('launch-backend').value = 'claude-sdk';
  else if ($('launch-backend').value === 'claude-sdk') $('launch-backend').value = 'codex';
  $('launch-submit').textContent = managed ? 'Launch feedback loop ↗' : value ? 'Launch workflow ↗' : 'Launch agent ↗';
  accessOptions();
});
$('launch-form').addEventListener('submit', async e => {
  e.preventDefault(); $('launch-submit').disabled = true;
  const spec = {name:$('launch-name').value, goal:$('launch-goal').value, backend:$('launch-backend').value,
    permission:$('launch-permission').value, model:$('launch-model').value,
    includes:$('launch-includes').value.split('\n').map(s=>s.trim()).filter(Boolean), workflow:$('launch-workflow').value};
  if (spec.workflow.startsWith('managed:')) {
    spec.agent_workflow = spec.workflow.slice('managed:'.length);spec.workflow = '';spec.backend = 'claude-sdk';
  }
  try {
    const result = await api(spec.workflow ? '/api/workflow' : '/api/jobs', spec);
    selected = Array.isArray(result) ? result[0].id : result.id;
    $('launch-dialog').close(); setView('agents'); await refresh(); toast(spec.workflow ? 'Workflow queued.' : 'Agent queued.');
  } catch(error) { toast(error.message); }
  finally { $('launch-submit').disabled = false; }
});
$('stop-agent').addEventListener('click', async () => {
  try { await api('/api/cancel',{id:selected}); await refresh(); toast('Stop requested.'); } catch(error) { toast(error.message); }
});
$('followup-form').addEventListener('submit', async e => {
  e.preventDefault(); $('followup-send').disabled = true;
  try {
    const current = jobById(selected);
    if (current && current.backend === 'claude-sdk' && activeStates.has(current.status)) {
      await api('/api/agent-message',{id:selected,to:$('message-recipient').value,message:$('followup-text').value});
      $('followup-text').value='';$('followup-send').disabled=false;toast('Message queued for the managed agent.');return;
    }
    const result = await api('/api/followup',{id:selected,goal:$('followup-text').value});
    selected = result.id; $('followup-text').value = ''; await refresh(); toast('Follow-up queued with previous result as context.');
  } catch(error) { toast(error.message); $('followup-send').disabled = false; }
});
$('project-info').addEventListener('click', async () => {
  if (!state) return;
  showText('Workspace', state.root + '\n\nInspecting repository evidence…');
  try { const result = await api('/api/inspect'); $('artifact-text').textContent = state.root + '\n\n' + JSON.stringify(result,null,2); }
  catch(error) { $('artifact-text').textContent = error.message; }
});
document.addEventListener('keydown', e => {
  const tag = document.activeElement.tagName;
  if (e.key.toLowerCase() === 'n' && !e.metaKey && !e.ctrlKey && !['INPUT','TEXTAREA','SELECT'].includes(tag) && !$('launch-dialog').open && !$('artifact-dialog').open) openLaunch();
});
setInterval(() => { $('clock').textContent = new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}); },1000);
refresh(); setInterval(refresh,1000);
