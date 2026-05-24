// Force-directed graph rendered on an HTML Canvas
// Physics: repulsion + spring attraction + friction

const CATEGORY_COLORS = {
  Sources:  '#7c6af7',
  Concepts: '#34d399',
  Entities: '#fbbf24',
  root:     '#6b7280',
};

let nodes = [], edges = [], sim = null, canvas = null, ctx = null;
let transform = { x: 0, y: 0, scale: 1 };
let dragging = null, dragOffset = { x: 0, y: 0 };
let hoveredNode = null;

// ─── Init ─────────────────────────────────────────────────────────────────
window.addEventListener('graph-activate', initGraph);
window.addEventListener('vault-changed', () => { if (!canvas?.classList?.contains('hidden')) initGraph(); });

async function initGraph() {
  canvas = document.getElementById('graph-canvas');
  ctx    = canvas.getContext('2d');
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);

  const vault = window.currentVault || document.getElementById('vault-select')?.value;
  if (!vault) return;

  const data = await fetch(`/api/vaults/${vault}/graph`).then(r => r.json());
  if (!data.nodes.length) {
    ctx.fillStyle = '#6b7280';
    ctx.font = '14px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No pages indexed yet.', canvas.width / 2, canvas.height / 2);
    return;
  }

  buildGraph(data.nodes, data.edges);
  attachEvents();
  if (sim) cancelAnimationFrame(sim);
  loop();
}

function resizeCanvas() {
  if (!canvas) return;
  canvas.width  = canvas.offsetWidth  || canvas.parentElement.offsetWidth;
  canvas.height = canvas.offsetHeight || canvas.parentElement.offsetHeight;
}

// ─── Build physics graph ──────────────────────────────────────────────────
function buildGraph(rawNodes, rawEdges) {
  const cx = canvas.width / 2, cy = canvas.height / 2;
  nodes = rawNodes.map((n, i) => {
    const angle = (i / rawNodes.length) * Math.PI * 2;
    const r     = Math.min(cx, cy) * 0.5;
    return {
      ...n,
      x: cx + r * Math.cos(angle) + (Math.random() - .5) * 60,
      y: cy + r * Math.sin(angle) + (Math.random() - .5) * 60,
      vx: 0, vy: 0,
      radius: 5 + Math.min(n.backlink_count * 2, 14),
    };
  });
  edges = rawEdges.map(e => ({
    source: nodes[e.source],
    target: nodes[e.target],
  })).filter(e => e.source && e.target);
}

// ─── Simulation ───────────────────────────────────────────────────────────
function tick() {
  const k   = 120;       // spring rest length
  const kr  = 3500;      // repulsion
  const fric = 0.82;

  // Repulsion between all node pairs
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx*dx + dy*dy) || 1;
      const force = kr / (dist * dist);
      const fx = force * dx / dist, fy = force * dy / dist;
      a.vx -= fx; a.vy -= fy;
      b.vx += fx; b.vy += fy;
    }
  }

  // Spring attraction along edges
  for (const e of edges) {
    const dx = e.target.x - e.source.x, dy = e.target.y - e.source.y;
    const dist = Math.sqrt(dx*dx + dy*dy) || 1;
    const force = (dist - k) * 0.04;
    const fx = force * dx / dist, fy = force * dy / dist;
    e.source.vx += fx; e.source.vy += fy;
    e.target.vx -= fx; e.target.vy -= fy;
  }

  // Center gravity (weak)
  const cx = canvas.width / 2, cy = canvas.height / 2;
  for (const n of nodes) {
    n.vx += (cx - n.x) * 0.002;
    n.vy += (cy - n.y) * 0.002;
  }

  // Integrate
  for (const n of nodes) {
    if (n === dragging) continue;
    n.vx *= fric; n.vy *= fric;
    n.x += n.vx; n.y += n.vy;
  }
}

function loop() {
  tick();
  draw();
  sim = requestAnimationFrame(loop);
}

// ─── Drawing ──────────────────────────────────────────────────────────────
function draw() {
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.save();
  ctx.translate(transform.x, transform.y);
  ctx.scale(transform.scale, transform.scale);

  // Edges
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  for (const e of edges) {
    ctx.beginPath();
    ctx.moveTo(e.source.x, e.source.y);
    ctx.lineTo(e.target.x, e.target.y);
    ctx.stroke();
  }

  // Nodes
  for (const n of nodes) {
    const color  = CATEGORY_COLORS[n.category] || '#6b7280';
    const isHov  = n === hoveredNode;
    const r      = n.radius * (isHov ? 1.4 : 1);

    // glow for hovered / hub nodes
    if (isHov || n.backlink_count > 3) {
      ctx.shadowBlur  = isHov ? 20 : 10;
      ctx.shadowColor = color;
    }

    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = isHov ? '#fff' : color;
    ctx.fill();
    ctx.shadowBlur = 0;

    // Label for hovered or hub nodes
    if (isHov || n.backlink_count > 4) {
      ctx.fillStyle = isHov ? '#fff' : 'rgba(255,255,255,0.6)';
      ctx.font = `${isHov ? 12 : 10}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.fillText(n.title.slice(0, 24), n.x, n.y - r - 4);
    }
  }

  ctx.restore();
}

// ─── Events ───────────────────────────────────────────────────────────────
function attachEvents() {
  canvas.onmousedown = e => {
    const pt = toGraph(e);
    dragging = nodes.find(n => dist2(n, pt) < (n.radius + 4) ** 2) || null;
    if (dragging) { dragOffset = { x: pt.x - dragging.x, y: pt.y - dragging.y }; }
    else { dragOffset = { x: e.clientX - transform.x, y: e.clientY - transform.y }; }
  };

  canvas.onmousemove = e => {
    if (dragging) {
      const pt = toGraph(e);
      dragging.x = pt.x - dragOffset.x;
      dragging.y = pt.y - dragOffset.y;
      dragging.vx = dragging.vy = 0;
    } else if (!dragging) {
      const lastDragging = null;
      // pan
      if (e.buttons === 1) {
        transform.x = e.clientX - dragOffset.x;
        transform.y = e.clientY - dragOffset.y;
      }
      const pt = toGraph(e);
      const prev = hoveredNode;
      hoveredNode = nodes.find(n => dist2(n, pt) < (n.radius + 4) ** 2) || null;
      if (hoveredNode !== prev) {
        document.getElementById('graph-info').textContent =
          hoveredNode ? `${hoveredNode.title} · ${hoveredNode.category} · ${hoveredNode.backlink_count} backlinks` : 'Hover a node to see its title';
      }
    }
  };

  canvas.onmouseup = () => { dragging = null; };

  canvas.onwheel = e => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    const mx = e.clientX, my = e.clientY;
    transform.x = mx - (mx - transform.x) * factor;
    transform.y = my - (my - transform.y) * factor;
    transform.scale *= factor;
  };

  document.getElementById('graph-reset').onclick = () => {
    transform = { x: 0, y: 0, scale: 1 };
  };
}

function toGraph(e) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - rect.left - transform.x) / transform.scale,
    y: (e.clientY - rect.top  - transform.y) / transform.scale,
  };
}

function dist2(a, b) {
  return (a.x - b.x) ** 2 + (a.y - b.y) ** 2;
}
