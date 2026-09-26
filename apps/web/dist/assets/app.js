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
const state = { fresh: true, cache: new Map(), items: new Map(), stash: new Map(), timers: new Map(), opened: new Set(), briefs: new Map(), audios: new Set(), posters: new Map(), searchTab: 'radar' };
const suppliedToken = new URLSearchParams(location.search).get('token');
if (suppliedToken) { store.set('radar-token', suppliedToken); history.replaceState(null, '', location.pathname + location.hash); }
const authToken = store.get('radar-token', null);
// Posts first seen after the previous visit get a small "new" dot.
state.lastVisit = Number(store.get('radar-last-visit', 0)) || 0;
setTimeout(() => store.set('radar-last-visit', String(Date.now())), 8000);

async function api(path, options = {}, attempt = 0) {
  const headers = {'Content-Type': 'application/json', ...(authToken ? {Authorization: `Bearer ${authToken}`} : {})};
  const response = await fetch('/api/v1' + path, {headers, ...options});
  const detail = response.ok ? null : (await response.json().catch(() => ({}))).detail;
  // The local database can be briefly busy while the worker writes; retry a couple of times.
  if (response.status === 503 && detail === 'database busy, retry' && attempt < 3) {
    await new Promise(r => setTimeout(r, 300 * (attempt + 1)));
    return api(path, options, attempt + 1);
  }
  if (!response.ok) throw new Error(detail || `HTTP ${response.status}`);
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
  if (item.type === 'youtube_video') return {label: item.source?.name?.replace(/^YouTube · /, '') || item.author || 'YouTube', href: item.source ? `#/videos/${item.source.id}` : '#/videos'};
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
  if (item.type === 'youtube_video' && item.external?.score) parts.push(`<span>${plural(item.external.score, 'view')}</span>`);
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
// A paragraph that is only a Reddit/imgur image link is shown as the picture itself.
const IMAGE_URL = /^https:\/\/(?:i\.redd\.it|preview\.redd\.it|i\.imgur\.com)\/[^\s]+$/;
function prose(text) {
  const src = String(text || '').replace(/\r/g, '').replace(/&amp;#x200B;|​/g, '').trim();
  const blocks = [];
  for (const part of src.split(/\n{2,}/)) {
    const b = part.trim();
    if (!b) continue;
    const lines = b.split('\n');
    if (IMAGE_URL.test(b)) blocks.push(`<figure class="inline-img"><img src="/media/remote?u=${encodeURIComponent(b)}" alt="" loading="lazy" decoding="async" data-act="zoom" data-src="/media/remote?u=${encodeURIComponent(b)}"></figure>`);
    else if (b.startsWith('```')) blocks.push(`<pre><code>${esc(b.replace(/^```[^\n]*\n?/, '').replace(/```$/, ''))}</code></pre>`);
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
// ---------------------------------------------------------------- media (pictures and videos)
// Pictures show at once: cached ones come with their size, uncached ones load through
// /media/item/<id> (fetched on first view) and are measured when they arrive. Videos play in place:
// YouTube through its privacy-enhanced embed, Reddit videos straight from v.redd.it.
const VIDEO_KINDS = new Set(['youtube', 'reddit_video']);
const isVideo = item => VIDEO_KINDS.has(item.media?.kind);
const MEDIA_BADGE = {gallery: 'Gallery', reddit_video: '▶ Video', youtube: '▶ YouTube'};
// Shape of a picture's box: its own, but never taller than ~60% of the screen (80% in the reader)
// nor wider than 2:1. Anything clamped is letterboxed instead of cropped.
const clampRatio = (r, reader = false) => {
  const col = Math.min(innerWidth - 32, 720), maxH = reader ? Math.min(760, innerHeight * 0.8) : Math.min(560, innerHeight * 0.6);
  return Math.max(0.8, col / maxH, Math.min(2, r));
};
// Photos, galleries and videos are the point of the post: shown large. Articles get a large card
// only when their picture is big enough to carry one; otherwise a thumbnail.
function showsLarge(item) {
  const w = item.image?.width;
  if (isVideo(item) || item.type === 'youtube_video') return true;
  if (!item.image_url) return false;
  if (['image', 'gallery'].includes(item.media?.kind)) return !w || w >= 320;  // a tiny picture stays a thumbnail
  return w >= 560;
}
function mediaHtml(item, {href = null, reader = false} = {}) {
  const src = item.image_url, video = isVideo(item) || (item.type === 'youtube_video' && item.media);
  if (!src && !video) return '';
  const w = item.image?.width, h = item.image?.height;
  const natural = video ? 16 / 9 : w && h ? w / h : null;
  const ratio = natural ? clampRatio(natural, reader) : Math.max(4 / 3, clampRatio(1, reader));
  const fit = !video && (!natural || Math.abs(natural - ratio) > 0.01);  // letterbox tall/wide or not yet measured
  const pics = src ? `${fit ? `<img class="backdrop" src="${esc(src)}" alt="" aria-hidden="true" loading="lazy" decoding="async">` : ''}<img class="main" src="${esc(src)}" alt="" loading="lazy" decoding="async"${natural ? '' : ' data-measure="1"'}>` : '';
  const badge = MEDIA_BADGE[item.media?.kind] ? `<span class="badge">${MEDIA_BADGE[item.media.kind]}</span>` : '';
  const cls = `media${fit ? ' fit' : ''}${video ? ' video' : ''}${reader ? ' in-reader' : ''}`;
  const style = `style="aspect-ratio:${ratio.toFixed(4)}"`;
  if (video) return `<button class="${cls}" ${style} data-act="play" data-id="${item.id}" aria-label="Play video">${pics}<span class="play" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5z"/></svg></span>${badge}</button>`;
  if (reader || !href) return `<div class="${cls}" ${style} data-act="zoom" data-src="${esc(src)}" title="Click to enlarge">${pics}${badge}</div>`;
  return `<a class="${cls}" ${style} href="${href}" tabindex="-1" aria-label="${esc(item.title)}">${pics}${badge}</a>`;
}
// A picture of unknown size takes its real shape once it arrives (scroll anchoring keeps the page still).
function measure(img) {
  const box = img.closest('.media'); if (!box || !img.naturalWidth) return;
  const natural = img.naturalWidth / img.naturalHeight, ratio = clampRatio(natural, box.classList.contains('in-reader'));
  box.style.aspectRatio = ratio.toFixed(4);
  if (Math.abs(natural - ratio) <= 0.01) { box.classList.remove('fit'); box.querySelector('.backdrop')?.remove(); }
}
const ytFrame = (vid, id) => `<div class="media video playing" style="aspect-ratio:1.7778" data-player="${id}"><iframe src="https://www.youtube-nocookie.com/embed/${encodeURIComponent(vid)}?autoplay=1&rel=0&playsinline=1" title="Video" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>`;
function stopPlayers(except) {
  for (const v of $$('video')) if (v !== except) v.pause();
  for (const a of state.audios) if (a !== except?._audio) a.pause();
}
async function play(el) {
  const item = state.items.get(Number(el.dataset.id)), m = item?.media;
  if (!m) return;
  stopPlayers();
  state.posters.set(String(item.id), el.outerHTML);  // put back when the page is cached for Back
  if (m.kind === 'youtube') { el.outerHTML = ytFrame(m.id, item.id); return; }
  const poster = item.image_url ? ` poster="${esc(item.image_url)}"` : '';
  el.outerHTML = `<div class="media video playing" style="aspect-ratio:1.7778" data-player="${item.id}"><video controls playsinline preload="auto"${poster}></video></div>`;
  const video = $(`[data-player="${item.id}"] video`);
  const failed = () => { const box = video.closest('.media'); if (box && !box.nextElementSibling?.classList.contains('video-fail')) box.insertAdjacentHTML('afterend', `<p class="note video-fail">This browser can't play this video here. <a class="link" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">Watch on Reddit ↗</a></p>`); };
  video.addEventListener('error', failed);
  try { await redditVideo(video, m); } catch { failed(); }
}
// Reddit serves HLS (Safari, iOS, Android) and DASH as separate progressive video and audio files,
// which any browser can play: the audio element follows the video element.
async function redditVideo(video, m) {
  if (!video.canPlayType('video/mp4; codecs="avc1.42E01E"') && !video.canPlayType('application/vnd.apple.mpegurl')) throw new Error('H.264 not supported');
  video.addEventListener('play', () => stopPlayers(video));
  if (video.canPlayType('application/vnd.apple.mpegurl')) { video.src = m.hls; return video.play().catch(() => {}); }
  const res = await fetch(m.dash); if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const xml = new DOMParser().parseFromString(await res.text(), 'application/xml');
  const base = m.dash.replace(/[^/]+$/, '');
  const reps = kind => [...new Set(xml.querySelectorAll(`AdaptationSet[contentType="${kind}"] Representation, AdaptationSet[mimeType^="${kind}"] Representation, Representation[mimeType^="${kind}"]`))]
    .map(r => ({url: new URL(r.querySelector('BaseURL')?.textContent.trim() || '', base).href, h: Number(r.getAttribute('height')) || 0, bw: Number(r.getAttribute('bandwidth')) || 0}))
    .filter(r => !r.url.endsWith('/'));
  const videos = reps('video').sort((a, b) => a.h - b.h);
  if (!videos.length) throw new Error('no video stream');
  const maxH = innerWidth * devicePixelRatio > 1400 ? 1080 : 720;
  video.src = (videos.filter(v => v.h <= maxH).pop() || videos[0]).url;
  const audio = reps('audio').sort((a, b) => a.bw - b.bw).pop();
  if (audio) pairAudio(video, audio.url);
  await video.play().catch(() => {});
}
function pairAudio(video, src) {
  const audio = new Audio(src); audio.preload = 'auto';
  video._audio = audio; state.audios.add(audio);
  const sync = () => { if (Math.abs(audio.currentTime - video.currentTime) > 0.3) audio.currentTime = video.currentTime; };
  const go = () => { sync(); if (!video.paused) audio.play().catch(() => {}); };
  for (const ev of ['play', 'playing', 'seeked']) video.addEventListener(ev, go);
  for (const ev of ['pause', 'waiting', 'ended', 'emptied']) video.addEventListener(ev, () => audio.pause());
  video.addEventListener('timeupdate', sync);
  video.addEventListener('ratechange', () => { audio.playbackRate = video.playbackRate; });
  video.addEventListener('volumechange', () => { audio.volume = video.volume; audio.muted = video.muted; });
}

function entry(item, {tune = false, text = true, source = true} = {}) {
  state.items.set(item.id, item);
  const big = showsLarge(item);
  const blurb = text ? describe(item) : '';
  const href = item.repository?.id && item.type === 'github_repository' ? `#/project/${item.repository.id}` : `#/item/${item.id}`;
  const title = item.repository?.full_name && item.type === 'github_repository' ? item.repository.full_name : item.title;
  return `<article class="entry ${item.user?.read ? 'read' : ''}" data-item="${item.id}"><div style="min-width:0">${metaLine(item, {source})}
    <h3><a href="${href}">${esc(title)}</a></h3>${big ? mediaHtml(item, {href}) : ''}${blurb ? `<p>${esc(blurb)}</p>` : ''}${tune ? tuneActions(item) : actions(item)}</div>
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
  const [d, hn, prof, vids] = await Promise.all([
    api('/digest?period=7d&limit=40&requested_by=ui'),
    api('/community?platform=hackernews&sort=hot&period=2d&limit=16').catch(() => ({items: []})),
    api('/profile').catch(() => null),
    api('/items?content_type=youtube_video&sort=score&period=3d&limit=8').catch(() => ({items: []})),
  ]);
  const by = Object.fromEntries(d.sections.map(s => [s.key, s.items]));
  const inDigest = new Set(d.sections.flatMap(s => s.items.map(i => i.id)));
  const reddit = (by.reddit || []).slice(0, 10);
  const hnItems = hn.items.filter(i => !inDigest.has(i.id)).slice(0, 6);
  const seen = new Set(); const rest = [];
  for (const key of ['important', 'for_you', 'adjacent', 'wildcard']) for (const i of by[key] || []) if (!seen.has(i.id)) { seen.add(i.id); rest.push(i); }
  const oss = (by.oss || []).slice(0, 4);
  const videos = vids.items.filter(i => !inDigest.has(i.id)).slice(0, 4);
  const date = new Date().toLocaleDateString(undefined, {weekday: 'long', day: 'numeric', month: 'long'});
  const total = reddit.length + hnItems.length + rest.length + oss.length + videos.length;
  let html = head('Today', total ? `${total} posts picked for you. Use ${icon('up')} and ${icon('down')} and Radar learns what you want.` : '', esc(date));
  if (!prof || prof.counts.ratings < 15) {
    html += `<div class="box accent nudge"><p>Not what you want to read? Rate a dozen posts and the feed adapts to you.</p><a class="btn primary" href="#/tune" data-fresh="1">Tune your feed</a></div>`;
  }
  if (reddit.length) html += `<section class="sec">${secHead('Reddit', '<a href="#/reddit" data-fresh="1">See all →</a>')}${list(reddit)}</section>`;
  if (hnItems.length) html += `<section class="sec">${secHead('Hacker News', '<a href="#/hn" data-fresh="1">See all →</a>')}${list(hnItems, {text: false, source: false})}</section>`;
  if (videos.length) html += `<section class="sec">${secHead('Videos', '<a href="#/videos" data-fresh="1">See all →</a>')}${list(videos, {text: false})}</section>`;
  if (rest.length) html += `<section class="sec">${secHead('Worth reading')}${list(rest)}</section>`;
  if (oss.length) html += `<section class="sec">${secHead('Open source', '<a href="#/projects" data-fresh="1">See all →</a>')}${list(oss)}</section>`;
  if (!total) html += empty('Nothing collected yet', '<p class="note">The worker collects every few minutes, or run <code>radar collect --force</code>.</p>');
  const shown = [...reddit, ...hnItems, ...rest, ...oss, ...videos].map(i => i.id);
  if (total) html += `<section class="sec">${secHead('Keep reading', '<a href="#/discover" data-fresh="1">Discover sources →</a>')}<div id="feed"><div class="list"></div></div><div class="more-btn"><button class="btn" data-act="more">Show more</button></div></section>`;
  state.feed = {q: `/feed?limit=20&exclude=${shown.join(',')}`, offset: 0, done: false, size: 20, opts: {}};
  $('#main').innerHTML = html;
  observeMore();
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
  f.offset += data.items.length; f.done = data.items.length < (f.size || 30);
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
// The reader never waits: it renders what the list already knew, then the stored post, then fills
// in the thread's full text, the linked article and fresh comments as each arrives — section by
// section, so a video that is already playing keeps playing.
async function detail(id, isProject = false) {
  const here = location.hash, known = !isProject && state.items.get(Number(id));
  if (known) renderDetail(known, null, {hydrating: true}); else loading();
  const d = await api(isProject ? `/projects/${id}` : `/items/${id}`);
  if (location.hash !== here) return;
  const item = d.project || d.item;
  state.items.set(item.id, item);
  state.detail = {item, story: null, hydrating: true, html: {}};
  if (!state.opened.has(item.id)) { state.opened.add(item.id); feedback(item.id, 'open').catch(() => {}); }
  if (known) updateDetail(item); else renderDetail(item, null, {hydrating: true});
  if (item.cluster_id) api(`/stories/${item.cluster_id}`).then(story => { if (location.hash === here) { state.detail.story = story; $('#d-also').innerHTML = alsoHtml(item, story); } }).catch(() => {});
  if (!state.briefs.has(item.id)) api(`/items/${item.id}/brief`).then(b => { if (b.brief && location.hash === here) { state.briefs.set(item.id, b.brief); $('#brief').outerHTML = briefBox(item); } }).catch(() => {});
  if (item.community && ['reddit', 'hackernews'].includes(item.community.platform)) loadComments(item.id, {first: true});
  const settle = fresh => { if (location.hash !== here) return; state.detail.hydrating = false; updateDetail(fresh); };
  post(`/items/${item.id}/hydrate`).then(h => settle(h.item)).catch(() => settle(state.detail.item));
}
const norm = t => String(t || '').replace(/\s+/g, ' ').trim().toLowerCase().slice(0, 240);
function detailParts(item, {hydrating = false} = {}) {
  const c = item.community, yt = item.media?.kind === 'youtube';
  let hero = '';
  if (isVideo(item) || item.type === 'youtube_video') hero = mediaHtml(item, {reader: true});
  else if (item.image_url && (['image', 'gallery'].includes(item.media?.kind) || !c || (item.image?.width || 999) >= 400)) {
    hero = mediaHtml(item, {reader: true});
    if (item.media?.kind === 'gallery') hero += `<p class="note">First image of a gallery — <a class="link" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">see all on Reddit ↗</a></p>`;
  }
  const bodyText = item.body || (item.type === 'youtube_video' ? item.content : '') || '';
  const lead = bodyText || describe(item);
  const body = bodyText ? `<div class="prose">${prose(bodyText)}</div>` : lead ? `<div class="prose"><p>${esc(lead)}</p></div>` : '';
  // The linked page's text, unless it only repeats what is already shown (a video description, say).
  const content = item.type !== 'youtube_video' ? item.content || '' : '';
  const repeats = !content || norm(content) === norm(lead) || norm(lead).startsWith(norm(content).slice(0, 120));
  const article = !repeats && (!item.body || content.length > item.body.length + 400) ? content : '';
  const host = hostOf(item.link_url || item.url);
  const linked = article ? `<section class="article"><div class="kicker">${c ? `From ${esc(yt ? 'the video description' : host)}` : 'Full text'}</div><div class="prose clamp-article">${prose(article)}</div>
    ${article.length > 1800 ? '<button class="btn" data-act="expand-article">Continue reading</button>' : ''}</section>`
    : hydrating && item.link_url && !yt && item.processing?.extraction !== 'done' ? `<p class="note loading-note">Fetching the article from ${esc(host)}…</p>` : '';
  return {hero, body, linked};
}
function renderDetail(item, story, {hydrating = false} = {}) {
  const c = item.community, r = item.repository;
  const liked = item.user?.vote === 1, saved = item.user?.saved;
  const platform = c?.platform === 'reddit' ? 'Reddit' : c?.platform === 'hackernews' ? 'Hacker News' : item.type === 'youtube_video' ? 'YouTube' : null;
  const out = [];
  if (platform) out.push(`<a class="btn" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">${icon('out')}<span class="wide">Open on </span>${platform}</a>`);
  if (item.link_url || !platform) out.push(`<a class="btn" href="${esc(item.link_url || item.url)}" target="_blank" rel="noopener">${icon('out')}${esc(hostOf(item.link_url || item.url) || 'Open original')}</a>`);
  const buttons = `<div class="row">${out.join('')}</div><div class="row" style="margin-top:8px">
    <button class="btn ${liked ? 'on' : ''}" data-act="like" data-id="${item.id}">${icon('up')}More<span class="wide"> like this</span></button>
    <button class="btn" data-act="dislike" data-id="${item.id}">${icon('down')}Less<span class="wide"> like this</span></button>
    <button class="btn ${saved ? 'on' : ''}" data-act="save" data-id="${item.id}">${icon('save')}Save</button></div><div id="detail-fb"></div>`;
  const {hero, body, linked} = detailParts(item, {hydrating});
  const why = item.why?.length ? `<div class="why"><div class="kicker">Why you're seeing this</div><ul>${item.why.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>` : '';
  const repo = r ? `<p class="note">${[r.stars != null && `★ ${compact(r.stars)} stars`, r.stars_delta_7d != null && `+${r.stars_delta_7d} this week`, r.license, r.language].filter(Boolean).map(esc).join(' · ')}</p>` : '';
  const talk = c && ['reddit', 'hackernews'].includes(c.platform) ? `<section class="sec">${secHead(c.comments != null ? `Comments · ${compact(c.comments)}` : 'Comments', `<a href="#" data-act="refresh-comments" data-id="${item.id}">Refresh</a>`)}<div id="comments"><div class="sk" style="width:70%"></div><div class="sk" style="width:50%"></div></div></section>` : '';
  $('#main').innerHTML = `<article class="detail" data-detail="${item.id}"><a class="back" href="javascript:history.back()">${icon('back')}Back</a><div id="d-meta">${metaLine(item, {author: true})}</div>
    <h1>${esc(r?.full_name && item.type === 'github_repository' ? r.full_name : item.title)}</h1>${repo}<div id="d-hero">${hero}</div>${buttons}${briefBox(item)}<div id="d-body">${body}</div><div id="d-article">${linked}</div><div id="d-why">${why}</div></article>
    <div id="d-also">${story ? alsoHtml(item, story) : ''}</div>${talk}`;
  if (state.detail?.item?.id === item.id) state.detail.html = {'#d-hero': hero, '#d-body': body, '#d-article': linked, '#d-meta': metaLine(item, {author: true})};
}
// Replace only the sections whose content changed; never interrupt a playing video.
function updateDetail(fresh) {
  const d = state.detail, cur = d?.item;
  const item = cur && cur.id === fresh.id ? {...cur, ...fresh, why: fresh.why || cur.why} : fresh;
  state.items.set(item.id, item);
  if (d) d.item = item;
  if (!$(`[data-detail="${item.id}"]`)) return renderDetail(item, d?.story, {hydrating: d?.hydrating});
  const parts = detailParts(item, {hydrating: !!d?.hydrating});
  const seen = d ? d.html : {};
  const swap = (sel, html) => { const el = $(sel); if (el && seen[sel] !== html && el.innerHTML !== html) { el.innerHTML = html; } seen[sel] = html; };
  if (!$('#d-hero .playing')) swap('#d-hero', parts.hero);
  swap('#d-body', parts.body); swap('#d-article', parts.linked);
  swap('#d-meta', metaLine(item, {author: true}));
  if (item.why?.length) swap('#d-why', `<div class="why"><div class="kicker">Why you're seeing this</div><ul>${item.why.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>`);
}
// Other posts of the same story: one compact line each (a repost says so instead of "coverage").
function alsoHtml(item, story) {
  const others = story?.story?.sources?.filter(x => x.id !== item.id) || [];
  if (!others.length) return '';
  const reposts = others.every(x => x.title === item.title);
  const lines = others.slice(0, 8).map(x => { state.items.set(x.id, x); const src = sourceOf(x), n = x.community?.comments;
    return `<a class="also" href="#/item/${x.id}"><span class="src">${esc(src.label)}</span><span>${age(x.published_at || x.first_seen_at)}</span>${n != null ? `<span>${plural(n, 'comment')}</span>` : ''}${x.title !== item.title ? `<b>${esc(x.title)}</b>` : ''}</a>`; }).join('');
  return `<section class="sec">${secHead(reposts ? (others.length === 1 ? 'Also posted' : `Also posted ${others.length} times`) : `Also covered by ${others.length} more`)}<div class="also-list">${lines}</div></section>`;
}
function briefBox(item) {
  const b = state.briefs.get(item.id);
  if (!b) return `<div id="brief" class="brief-cta"><button class="btn" data-act="brief" data-id="${item.id}">✦ Summarize${item.community ? ' post & discussion' : ''}</button><span class="note" id="brief-note"></span></div>`;
  const points = xs => xs?.length ? `<ul>${xs.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '';
  return `<div id="brief" class="brief"><div class="kicker">✦ Summary</div><p>${esc(b.tldr)}</p>${points(b.key_points)}
    ${b.discussion ? `<div class="kicker" style="margin-top:10px">What people say</div><p>${esc(b.discussion)}</p>${points(b.discussion_points)}` : ''}
    <p class="note">${esc(b.model || 'local model')} · <a href="#" class="link" data-act="brief" data-refresh="1" data-id="${item.id}">Refresh</a></p></div>`;
}
async function summarize(btn) {
  const id = Number(btn.dataset.id), item = state.items.get(id) || {id};
  const note = $('#brief-note');
  if (note) note.textContent = 'Summarizing with your local model…';
  btn.setAttribute('aria-busy', 'true');
  try {
    const r = await post(`/items/${id}/brief${btn.dataset.refresh ? '?refresh=true' : ''}`);
    state.briefs.set(id, r.brief);
    $('#brief').outerHTML = briefBox(item);
  } catch (err) { if ($('#brief-note')) $('#brief-note').textContent = err.message; else toast(err.message); }
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
function commentsHtml(t, item) {
  if (!t.comments.length) return `<p class="note">${t.error ? `Couldn't load comments (${esc(t.error)}).` : 'No comments yet.'}</p>`;
  const roots = commentTree(t.comments);
  const shown = roots.slice(0, COMPACT_TOP), rest = roots.slice(COMPACT_TOP);
  const total = item?.community?.comments;
  return `<div class="comments">${shown.map(commentHtml).join('')}${rest.length ? `<div class="more-comments" hidden>${rest.map(commentHtml).join('')}</div>` : ''}</div>
    ${rest.length ? `<button class="show-all" data-act="all-comments">Show ${plural(rest.length, 'more comment')}</button>` : ''}
    <p class="note">${total && total > t.comments.length ? `Showing ${t.comments.length} of ${compact(total)}. <a class="link" href="${esc(item ? discussionUrl(item) : '#')}" target="_blank" rel="noopener">All comments on ${item?.community?.platform === 'hackernews' ? 'Hacker News' : 'Reddit'} ↗</a>` : ''}</p>`;
}
// Comments: the stored copy first (instant), then a refresh when it is stale. When Reddit asks us to
// slow down, the stored copy stays up and Radar tries again by itself after the pause.
async function loadComments(id, {first = false, refresh = false} = {}) {
  const box = $('#comments'), here = location.hash; if (!box) return;
  clearInterval(state.retryTimer);
  const item = () => state.items.get(Number(id));
  let shown = false;
  if (first) {
    const cached = await api(`/items/${id}/comments?cached=true`).catch(() => null);
    if (location.hash !== here) return;
    if (cached?.comments.length) { box.innerHTML = commentsHtml(cached, item()) + (cached.stale ? '<p class="note loading-note">Checking for new comments…</p>' : ''); shown = true; }
    if (cached?.comments.length && !cached.stale) return;
  } else if (refresh) box.insertAdjacentHTML('afterbegin', '<p class="note loading-note">Refreshing…</p>');
  try {
    const t = await api(`/items/${id}/comments${refresh ? '?refresh=true' : ''}`);
    if (location.hash !== here) return;
    if (t.item) updateDetail(t.item);
    if (t.retry_in) {
      if (!t.comments.length && !shown) box.innerHTML = '';
      else if (t.comments.length) box.innerHTML = commentsHtml(t, item());
      box.querySelector('.loading-note')?.remove();
      box.insertAdjacentHTML('afterbegin', `<p class="note wait-note">Reddit asked Radar to slow down${t.comments.length ? ' — showing the saved comments' : ''}. Trying again in <b>${t.retry_in}</b>s.</p>`);
      let left = t.retry_in;
      state.retryTimer = setInterval(() => {
        if (location.hash !== here) return clearInterval(state.retryTimer);
        left -= 1; const b = $('.wait-note b', box); if (b) b.textContent = Math.max(0, left);
        if (left <= 0) { clearInterval(state.retryTimer); loadComments(id); }
      }, 1000);
      return;
    }
    box.innerHTML = commentsHtml(t, item());
  } catch (err) { if (location.hash === here && !shown) box.innerHTML = `<p class="note">Couldn't load comments (${esc(err.message)}).</p>`; else box.querySelector('.loading-note')?.remove(); }
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
const SEARCH_TABS = [['radar', 'In Radar'], ['reddit', 'Reddit'], ['hackernews', 'Hacker News']];
const SEARCH_PERIODS = [['week', 'Past week'], ['month', 'Past month'], ['year', 'Past year'], ['all', 'All time']];
async function search(query) {
  loading();
  $('#search').value = query;
  const tab = state.searchTab || 'radar', period = store.get('radar-search-period', 'year');
  const tabs = `<div class="sorts">${SEARCH_TABS.map(([k, l]) => `<button class="${tab === k ? 'active' : ''}" data-act="search-tab" data-tab="${k}">${l}</button>`).join('')}
    ${tab === 'reddit' ? `<select data-act="search-period" aria-label="Time range">${SEARCH_PERIODS.map(([k, l]) => `<option value="${k}" ${period === k ? 'selected' : ''}>${l}</option>`).join('')}</select>` : ''}</div>`;
  let body, count;
  if (tab === 'radar') {
    const d = await api(`/search?q=${encodeURIComponent(query)}`);
    count = d.count;
    body = d.results.length ? list(d.results) : empty('Nothing collected matches', '<p class="note">Try the Reddit or Hacker News tab to search beyond your sources.</p>');
  } else {
    try {
      const d = await api(`/search/live?q=${encodeURIComponent(query)}&where=${tab}${tab === 'reddit' ? `&period=${period}` : ''}`);
      count = d.items.length;
      body = d.items.length ? list(d.items) : empty('No results');
    } catch (err) { count = 0; body = empty('Search failed', `<p class="note">${esc(err.message)}</p>`); }
  }
  $('#main').innerHTML = head(`“${query}”`, tab === 'radar' ? `${plural(count, 'result')} in what Radar has collected` : `${plural(count, 'result')} from ${tab === 'reddit' ? 'Reddit' : 'Hacker News'} — open any to read it here`) + tabs + body;
}

// ---------------------------------------------------------------- Videos & Discover
async function videos(sourceId) {
  loading();
  const sort = store.get('radar-videos-sort', 'newest'), period = store.get('radar-videos-period', '7d');
  const q = `/items?content_type=youtube_video&sort=${sort === 'best' ? 'score' : 'newest'}&period=${period}&limit=24${sourceId ? `&source_id=${sourceId}` : ''}`;
  const [data, src] = await Promise.all([api(q), api('/sources').catch(() => ({sources: []}))]);
  const channels = src.sources.filter(x => x.kind === 'youtube' && x.status !== 'muted');
  state.feed = {q, offset: data.items.length, done: data.items.length < 24, size: 24, opts: {text: false}};
  const chips = `<div class="chips"><a class="chip ${!sourceId ? 'active' : ''}" href="#/videos" data-fresh="1">All</a>${channels.map(ch => `<a class="chip ${String(ch.id) === String(sourceId) ? 'active' : ''}" href="#/videos/${ch.id}" data-fresh="1">${esc(ch.name.replace(/^YouTube · /, ''))}</a>`).join('')}<a class="chip" href="#/discover" data-fresh="1">+ Add channels</a></div>`;
  const sorts = `<div class="sorts">${[['newest', 'Newest'], ['best', 'For you']].map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="videos-sort" data-sort="${k}">${l}</button>`).join('')}
    <select data-act="videos-period" aria-label="Time range">${PERIODS.map(([k, l]) => `<option value="${k}" ${period === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
  $('#main').innerHTML = head('Videos', 'Tech news and explainers from YouTube — watch them right here.') + chips + sorts +
    `<div id="feed">${data.items.length ? list(data.items, {text: false}) : empty('No videos in this range', '<p class="note">Add channels in Discover, or try a longer range.</p>')}</div><div class="more-btn">${state.feed.done ? '' : '<button class="btn" data-act="more">Show more</button>'}</div>`;
  observeMore();
}
const KIND_LABEL = {reddit: 'Reddit', youtube: 'YouTube', rss: 'Web'};
async function discover() {
  loading();
  const d = await api('/catalog');
  const groups = d.groups.map(g => `<section class="sec">${secHead(g.topic)}<div class="lines">${g.items.map(x => `<div class="line"><span class="kind k-${x.kind}">${KIND_LABEL[x.kind]}</span><span class="name"><b>${esc(x.name)}</b> <small>${esc(x.note)}</small></span>
      <button class="btn ${x.following ? 'on' : ''}" data-act="follow" data-kind="${x.kind}" data-identifier="${esc(x.identifier)}" data-name="${esc(x.source_name)}" data-source="${x.source_id || ''}" data-following="${x.following ? 1 : ''}">${x.following ? 'Following' : 'Follow'}</button></div>`).join('')}</div></section>`).join('');
  $('#main').innerHTML = head('Discover sources', 'Follow subreddits, YouTube channels and sites. New sources start collecting within a few minutes.') + groups;
}
async function follow(btn) {
  const {kind, identifier, name, source, following} = btn.dataset;
  btn.disabled = true;
  try {
    if (following) { await api(`/sources/${source}`, {method: 'PATCH', body: JSON.stringify({status: 'muted'})}); btn.dataset.following = ''; btn.classList.remove('on'); btn.textContent = 'Follow'; }
    else if (source) { await api(`/sources/${source}`, {method: 'PATCH', body: JSON.stringify({status: 'active'})}); btn.dataset.following = '1'; btn.classList.add('on'); btn.textContent = 'Following'; }
    else { const r = await post('/sources', {kind, identifier, name}); btn.dataset.source = r.source_id; btn.dataset.following = '1'; btn.classList.add('on'); btn.textContent = 'Following'; }
    state.cache.clear();
  } catch (err) { toast(err.message); }
  btn.disabled = false;
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
  else if (act === 'refresh-comments') { e.preventDefault(); loadComments(el.dataset.id, {refresh: true}); }
  else if (act === 'zoom') openLightbox(el.dataset.src);
  else if (act === 'brief') { e.preventDefault(); summarize(el); }
  else if (act === 'play') { e.preventDefault(); play(el); }
  else if (act === 'refresh-page') { el.remove(); state.fresh = true; scrollTo(0, 0); route(); }
  else if (act === 'expand-article') { $('.clamp-article')?.classList.add('open'); el.remove(); }
  else if (act === 'search-tab') { state.searchTab = el.dataset.tab; search(decodeURIComponent(location.hash.replace(/^#\/search\//, ''))); }
  else if (act === 'videos-sort') { store.set('radar-videos-sort', el.dataset.sort); state.fresh = true; route(); }
  else if (act === 'follow') follow(el);
  else if (act === 'expand-comment') { const c = el.closest('.cmt'); const t = $('.cmt-text', c); t.classList.remove('clamp'); t.removeAttribute('data-act'); $('.cmt-actions [data-act="expand-comment"]', c)?.remove(); }
  else if (act === 'toggle-replies') { const r = el.closest('.cmt').querySelector(':scope > .replies'); r.hidden = !r.hidden; el.textContent = el.textContent.replace(/^[▸▾]/, r.hidden ? '▸' : '▾'); }
  else if (act === 'all-comments') { $('.more-comments').hidden = false; el.remove(); }
  else if (act === 'sync-reddit') accountAction('sync', el);
  else if (act === 'disconnect-reddit') accountAction('disconnect', el);
});
document.addEventListener('change', e => {
  const el = e.target;
  if (el.dataset?.act === 'period') { store.set(`radar-${el.dataset.platform}-period`, el.value); state.fresh = true; route(); }
  if (el.dataset?.act === 'videos-period') { store.set('radar-videos-period', el.value); state.fresh = true; route(); }
  if (el.dataset?.act === 'search-period') { store.set('radar-search-period', el.value); search(decodeURIComponent(location.hash.replace(/^#\/search\//, ''))); }
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

// Inline post images keep placeholder height until loaded (so lazy loading triggers), and vanish if gone.
document.addEventListener('load', e => {
  const t = e.target;
  if (t.matches?.('.inline-img img')) t.closest('figure').classList.add('loaded');
  if (t.matches?.('.media img[data-measure]')) measure(t);
}, true);
// A picture that turns out not to exist leaves no empty frame behind (a video keeps its play button).
document.addEventListener('error', e => {
  const t = e.target;
  if (t.matches?.('.inline-img img')) t.closest('figure').classList.add('failed');
  else if (t.matches?.('.thumb img')) t.closest('.thumb').remove();
  else if (t.matches?.('.media img')) { const box = t.closest('.media'); if (box?.classList.contains('video')) box.querySelectorAll('img').forEach(i => i.remove()); else box?.remove(); }
}, true);

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

// ---------------------------------------------------------------- "N new posts" (open pages stay current without reloading)
const PILL_SCOPES = {today: 'today', reddit: 'reddit', hn: 'hn', videos: 'videos'};
async function checkUpdates() {
  const page = (location.hash || '#/today').replace(/^#\//, '').split('/')[0] || 'today', scope = PILL_SCOPES[page];
  if (!scope || !state.loadedAt || document.visibilityState !== 'visible') return;
  try {
    const u = await api(`/updates?since=${encodeURIComponent(state.loadedAt)}&scope=${scope}`);
    if (u.new > 0 && PILL_SCOPES[(location.hash || '#/today').replace(/^#\//, '').split('/')[0] || 'today'] === scope) showPill(u.new);
  } catch { /* offline for a moment */ }
}
function showPill(n) {
  let pill = $('#pill');
  if (!pill) { pill = document.createElement('button'); pill.id = 'pill'; pill.className = 'pill'; pill.dataset.act = 'refresh-page'; document.body.append(pill); }
  pill.textContent = `↑ ${n} new ${n === 1 ? 'post' : 'posts'}`;
}
setInterval(checkUpdates, 90e3);
let hiddenAt = 0;
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') hiddenAt = Date.now(); else if (Date.now() - hiddenAt > 60e3) checkUpdates(); });

// ---------------------------------------------------------------- routing (Back restores list pages and scroll)
const CACHEABLE = new Set(['today', 'reddit', 'hn', 'videos', 'explore', 'projects', 'saved', 'history', 'search']);
let currentHash = null;
async function route() {
  const hash = location.hash || '#/today';
  const [name, ...rest] = hash.replace(/^#\//, '').split('/');
  const arg = decodeURIComponent(rest.join('/'));
  // A page kept for Back keeps its video covers, not a player that would come back silent.
  $$('#main .media.playing[data-player]').forEach(box => { const cover = state.posters.get(box.dataset.player); if (cover) box.outerHTML = cover; });
  if (currentHash && CACHEABLE.has(currentHash.split('/')[1])) state.cache.set(currentHash, {html: $('#main').innerHTML, y: scrollY, feed: state.feed, at: Date.now(), loadedAt: state.loadedAt});
  const prev = currentHash; currentHash = hash;
  const page = name || 'today';
  stopPlayers(); state.audios.clear(); clearInterval(state.retryTimer); $('#pill')?.remove();
  $$('[data-route]').forEach(a => a.classList.toggle('active', a.dataset.route === page));
  $('#main').classList.remove('wide');
  $('.top').classList.remove('searching');
  const cached = state.cache.get(hash);
  if (!state.fresh && cached && prev !== hash && Date.now() - cached.at < 15 * 60e3) {
    $('#main').innerHTML = cached.html; state.feed = cached.feed; state.loadedAt = cached.loadedAt; requestAnimationFrame(() => scrollTo(0, cached.y)); checkUpdates(); return;
  }
  state.fresh = false;
  state.loadedAt = new Date().toISOString();
  try {
    if (page === 'today') await today();
    else if (page === 'reddit') await communityPage('reddit', arg || null);
    else if (page === 'hn') await communityPage('hackernews', null);
    else if (page === 'tune') await tune();
    else if (page === 'videos') await videos(arg || null);
    else if (page === 'discover') await discover();
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
$('#search').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.value.trim()) { state.fresh = true; state.searchTab = 'radar'; location.hash = `#/search/${encodeURIComponent(e.target.value.trim())}`; e.target.blur(); } });
$('#search-toggle').addEventListener('click', () => { $('.top').classList.toggle('searching'); $('#search').focus(); });
document.addEventListener('keydown', e => { if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) { e.preventDefault(); $('.top').classList.add('searching'); $('#search').focus(); } });
$('#theme').onclick = () => { const light = document.documentElement.classList.toggle('light'); store.set('radar-theme', light ? 'light' : 'dark'); };
window.addEventListener('hashchange', route);
route();
