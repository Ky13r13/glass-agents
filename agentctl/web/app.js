'use strict';
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="agentctl-token"]').content;
const SVG = 'http://www.w3.org/2000/svg';
let state = null, selected = null, filter = 'all', currentView = 'agents', tab = 'activity';
let busy = false, toastTimer, connectionsKey = '', resultsKey = '', renderedFeed = '', workflowKey = '', flowKey = '';
let focus = null; // {id, label, actors:Set} chosen by clicking a map node
let listKey = '';
const feeds = new Map();
const activeStates = new Set(['preparing', 'running', 'stopping']);
const finishedStates = new Set(['completed', 'failed', 'cancelled', 'blocked', 'scope_violation', 'interrupted']);
const BACKENDS = {codex:'Codex', opencode:'OpenCode', 'claude-sdk':'Managed', local:'Ollama', 'codex-local':'Codex + Ollama'};
const PROVIDERS = {codex:'OpenAI via Codex CLI', opencode:'Provider via OpenCode', 'claude-sdk':'Anthropic API', local:'Ollama (this PC)', 'codex-local':'Ollama via Codex'};
// Agent-runtime bookkeeping tools add noise to the map; delegation/messages are drawn as their own arrows.
const QUIET_TOOLS = new Set(['call_agent', 'send_message', 'read_messages', 'list_agents', 'get_tool_result', 'list_tool_results', 'StructuredOutput']);

function node(tag, cls, text) {
  const result = document.createElement(tag);
  if (cls) result.className = cls;
  if (text !== undefined && text !== null) result.textContent = String(text);
  return result;
}
function svg(tag, attrs = {}, text) {
  const result = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) if (value !== undefined && value !== null) result.setAttribute(key, String(value));
  if (text !== undefined) result.textContent = String(text);
  return result;
}
function button(text, cls, handler) {
  const result = node('button', cls, text);
  result.type = 'button';
  result.addEventListener('click', handler);
  return result;
}
function badge(status) { return node('span', 'status ' + status, String(status).replaceAll('_', ' ')); }
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
  if (!job.started) return 'queued';
  const seconds = Math.max(0, Math.floor((Date.parse(job.finished || new Date().toISOString()) - Date.parse(job.started)) / 1000));
  return seconds >= 60 ? Math.floor(seconds / 60) + 'm ' + seconds % 60 + 's' : seconds + 's';
}
function clip(text, size) { text = String(text || ''); return text.length > size ? text.slice(0, size - 1) + '…' : text; }
function setView(view) {
  currentView = view;
  renderSetupCard();
  for (const item of document.querySelectorAll('.view')) item.hidden = item.id !== 'view-' + view;
  for (const item of document.querySelectorAll('[data-view]')) item.classList.toggle('active', item.dataset.view === view);
  if (state) { renderConnections(true); renderHistory(); }
}
function renderSetupCard() {
  $('setup-card').hidden = !state || state.demo || state.managed.configured || currentView !== 'agents';
}
function selectJob(id) {
  if (selected !== id) focus = null;
  selected = id; resultsKey = ''; renderedFeed = ''; flowKey = '';
  renderList(); renderDetail(); loadEvents();
}
function infoRow(parent, name, value) {
  const row = node('div', 'info-row');
  row.append(node('span', '', name), node('strong', '', value || 'Not reported'));
  parent.append(row);
}

/* ---------- Agent list ---------- */
function renderList() {
  const list = $('agent-list');
  const jobs = state.jobs.filter(j => filter === 'all' || (filter === 'active' && activeStates.has(j.status)) ||
    (filter === 'queued' && j.status === 'queued') || (filter === 'finished' && finishedStates.has(j.status)));
  const key = JSON.stringify([selected, filter, state.jobs.length, jobs.map(j => [j.id, j.name, j.status, j.backend, j.permission])]);
  if (key === listKey) {
    list.querySelectorAll('.time').forEach((el, i) => { el.textContent = elapsed(jobs[i]); });
    return;
  }
  listKey = key;
  const focusedJob = list.contains(document.activeElement) && document.activeElement.dataset.job;
  list.replaceChildren();
  if (!jobs.length) {
    list.append(node('div', 'empty-list', state.jobs.length ? 'Nothing in this view.' : 'No agents yet.'));
    return;
  }
  for (const job of jobs) {
    const card = node('button', 'agent-card' + (selected === job.id ? ' selected' : '') + (activeStates.has(job.status) ? ' live' : ''));
    card.type = 'button'; card.dataset.job = job.id;
    card.setAttribute('aria-pressed', String(selected === job.id));
    const top = node('div', 'agent-card-top'), label = node('div', 'agent-label');
    const portrait = node('span', 'agent-avatar');
    setPortrait(portrait, jobPortraitId(job), job.status);
    label.append(portrait, node('strong', '', job.name));
    top.append(label, badge(job.status));
    const meta = node('div', 'agent-meta');
    meta.append(node('span', '', BACKENDS[job.backend] || job.backend), node('span', '', job.permission === 'read-only' ? 'read-only' : 'writes'),
      node('span', 'time', elapsed(job)));
    card.append(top, meta);
    card.addEventListener('click', () => selectJob(job.id));
    list.append(card);
    if (focusedJob === job.id) card.focus({preventScroll: true});
  }
}

/* ---------- Communication map ---------- */
function eventTime(event) {
  const parsed = Date.parse(event.timestamp || '');
  return Number.isFinite(parsed) ? parsed : null;
}
function buildFlow(job) {
  const nodes = new Map(), edges = new Map();
  const run = job.run || {}, cache = feeds.get(job.id) || {events: []}, events = cache.events;
  const running = activeStates.has(job.status);
  const add = (id, props) => {
    const current = nodes.get(id);
    if (!current) { nodes.set(id, {id, label: id, sub: '', kind: 'agent', status: 'idle', depth: 1, actors: new Set(), ...props}); return nodes.get(id); }
    for (const [key, value] of Object.entries(props)) if (value !== undefined && value !== null && value !== '') current[key] = value;
    return current;
  };
  const link = (from, to, type, info = {}) => {
    if (!from || !to || from === to || !nodes.has(from) || !nodes.has(to)) return;
    const key = from + '>' + to + '>' + type;
    const edge = edges.get(key) || {key, from, to, type, count: 0, open: 0, last: null, lastIndex: -1, text: ''};
    if (!info.passive) edge.count += 1;
    if (info.open) edge.open += 1;
    if (info.close) edge.open = Math.max(0, edge.open - 1);
    if (info.time) edge.last = Math.max(edge.last || 0, info.time);
    if (info.index !== undefined) edge.lastIndex = Math.max(edge.lastIndex, info.index);
    if (info.text) edge.text = clip(info.text, 160);
    if (info.failed) edge.failed = true;
    edges.set(key, edge);
  };
  add('you', {label: 'You', sub: 'operator', kind: 'operator', depth: 0});
  if (job.backend !== 'claude-sdk') {
    const info = job.connection || {};
    add('agent', {label: job.name, sub: (BACKENDS[job.backend] || job.backend) + ' · ' + (job.model || info.model || 'default model'),
      kind: 'agent', status: job.status, depth: 1, portrait: jobPortraitId(job), task: job.goal, single: true});
    add('provider', {label: info.provider || PROVIDERS[job.backend] || 'Provider', sub: info.endpoint ? clip(info.endpoint, 26) : 'model API', kind: 'provider', depth: 2, status: running ? 'running' : 'idle'});
    link('you', 'agent', 'task', {open: running, text: job.goal});
    link('agent', 'provider', 'model', {open: running, passive: true});
    events.forEach((event, index) => {
      if (event.kind === 'message') link('agent', 'you', 'result', {index, time: eventTime(event), text: event.text});
      if (event.kind !== 'tool') return;
      const name = event.label === 'Shell command' ? 'Shell' : clip(event.label, 22);
      add('tool:' + name, {label: name, sub: 'tool', kind: 'tool', depth: 2});
      link('agent', 'tool:' + name, 'tool', {index, time: eventTime(event), text: event.text});
    });
    for (const socket of job.sockets || []) {
      const id = 'net:' + socket.address + ':' + socket.port;
      add(id, {label: socket.address, sub: 'TCP ' + socket.port + ' · live', kind: 'network', depth: 2, status: 'running'});
      link('agent', id, 'network', {open: true, passive: true});
    }
    return {nodes, edges, running, total: events.length};
  }
  // Managed workflows: controller, named agents, swarm candidates, critics, tools and model providers.
  add('controller', {label: 'Controller', sub: job.agent_workflow || 'workflow', kind: 'controller', depth: 1, status: job.status});
  link('you', 'controller', 'task', {open: running, text: job.goal});
  const slotAgent = new Map(), callRecords = new Map(Object.entries(run.agent_activity || {}));
  let swarm = run.workflow_type === 'swarm';
  for (const event of events) {
    const f = event.flow || {};
    if (f.type === 'candidate.created') { swarm = true; slotAgent.set(f.slot, f.agent); }
    if (f.call_id && ['agent.started', 'agent.completed', 'agent.failed'].includes(f.type)) {
      const call = {...callRecords.get(f.call_id), name: f.actor};
      if (f.type === 'agent.started') {
        call.status ||= 'running';
        Object.assign(call, {backend: f.backend, model: f.model, parent: f.parent, task: event.text, started: event.timestamp});
      } else {
        call.status = f.type === 'agent.failed' ? 'failed' : 'completed';
      }
      callRecords.set(f.call_id, call);
    }
    if (f.actor === 'swarm') swarm = true;
  }
  const calls = [...callRecords.values()];
  swarm ||= calls.some(call => call.name.includes('.'));
  if (swarm) add('controller', {label: 'Swarm', sub: job.agent_workflow || 'workflow'});
  const split = (actor) => {
    const dot = swarm ? String(actor).indexOf('.') : -1;
    return dot > 0 ? [actor.slice(0, dot), actor.slice(dot + 1)] : [null, actor];
  };
  // Long runs can outlive the event window. Run records retain each call's actor and parent.
  const critics = new Set();
  if (swarm) {
    for (const call of calls) {
      const [slot, name] = split(call.name);
      if (slot && !slotAgent.has(slot) && (slot === name || slot.startsWith(name + '-'))) slotAgent.set(slot, name);
    }
    for (const call of calls) {
      const [slot, name] = split(call.name);
      if (slot && slotAgent.has(slot) && name !== slotAgent.get(slot) && !call.parent) critics.add(name);
    }
  }
  const resolve = (actor) => {
    if (!actor || actor === 'controller' || actor === 'swarm') return 'controller';
    if (actor === 'operator') return 'you';
    const [slot, name] = split(actor);
    if (slot && (name === 'controller' || name === slotAgent.get(slot))) return 'cand:' + slot;
    if (slot && critics.has(name)) return 'agent:' + name; // one shared node per critic
    return 'agent:' + actor;
  };
  const touch = (id, actor, props) => { const n = add(id, props); if (actor) n.actors.add(actor); return n; };
  for (const [slot, name] of slotAgent) touch('cand:' + slot, slot + '.' + name, {label: slot, sub: name, kind: 'candidate', depth: 2});
  if (!swarm) {
    for (const name of run.agent_names || []) touch(resolve(name), name, {label: name, depth: 2});
  }
  const depthOf = (id) => (nodes.get(id) || {depth: 1}).depth;
  let leader = null;
  events.forEach((event, index) => {
    const f = event.flow; if (!f) return;
    const time = eventTime(event), actor = f.actor || 'controller';
    const [slot, name] = split(actor);
    if (f.type === 'agent.started' || f.type === 'agent.completed' || f.type === 'agent.failed') {
      let from, to = resolve(actor);
      const parent = f.parent || callRecords.get(f.call_id)?.parent;
      const criticCall = slot && name !== slotAgent.get(slot) && !parent;
      if (criticCall) { critics.add(name); to = 'agent:' + name; from = 'cand:' + slot; }
      else if (parent) from = resolve(slot ? slot + '.' + parent : parent);
      else from = 'controller';
      const parentDepth = depthOf(from);
      touch(to, actor, {label: criticCall ? name : (slot && to.startsWith('agent:') ? name + ' (' + slot + ')' : name), kind: criticCall ? 'critic' : (to.startsWith('cand:') ? 'candidate' : 'agent'),
        depth: criticCall ? 3 : to.startsWith('cand:') ? 2 : nodes.get(to)?.callDepth || parentDepth + 1,
        sub: f.backend ? agentModel(f.backend, f.model) : undefined});
      nodes.get(to).callDepth = nodes.get(to).depth;
      if (f.type === 'agent.started') nodes.get(to).task = event.text;
      if (f.type === 'agent.started' && f.backend) {
        // Each agent call talks to its model provider while it runs.
        const provider = 'prov:' + f.backend;
        add(provider, {label: PROVIDERS[f.backend] || f.backend, sub: 'model API', kind: 'provider', depth: 99});
        link(to, provider, 'model', {open: true, index, time});
      } else if (f.type !== 'agent.started') {
        for (const edge of edges.values()) if (edge.from === to && edge.type === 'model') edge.open = Math.max(0, edge.open - 1);
      }
      if (f.type === 'agent.started') { link(from, to, criticCall ? 'review' : 'task', {open: true, index, time, text: event.text}); nodes.get(to).status = 'running'; }
      else {
        link(from, to, criticCall ? 'review' : 'task', {close: true, passive: true});
        link(to, from, 'result', {index, time, failed: f.type === 'agent.failed', text: f.decision ? 'decision: ' + f.decision : event.text});
        nodes.get(to).status = f.type === 'agent.failed' ? 'failed' : 'completed';
      }
    } else if (f.type === 'message.sent') {
      const recipient = slot ? slot + '.' + f.recipient : f.recipient, target = resolve(recipient);
      touch(target, recipient, nodes.has(target) ? {} : {label: f.recipient, depth: 2});
      link(resolve(actor), target, 'message', {index, time, text: event.text});
    } else if (f.type === 'review.decision') {
      link(resolve(actor), 'controller', 'review', {index, time, text: f.decision ? 'decision: ' + f.decision : ''});
    } else if (f.type === 'revision.requested') {
      link('controller', resolve(actor), 'task', {index, time, text: 'revise: ' + clip(event.text, 120)});
    } else if (f.type === 'tool.started' || f.type === 'tool.requested' || f.type === 'project_tool.started' || f.type === 'project_tool.completed' || f.type === 'tool.completed' || f.type === 'tool.failed') {
      const tool = String(f.tool || 'tool');
      if (QUIET_TOOLS.has(tool) || (f.type.startsWith('tool.') && tool.startsWith('project_'))) return;
      const files = ['read_file', 'write_file', 'list_files'].includes(tool);
      const id = files ? 'tool:files' : f.type.startsWith('project_tool') ? 'tool:cmd:' + tool : 'tool:' + tool;
      add(id, {label: files ? 'Project files' : clip(tool.replace(/^mcp__/, '').replace(/__/g, ' › '), 22), sub: files ? 'read / write' : f.type.startsWith('project_tool') ? 'command' : 'tool', kind: 'tool', depth: 99});
      const begins = f.type.endsWith('started') || f.type.endsWith('requested');
      link(resolve(actor), id, 'tool', {open: begins && f.type !== 'tool.requested', close: !begins, passive: !begins, index, time, text: tool + (event.text ? ': ' + clip(event.text, 100) : '')});
    } else if (f.type === 'swarm.round') {
      leader = f.leader || leader;
    } else if (f.type === 'candidate.dropped' && f.slot) {
      add('cand:' + f.slot, {status: 'failed', dropped: true});
    } else if (f.type === 'loop.finished') {
      add('controller', {status: f.outcome === 'accepted' || f.outcome === 'completed' ? 'completed' : f.outcome});
    }
  });
  const latestCalls = new Map();
  for (const call of calls) {
    const id = resolve(call.name);
    const [slot, name] = split(call.name);
    touch(id, call.name, nodes.has(id) ? {} : {label: name, kind: slot && critics.has(name) ? 'critic' : 'agent', depth: slot ? 3 : 2});
    const latest = latestCalls.get(id);
    if (latest && latest.status === 'running' && call.status !== 'running') continue;
    if (latest && call.status !== 'running' && latest.started > call.started) continue;
    latestCalls.set(id, call);
  }
  for (const [id, call] of latestCalls) {
    const n = nodes.get(id);
    if (call.task) n.task = call.task;
    if (call.backend) n.sub = agentModel(call.backend, call.model);
    if (!n.dropped) n.status = call.status;
  }
  for (const n of nodes.values()) {
    if (!running && n.status === 'running') n.status = 'interrupted';
  }
  if (leader && nodes.has('cand:' + leader)) nodes.get('cand:' + leader).leader = true;
  return {nodes, edges, running, total: events.length};
}
function agentModel(backend, model) {
  return [backend === 'claude-sdk' ? 'Claude SDK' : BACKENDS[backend] || backend, model].filter(Boolean).join(' · ');
}
function isAgent(n) { return ['agent', 'candidate', 'critic'].includes(n.kind); }
function layoutFlow(graph) {
  const nodes = [...graph.nodes.values()];
  const deepest = Math.max(1, ...nodes.filter(n => n.depth < 99).map(n => n.depth));
  for (const n of nodes) if (n.depth >= 99) n.depth = deepest + 1;
  // Keep the right-hand column readable: the eight busiest tool/provider nodes, the rest summarized.
  const columns = [];
  const order = {provider: 0, network: 1, tool: 2};
  nodes.sort((a, b) => (order[a.kind] ?? -1) - (order[b.kind] ?? -1));
  for (const n of nodes) (columns[n.depth] ||= []).push(n);
  const weight = (n) => [...graph.edges.values()].filter(e => e.to === n.id || e.from === n.id).reduce((s, e) => s + e.count + e.open * 5, 0);
  for (const [index, column] of columns.entries()) {
    if (!column || column.length <= 8) continue;
    const auxiliary = column.filter(n => n.kind in order);
    if (auxiliary.length <= 8) continue;
    auxiliary.sort((a, b) => weight(b) - weight(a));
    const hidden = auxiliary.slice(7);
    for (const n of hidden) graph.nodes.delete(n.id);
    const more = {id: 'more:' + index, label: '+' + hidden.length + ' more', sub: hidden.map(n => n.label).slice(0, 3).join(', '), kind: 'tool', depth: index, status: 'idle', actors: new Set()};
    columns[index] = column.filter(n => graph.nodes.has(n.id)).concat(more); graph.nodes.set(more.id, more);
  }
  const NODE_W = 142, GAP = 16, PAD = 20, MIN_GAP = 42;
  const filled = columns.filter(Boolean);
  const count = filled.length;
  const W = Math.max(640, PAD * 2 + count * NODE_W + (count - 1) * MIN_GAP);
  const columnHeights = filled.map(column => {
    for (const n of column) { n.h = isAgent(n) ? 112 : 44; n.w = NODE_W; }
    return column.reduce((total, n) => total + n.h + GAP, -GAP);
  });
  const H = Math.max(310, PAD * 2 + Math.max(...columnHeights));
  const step = count > 1 ? (W - PAD * 2 - NODE_W) / (count - 1) : 0;
  filled.forEach((column, depth) => {
    let y = (H - columnHeights[depth]) / 2;
    for (const n of column) { n.x = PAD + depth * step; n.y = y; y += n.h + GAP; }
  });
  return {W, H};
}
// The droplet occupies the upper part of its selection area, above the two labels.
function edgePort(node, right, level) {
  const agent = isAgent(node);
  const inset = agent ? 22 : 0;
  return [node.x + (right ? node.w - inset : inset), node.y + (agent ? 9 + 64 * level : node.h * level)];
}
function edgePath(a, b, type) {
  if (b.x === a.x) {
    const [x1, y1] = edgePort(a, true, .5), [x2, y2] = edgePort(b, true, .5);
    return `M${x1},${y1} C${x1 + 60},${y1} ${x2 + 60},${y2} ${x2 + 2},${y2}`;
  }
  const back = b.x < a.x;
  if (back) {
    const upper = type === 'message', level = upper ? 0.25 : 0.72, bend = upper ? -25 : 26;
    const [x1, y1] = edgePort(a, false, level), [x2, y2] = edgePort(b, true, level), dx = (x1 - x2) / 2;
    return `M${x1},${y1} C${x1 - dx},${y1 + bend} ${x2 + dx},${y2 + bend} ${x2 + 2},${y2}`;
  }
  const level = type === 'message' ? .29 : .4;
  const [x1, y1] = edgePort(a, true, level), [x2, y2] = edgePort(b, false, level), dx = (x2 - x1) / 2;
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2 - 2},${y2}`;
}
function renderFlow(job) {
  const graph = buildFlow(job);
  if (focus) focus = graph.nodes.get(focus.id) || null;
  const now = Date.now();
  for (const edge of graph.edges.values()) {
    const recent = graph.running && (edge.last ? now - edge.last < 6000 : edge.lastIndex >= 0 && edge.lastIndex >= graph.total - 3);
    edge.active = graph.running && (edge.open > 0 || recent);
  }
  const key = JSON.stringify([job.id, job.status, [...graph.nodes.values()].map(n => [n.id, n.kind, n.label, n.sub, n.status, n.leader, n.depth, n.task, ...n.actors]),
    [...graph.edges.values()].map(e => [e.key, e.count, e.active, e.failed, e.text]), focus?.id]);
  if (key === flowKey) return;
  flowKey = key;
  const {W, H} = layoutFlow(graph);
  const root = svg('svg', {viewBox: `0 0 ${W} ${H}`, width: W * .9, height: H * .9, class: 'flow-svg', role: 'group', 'aria-label': 'Participants and connections'});
  const defs = svg('defs');
  for (const type of ['task', 'result', 'message', 'review', 'tool', 'model', 'network']) {
    const marker = svg('marker', {id: 'arrow-' + type, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse', class: 'arrowhead ' + type});
    marker.append(svg('path', {d: 'M0,0 L10,5 L0,10 z'}));
    defs.append(marker);
  }
  root.append(defs);
  const edgeLayer = svg('g', {class: 'edges'}), labelLayer = svg('g', {class: 'edge-labels'}), nodeLayer = svg('g', {class: 'nodes'});
  let index = 0;
  for (const edge of graph.edges.values()) {
    const a = graph.nodes.get(edge.from), b = graph.nodes.get(edge.to);
    if (!a || !b) continue;
    const id = 'edge-' + (index++);
    const dim = focus && !(focus.id === a.id || focus.id === b.id);
    const d = edgePath(a, b, edge.type);
    // Agent-to-agent arrows stay prominent; tool and model traffic fades unless it is live.
    const quiet = !edge.active && ['tool', 'model', 'network'].includes(edge.type);
    const weight = edge.count >= 4 ? ' w3' : edge.count >= 2 ? ' w2' : '';
    const group = svg('g', {class: `edge ${edge.type}${weight}${edge.active ? ' active' : ''}${quiet ? ' quiet' : ''}${edge.failed ? ' failed' : ''}${dim ? ' dim' : ''}`});
    group.append(svg('title', {}, `${a.label} → ${b.label} · ${edge.type}${edge.count > 1 ? ' ×' + edge.count : ''}${edge.text ? '\n' + edge.text : ''}`));
    group.append(svg('path', {d, class: 'edge-hit'}));
    group.append(svg('path', {d, id, class: 'edge-line', 'marker-end': `url(#arrow-${edge.type})`}));
    if (edge.active) {
      for (const offset of [0, -0.8]) {
        const dot = svg('circle', {r: 3.6, class: 'packet'});
        const motion = svg('animateMotion', {dur: edge.type === 'model' || edge.type === 'network' ? '1.1s' : '1.7s', repeatCount: 'indefinite', begin: offset + 's'});
        motion.append(svg('mpath', {href: '#' + id}));
        dot.append(motion); group.append(dot);
      }
    }
    edgeLayer.append(group);
    if (edge.active) labelLayer.append(svg('text', {class: 'edge-label' + (dim ? ' dim' : ''), 'data-edge': id}, edge.type));
  }
  for (const n of graph.nodes.values()) {
    const dim = focus && focus.id !== n.id && ![...graph.edges.values()].some(e => (e.from === focus.id && e.to === n.id) || (e.to === focus.id && e.from === n.id));
    const selectable = isAgent(n);
    const group = svg('g', {class: `flow-node ${n.kind} ${n.status}${n.leader ? ' leader' : ''}${focus && focus.id === n.id ? ' focused' : ''}${dim ? ' dim' : ''}`, transform: `translate(${n.x},${n.y})`});
    group.append(svg('title', {}, `${n.label}${n.sub ? '\n' + n.sub : ''}\n${n.status}`));
    group.append(svg('rect', {class: 'node-box', width: n.w, height: n.h, rx: selectable ? 26 : 18}));
    if (selectable) {
      const portrait = agentPortrait(n.portrait || n.id, n.status);
      for (const [attr, value] of Object.entries({x: (n.w - 100) / 2, y: -7, width: 100, height: 100})) portrait.setAttribute(attr, value);
      group.append(portrait);
      attachDropletPull(group, portrait);
      group.dataset.agentId = n.id;
      group.setAttribute('tabindex', '0');
      group.setAttribute('role', 'button');
      group.setAttribute('aria-label', `${n.label}, ${n.status}. Show agent activity`);
      group.setAttribute('aria-pressed', String(focus?.id === n.id));
      const choose = () => {
        focus = focus?.id === n.id ? null : n;
        setTab('activity'); renderFlow(job); renderFeed();
      };
      group.addEventListener('click', choose);
      group.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(); } });
    } else {
      group.append(svg('circle', {class: 'node-dot', cx: 15, cy: n.h / 2, r: 4.5}));
    }
    const textX = selectable ? n.w / 2 : 26, anchor = selectable ? 'middle' : 'start';
    group.append(svg('text', {class: 'node-label', x: textX, y: selectable ? 88 : 19, 'text-anchor': anchor}, clip(n.label, selectable ? 17 : 15)));
    group.append(svg('text', {class: 'node-sub', x: textX, y: selectable ? 104 : 34, 'text-anchor': anchor}, clip(n.leader ? '★ leader · ' + n.sub : n.sub || n.status, selectable ? 21 : 18)));
    nodeLayer.append(group);
  }
  root.append(edgeLayer, nodeLayer, labelLayer);
  const host = $('flow-map');
  const focusedId = host.contains(document.activeElement) && document.activeElement.dataset.agentId;
  host.replaceChildren(root);
  if (focusedId) [...nodeLayer.children].find(group => group.dataset.agentId === focusedId)?.focus({preventScroll: true});
  // Place labels at real path midpoints once the SVG is in the document.
  for (const label of labelLayer.querySelectorAll('text')) {
    const path = root.querySelector('#' + label.dataset.edge);
    if (!path) continue;
    const point = path.getPointAtLength(path.getTotalLength() / 2);
    label.setAttribute('x', point.x); label.setAttribute('y', point.y - 6);
  }
  const live = [...graph.edges.values()].filter(e => e.active).length;
  $('flow-caption').textContent = graph.running && live ? live + ' live connection' + (live === 1 ? '' : 's') + ' · Select an agent to inspect its activity.' : 'Select an agent to inspect its activity.';
  $('flow-focus').hidden = !focus; $('flow-caption').hidden = !!focus;
  if (focus) {
    $('flow-focus-name').textContent = focus.label;
    $('inspector-meta').textContent = [focus.sub, focus.status.replaceAll('_', ' ')].filter(Boolean).join(' · ');
    $('inspector-task').textContent = focus.task || '';
    $('inspector-task').hidden = !focus.task;
    setPortrait($('inspector-portrait'), focus.portrait || focus.id, focus.status);
    $('inspector-message').hidden = $('message-recipient').hidden || !focus.actors.has(focus.label) ||
      !(job.run?.agent_names || []).includes(focus.label);
  }
}

/* ---------- Detail ---------- */
function renderDetail() {
  const job = jobById(selected);
  $('selection-empty').hidden = !!job; $('selection-detail').hidden = !job;
  if (!job) return;
  const running = activeStates.has(job.status);
  const canContinue = finishedStates.has(job.status);
  $('detail-name').textContent = job.name;
  setPortrait($('detail-avatar'), jobPortraitId(job), job.status);
  $('detail-subtitle').textContent = (BACKENDS[job.backend] || job.backend) + ' · ' + (job.model || 'default model') + ' · ' + elapsed(job);
  $('detail-status').replaceWith(Object.assign(badge(job.status), {id: 'detail-status'}));
  $('stop-agent').hidden = !!job.external || (!running && job.status !== 'queued');
  $('repeat-agent').hidden = !canContinue;
  const liveMessage = job.backend === 'claude-sdk' && running && !!job.run_id;
  $('followup-text').disabled = !liveMessage && (!canContinue || !!job.external);
  $('followup-send').disabled = $('followup-text').disabled;
  $('followup-text').placeholder = liveMessage ? 'Message an agent…' : 'Follow-up instruction…';
  $('message-recipient').hidden = !liveMessage;
  if (liveMessage) {
    const recipient = $('message-recipient'), names = (job.run || {}).agent_names || [];
    if (recipient.dataset.names !== JSON.stringify(names)) {
      const previous = recipient.value; recipient.replaceChildren();
      for (const name of names) { const option = node('option', '', name); option.value = name; recipient.append(option); }
      if (names.includes(previous)) recipient.value = previous;
      recipient.dataset.names = JSON.stringify(names);
    }
  }
  $('followup-hint').textContent = liveMessage ? 'Read on the agent’s next turn; it does not interrupt a response.' :
    job.external ? 'Started from the terminal; stop or resume it there.' :
    canContinue ? 'Starts a fresh task with this result as context.' :
    'Available when the task finishes.';
  $('stream-label').textContent = running ? 'Live' : job.status === 'queued' ? 'Waiting for a slot' : 'Saved activity';
  renderFlow(job); renderFeed(); renderTeam(job); renderJobConnections(job); renderResults(job);
}
function renderTeam(job) {
  const root = $('details-team'); root.replaceChildren();
  const run = job.run || {}, calls = Object.values(run.agent_activity || {});
  if (job.backend !== 'claude-sdk' && !calls.length) return;
  const block = node('div', 'info-block');
  block.append(node('h3', '', 'Agent calls'));
  if (run.loop_outcome) infoRow(block, 'Outcome', run.loop_outcome + ' · round ' + run.loop_iteration);
  if (!calls.length) block.append(node('p', 'info-caption', 'Named agents appear when the workflow starts.'));
  for (const call of calls) {
    const card = node('div', 'managed-agent'), head = node('div', 'managed-agent-heading');
    head.append(node('strong', '', call.name), badge(call.status));
    card.append(head, node('p', 'info-caption', [call.backend, call.model || 'default model', 'from ' + (call.parent || 'controller')].join(' · ')));
    if (call.task) card.append(node('p', 'call-task', clip(call.task, 400)));
    if (call.error) card.append(node('p', 'error-text', call.error));
    block.append(card);
  }
  root.append(block);
}
function renderFeed() {
  const job = jobById(selected); if (!job) return;
  const cache = feeds.get(job.id) || {events: []};
  const key = job.id + ':' + cache.offset + ':' + job.status + ':' + (cache.diagnostic || '') + ':' + (focus ? focus.id : '');
  if (renderedFeed === key) return;
  renderedFeed = key;
  const feed = $('activity-feed'), previousScroll = feed.scrollTop;
  feed.replaceChildren();
  let events = cache.events;
  if (focus && !focus.single) events = events.filter(e => {
    const f = e.flow;
    if (!f) return false;
    const qualify = name => f.candidate ? f.candidate + '.' + name : name;
    return focus.actors.has(f.actor) || (f.type === 'message.sent' && focus.actors.has(qualify(f.recipient))) ||
      (f.type === 'agent.started' && focus.actors.has(qualify(f.parent)));
  });
  if (!events.length) {
    feed.append(node('div', 'feed-empty', focus ? 'No activity recorded for this node yet.' : job.error || job.waiting_reason ||
      (job.status === 'queued' ? 'Queued. Context is prepared when it starts.' : 'Waiting for activity…')));
  }
  for (const event of events.slice(-350)) {
    const item = node('article', 'event ' + event.kind);
    const title = node('div', 'event-title');
    title.append(node('span', '', event.label), node('span', 'event-status', event.status || ''));
    item.append(title);
    if (event.text) item.append(node('div', 'event-text', event.text));
    if (event.detail) {
      const details = node('details');
      details.append(node('summary', '', 'Output'), node('pre', '', event.detail));
      item.append(details);
    }
    feed.append(item);
  }
  if (job.error && cache.events.length && !focus) {
    const error = node('article', 'event error'); error.append(node('div', 'event-title', 'Task status'), node('div', 'event-text', job.error)); feed.append(error);
  }
  if (cache.diagnostic && !focus) {
    const diagnostic = node('article', 'event diagnostic'), details = node('details');
    details.append(node('summary', '', 'CLI diagnostics (stderr)'), node('pre', '', cache.diagnostic));
    diagnostic.append(details); feed.append(diagnostic);
  }
  feed.scrollTop = $('autoscroll').checked ? feed.scrollHeight : previousScroll;
}
function renderJobConnections(job) {
  const root = $('details-connections'); root.replaceChildren();
  const block = node('div', 'info-block'), run = job.run || {}, info = job.connection || {};
  block.append(node('h3', '', 'Execution route'));
  infoRow(block, 'Working directory', state.root);
  infoRow(block, 'Executable', run.argv ? run.argv[0] : state.settings[job.backend + '_bin'] || job.backend);
  infoRow(block, 'Process ID', activeStates.has(job.status) ? run.pid || 'Starting' : run.pid ? run.pid + ' (finished)' : 'Not started');
  infoRow(block, 'Provider', info.provider);
  infoRow(block, 'Model', info.model);
  infoRow(block, 'Endpoint', info.endpoint || 'Resolved inside the CLI');
  infoRow(block, 'Access', job.backend === 'opencode' ? 'OpenCode permissions · exclusive' : job.permission);
  infoRow(block, 'Native session', run.thread_id);
  if (run.argv) infoRow(block, 'Launch arguments', JSON.stringify(run.argv, null, 2));
  block.append(node('p', 'info-caption', info.source || 'Configured route; not a network trace.'));
  block.append(node('h3', '', 'Live network connections'));
  if ((job.sockets || []).length) {
    for (const socket of job.sockets) block.append(node('div', 'socket-row', socket.address + ':' + socket.port + ' · ' + socket.state));
  } else block.append(node('p', 'info-caption', activeStates.has(job.status) ? 'None visible in this sample.' : 'Sampled only while the process runs.'));
  block.append(node('p', 'info-caption', state.socket_observation));
  if ((info.integrations || []).length) {
    block.append(node('h3', '', 'MCP integrations'));
    for (const entry of info.integrations) infoRow(block, entry.name, (entry.endpoint || entry.executable || 'CLI-managed') + '\n' + entry.state);
  }
  root.append(block);
}
function renderResults(job) {
  const run = job.run || {};
  const key = job.id + JSON.stringify([job.status, run.answer_excerpt, run.checks, run.changes, run.usage, run.loop_outcome, state.verifying]);
  if (resultsKey === key) return;
  resultsKey = key;
  const root = $('tab-results'); root.replaceChildren();
  const block = node('div', 'info-block');
  block.append(node('h3', '', 'Reported result'));
  if (run.loop_outcome) infoRow(block, 'Outcome', run.loop_outcome + ' · round ' + run.loop_iteration);
  block.append(node('div', 'answer-preview', run.answer_excerpt || job.error || 'The answer appears when the task finishes.'));
  block.append(node('p', 'info-caption', 'Reported by the agent. Verification is recorded separately below.'));
  block.append(node('h3', '', 'Verification'));
  if ((run.checks || []).length) {
    for (const check of run.checks) {
      const ok = check.exit_code === 0 && check.contract_passed !== false;
      block.append(node('div', 'check-result ' + (ok ? 'pass' : 'fail'), (ok ? '✓' : '×') + ' exit ' + check.exit_code + ' · ' + check.argv.join(' ') + (check.contract_passed === false ? ' · output contract failed' : '') + (check.source_changed_during_check ? ' · source changed during check' : '')));
    }
    block.append(node('p', 'info-caption', 'Historical results; later source changes make them stale.'));
  } else block.append(node('p', 'info-caption', 'No verification recorded.'));
  if (finishedStates.has(job.status) && job.run_id) {
    const form = node('form', 'check-form'), input = node('input');
    input.placeholder = 'python3 -m unittest discover -s tests'; input.required = true; input.setAttribute('aria-label', 'Verification command');
    const submit = node('button', 'button secondary small', state.verifying ? 'Checking…' : 'Run check'); submit.disabled = state.verifying;
    form.append(input, submit);
    form.addEventListener('submit', async e => { e.preventDefault(); try { await api('/api/verify', {id: job.id, command: input.value}); toast('Verification started.'); await refresh(); } catch (err) { toast(err.message); } });
    block.append(form);
  }
  if (run.changes) {
    const paths = ['added', 'changed', 'removed'].flatMap(k => (run.changes[k] || []).map(p => k + ': ' + p));
    infoRow(block, 'Changed files', paths.join('\n') || 'None in scanned source');
  }
  infoRow(block, 'Reported usage', run.usage && Object.keys(run.usage).length ? JSON.stringify(run.usage) : 'Not reported');
  if (run.reported_cost_usd !== null && run.reported_cost_usd !== undefined) infoRow(block, 'Reported cost', '$' + run.reported_cost_usd.toFixed(4) + ' (estimate)');
  block.append(node('h3', '', 'Files'));
  const artifacts = node('div', 'artifacts-grid');
  const fileNames = ['answer.md', 'PACKET.md', 'events.jsonl', 'stderr.log', 'manifest.json', 'launch.json'];
  if (job.backend === 'claude-sdk') fileNames.push('loop/state.json', 'loop/messages.jsonl');
  for (const file of fileNames) {
    const b = button(file, 'artifact-button', () => openArtifact(job.id, file)); b.disabled = !job.run_id; artifacts.append(b);
  }
  block.append(artifacts);
  root.append(block);
}
async function openArtifact(id, name) {
  try {
    const result = await api('/api/artifact?id=' + encodeURIComponent(id) + '&name=' + encodeURIComponent(name));
    showText(name, result.text);
  } catch (error) { toast(error.message); }
}
function showText(title, text) {
  $('artifact-title').textContent = title; $('artifact-text').textContent = text;
  if (!$('artifact-dialog').open) $('artifact-dialog').showModal();
}

/* ---------- Connections & history ---------- */
function renderConnections(force = false) {
  if (currentView !== 'connections') return;
  const key = JSON.stringify([state.settings, state.backends, state.demo, state.managed]);
  if (!force && key === connectionsKey) return;
  connectionsKey = key;
  const root = $('connections-content'); root.replaceChildren();
  const grid = node('div', 'connection-layout');
  for (const backend of state.backends) {
    const card = node('form', 'panel connection-card'), head = node('div', 'connection-head');
    head.append(node('h2', '', backend.id === 'codex' ? 'Codex' : 'OpenCode'), badge(backend.resolved ? 'detected' : 'not found'));
    card.append(head);
    const label = node('label', 'field', 'Executable path'), input = node('input');
    input.value = state.settings[backend.id + '_bin']; input.required = true; input.disabled = state.demo;
    label.append(input); card.append(label);
    const output = node('div', 'probe-output'); output.hidden = true;
    const actions = node('div', 'connection-buttons');
    const save = node('button', 'button primary small', 'Save'); save.disabled = state.demo;
    const probe = button('Check CLI', 'button secondary small', async () => {
      probe.disabled = true;
      try { const result = await api('/api/probe', {backend: backend.id}); output.hidden = false; output.textContent = result.message + '\n' + (result.note || ''); }
      catch (error) { toast(error.message); }
      finally { probe.disabled = false; }
    });
    actions.append(save, probe); card.append(actions, output);
    card.addEventListener('submit', async e => {
      e.preventDefault();
      try { await api('/api/settings', {[backend.id + '_bin']: input.value}); connectionsKey = ''; await refresh(); toast('Saved.'); }
      catch (error) { toast(error.message); }
    });
    const info = backend.connection || {};
    infoRow(card, 'Resolved binary', backend.resolved || 'Not on PATH');
    infoRow(card, 'Default provider', info.provider);
    infoRow(card, 'Default model', info.model);
    grid.append(card);
  }
  const managed = node('div', 'panel connection-card');
  const managedHead = node('div', 'connection-head');
  managedHead.append(node('h2', '', 'Managed agents'), badge(state.managed && state.managed.configured ? 'detected' : 'not found'));
  managed.append(managedHead);
  infoRow(managed, 'Claude SDK', state.managed && state.managed.sdk_installed ? 'Installed' : 'Not installed (pip install ".[agents]")');
  infoRow(managed, 'Definitions', state.managed && state.managed.configured ? '.agentctl/agents.json' : 'Run agentctl agents init');
  infoRow(managed, 'Workflows', ((state.managed && state.managed.workflows) || []).join(', ') || 'None');
  grid.append(managed);
  root.append(grid, node('p', 'info-caption page-note', 'Log in with each CLI in your terminal first; agentctl reuses that authentication and never asks for API keys. Project: ' + state.root));
}
function renderHistory() {
  if (currentView !== 'history') return;
  const root = $('history-content'); root.replaceChildren();
  const rows = [...state.jobs.filter(j => finishedStates.has(j.status)).map(j => ({...j, dashboard_id: j.id})), ...state.history];
  rows.sort((a, b) => b.created.localeCompare(a.created));
  if (!rows.length) { root.append(node('div', 'empty-list', 'Finished runs appear here.')); return; }
  const table = node('table', 'history-table'), head = node('thead'), hr = node('tr');
  for (const text of ['Task', 'Status', 'Runtime', 'Started']) hr.append(node('th', '', text));
  head.append(hr); table.append(head); const body = node('tbody');
  for (const row of rows) {
    const tr = node('tr'), cell = node('td');
    cell.append(button(row.name || row.goal, 'text-button', () => {
      if (row.dashboard_id) { setView('agents'); selectJob(row.dashboard_id); }
      else openArtifact(row.id, 'answer.md');
    }));
    const status = node('td'); status.append(badge(row.status));
    tr.append(cell, status, node('td', 'mono', BACKENDS[row.backend] || row.backend), node('td', 'muted', new Date(row.created).toLocaleString()));
    body.append(tr);
  }
  table.append(body); root.append(table);
}

/* ---------- Data loop ---------- */
async function loadEvents() {
  const job = jobById(selected); if (!job || !job.run_id) return;
  const id = job.id, cache = feeds.get(id) || {offset: 0, events: []};
  if (cache.loading || (cache.done && finishedStates.has(job.status))) return;
  cache.loading = true; feeds.set(id, cache);
  try {
    let result;
    do { // Catch up on finished runs quickly; the server returns at most 150 events per call.
      result = await api('/api/events?id=' + encodeURIComponent(id) + '&offset=' + cache.offset);
      cache.offset = result.offset; cache.events.push(...result.events);
    } while (result.events.length >= 150);
    cache.diagnostic = result.diagnostic;
    if (cache.events.length > 2000) cache.events = cache.events.slice(-2000);
    if (!result.events.length && finishedStates.has(job.status)) cache.done = true;
    if (selected === id) { renderFlow(job); renderFeed(); }
  } catch (error) { /* A queued task may not have written events yet; the next poll retries. */ }
  finally { cache.loading = false; }
}
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    state = await api('/api/state');
    $('server-state').textContent = 'Connected'; $('server-dot').classList.remove('down');
    $('project-name').textContent = state.project; $('project-info').title = state.root;
    $('demo-banner').hidden = !state.demo;
    renderSetupCard();
    const active = state.jobs.filter(j => activeStates.has(j.status)).length;
    const queued = state.jobs.filter(j => j.status === 'queued').length;
    $('agent-count').textContent = state.jobs.length;
    $('live-counts').textContent = state.verifying ? 'verifying…' : active + ' running · ' + queued + ' queued';
    if (!selected && state.jobs.length) selected = state.jobs.find(j => activeStates.has(j.status))?.id || state.jobs[0].id;
    const workflow = $('launch-workflow');
    const nextWorkflowKey = JSON.stringify([state.workflows, state.managed && state.managed.workflows]);
    if (workflowKey !== nextWorkflowKey && !$('launch-dialog').open) {
      const previous = workflow.value;
      workflow.replaceChildren();
      const single = node('option', '', 'Single agent'); single.value = ''; workflow.append(single);
      for (const name of (state.managed && state.managed.workflows) || []) {
        const option = node('option', '', 'Managed · ' + name); option.value = 'managed:' + name; workflow.append(option);
      }
      for (const item of state.workflows) {
        const option = node('option', '', item.name + ' · ' + item.agents + ' agents'); option.value = item.name; workflow.append(option);
      }
      workflow.value = previous; workflowKey = nextWorkflowKey;
    }
    renderList(); renderDetail(); renderConnections(); renderHistory(); await loadEvents();
  } catch (error) {
    $('server-state').textContent = 'Disconnected'; $('server-dot').classList.add('down');
    if (!state) toast(error.message);
  } finally { busy = false; }
}

/* ---------- Launch & controls ---------- */
function accessOptions() {
  const backend = $('launch-backend').value, select = $('launch-permission');
  select.replaceChildren();
  const options = backend === 'claude-sdk' ? [['workspace-write', 'Scopes from agents.json']] : backend === 'opencode' ? [['workspace-write', 'OpenCode permissions · exclusive']] :
    backend === 'local' || backend === 'codex-local' ? [['read-only', 'Read-only']] :
    [['read-only', 'Read-only'], ['workspace-write', 'Can write · exclusive']];
  for (const [value, text] of options) { const option = node('option', '', text); option.value = value; select.append(option); }
  $('launch-note').textContent = backend === 'claude-sdk' ? 'Runs a workflow from .agentctl/agents.json (feedback loop or swarm).' :
    backend === 'opencode' ? 'Uses your OpenCode permissions; runs alone in this project.' : 'Uses your existing CLI login. Writers run one at a time.';
}
function openLaunch(job) {
  $('launch-form').reset(); $('launch-name').disabled = false; $('launch-backend').disabled = false;
  $('launch-submit').textContent = 'Launch';
  if (job) {
    $('launch-name').value = job.name; $('launch-goal').value = job.goal;
    $('launch-backend').value = job.backend; $('launch-model').value = job.model || '';
    $('launch-includes').value = (job.includes || []).join('\n');
    if (job.backend === 'claude-sdk') { $('launch-workflow').value = 'managed:' + (job.agent_workflow || 'default'); $('launch-backend').disabled = true; }
  }
  accessOptions();
  if (job) $('launch-permission').value = job.permission;
  $('launch-dialog').showModal(); setTimeout(() => $('launch-goal').focus(), 20);
}
for (const item of document.querySelectorAll('[data-view]')) item.addEventListener('click', () => setView(item.dataset.view));
for (const item of document.querySelectorAll('[data-filter]')) item.addEventListener('click', () => {
  filter = item.dataset.filter; for (const b of document.querySelectorAll('[data-filter]')) b.classList.toggle('active', b === item); renderList();
});
function setTab(name) {
  tab = name;
  for (const b of document.querySelectorAll('[data-tab]')) { b.classList.toggle('active', b.dataset.tab === tab); b.setAttribute('aria-selected', String(b.dataset.tab === tab)); }
  for (const name of ['activity', 'results', 'details']) $('tab-' + name).hidden = name !== tab;
}
for (const item of document.querySelectorAll('[data-tab]')) item.addEventListener('click', () => setTab(item.dataset.tab));
for (const item of document.querySelectorAll('[data-close]')) item.addEventListener('click', () => $(item.dataset.close).close());
$('new-agent').addEventListener('click', () => openLaunch());
$('empty-launch').addEventListener('click', () => openLaunch());
$('setup-agents').addEventListener('click', async () => {
  $('setup-agents').disabled = true;
  try { await api('/api/setup', {agents: true}); await refresh(); toast('Starter agents are ready.'); }
  catch (error) { toast(error.message); }
  finally { $('setup-agents').disabled = false; }
});
$('setup-project').addEventListener('click', async () => {
  $('setup-project').disabled = true;
  try { await api('/api/setup', {agents: false}); await refresh(); toast('Workspace settings are ready.'); }
  catch (error) { toast(error.message); }
  finally { $('setup-project').disabled = false; }
});
$('repeat-agent').addEventListener('click', () => openLaunch(jobById(selected)));
$('flow-clear').addEventListener('click', () => {
  const id = focus?.id;
  focus = null;
  const job = jobById(selected);
  if (job) {
    renderFlow(job); renderFeed();
    [...$('flow-map').querySelectorAll('[data-agent-id]')].find(group => group.dataset.agentId === id)?.focus();
  }
});
$('inspector-message').addEventListener('click', () => {
  if (!focus || $('message-recipient').hidden) return;
  setTab('activity');
  $('message-recipient').value = focus.label;
  $('followup-text').focus();
});
$('launch-backend').addEventListener('change', accessOptions);
$('launch-workflow').addEventListener('change', () => {
  const value = $('launch-workflow').value, managed = value.startsWith('managed:');
  $('launch-name').disabled = !!value && !managed;
  $('launch-backend').disabled = managed;
  if (managed) $('launch-backend').value = 'claude-sdk';
  else if ($('launch-backend').value === 'claude-sdk') $('launch-backend').value = 'codex';
  accessOptions();
});
$('launch-form').addEventListener('submit', async e => {
  e.preventDefault(); $('launch-submit').disabled = true;
  const spec = {name: $('launch-name').value, goal: $('launch-goal').value, backend: $('launch-backend').value,
    permission: $('launch-permission').value, model: $('launch-model').value,
    includes: $('launch-includes').value.split('\n').map(s => s.trim()).filter(Boolean), workflow: $('launch-workflow').value};
  if (spec.backend === 'claude-sdk' && !spec.workflow.startsWith('managed:')) spec.workflow = 'managed:default';
  if (spec.workflow.startsWith('managed:')) { spec.agent_workflow = spec.workflow.slice('managed:'.length); spec.workflow = ''; spec.backend = 'claude-sdk'; }
  try {
    const result = await api(spec.workflow ? '/api/workflow' : '/api/jobs', spec);
    selected = Array.isArray(result) ? result[0].id : result.id; focus = null;
    $('launch-dialog').close(); setView('agents'); await refresh(); toast(spec.workflow ? 'Workflow queued.' : 'Agent queued.');
  } catch (error) { toast(error.message); }
  finally { $('launch-submit').disabled = false; }
});
$('stop-agent').addEventListener('click', async () => {
  try { await api('/api/cancel', {id: selected}); await refresh(); toast('Stop requested.'); } catch (error) { toast(error.message); }
});
$('followup-text').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('followup-form').requestSubmit(); } });
$('followup-form').addEventListener('submit', async e => {
  e.preventDefault(); $('followup-send').disabled = true;
  try {
    const current = jobById(selected);
    if (current && current.backend === 'claude-sdk' && activeStates.has(current.status)) {
      await api('/api/agent-message', {id: selected, to: $('message-recipient').value, message: $('followup-text').value});
      $('followup-text').value = ''; $('followup-send').disabled = false; toast('Message queued.'); return;
    }
    const result = await api('/api/followup', {id: selected, goal: $('followup-text').value});
    selected = result.id; focus = null; $('followup-text').value = ''; await refresh(); toast('Follow-up queued.');
  } catch (error) { toast(error.message); $('followup-send').disabled = false; }
});
$('project-info').addEventListener('click', async () => {
  if (!state) return;
  showText('Workspace', state.root + '\n\nInspecting repository evidence…');
  try { const result = await api('/api/inspect'); $('artifact-text').textContent = state.root + '\n\n' + JSON.stringify(result, null, 2); }
  catch (error) { $('artifact-text').textContent = error.message; }
});
document.addEventListener('keydown', e => {
  const tag = document.activeElement.tagName;
  if (e.key.toLowerCase() === 'n' && !e.metaKey && !e.ctrlKey && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) && !$('launch-dialog').open && !$('artifact-dialog').open) openLaunch();
});
$('glass-mascot').append(brandPortrait());
refresh(); setInterval(refresh, 1000);
