'use strict';

// The atlas contains Vitreum's opal, glacier, ember and graphite droplet renders.
const materialCell = 144;
const reducedPortraitMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

function dropletEyes(status, spacing) {
  const gaze = svg('g', {class: 'droplet-gaze'});
  const lids = svg('g', {class: 'droplet-eyes', fill: '#272932'});
  for (const side of [-1, 1]) {
    const x = 32 + side * spacing, y = 32;
    if (status === 'completed' || status === 'interrupted' || status === 'cancelled') {
      const rise = status === 'completed' ? -3.5 : 1.4;
      lids.append(svg('path', {d: `M${x-2.8} ${y}q2.8 ${rise} 5.6 0`, fill: 'none', stroke: '#292a32',
        'stroke-width': 1.9, 'stroke-linecap': 'round'}));
    } else {
      const worried = ['failed', 'blocked', 'scope_violation'].includes(status);
      const eye = svg('g', {transform: `rotate(${worried ? -side * 22 : 0} ${x} ${y})`});
      eye.append(svg('ellipse', {cx: x, cy: y, rx: 2.9, ry: status === 'running' ? 4.5 : 3.4}));
      eye.append(svg('circle', {cx: x - .8, cy: y - 1.35, r: .85, fill: '#fff'}));
      lids.append(eye);
    }
  }
  gaze.append(lids);
  return gaze;
}

function agentPortrait(identity, status) {
  let seed = 2166136261;
  for (const char of identity) seed = Math.imul(seed ^ char.codePointAt(0), 16777619) >>> 0;
  const root = svg('svg', {viewBox: '0 0 64 64', class: 'agent-portrait ' + status, 'aria-hidden': true});
  const material = seed % 4, angle = (seed >>> 12) % 31 - 15;
  const field = svg('g', {class: 'droplet-field', transform: `rotate(${angle - 14} 32 36)`,
    fill: 'none', stroke: '#505965', 'stroke-width': .6});
  for (const [rx, ry] of [[30, 11], [26, 15]]) field.append(svg('ellipse', {cx: 32, cy: 36, rx, ry}));
  const pull = svg('g', {class: 'droplet-pull'}), body = svg('g', {class: 'droplet-body'});
  const crop = svg('svg', {x: 0, y: 0, width: 64, height: 64, overflow: 'hidden',
    viewBox: `${material * materialCell} 0 ${materialCell} ${materialCell}`, transform: `rotate(${angle} 32 32)`});
  crop.append(svg('image', {href: '/droplets.webp', width: 4 * materialCell, height: materialCell}));
  body.append(crop, dropletEyes(status, 5.3 + (seed >>> 20) % 3));
  pull.append(body);
  root.append(field, pull);
  return root;
}

function brandPortrait() {
  const portrait = agentPortrait('glass-mascot', 'completed');
  portrait.querySelector('.droplet-body').append(svg('path', {
    d: 'M27 38 Q32 45 37 38', fill: 'none', stroke: '#272932',
    'stroke-width': 2.4, 'stroke-linecap': 'round',
  }));
  return portrait;
}

// Translate in portrait coordinates so the response stays small at every viewport size.
function attachDropletPull(target, portrait) {
  const pull = portrait.querySelector('.droplet-pull'), gaze = portrait.querySelector('.droplet-gaze');
  const release = () => {
    pull.removeAttribute('transform');
    gaze.removeAttribute('transform');
  };
  target.addEventListener('pointermove', event => {
    if (!event.isPrimary || event.pointerType === 'touch' || reducedPortraitMotion.matches) { release(); return; }
    const rect = portrait.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = (event.clientX - rect.left) / rect.width - .5, dy = (event.clientY - rect.top) / rect.height - .5;
    const length = Math.max(.5, Math.hypot(dx, dy));
    pull.setAttribute('transform', `translate(${2.1 * dx / length} ${1.8 * dy / length})`);
    gaze.setAttribute('transform', `translate(${2.4 * dx / length} ${1.5 * dy / length})`);
  });
  target.addEventListener('pointerleave', release);
  target.addEventListener('pointercancel', release);
}

function jobPortraitId(job) { return job.backend + '/' + job.name; }

function setPortrait(host, identity, status) {
  const key = JSON.stringify([identity, status]);
  if (host.dataset.portrait !== key) {
    host.replaceChildren(agentPortrait(identity, status));
    host.dataset.portrait = key;
  }
}
