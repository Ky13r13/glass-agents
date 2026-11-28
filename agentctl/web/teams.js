'use strict';

{
  const fields = {
    'team-backend': 'backend', 'team-model': 'model', 'team-instructions': 'instructions', 'team-scope': 'write_scope'
  };
  let saved, roster = [], active, dirty = false, pending = false, promptDraft = false;
  const form = $('team-form');
  const workflow = $('team-workflow');
  const lead = $('team-lead');
  const reviewer = $('team-reviewer');
  const status = $('team-status');
  const draftSummary = $('team-draft-summary');
  let watchingDraft = null;

  function options(select, entries, selected) {
    select.replaceChildren(...entries.map(([value, label]) => new Option(label, value)));
    select.value = entries.some(([value]) => selected === value) ? selected : '';
  }

  function controls() {
    form.inert = pending;
    $('team-reload').disabled = pending;
    $('team-run').disabled = dirty || !workflow.value || pending;
    $('team-copy').disabled = !active || roster.length === saved?.max_agents;
    $('team-add').disabled = !saved || roster.length === saved.max_agents;
  }

  function changed() {
    dirty = true;
    promptDraft = false;
    draftSummary.hidden = true;
    status.textContent = 'Unsaved changes';
    controls();
  }

  function renderRoster() {
    $('team-count').textContent = roster.length + ' / ' + saved.max_agents;
    $('team-count-add').max = saved.max_agents;
    $('team-roster').replaceChildren(...roster.map(agent => {
      const row = button('', 'team-agent', () => {
        active = agent;
        renderRoster();
        renderAgent();
        $('team-agent-name').focus();
      });
      row.setAttribute('aria-pressed', String(agent === active));
      row.append(node('strong', '', agent.name), node('span', 'muted',
        agent.backend === 'claude-sdk' ? 'Claude' : BACKENDS[agent.backend]));
      return row;
    }));
  }

  function renderAgent() {
    $('team-editor').hidden = !active;
    $('team-no-agent').hidden = !!active;
    controls();
    if (!active) return;
    $('team-agent-name').value = active.name;
    $('team-agent-name').readOnly = saved.agents.some(agent => agent.name === active.name);
    for (const [id, field] of Object.entries(fields)) {
      $(id).value = field === 'write_scope' ? active[field].join(', ') : active[field] ?? '';
    }
    $('team-peers').replaceChildren(...roster.filter(agent => agent !== active).map(agent => {
      const label = node('label');
      const checkbox = node('input');
      checkbox.type = 'checkbox';
      checkbox.checked = active.peers.includes(agent.name);
      checkbox.addEventListener('change', () => {
        active.peers = checkbox.checked ? [...active.peers, agent.name] : active.peers.filter(name => name !== agent.name);
        changed();
      });
      label.append(checkbox, document.createTextNode(agent.name));
      return label;
    }));
  }

  function renderRoles(leadName = lead.value, reviewerName = reviewer.value) {
    options(lead, [['', 'Choose lead'], ...roster.map(agent => [agent.name, agent.name])], leadName);
    const eligible = roster.filter(agent => agent.name !== lead.value && !agent.write_scope.length);
    options(reviewer, [['', 'No reviewer'], ...eligible.map(agent => [agent.name, agent.name])], reviewerName);
  }

  function chooseWorkflow() {
    const spec = saved.workflows[workflow.value];
    $('team-workflow-new').hidden = !!spec;
    renderRoles(spec?.agent || spec?.worker || '', spec?.reviewer || '');
    controls();
  }

  function showTeam(result, name = '') {
    saved = result;
    roster = structuredClone(saved.agents);
    active = roster.find(agent => agent.name === active?.name) || roster[0];
    const editable = Object.entries(saved.workflows).filter(([, spec]) => spec.type !== 'swarm');
    options(workflow, [['', 'Create workflow'], ...editable.map(([key]) => [key, key])], name);
    dirty = false;
    promptDraft = false;
    draftSummary.hidden = true;
    status.textContent = '';
    form.hidden = false;
    $('team-reload').hidden = false;
    chooseWorkflow();
    renderRoster();
    renderAgent();
  }

  async function loadTeam() {
    if (pending || (dirty && !window.confirm('Discard unsaved team changes?'))) return;
    pending = true;
    controls();
    status.textContent = 'Loading agent definitions…';
    try {
      const result = await api('/api/team');
      showTeam(result, workflow.value || Object.keys(result.workflows).find(name => result.workflows[name].type !== 'swarm'));
      const current = await api('/api/team/draft');
      if (current && ['running', 'cancelling'].includes(current.status)) {
        await watchDraft(current);
      }
    } catch (error) {
      status.textContent = error.message;
      $('team-reload').hidden = false;
    } finally { pending = false; controls(); }
  }

  function addAgents(source) {
    const count = $('team-count-add').valueAsNumber;
    if (!Number.isInteger(count) || count < 1 || count + roster.length > saved.max_agents) {
      toast('Enter a count that keeps the roster within ' + saved.max_agents + ' agents.');
      return;
    }
    const template = source || {backend: 'claude-sdk', model: null, instructions: '', write_scope: [], peers: []};
    let index = 1;
    for (let added = 0; added < count; added++) {
      while (roster.some(agent => agent.name === 'agent-' + index)) index++;
      const agent = {...structuredClone(template), name: 'agent-' + index++};
      roster.push(agent);
      if (added === 0) active = agent;
    }
    changed();
    renderRoster();
    renderRoles(lead.value || active.name);
    renderAgent();
    $('team-agent-name').focus();
  }

  document.querySelector('[data-view="teams"]').addEventListener('click', () => { if (!saved) loadTeam(); });
  $('team-reload').addEventListener('click', loadTeam);
  $('team-add').addEventListener('click', () => addAgents());
  $('team-copy').addEventListener('click', () => addAgents(active));
  $('team-remove').addEventListener('click', () => {
    roster = roster.filter(agent => agent !== active);
    for (const agent of roster) agent.peers = agent.peers.filter(name => name !== active.name);
    active = roster[0];
    changed();
    renderRoster();
    renderRoles();
    renderAgent();
  });

  $('team-agent-name').addEventListener('change', e => {
    const previous = active.name;
    const name = e.target.value.trim();
    if (!/^[a-zA-Z][a-zA-Z0-9_-]{0,39}$/.test(name) || name === 'controller' ||
        roster.some(agent => agent !== active && agent.name === name)) {
      toast('Choose a unique agent name beginning with a letter.');
      e.target.value = previous;
      return;
    }
    const renamed = value => value === previous ? name : value;
    active.name = name;
    for (const agent of roster) agent.peers = agent.peers.map(renamed);
    changed();
    renderRoster();
    renderRoles(renamed(lead.value), renamed(reviewer.value));
  });
  for (const [id, field] of Object.entries(fields)) {
    $(id).addEventListener('input', e => {
      const value = e.target.value;
      if (field === 'write_scope') active[field] = value.split(',').map(path => path.trim()).filter(Boolean);
      else if (field === 'model') active[field] = value.trim() || null;
      else active[field] = value;
      if (field === 'backend') renderRoster();
      if (field === 'write_scope') renderRoles();
      changed();
    });
  }
  workflow.addEventListener('change', chooseWorkflow);
  lead.addEventListener('change', () => { renderRoles(); changed(); });
  reviewer.addEventListener('change', changed);
  $('team-workflow-name').addEventListener('input', changed);

  function showDraftProgress(draft) {
    const provider = draft.provider === 'claude-sdk' ? 'Claude' : BACKENDS[draft.provider] || 'Planner';
    const stages = {starting: 'Starting ' + provider, drafting: provider + ' is drafting roles and communication links',
      validating: 'Checking team configuration'};
    const outcomes = {completed: 'Team proposal ready', failed: 'Draft failed', cancelled: 'Draft cancelled',
      cancelling: 'Stopping ' + provider};
    $('team-progress').hidden = false;
    $('team-progress-stage').textContent = outcomes[draft.status] || stages[draft.stage];
    const minutes = Math.floor(draft.elapsed_seconds / 60);
    const seconds = String(draft.elapsed_seconds % 60).padStart(2, '0');
    $('team-progress-time').textContent = minutes + ':' + seconds;
    const active = ['running', 'cancelling'].includes(draft.status);
    $('team-progress-bar').hidden = !active;
    $('team-draft-cancel').hidden = draft.status !== 'running';
    const idle = draft.seconds_since_activity ?? draft.elapsed_seconds;
    const waiting = draft.seconds_since_activity === null ? 'Waiting for the first ' + provider + ' event.'
      : 'Last ' + provider + ' output ' + draft.seconds_since_activity + 's ago.';
    const quiet = idle >= 30 ? ' You can keep waiting or cancel.' : '';
    $('team-progress-activity').textContent = draft.error || (active && draft.stage !== 'validating' ? waiting + quiet : '');
  }

  function applyDraft(result) {
    if (result.revision !== saved.revision) throw new Error('Agent definitions changed. Reload Teams before drafting.');
    if (saved.agents.length + result.agents.length > saved.max_agents) throw new Error('The draft would exceed the roster limit.');
    roster = [...structuredClone(saved.agents), ...result.agents];
    active = roster.find(agent => agent.name === result.workflow.lead);
    workflow.value = '';
    $('team-workflow-new').hidden = false;
    $('team-workflow-name').value = result.workflow.name;
    renderRoles(result.workflow.lead, result.workflow.reviewer);
    changed();
    promptDraft = true;
    draftSummary.textContent = result.summary + ' Review the draft, then save it before launching.';
    draftSummary.hidden = false;
    renderRoster();
    renderAgent();
  }

  async function watchDraft(draft) {
    const id = draft.id;
    watchingDraft = id;
    try {
      showDraftProgress(draft);
      let failures = 0;
      while (['running', 'cancelling'].includes(draft.status)) {
        await new Promise(resolve => setTimeout(resolve, 750));
        try {
          draft = await api('/api/team/draft');
          failures = 0;
        } catch (error) {
          failures++;
          $('team-progress-activity').textContent = 'Reconnecting to the dashboard…';
          if (failures >= 5) throw new Error('Could not check the draft. Reload saved team to reconnect.');
          continue;
        }
        if (draft?.id !== id) throw new Error('This draft is no longer available.');
        showDraftProgress(draft);
      }
      if (draft.status === 'completed') {
        if (!$('team-goal').value) $('team-goal').value = draft.goal;
        applyDraft(draft.result);
      } else {
        status.textContent = dirty ? 'Unsaved changes' : '';
      }
    } finally { watchingDraft = null; }
  }

  $('team-draft-cancel').addEventListener('click', async () => {
    if (!watchingDraft) return;
    try { showDraftProgress(await api('/api/team/draft/cancel', {id: watchingDraft})); }
    catch (error) { toast(error.message); }
  });

  $('team-draft').addEventListener('click', async () => {
    if (pending) return;
    if (dirty && !promptDraft) {
      toast('Save or reload your manual edits before drafting a new team.');
      return;
    }
    const goal = $('team-goal').value.trim();
    if (!goal) {
      $('team-goal').focus();
      toast('Describe the task first.');
      return;
    }
    const backends = [...$('team-draft-backends').querySelectorAll('input:checked')].map(input => input.value);
    if (!backends.length) {
      toast('Select the runtimes the team can use.');
      return;
    }
    pending = true;
    controls();
    status.textContent = '';
    try {
      await watchDraft(await api('/api/team/draft', {
        goal, mode: $('team-draft-mode').value, provider: $('team-draft-provider').value, backends,
      }));
    } catch (error) {
      status.textContent = dirty ? 'Unsaved changes' : '';
      $('team-progress').hidden = true;
      toast(error.message);
    }
    finally { pending = false; controls(); }
  });

  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (pending) return;
    const missing = roster.find(agent => !agent.instructions.trim());
    if (missing) {
      active = missing;
      renderRoster();
      renderAgent();
      $('team-instructions').focus();
      toast('Add instructions for ' + missing.name + '.');
      return;
    }
    const name = workflow.value || $('team-workflow-name').value.trim();
    if (!workflow.value && Object.hasOwn(saved.workflows, name)) {
      toast('Choose a unique name or select the existing workflow.');
      return;
    }
    pending = true;
    controls();
    try {
      const result = await api('/api/team', {revision: saved.revision, agents: roster,
        workflow: {name, lead: lead.value, reviewer: reviewer.value}});
      showTeam(result, name);
      toast('Team saved.');
      refresh();
    } catch (error) { toast(error.message); }
    finally { pending = false; controls(); }
  });

  $('team-run').addEventListener('click', async () => {
    if (dirty || pending || !workflow.value) return;
    const goal = $('team-goal').value.trim();
    if (!goal) {
      $('team-goal').focus();
      toast('Describe what the team should do.');
      return;
    }
    pending = true;
    controls();
    try {
      const job = await api('/api/team/launch', {revision: saved.revision, workflow: workflow.value, goal});
      selected = job.id;
      focus = null;
      $('team-goal').value = '';
      setView('agents');
      await refresh();
      toast('Team queued.');
    } catch (error) { toast(error.message); }
    finally { pending = false; controls(); }
  });
}
