// Nyra Radar web UI — dependency-free ES module. Every list renders the same `entry`, and every
// interaction is delegated from `document` through data-act attributes, so cached page HTML can be
// restored on Back without rebinding anything.
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const icon = name => `<svg class="ico" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const store = {
  get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};
const state = { fresh: true, cache: new Map(), items: new Map(), stash: new Map(), timers: new Map(), opened: new Set() };
const suppliedToken = new URLSearchParams(location.search).get('token');
if (suppliedToken) { store.set('radar-token', suppliedToken); history.replaceState(null, '', location.pathname + location.hash); }
const authToken = store.get('radar-token', null);
// Posts first seen after the previous visit get a small "new" dot.
state.lastVisit = Number(store.get('radar-last-visit', 0)) || 0;
setTimeout(() => store.set('radar-last-visit', String(Date.now())), 8000);

async function api(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(authToken ? {Authorization: `Bearer ${authToken}`} : {})};
  const response = await fetch('/api/v1' + path, {headers, ...options});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
  return response.json();
}
const post = (path, body = {}) => api(path, {method: 'POST', body: JSON.stringify(body)});
const feedback = (id, action, context) => post(`/items/${id}/feedback`, {action, ...(context ? {context} : {})});

// ---------------------------------------------------------------- formatting
function age(value) {
  if (!value) return '';
  const h = Math.max(0, (Date.now() - new Date(value).getTime()) / 36e5);
  return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
}
const fullDate = v => v ? new Date(v).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'}) : '';
const compact = n => n == null ? '' : n >= 1e6 ? `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}k` : String(n);
const plural = (n, word, many = `${word}s`) => `${compact(n)} ${n === 1 ? word : many}`;
function hostOf(url) { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; } }
const describe = item => item.summary || item.excerpt || item.repository?.description || '';
const discussionUrl = item => item.community?.discussion_url || item.url;

function sourceOf(item) {
  const c = item.community;
  if (c?.platform === 'reddit' && c.name) return {label: `r/${c.name}`, href: `#/reddit/${encodeURIComponent(c.name)}`};
  if (c?.platform === 'hackernews') return {label: 'Hacker News', href: '#/hn'};
  if (item.type?.startsWith('github')) return {label: 'GitHub', href: '#/projects'};
  if (item.type === 'youtube_video') return {label: item.source?.name?.replace(/^YouTube · /, '') || 'YouTube'};
  return {label: item.source?.name || hostOf(item.url) || 'Web'};
}
function metaLine(item, {author = false, source = true} = {}) {
  const src = sourceOf(item), c = item.community, r = item.repository;
  const parts = source ? [src.href ? `<a class="src" href="${src.href}">${esc(src.label)}</a>` : `<span class="src">${esc(src.label)}</span>`] : [];
  if (state.lastVisit && item.first_seen_at && new Date(item.first_seen_at).getTime() > state.lastVisit) parts.unshift('<i class="new-dot" title="New since your last visit"></i>');
  if (author && item.author) parts.push(`<span>${c?.platform === 'reddit' ? 'u/' : ''}${esc(item.author)}</span>`);
  const when = item.published_at || item.first_seen_at;
  if (when) parts.push(`<span title="${esc(fullDate(when))}">${age(when)}</span>`);
  if (c?.votes_known && c.score != null) parts.push(`<span>${plural(c.score, 'point')}</span>`);
  if (r) {
    if (r.stars != null) parts.push(`<span>★ ${compact(r.stars)}</span>`);
    if (r.stars_delta_7d) parts.push(`<span>+${compact(r.stars_delta_7d)} this week</span>`);
    if (r.language) parts.push(`<span>${esc(r.language)}</span>`);
  }
  const link = item.link_url && hostOf(item.link_url);
  if (link) parts.push(`<a href="${esc(item.link_url)}" target="_blank" rel="noopener">${esc(link)}</a>`);
  if (c?.flair) parts.push(`<span>${esc(c.flair)}</span>`);
  return `<div class="meta">${parts.join('<span class="sep"></span>')}</div>`;
}

// ---------------------------------------------------------------- markdown-lite (escaped first)
function inline(text) {
  let s = esc(text);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
  return s.replace(/\n/g, '<br>');
}
function prose(text) {
  const src = String(text || '').replace(/\r/g, '').replace(/&amp;#x200B;|​/g, '').trim();
  const blocks = [];
  for (const part of src.split(/\n{2,}/)) {
    const b = part.trim();
    if (!b) continue;
    const lines = b.split('\n');
    if (b.startsWith('```')) blocks.push(`<pre><code>${esc(b.replace(/^```[^\n]*\n?/, '').replace(/```$/, ''))}</code></pre>`);
    else if (lines.every(l => /^\s*([-*•]|\d+[.)])\s+/.test(l))) blocks.push(`<ul>${lines.map(l => `<li>${inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ''))}</li>`).join('')}</ul>`);
    else if (lines.every(l => /^\s*>/.test(l))) blocks.push(`<blockquote>${inline(b.replace(/^\s*>\s?/gm, ''))}</blockquote>`);
    else if (/^#{1,6}\s/.test(b)) blocks.push(`<p><strong>${inline(b.replace(/^#{1,6}\s+/, ''))}</strong></p>`);
    else blocks.push(`<p>${inline(b)}</p>`);
  }
  return blocks.join('');
}

// ---------------------------------------------------------------- building blocks
const empty = (title, body = '') => `<div class="empty"><b>${esc(title)}</b>${body}</div>`;
const head = (title, lede = '', kicker = '') => `<header class="page-head">${kicker ? `<div class="kicker">${kicker}</div>` : ''}<h1>${esc(title)}</h1>${lede ? `<p class="lede">${lede}</p>` : ''}</header>`;
const secHead = (title, more = '') => `<div class="sec-head"><h2>${esc(title)}</h2>${more}</div>`;
function loading() { $('#main').innerHTML = `<div class="sk" style="width:30%;height:26px;margin-top:6px"></div>${'<div class="sk" style="width:88%"></div><div class="sk" style="width:64%;margin-bottom:26px"></div>'.repeat(5)}`; }
function toast(text, undoId) {
  const el = $('#toast');
  el.innerHTML = `<span>${esc(text)}</span>${undoId ? `<button data-act="undo" data-id="${undoId}">Undo</button>` : ''}`;
  el.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), undoId ? 5000 : 2200);
}

function actions(item) {
  const c = item.community, liked = item.user?.vote === 1, saved = item.user?.saved;
  const talk = c ? `<a class="act label" href="#/item/${item.id}" title="Comments">${icon('comment')}${c.comments != null ? compact(c.comments) : ''}</a>` : '';
  return `<div class="acts">${talk}
    <button class="act ${liked ? 'on' : ''}" data-act="like" data-id="${item.id}" title="More like this" aria-label="More like this" aria-pressed="${liked}">${icon('up')}</button>
    <button class="act" data-act="dislike" data-id="${item.id}" title="Less like this" aria-label="Less like this">${icon('down')}</button>
    <button class="act ${saved ? 'on' : ''}" data-act="save" data-id="${item.id}" title="Save for later" aria-label="Save" aria-pressed="${!!saved}">${icon('save')}</button>
    <button class="act" data-act="hide" data-id="${item.id}" title="Hide (no opinion)" aria-label="Hide">${icon('hide')}</button></div>`;
}
function tuneActions(item) {
  return `<div class="tune-acts"><button class="btn" data-act="tune" data-vote="like" data-id="${item.id}">${icon('up')}More like this</button>
    <button class="btn" data-act="tune" data-vote="dislike" data-id="${item.id}">${icon('down')}Less like this</button>
    <button class="btn" data-act="tune" data-vote="skip" data-id="${item.id}">Not sure</button></div>`;
}
const MEDIA_BADGE = {gallery: 'Gallery', video: '▶ Video'};
// Reddit image, gallery and video posts show their picture large, like Reddit; everything else a thumbnail.
const isMediaPost = item => item.community?.kind === 'media' && item.image_url;
function mediaBlock(item, href) {
  const w = item.image?.width, h = item.image?.height, tall = w && h && h / w > 1.25;
  const ratio = w && h && !tall ? ` style="aspect-ratio:${w} / ${h}"` : '';
  const badge = MEDIA_BADGE[item.community?.media];
  const backdrop = tall ? `<img class="backdrop" src="${esc(item.image_url)}" alt="" aria-hidden="true" loading="lazy">` : '';
  return `<a class="media ${tall ? 'tall' : ''}" href="${href}"${ratio} aria-label="${esc(item.title)}">${backdrop}<img src="${esc(item.image_url)}" alt="" loading="lazy" decoding="async">${badge ? `<span class="badge">${badge}</span>` : ''}</a>`;
}
function entry(item, {tune = false, text = true, source = true} = {}) {
  state.items.set(item.id, item);
  const big = isMediaPost(item);
  const blurb = text ? describe(item) : '';
  const href = item.repository?.id && item.type === 'github_repository' ? `#/project/${item.repository.id}` : `#/item/${item.id}`;
  const title = item.repository?.full_name && item.type === 'github_repository' ? item.repository.full_name : item.title;
  return `<article class="entry ${item.user?.read ? 'read' : ''}" data-item="${item.id}"><div style="min-width:0">${metaLine(item, {source})}
    <h3><a href="${href}">${esc(title)}</a></h3>${big ? mediaBlock(item, href) : ''}${blurb ? `<p>${esc(blurb)}</p>` : ''}${tune ? tuneActions(item) : actions(item)}</div>
    ${item.image_url && !big ? `<a class="thumb" href="${href}" tabindex="-1" aria-hidden="true"><img src="${esc(item.image_url)}" alt="" loading="lazy" decoding="async"></a>` : ''}</article>`;
}
const list = (items, opts) => `<div class="list">${items.map(i => entry(i, opts)).join('')}</div>`;

// ---------------------------------------------------------------- feedback
const REASONS = item => [['topic', 'Not interested in this'], ['source', `Less from ${sourceOf(item).label}`], ['quality', 'Low quality / clickbait'], ['seen', 'Already seen it']];
const REASON_DONE = {topic: 'Got it — fewer posts like this.', source: 'Got it — less from there.', quality: 'Got it — fewer low-quality posts like this.', seen: 'Hidden.'};
function feedbackRow(el, html) {
  el.querySelector('.fb')?.remove();
  el.insertAdjacentHTML('beforeend', `<div class="fb">${html}</div>`);
}
function scheduleRemoval(el, id, ms) {
  clearTimeout(state.timers.get(id));
  state.timers.set(id, setTimeout(() => { el.remove(); state.timers.delete(id); }, ms));
}
async function react(btn) {
  const id = Number(btn.dataset.id), act = btn.dataset.act, item = state.items.get(id) || {id, user: {}};
  const el = btn.closest('[data-item]');
  try {
    if (act === 'like') {
      const on = item.user?.vote === 1;
      const r = await feedback(id, on ? 'undo' : 'like');
      item.user = r.user; btn.classList.toggle('on', !on); btn.setAttribute('aria-pressed', String(!on));
      if (!on) toast('Noted — Radar will show more like this.');
    } else if (act === 'save') {
      const on = !!item.user?.saved;
      await (on ? api(`/items/${id}/save`, {method: 'DELETE'}) : post(`/items/${id}/save`));
      item.user = {...item.user, saved: !on}; btn.classList.toggle('on', !on);
      toast(on ? 'Removed from Saved' : 'Saved');
    } else if (act === 'dislike') {
      await feedback(id, 'dislike');
      if (el?.classList.contains('entry')) {
        state.stash.set(id, el.outerHTML); el.classList.add('dim');
        feedbackRow(el, `<b>Less like this.</b><span>Why?</span>${REASONS(item).map(([k, l]) => `<button class="chip" data-act="reason" data-reason="${k}" data-id="${id}">${esc(l)}</button>`).join('')}<button class="undo" data-act="undo" data-id="${id}">Undo</button>`);
        scheduleRemoval(el, id, 20000);
      } else { detailFeedback(item); }
    } else if (act === 'reason') {
      await feedback(id, 'dislike', {reason: btn.dataset.reason, refine: true});
      const done = REASON_DONE[btn.dataset.reason];
      if (el?.classList.contains('entry')) { feedbackRow(el, `<span>${esc(done)}</span><button class="undo" data-act="undo" data-id="${id}">Undo</button>`); scheduleRemoval(el, id, 3500); }
      else { $('#detail-fb').innerHTML = `<div class="fb"><span>${esc(done)}</span><button class="undo" data-act="undo" data-id="${id}">Undo</button></div>`; }
    } else if (act === 'hide') {
      await feedback(id, 'skip');
      if (el?.classList.contains('entry')) { state.stash.set(id, el.outerHTML); el.classList.add('dim'); feedbackRow(el, `<span>Hidden.</span><button class="undo" data-act="undo" data-id="${id}">Undo</button>`); scheduleRemoval(el, id, 4000); }
      else { toast('Hidden', id); }
    } else if (act === 'undo') {
      await feedback(id, 'undo');
      clearTimeout(state.timers.get(id)); state.timers.delete(id);
      const stashed = state.stash.get(id), row = $(`[data-item="${id}"].entry`);
      if (row && stashed) row.outerHTML = stashed;
      if ($('#detail-fb')) $('#detail-fb').innerHTML = '';
      $('#toast').classList.remove('show');
      toast('Undone');
    }
  } catch (err) { toast(err.message); }
}
function detailFeedback(item) {
  $('#detail-fb').innerHTML = `<div class="fb"><b>Less like this.</b><span>Why?</span>${REASONS(item).map(([k, l]) => `<button class="chip" data-act="reason" data-reason="${k}" data-id="${item.id}">${esc(l)}</button>`).join('')}<button class="undo" data-act="undo" data-id="${item.id}">Undo</button></div>`;
}

// ---------------------------------------------------------------- Today
async function today() {
  loading();
  const [d, hn, prof] = await Promise.all([
    api('/digest?period=7d&limit=40&requested_by=ui'),
    api('/community?platform=hackernews&sort=hot&period=2d&limit=16').catch(() => ({items: []})),
    api('/profile').catch(() => null),
  ]);
  const by = Object.fromEntries(d.sections.map(s => [s.key, s.items]));
  const inDigest = new Set(d.sections.flatMap(s => s.items.map(i => i.id)));
  const reddit = (by.reddit || []).slice(0, 10);
  const hnItems = hn.items.filter(i => !inDigest.has(i.id)).slice(0, 6);
  const seen = new Set(); const rest = [];
  for (const key of ['important', 'for_you', 'adjacent', 'wildcard']) for (const i of by[key] || []) if (!seen.has(i.id)) { seen.add(i.id); rest.push(i); }
  const oss = (by.oss || []).slice(0, 4);
  const date = new Date().toLocaleDateString(undefined, {weekday: 'long', day: 'numeric', month: 'long'});
  const total = reddit.length + hnItems.length + rest.length + oss.length;
  let html = head('Today', total ? `${total} posts picked for you. Use ${icon('up')} and ${icon('down')} and Radar learns what you want.` : '', esc(date));
  if (!prof || prof.counts.ratings < 15) {
    html += `<div class="box accent nudge"><p>Not what you want to read? Rate a dozen posts and the feed adapts to you.</p><a class="btn primary" href="#/tune" data-fresh="1">Tune your feed</a></div>`;
  }
  if (reddit.length) html += `<section class="sec">${secHead('Reddit', '<a href="#/reddit" data-fresh="1">See all →</a>')}${list(reddit)}</section>`;
  if (hnItems.length) html += `<section class="sec">${secHead('Hacker News', '<a href="#/hn" data-fresh="1">See all →</a>')}${list(hnItems, {text: false, source: false})}</section>`;
  if (rest.length) html += `<section class="sec">${secHead('Worth reading')}${list(rest)}</section>`;
  if (oss.length) html += `<section class="sec">${secHead('Open source', '<a href="#/projects" data-fresh="1">See all →</a>')}${list(oss)}</section>`;
  if (!total) html += empty('Nothing collected yet', '<p class="note">The worker collects every few minutes, or run <code>radar collect --force</code>.</p>');
  $('#main').innerHTML = html;
}

// ---------------------------------------------------------------- Reddit / Hacker News
const SORTS = [['hot', 'Hot'], ['top', 'Top'], ['new', 'New'], ['discussed', 'Discussed'], ['best', 'For you']];
const PERIODS = [['24h', 'Today'], ['3d', '3 days'], ['7d', 'This week'], ['30d', 'This month']];
async function communityPage(platform, name) {
  loading();
  const reddit = platform === 'reddit';
  const sort = store.get(`radar-${platform}-sort`, 'hot'), period = store.get(`radar-${platform}-period`, '3d');
  const q = `/community?platform=${platform}&sort=${sort}&period=${period}&limit=30${name ? `&community=${encodeURIComponent(name)}` : ''}`;
  const [feed, comm] = await Promise.all([api(q), reddit ? api('/communities').catch(() => ({communities: []})) : null]);
  const subs = (comm?.communities || []).filter(s => !s.feed && s.source?.status !== 'muted');
  state.feed = {q, offset: feed.items.length, done: feed.items.length < 30};
  const title = reddit ? (name ? `r/${name}` : 'Reddit') : 'Hacker News';
  const lede = reddit
    ? (name ? `<a class="link" href="https://www.reddit.com/r/${encodeURIComponent(name)}" target="_blank" rel="noopener">Open r/${esc(name)} on Reddit ↗</a>` : `Your ${subs.length} subreddits in one place. <a class="link" href="#/settings" data-fresh="1">Manage</a>`)
    : 'Top and best stories from news.ycombinator.com.';
  const chips = reddit ? `<div class="chips"><a class="chip ${!name ? 'active' : ''}" href="#/reddit" data-fresh="1">All</a>${subs.map(s => `<a class="chip ${s.name.toLowerCase() === (name || '').toLowerCase() ? 'active' : ''}" href="#/reddit/${encodeURIComponent(s.name)}" data-fresh="1">${esc(s.name)}${s.items_24h ? ` <small>${s.items_24h}</small>` : ''}</a>`).join('')}</div>` : '';
  const sorts = `<div class="sorts">${SORTS.map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="sort" data-platform="${platform}" data-sort="${k}">${l}</button>`).join('')}
    <select data-act="period" data-platform="${platform}" aria-label="Time range">${PERIODS.map(([k, l]) => `<option value="${k}" ${period === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
  const failing = subs.filter(s => s.source?.consecutive_failures > 0);
  const warn = failing.length ? `<p class="note">Couldn't reach ${esc(failing.map(s => 'r/' + s.name).join(', '))} last time; Radar will retry.</p>` : '';
  const oneSource = !reddit || !!name;
  state.feed.opts = {text: reddit, source: !oneSource};
  const body = feed.items.length ? list(feed.items, state.feed.opts) : empty(name ? `Nothing from r/${name} in this range` : 'Nothing in this range', '<p class="note">Try a longer time range.</p>');
  $('#main').innerHTML = head(title, lede) + chips + sorts + warn + `<div id="feed">${body}</div><div class="more-btn">${state.feed.done ? '' : '<button class="btn" data-act="more">Show more</button>'}</div>`;
  observeMore();
}
// Infinite scroll: load the next page when "Show more" comes into view (the button still works).
let moreObserver;
function observeMore() {
  moreObserver?.disconnect();
  const btn = $('[data-act="more"]');
  if (!btn || !('IntersectionObserver' in window)) return;
  moreObserver = new IntersectionObserver(entries => { if (entries.some(e => e.isIntersecting) && !btn.disabled) loadMore(btn).then(observeMore).catch(() => {}); }, {rootMargin: '600px'});
  moreObserver.observe(btn);
}
async function loadMore(btn) {
  const f = state.feed; if (!f || f.done) return;
  btn.disabled = true; btn.textContent = 'Loading…';
  const data = await api(`${f.q}&offset=${f.offset}`);
  $('#feed .list').insertAdjacentHTML('beforeend', data.items.map(i => entry(i, f.opts)).join(''));
  f.offset += data.items.length; f.done = data.items.length < 30;
  btn.parentElement.innerHTML = f.done ? '' : '<button class="btn" data-act="more">Show more</button>';
}

// ---------------------------------------------------------------- Tune your feed
async function tune() {
  loading();
  const d = await api('/tune?limit=12');
  state.tune = {total: d.items.length, done: 0};
  $('#main').innerHTML = head('Tune your feed', 'Tell Radar what you want to read. Every answer changes the ranking immediately — about a minute for a dozen posts.') +
    `<div class="progress"><i id="tune-bar"></i></div><p class="note" id="tune-count">0 of ${d.items.length} rated</p>` +
    (d.items.length ? list(d.items, {tune: true}) : empty('Nothing new to rate', '<p class="note">Come back after the next collection.</p>')) + '<div id="tune-done"></div>';
}
async function tuneVote(btn) {
  const id = Number(btn.dataset.id), vote = btn.dataset.vote, el = btn.closest('.entry');
  try {
    if (vote !== 'skip') await feedback(id, vote, {via: 'tune'});
    el.classList.add('dim');
    el.querySelector('.tune-acts').outerHTML = `<div class="fb"><span>${vote === 'like' ? 'More like this ✓' : vote === 'dislike' ? 'Less like this ✓' : 'Skipped'}</span>${vote !== 'skip' ? `<button class="undo" data-act="tune-undo" data-id="${id}">Undo</button>` : ''}</div>`;
    const t = state.tune; t.done += 1;
    $('#tune-bar').style.width = `${Math.round(100 * t.done / t.total)}%`;
    $('#tune-count').textContent = `${t.done} of ${t.total} rated`;
    if (t.done >= t.total) $('#tune-done').innerHTML = `<div class="box nudge"><p>Thanks — your feed is updated.</p><div class="row"><a class="btn primary" href="#/today" data-fresh="1">See Today</a><button class="btn" data-act="tune-more">Rate more</button></div></div>`;
  } catch (err) { toast(err.message); }
}
async function tuneUndo(btn) {
  const id = Number(btn.dataset.id), el = btn.closest('.entry');
  try {
    await feedback(id, 'undo');
    el.classList.remove('dim');
    el.querySelector('.fb').outerHTML = tuneActions(state.items.get(id) || {id});
    state.tune.done -= 1; $('#tune-count').textContent = `${state.tune.done} of ${state.tune.total} rated`;
    $('#tune-bar').style.width = `${Math.round(100 * state.tune.done / state.tune.total)}%`;
  } catch (err) { toast(err.message); }
}

// ---------------------------------------------------------------- detail
async function detail(id, isProject = false) {
  loading();
  const d = await api(isProject ? `/projects/${id}` : `/items/${id}`);
  const item = d.project || d.item, c = item.community, r = item.repository;
  state.items.set(item.id, item);
  if (!state.opened.has(item.id)) { state.opened.add(item.id); feedback(item.id, 'open').catch(() => {}); }
  const story = item.cluster_id ? await api(`/stories/${item.cluster_id}`).catch(() => null) : null;
  const liked = item.user?.vote === 1, saved = item.user?.saved;
  const platform = c?.platform === 'reddit' ? 'Reddit' : c?.platform === 'hackernews' ? 'Hacker News' : null;
  const out = [];
  if (platform) out.push(`<a class="btn" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">${icon('out')}Open on ${platform}</a>`);
  if (item.link_url || !platform) out.push(`<a class="btn" href="${esc(item.link_url || item.url)}" target="_blank" rel="noopener">${icon('out')}${esc(hostOf(item.link_url || item.url) || 'Open original')}</a>`);
  const buttons = `<div class="row">${out.join('')}</div><div class="row" style="margin-top:8px">
    <button class="btn ${liked ? 'on' : ''}" data-act="like" data-id="${item.id}">${icon('up')}More like this</button>
    <button class="btn" data-act="dislike" data-id="${item.id}">${icon('down')}Less like this</button>
    <button class="btn ${saved ? 'on' : ''}" data-act="save" data-id="${item.id}">${icon('save')}Save</button></div><div id="detail-fb"></div>`;
  const hero = item.image_url && (c?.kind === 'media' || !c)
    ? `<div class="hero" data-act="zoom" data-src="${esc(item.image_url)}" title="Click to enlarge"><img src="${esc(item.image_url)}" alt=""></div>${c?.media === 'gallery' ? `<p class="note">First image of a gallery — <a class="link" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">see all on Reddit ↗</a></p>` : c?.media === 'video' ? `<p class="note">Video — <a class="link" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">play on Reddit ↗</a></p>` : ''}` : '';
  const body = item.body ? `<div class="prose">${prose(item.body)}</div>` : describe(item) ? `<div class="prose"><p>${esc(describe(item))}</p></div>` : '';
  const linked = item.content && (!item.body || item.content.length > item.body.length + 400)
    ? `<details class="more-text"><summary>${c ? `Read the linked page${item.link_url ? ` (${esc(hostOf(item.link_url))})` : ''}` : 'Read the full text'}</summary><div class="prose">${prose(item.content)}</div></details>` : '';
  const why = item.why?.length ? `<div class="why"><div class="kicker">Why you're seeing this</div><ul>${item.why.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>` : '';
  const coverage = story && story.story.sources.length > 1 ? `<section class="sec">${secHead(`Also covered by ${story.story.source_count - 1} more`)}${list(story.story.sources.filter(s => s.id !== item.id), {text: false})}</section>` : '';
  const repo = r ? `<p class="note">${[r.stars != null && `★ ${compact(r.stars)} stars`, r.stars_delta_7d != null && `+${r.stars_delta_7d} this week`, r.license, r.language].filter(Boolean).map(esc).join(' · ')}</p>` : '';
  const talk = c && ['reddit', 'hackernews'].includes(c.platform) ? `<section class="sec">${secHead(c.comments != null ? `Comments · ${compact(c.comments)}` : 'Comments', `<a href="#" data-act="refresh-comments" data-id="${item.id}">Refresh</a>`)}<div id="comments"><div class="sk" style="width:70%"></div><div class="sk" style="width:50%"></div></div></section>` : '';
  $('#main').innerHTML = `<article class="detail"><a class="back" href="javascript:history.back()">${icon('back')}Back</a>${metaLine(item, {author: true})}
    <h1>${esc(r?.full_name && item.type === 'github_repository' ? r.full_name : item.title)}</h1>${repo}${buttons}${hero}${body}${linked}${why}</article>${coverage}${talk}`;
  if (talk) loadComments(item.id, false);
}
// Comments arrive compact: three top-level comments clamped to three lines, replies folded.
const COMPACT_TOP = 3;
function commentTree(comments) {
  const roots = [], stack = [];
  for (const c of comments) {
    const node = {...c, children: []}, depth = Math.max(0, Math.min(c.depth || 0, stack.length));
    stack.length = depth;
    (depth ? stack[depth - 1].children : roots).push(node);
    stack.push(node);
  }
  return roots;
}
const countAll = nodes => nodes.reduce((n, x) => n + 1 + countAll(x.children), 0);
function commentHtml(node) {
  const long = (node.text || '').length > 260 || (node.text || '').split('\n').length > 3;
  const replies = countAll(node.children);
  return `<div class="cmt"><div class="meta"><b>${esc(node.author || 'anonymous')}</b><span class="sep"></span><span title="${esc(fullDate(node.created_at))}">${age(node.created_at)}</span>${node.score != null ? `<span class="sep"></span><span>${plural(node.score, 'point')}</span>` : ''}</div>
    <div class="cmt-text ${long ? 'clamp' : ''}" ${long ? 'data-act="expand-comment" title="Show the whole comment"' : ''}>${prose(node.text)}</div>
    ${long || replies ? `<div class="cmt-actions">${long ? '<button data-act="expand-comment">Read more</button>' : ''}${replies ? `<button data-act="toggle-replies">▸ ${plural(replies, 'reply', 'replies')}</button>` : ''}</div>` : ''}
    ${replies ? `<div class="replies" hidden>${node.children.map(commentHtml).join('')}</div>` : ''}</div>`;
}
async function loadComments(id, refresh) {
  const box = $('#comments'); if (!box) return;
  try {
    const t = await api(`/items/${id}/comments${refresh ? '?refresh=true' : ''}`);
    if (!t.comments.length) { box.innerHTML = `<p class="note">${t.error ? `Couldn't load comments (${esc(t.error)}).` : 'No comments yet.'}</p>`; return; }
    const roots = commentTree(t.comments), item = state.items.get(Number(id));
    const shown = roots.slice(0, COMPACT_TOP), rest = roots.slice(COMPACT_TOP);
    const total = item?.community?.comments;
    box.innerHTML = `<div class="comments">${shown.map(commentHtml).join('')}${rest.length ? `<div class="more-comments" hidden>${rest.map(commentHtml).join('')}</div>` : ''}</div>
      ${rest.length ? `<button class="show-all" data-act="all-comments">Show ${plural(rest.length, 'more comment')}</button>` : ''}
      <p class="note">${total && total > t.comments.length ? `Showing ${t.comments.length} of ${compact(total)}. <a class="link" href="${esc(item ? discussionUrl(item) : '#')}" target="_blank" rel="noopener">All comments on ${item?.community?.platform === 'hackernews' ? 'Hacker News' : 'Reddit'} ↗</a>` : ''}</p>`;
  } catch (err) { box.innerHTML = `<p class="note">Couldn't load comments (${esc(err.message)}).</p>`; }
}

// ---------------------------------------------------------------- lists: explore, projects, saved, history, search
const EXPLORE_TYPES = [['', 'All'], ['reddit_post', 'Reddit'], ['hackernews_post', 'Hacker News'], ['rss_entry', 'News'], ['article', 'Articles'], ['youtube_video', 'Video'], ['github_repository', 'Projects']];
async function explore(params = {}) {
  loading();
  const offset = params.offset || 0, sort = params.sort || 'newest';
  const data = await api(`/items?sort=${sort}&limit=50&offset=${offset}${params.type ? `&content_type=${params.type}` : ''}`);
  state.explore = {...params, offset, sort};
  $('#main').innerHTML = head('Everything collected', 'The complete stream, unranked.') +
    `<div class="chips">${EXPLORE_TYPES.map(([k, l]) => `<button class="chip ${(params.type || '') === k ? 'active' : ''}" data-act="explore-type" data-type="${k}">${l}</button>`).join('')}</div>
     <div class="sorts">${[['newest', 'Newest'], ['score', 'Best match'], ['novelty', 'Most novel']].map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="explore-sort" data-sort="${k}">${l}</button>`).join('')}</div>
     ${data.items.length ? list(data.items) : empty('Nothing here')}
     <div class="more-btn row" style="justify-content:center">${offset ? '<button class="btn" data-act="explore-page" data-dir="-1">← Newer</button>' : ''}${data.items.length === 50 ? '<button class="btn" data-act="explore-page" data-dir="1">Older →</button>' : ''}</div>`;
}
async function projects(sort = 'momentum') {
  loading();
  const data = await api(`/projects?sort=${sort}&limit=60`);
  $('#main').innerHTML = head('Open-source projects', 'Repositories gaining stars, releases and mentions.') +
    `<div class="sorts">${[['momentum', 'Rising'], ['newest', 'Newest'], ['stars', 'Most stars']].map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="project-sort" data-sort="${k}">${l}</button>`).join('')}</div>` +
    (data.projects.length ? list(data.projects) : empty('No projects yet'));
}
async function listMode(mode) {
  loading();
  const data = await api(`/items?${mode}=true&sort=newest&limit=100`);
  const saved = mode === 'saved';
  $('#main').innerHTML = head(saved ? 'Saved' : 'History', saved ? 'Posts you kept for later, including ones saved on Reddit.' : 'Everything you opened.') +
    (data.items.length ? list(data.items) : empty(saved ? 'Nothing saved yet' : 'Nothing opened yet', `<p class="note">${saved ? `Use ${icon('save')} on any post.` : 'Open a post to see it here.'}</p>`));
}
async function search(query) {
  loading();
  const d = await api(`/search?q=${encodeURIComponent(query)}`);
  $('#search').value = query;
  $('#main').innerHTML = head(`“${query}”`, `${plural(d.count, 'result')}`) + (d.results.length ? list(d.results) : empty('No matches', '<p class="note">Try fewer or broader words.</p>'));
}

// ---------------------------------------------------------------- settings & sources
const ROLE_LABEL = {front: 'Front page (your subscriptions)', upvoted: 'Upvotes → “more like this”', saved: 'Saved posts → Saved'};
function accountBox(acc) {
  if (!acc?.connected) {
    return `<div class="box"><p>Connect your Reddit account so Radar follows your subscriptions and learns from what you upvote and save. No password needed.</p>
      <ol class="steps"><li>Open <a href="https://www.reddit.com/prefs/feeds" target="_blank" rel="noopener">reddit.com/prefs/feeds</a> while logged in.</li><li>Copy the <b>RSS</b> link next to “your front page”.</li><li>Paste it below.</li></ol>
      <form class="form" data-act="connect-reddit"><input class="field" name="feed_url" placeholder="https://www.reddit.com/.rss?feed=…&user=…" aria-label="Private Reddit feed link" required autocomplete="off"><button class="btn primary" type="submit">Connect</button></form>
      <p class="note">The link is read-only, stays on this computer and is never shown in full.</p></div>`;
  }
  const rows = acc.feeds.map(f => `<div class="line"><span>${esc(ROLE_LABEL[f.role] || f.name)}</span><small class="${f.last_error ? 'err' : ''}">${f.last_error ? 'error' : f.last_success_at ? `${f.items} · synced ${age(f.last_success_at)} ago` : 'waiting'}</small></div>`).join('');
  const failed = acc.feeds.find(f => f.last_error);
  return `<div class="box"><p>Connected as <b>u/${esc(acc.user)}</b>. Learned from <b>${acc.learned?.upvoted || 0}</b> upvotes and <b>${acc.learned?.saved || 0}</b> saves.</p><div class="lines">${rows}</div>
    ${failed ? `<p class="note err" style="color:var(--bad)">${esc(failed.last_error.slice(0, 160))}${/403/.test(failed.last_error) ? ' — Reddit rejected the link; copy it again while logged in.' : ''}</p>` : ''}
    <div class="row" style="margin-top:12px"><button class="btn" data-act="sync-reddit">Sync now</button><button class="btn" data-act="disconnect-reddit">Disconnect</button></div></div>`;
}
function learnedBox(p) {
  if (!p) return '';
  const c = p.counts, parts = [];
  const join = xs => xs.map(esc).join(', ');
  if (p.more_from.length) parts.push(`<div><span>More from:</span> ${join(p.more_from)}</div>`);
  if (p.less_from.length) parts.push(`<div><span>Less from:</span> ${join(p.less_from)}</div>`);
  if (p.topics_up.length) parts.push(`<div><span>Topics you like:</span> ${join(p.topics_up)}</div>`);
  if (p.topics_down.length) parts.push(`<div><span>Topics you skip:</span> ${join(p.topics_down)}</div>`);
  return `<div class="box"><p>From <b>${c.ratings}</b> ratings: ${plural(c.likes, 'like')}, ${plural(c.dislikes, '“less like this”', '“less like this”')}, ${compact(c.saved)} saved, ${compact(c.hidden)} hidden.</p>
    ${parts.length ? `<div class="learned">${parts.join('')}</div>` : '<p class="note">Nothing learned yet — rate a few posts.</p>'}
    <div class="row" style="margin-top:12px"><a class="btn primary" href="#/tune" data-fresh="1">Tune your feed</a></div></div>`;
}
const MIX = [
  ['feed.budget', 'reddit', 'Reddit share of Today', 0, 0.6, 0.01, v => `${Math.round(v * 100)}%`],
  ['ranking.content_type_preference', 'reddit_post', 'Reddit', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'hackernews_post', 'Hacker News', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'default', 'News & articles', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'github_repository', 'GitHub projects', 0, 1, 0.05, v => v.toFixed(2)],
];
async function settings() {
  loading();
  const [prof, account, comm, cfg, ints] = await Promise.all([api('/profile').catch(() => null), api('/reddit/account').catch(() => null),
    api('/communities').catch(() => ({communities: []})), api('/settings'), api('/interests')]);
  state.settings = cfg.settings;
  const subs = comm.communities.filter(s => !s.feed && s.source);
  const subRows = subs.map(s => { const f = s.source.consecutive_failures > 0, m = s.source.status === 'muted';
    return `<div class="line"><i class="dot ${m ? 'off' : f ? 'bad' : s.source.last_success_at ? '' : 'warn'}"></i><span class="name">r/${esc(s.name)}</span><small>${m ? 'muted' : f ? 'failing' : `${s.items_24h} today`}</small><button class="act label" data-act="source-status" data-id="${s.source.id}" data-status="${m ? 'active' : 'muted'}">${m ? 'Resume' : 'Mute'}</button></div>`; }).join('');
  const sliders = MIX.map(([key, sub, label, min, max, step, fmt], n) => { const v = Number(cfg.settings[key]?.[sub] ?? 0); return `<label class="slider"><span>${esc(label)}</span><input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-act="mix" data-n="${n}"><output>${fmt(v)}</output></label>`; }).join('');
  const topicRows = ints.interests.map(x => `<tr><td>${esc(x.topic)}</td><td>${x.weight.toFixed(2)}</td><td>+${x.positive_evidence} / −${x.negative_evidence}</td></tr>`).join('');
  $('#main').innerHTML = head('Settings', 'What Radar knows about you, and where it reads from.') +
    `<section class="sec">${secHead('What Radar has learned')}${learnedBox(prof)}</section>
     <section class="sec" id="reddit">${secHead('Reddit account')}${accountBox(account)}</section>
     <section class="sec">${secHead('Subreddits')}<form class="form" data-act="add-subs"><input class="field" name="subs" placeholder="Add subreddits: rust, golang, r/selfhosted" aria-label="Subreddits to add"><button class="btn primary" type="submit">Add</button></form><div class="lines">${subRows || '<p class="note">No subreddits yet.</p>'}</div></section>
     <section class="sec">${secHead('Feed mix')}<p class="note">How much each kind of source counts in ranking.</p><div style="margin-top:8px">${sliders}</div></section>
     <section class="sec"><details class="more-text" style="border:0;padding:0;margin:0"><summary>Interest topics (${ints.interests.length})</summary><div class="table-wrap"><table class="table"><thead><tr><th>Topic</th><th>Weight</th><th>Evidence</th></tr></thead><tbody>${topicRows}</tbody></table></div></details></section>`;
}
let mixTimer;
function onMix(input) {
  const [key, sub, , , , , fmt] = MIX[Number(input.dataset.n)];
  const value = Number(input.value);
  input.nextElementSibling.textContent = fmt(value);
  state.settings[key] = {...state.settings[key], [sub]: value};
  clearTimeout(mixTimer);
  mixTimer = setTimeout(() => api(`/settings/${key}`, {method: 'PATCH', body: JSON.stringify({value: state.settings[key]})}).then(() => toast('Saved')).catch(e => toast(e.message)), 400);
}
const KINDS = [['reddit', 'Subreddit or Reddit feed', 'LocalLLaMA'], ['rss', 'RSS / Atom feed', 'https://example.com/feed.xml'], ['hn_search', 'Hacker News search', 'local llm'], ['youtube', 'YouTube channel id', 'UC…'], ['github_search', 'GitHub search', 'topic:self-hosted stars:>50'], ['google_news', 'Google News search', '"open source" AI'], ['website_watch', 'Website to watch', 'https://example.com/changelog']];
async function sources() {
  loading();
  const d = await api('/sources');
  $('#main').classList.add('wide');
  const rows = d.sources.map(s => { const failing = s.last_error && s.status === 'active';
    return `<tr><td>${esc(s.name)}<br><small>${esc(s.identifier)}</small>${failing ? `<br><small style="color:var(--bad)" title="${esc(s.last_error)}">${esc(s.last_error.slice(0, 120))}</small>` : ''}</td><td>${esc(s.kind)}</td><td>${failing ? 'failing' : esc(s.status)}</td><td>${s.item_count}</td><td>${age(s.last_success_at) || '—'}</td>
      <td><button class="act label" data-act="source-status" data-id="${s.id}" data-status="${s.status === 'active' || s.status === 'trial' ? 'muted' : 'active'}">${s.status === 'active' || s.status === 'trial' ? 'Mute' : 'Resume'}</button></td></tr>`; }).join('');
  $('#main').innerHTML = head('Sources', 'Where Radar reads from, and whether it is working.') +
    `<form class="form" data-act="add-source"><select class="field" name="kind" aria-label="Source type">${KINDS.map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select><input class="field" name="identifier" placeholder="${esc(KINDS[0][2])}" aria-label="Identifier" required><button class="btn primary" type="submit">Add</button></form>
     <div class="table-wrap"><table class="table"><thead><tr><th>Source</th><th>Type</th><th>Status</th><th>Items</th><th>Last OK</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
async function accountAction(kind, el) {
  try {
    if (kind === 'connect') {
      const b = el.querySelector('button'); b.disabled = true; b.textContent = 'Connecting…';
      const r = await post('/reddit/account', {feed_url: el.feed_url.value});
      toast(`Connected u/${r.user}: ${r.learned.upvoted} upvotes, ${r.learned.saved} saves learned`);
    } else if (kind === 'sync') {
      el.disabled = true; el.textContent = 'Syncing…';
      const r = await post('/reddit/account/sync');
      toast(`Synced: ${r.learned.upvoted} upvotes, ${r.learned.saved} saves learned`);
    } else if (kind === 'disconnect') {
      if (!confirm('Disconnect Reddit? The stored link is deleted; what Radar learned stays.')) return;
      await api('/reddit/account', {method: 'DELETE'});
      toast('Reddit disconnected');
    }
  } catch (err) { toast(err.message); }
  state.fresh = true; route();
}
async function addSubs(form) {
  const names = form.subs.value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  let added = 0; const failed = [];
  for (const n of names) { try { await post('/sources', {kind: 'reddit', identifier: n}); added++; } catch (err) { failed.push(`${n}: ${err.message}`); } }
  toast(added ? `${plural(added, 'subreddit')} added — first posts in a few minutes` : failed[0] || 'Nothing added');
  state.fresh = true; route();
}

// ---------------------------------------------------------------- delegated events
document.addEventListener('click', e => {
  const fresh = e.target.closest('[data-fresh]');
  if (fresh) { state.fresh = true; $('.menu')?.removeAttribute('open'); }
  const el = e.target.closest('[data-act]');
  if (!el || (el.tagName === 'A' && el.dataset.act !== 'refresh-comments')) return;
  const act = el.dataset.act;
  if (['like', 'dislike', 'save', 'hide', 'reason', 'undo'].includes(act)) { e.preventDefault(); react(el); }
  else if (act === 'tune') tuneVote(el);
  else if (act === 'tune-undo') tuneUndo(el);
  else if (act === 'tune-more') { state.fresh = true; route(); }
  else if (act === 'sort') { store.set(`radar-${el.dataset.platform}-sort`, el.dataset.sort); state.fresh = true; route(); }
  else if (act === 'more') loadMore(el).catch(err => toast(err.message));
  else if (act === 'explore-type') explore({type: el.dataset.type, sort: state.explore?.sort});
  else if (act === 'explore-sort') explore({type: state.explore?.type, sort: el.dataset.sort});
  else if (act === 'explore-page') explore({...state.explore, offset: Math.max(0, (state.explore?.offset || 0) + Number(el.dataset.dir) * 50)});
  else if (act === 'project-sort') projects(el.dataset.sort);
  else if (act === 'source-status') api(`/sources/${el.dataset.id}`, {method: 'PATCH', body: JSON.stringify({status: el.dataset.status})}).then(() => { toast('Updated'); state.fresh = true; route(); }).catch(err => toast(err.message));
  else if (act === 'refresh-comments') { e.preventDefault(); loadComments(el.dataset.id, true); }
  else if (act === 'zoom') openLightbox(el.dataset.src);
  else if (act === 'expand-comment') { const c = el.closest('.cmt'); const t = $('.cmt-text', c); t.classList.remove('clamp'); t.removeAttribute('data-act'); $('.cmt-actions [data-act="expand-comment"]', c)?.remove(); }
  else if (act === 'toggle-replies') { const r = el.closest('.cmt').querySelector(':scope > .replies'); r.hidden = !r.hidden; el.textContent = el.textContent.replace(/^[▸▾]/, r.hidden ? '▸' : '▾'); }
  else if (act === 'all-comments') { $('.more-comments').hidden = false; el.remove(); }
  else if (act === 'sync-reddit') accountAction('sync', el);
  else if (act === 'disconnect-reddit') accountAction('disconnect', el);
});
document.addEventListener('change', e => {
  const el = e.target;
  if (el.dataset?.act === 'period') { store.set(`radar-${el.dataset.platform}-period`, el.value); state.fresh = true; route(); }
  if (el.name === 'kind' && el.form?.dataset.act === 'add-source') el.form.identifier.placeholder = KINDS.find(k => k[0] === el.value)?.[2] || '';
});
document.addEventListener('input', e => { if (e.target.dataset?.act === 'mix') onMix(e.target); });
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-act]'); if (!form) return;
  e.preventDefault();
  if (form.dataset.act === 'add-subs') addSubs(form);
  if (form.dataset.act === 'connect-reddit') accountAction('connect', form);
  if (form.dataset.act === 'add-source') post('/sources', {kind: form.kind.value, identifier: form.identifier.value}).then(() => { toast('Source added'); sources(); }).catch(err => toast(err.message));
});
document.addEventListener('click', e => { const menu = $('.menu'); if (menu?.open && !e.target.closest('.menu')) menu.removeAttribute('open'); });

// ---------------------------------------------------------------- lightbox & keyboard
function openLightbox(src) {
  if (!src) return;
  const box = document.createElement('div');
  box.className = 'lightbox'; box.innerHTML = `<img src="${esc(src)}" alt="">`;
  box.onclick = () => box.remove();
  document.body.append(box);
}
const KEYS = [['j / k', 'next / previous post'], ['o or Enter', 'open'], ['l', 'more like this'], ['d', 'less like this'], ['s', 'save'], ['x', 'hide'], ['/', 'search'], ['?', 'this help'], ['Esc', 'close']];
function toggleHelp() {
  const open = $('.kbd-help');
  if (open) { open.remove(); return; }
  const box = document.createElement('div');
  box.className = 'kbd-help';
  box.innerHTML = `<div><h2>Keyboard</h2><dl>${KEYS.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl></div>`;
  box.onclick = () => box.remove();
  document.body.append(box);
}
function moveFocus(step) {
  const entries = $$('#main .entry');
  if (!entries.length) return;
  const current = entries.findIndex(x => x.classList.contains('kb'));
  const next = entries[Math.max(0, Math.min(entries.length - 1, current + step))];
  entries.forEach(x => x.classList.remove('kb'));
  next.classList.add('kb');
  next.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
}
document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 'Escape') { $('.lightbox')?.remove(); $('.kbd-help')?.remove(); return; }
  if (e.key === '?') { toggleHelp(); return; }
  if (e.key === 'j' || e.key === 'k') { e.preventDefault(); moveFocus(e.key === 'j' ? 1 : -1); return; }
  const focused = $('#main .entry.kb');
  if (!focused) return;
  const press = act => focused.querySelector(`[data-act="${act}"]`)?.click();
  if (e.key === 'o' || e.key === 'Enter') focused.querySelector('h3 a')?.click();
  else if (e.key === 'l') press('like');
  else if (e.key === 'd') press('dislike');
  else if (e.key === 's') press('save');
  else if (e.key === 'x') press('hide');
});

// ---------------------------------------------------------------- routing (Back restores list pages and scroll)
const CACHEABLE = new Set(['today', 'reddit', 'hn', 'explore', 'projects', 'saved', 'history', 'search']);
let currentHash = null;
async function route() {
  const hash = location.hash || '#/today';
  const [name, ...rest] = hash.replace(/^#\//, '').split('/');
  const arg = decodeURIComponent(rest.join('/'));
  if (currentHash && CACHEABLE.has(currentHash.split('/')[1])) state.cache.set(currentHash, {html: $('#main').innerHTML, y: scrollY, feed: state.feed, at: Date.now()});
  const prev = currentHash; currentHash = hash;
  const page = name || 'today';
  $$('[data-route]').forEach(a => a.classList.toggle('active', a.dataset.route === page));
  $('#main').classList.remove('wide');
  $('.top').classList.remove('searching');
  const cached = state.cache.get(hash);
  if (!state.fresh && cached && prev !== hash && Date.now() - cached.at < 15 * 60e3) {
    $('#main').innerHTML = cached.html; state.feed = cached.feed; requestAnimationFrame(() => scrollTo(0, cached.y)); return;
  }
  state.fresh = false;
  try {
    if (page === 'today') await today();
    else if (page === 'reddit') await communityPage('reddit', arg || null);
    else if (page === 'hn') await communityPage('hackernews', null);
    else if (page === 'tune') await tune();
    else if (page === 'explore') await explore();
    else if (page === 'projects') await projects();
    else if (page === 'saved' || page === 'history') await listMode(page);
    else if (page === 'sources') await sources();
    else if (page === 'settings') await settings();
    else if (page === 'item') await detail(arg);
    else if (page === 'project') await detail(arg, true);
    else if (page === 'search') await search(arg);
    else await today();
  } catch (err) {
    $('#main').innerHTML = head('Radar is unavailable', esc(err.message)) + empty('Check that the local API service is running.');
  }
  if (prev !== hash) scrollTo(0, 0);
}
$('#search').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.value.trim()) { state.fresh = true; location.hash = `#/search/${encodeURIComponent(e.target.value.trim())}`; e.target.blur(); } });
$('#search-toggle').addEventListener('click', () => { $('.top').classList.toggle('searching'); $('#search').focus(); });
document.addEventListener('keydown', e => { if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) { e.preventDefault(); $('.top').classList.add('searching'); $('#search').focus(); } });
$('#theme').onclick = () => { const light = document.documentElement.classList.toggle('light'); store.set('radar-theme', light ? 'light' : 'dark'); };
window.addEventListener('hashchange', route);
route();
