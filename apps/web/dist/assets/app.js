// Nyra Radar web UI: dependency-free ES module. All interactions are delegated from `document`
// via data-act attributes, so cached page HTML can be restored on Back without rebinding.
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name, cls = '') => `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const store = {
  get(key, fallback) { try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};
const state = { route: 'today', fresh: true, subs: [], opened: new Set(), cache: new Map() };
const suppliedToken = new URLSearchParams(location.search).get('token');
if (suppliedToken) { store.set('radar-token', suppliedToken); history.replaceState(null, '', location.pathname + location.hash); }
const authToken = store.get('radar-token', null);

async function api(path, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(authToken ? {Authorization: `Bearer ${authToken}`} : {})};
  const response = await fetch('/api/v1' + path, {headers, ...options});
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
  return response.json();
}
const post = (path, body = {}) => api(path, {method: 'POST', body: JSON.stringify(body)});

// ---------------------------------------------------------------- formatting
function age(value) {
  if (!value) return '';
  const h = Math.max(0, (Date.now() - new Date(value).getTime()) / 36e5);
  return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
}
const fullDate = v => v ? new Date(v).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'}) : '';
const compact = n => n == null ? '—' : n >= 1e6 ? `${(n / 1e6).toFixed(1).replace(/\.0$/, '')}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1).replace(/\.0$/, '')}k` : String(n);
const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
const RANKED = new Set(['hot', 'top', 'best', 'rising', 'front']);
const heat = c => c?.listing?.rank && RANKED.has(c.listing.sort) ? c.listing : null;
function hue(text) { let h = 0; for (const ch of String(text || '').toLowerCase()) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; }
function hostOf(url) { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; } }
const platformOf = item => item.community?.platform || ({reddit_post: 'reddit', hackernews_post: 'hackernews'})[item.type] || null;
const isReddit = item => platformOf(item) === 'reddit';
const discussionUrl = item => item.community?.discussion_url || item.url;
const outboundUrl = item => item.link_url || item.community?.media_url || item.url;
const describe = item => item.summary || item.excerpt || item.repository?.description || '';

const TYPE_MARK = {
  reddit_post: ['reddit', 'r/', 'Reddit'], hackernews_post: ['hn', 'Y', 'Hacker News'],
  github_repository: ['gh', 'GH', 'GitHub'], github_release: ['release', 'R', 'Release'],
  youtube_video: ['yt', '▶', 'YouTube'], article: ['article', 'A', 'Article'], rss_entry: ['article', 'A', 'News'],
  website_change: ['article', 'W', 'Website'], telegram_post: ['article', 'T', 'Telegram'],
};
function sourceMark(item) {
  const [cls, glyph, label] = TYPE_MARK[item.type] || ['article', 'N', 'Signal'];
  const name = item.type === 'rss_entry' || item.type === 'article' ? (item.source?.name || item.domain || label) : label;
  return `<span class="src"><span class="mark ${cls}">${esc(glyph)}</span>${esc(name)}</span>`;
}
function subLink(name) {
  if (!name) return '';
  return `<a class="sub-link" href="#/reddit/${encodeURIComponent(name)}"><span class="sub-dot" style="--h:${hue(name)}">r/</span>r/${esc(name)}</a>`;
}
function media(item, cls = '', label = true) {
  if (item.image_url) return `<div class="media ${cls}"><img src="${esc(item.image_url)}" alt="" loading="lazy" decoding="async"></div>`;
  const key = item.community?.name || item.source?.name || item.domain || item.type;
  const name = item.community?.name ? `r/${item.community.name}` : (item.source?.name || item.domain || '').replace(/^www\./, '');
  const text = label ? `<span><small>${esc(TYPE_MARK[item.type]?.[2] || 'Signal')}</small>${esc(name)}</span>` : esc((name.replace(/^r\//, '') || 'N')[0].toUpperCase());
  return `<div class="media ${cls}"><div class="ph ${label ? '' : 'sm'}" style="--h:${hue(key)}">${text}</div></div>`;
}
function votes(item, cls = '') {
  const c = item.community;
  if (!c) return item.external?.score ? `<span class="vote ${cls}">${icon('up')}${compact(item.external.score)}</span>` : '';
  if (c.votes_known && c.score != null) return `<span class="vote ${c.platform === 'hackernews' ? 'hn' : ''} ${cls}" title="${c.score} points">${icon('up')}${compact(c.score)}</span>`;
  const h = heat(c);
  if (h) return `<span class="vote rank" title="Position in r/${esc(c.name)} ${esc(h.sort)} (vote counts are not in Reddit's public feed)">${icon('bolt', 'fill')}${esc(h.sort === 'top' ? 'Top' : 'Hot')} #${h.rank}</span>`;
  return '';
}
function commentsLink(item) {
  const c = item.community;
  if (!c) return '';
  const label = c.comments != null ? compact(c.comments) : 'Discuss';
  return `<a class="cmt" href="#/item/${item.id}" title="Open discussion">${icon('comment')}${label}</a>`;
}
function actions(item, style = 'foot') {
  const liked = item.user?.vote === 1;
  return `<button class="pill-btn ${liked ? 'on' : ''}" data-act="like" data-id="${item.id}" title="More like this" aria-label="More like this">${icon('heart')}${style === 'foot' ? '<span>Like</span>' : ''}</button>`
    + `<button class="pill-btn" data-act="save" data-id="${item.id}" title="Save" aria-label="Save">${icon('saved')}${style === 'foot' ? '<span>Save</span>' : ''}</button>`
    + `<button class="pill-btn" data-act="dismiss" data-id="${item.id}" title="Hide" aria-label="Hide">${icon('x')}</button>`;
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
  if (!src) return '';
  const blocks = [];
  for (const part of src.split(/\n{2,}(?=(?:[^`]|`(?!``))*$)|(?<=```)\n/)) {
    const b = part.trim();
    if (!b) continue;
    if (b.startsWith('```')) blocks.push(`<pre><code>${esc(b.replace(/^```[^\n]*\n?/, '').replace(/```$/, ''))}</code></pre>`);
    else if (b.split('\n').every(l => /^\s*([-*•]|\d+[.)])\s+/.test(l))) blocks.push(`<ul>${b.split('\n').map(l => `<li>${inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ''))}</li>`).join('')}</ul>`);
    else if (b.split('\n').every(l => /^\s*(>|&gt;)/.test(l))) blocks.push(`<blockquote>${inline(b.replace(/^\s*(>|&gt;)\s?/gm, ''))}</blockquote>`);
    else if (/^#{1,6}\s/.test(b)) blocks.push(`<p><strong>${inline(b.replace(/^#{1,6}\s+/, ''))}</strong></p>`);
    else blocks.push(`<p>${inline(b)}</p>`);
  }
  return blocks.join('');
}

// ---------------------------------------------------------------- page scaffolding
function header(kicker, title, sub, right = '') {
  return `<header class="page-head"><div><div class="eyebrow">${kicker}</div><h1>${esc(title)}</h1>${sub ? `<p class="subhead">${sub}</p>` : ''}</div>${right}</header>`;
}
function sectionTitle(title, sub = '', badge = '', more = '') {
  return `<div class="section-title"><div class="left">${badge}<div><h2>${esc(title)}</h2>${sub ? `<p>${sub}</p>` : ''}</div></div>${more}</div>`;
}
const empty = (title, body = '') => `<div class="empty"><b>${esc(title)}</b>${body}</div>`;
function skeleton(kind = 'list', n = 5) {
  const card = `<div class="sk-card"><div class="sk sk-line" style="width:32%"></div><div class="sk sk-line" style="height:18px;width:86%"></div><div class="sk sk-line" style="width:64%"></div></div>`;
  const hero = `<div class="reddit-hero"><div class="sk" style="aspect-ratio:16/10;border-radius:14px"></div><div class="stack">${card.repeat(3)}</div></div>`;
  return `<div style="display:grid;gap:12px">${kind === 'hero' ? hero : ''}${card.repeat(n)}</div>`;
}
function loading(kind) { $('#main').innerHTML = `<div class="sk sk-line" style="width:180px;margin-bottom:14px"></div><div class="sk" style="height:40px;width:min(420px,80%);margin-bottom:30px"></div>${skeleton(kind)}`; }
function toast(text) { const el = $('#toast'); el.textContent = text; el.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => el.classList.remove('show'), 1900); }

// ---------------------------------------------------------------- cards
function leadCard(item) {
  const c = item.community || {};
  return `<article class="lead ${item.image_url ? '' : 'noimg'}" data-item="${item.id}" style="--h:${hue(c.name)}">
    ${item.image_url ? `<a href="#/item/${item.id}" tabindex="-1" aria-hidden="true">${media(item)}</a>` : ''}
    <div class="lead-body">
      <div class="meta">${subLink(c.name)}<span class="dot"></span><span title="${esc(fullDate(item.published_at))}">${age(item.published_at || item.first_seen_at)}</span>${c.flair ? `<span class="flair">${esc(c.flair)}</span>` : ''}</div>
      <h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>
      ${describe(item) ? `<p>${esc(describe(item))}</p>` : ''}
      <div class="post-foot">${votes(item)}${commentsLink(item)}${item.link_url ? `<a class="domain pill-btn" href="${esc(item.link_url)}" target="_blank" rel="noopener">${icon('out', 'sm')}${esc(hostOf(item.link_url))}</a>` : ''}<span class="grow"></span>${actions(item, 'icon')}</div>
    </div></article>`;
}
function miniCard(item) {
  const c = item.community || {};
  return `<article class="mini ${item.image_url ? '' : 'noimg'}" data-item="${item.id}"><div>
      <div class="meta">${c.name ? subLink(c.name) : sourceMark(item)}<span class="dot"></span><span>${age(item.published_at || item.first_seen_at)}</span></div>
      <h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>
      <div class="stats-row">${votes(item)}${commentsLink(item)}${item.link_url ? `<span class="domain">${esc(hostOf(item.link_url))}</span>` : ''}</div>
    </div>${item.image_url ? `<a href="#/item/${item.id}" tabindex="-1" aria-hidden="true">${media(item, '', false)}</a>` : ''}</article>`;
}
function newsCard(item) {
  return `<article class="news" data-item="${item.id}"><a href="#/item/${item.id}" tabindex="-1" aria-hidden="true">${media(item)}</a>
    <div class="news-body"><div class="meta">${isReddit(item) ? subLink(item.community?.name) : sourceMark(item)}<span class="dot"></span><span>${age(item.published_at || item.first_seen_at)}</span>${item.cluster_id ? '<span class="dot"></span><span>multi-source</span>' : ''}</div>
      <h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>${describe(item) ? `<p>${esc(describe(item))}</p>` : ''}
      <div class="stats-row">${votes(item)}${commentsLink(item)}</div></div></article>`;
}
function rowCard(item) {
  const c = item.community;
  const why = item.reason ? `<div class="why">Why: ${esc(item.reason)}</div>` : '';
  return `<article class="row ${item.image_url ? '' : 'noimg'}" data-item="${item.id}">${item.image_url ? `<a href="#/item/${item.id}" tabindex="-1" aria-hidden="true">${media(item, '', false)}</a>` : ''}
    <div style="min-width:0"><div class="meta">${c?.name && c.platform === 'reddit' ? subLink(c.name) : sourceMark(item)}<span class="dot"></span><span title="${esc(fullDate(item.published_at))}">${age(item.published_at || item.first_seen_at)}</span>${item.link_url ? `<span class="dot"></span><span class="domain">${esc(hostOf(item.link_url))}</span>` : (item.type === 'rss_entry' || item.type === 'article') ? '' : item.domain ? `<span class="dot"></span><span class="domain">${esc(item.domain.replace(/^www\./, ''))}</span>` : ''}</div>
      <h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>${describe(item) ? `<p>${esc(describe(item))}</p>` : ''}
      ${c ? `<div class="stats-row" style="margin-top:8px">${votes(item)}${commentsLink(item)}</div>` : ''}${why}</div>
    <div class="row-actions">${actions(item, 'icon')}</div></article>`;
}
function postCard(item, view = 'card') {
  const c = item.community || {};
  const hn = c.platform === 'hackernews';
  const rail = c.votes_known && c.score != null
    ? `<div class="rail-v ${hn ? 'hn' : ''}">${icon('up')}<b>${compact(c.score)}</b></div>`
    : heat(c) ? `<div class="rail-v">${icon('bolt')}<b>#${heat(c).rank}</b><small>${esc(heat(c).sort === 'front' ? 'home' : heat(c).sort)}</small></div>`
    : `<div class="rail-v">${icon('bolt')}<small>new</small></div>`;
  const head = `<div class="meta">${hn ? sourceMark(item) : subLink(c.name)}<span class="dot"></span>${item.author ? `<span>${hn ? '' : 'u/'}${esc(item.author)}</span><span class="dot"></span>` : ''}<span title="${esc(fullDate(item.published_at))}">${age(item.published_at || item.first_seen_at)}</span>${c.flair ? `<span class="flair">${esc(c.flair)}</span>` : ''}</div>`;
  const foot = `<div class="post-foot"><a class="pill-btn" href="#/item/${item.id}">${icon('comment')}<span>${c.comments != null ? plural(c.comments, 'comment') : 'Comments'}</span></a><a class="pill-btn" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">${icon('out')}<span>${hn ? 'HN' : 'Reddit'}</span></a><span class="grow"></span>${actions(item, view === 'card' ? 'foot' : 'icon')}</div>`;
  if (view === 'compact') {
    return `<article class="post compact ${item.image_url ? '' : 'noimg'}" data-item="${item.id}">${rail}<div class="post-body">${head}<h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>${foot}</div>${item.image_url ? `<a class="thumb" href="#/item/${item.id}" tabindex="-1" aria-hidden="true"><div class="media"><img src="${esc(item.image_url)}" alt="" loading="lazy" decoding="async"></div></a>` : ''}</article>`;
  }
  let body = '';
  if (c.kind === 'media' && item.image_url) body = `<a href="#/item/${item.id}">${media(item)}</a>`;
  else if (item.link_url) {
    const blurb = item.summary || item.excerpt;
    body = `<a class="link-card ${item.image_url ? '' : 'noimg'}" href="${esc(item.link_url)}" target="_blank" rel="noopener"><div style="min-width:0"><span class="domain">${icon('out', 'sm')}${esc(hostOf(item.link_url))}</span>${blurb ? `<strong>${esc(blurb)}</strong>` : ''}</div>${item.image_url ? media(item) : ''}</a>`;
  }
  if (!item.link_url && describe(item)) body += `<p class="post-text">${esc(describe(item))}</p>`;
  return `<article class="post" data-item="${item.id}">${rail}<div class="post-body">${head}<h3><a href="#/item/${item.id}">${esc(item.title)}</a></h3>${body}${foot}</div></article>`;
}
function projectCard(item) {
  const r = item.repository || {};
  const growth = r.stars_delta_7d != null ? `<span class="up">+${compact(r.stars_delta_7d)}</span> / 7d` : '<b>—</b> / 7d';
  return `<article class="project" data-item="${item.id}"><div class="meta"><span class="tag">${esc(r.language || 'project')}</span><span>${esc(r.license || 'license unknown')}</span></div>
    <h3><a href="#/project/${r.id}">${esc(r.full_name || item.title)}</a></h3><p>${esc(r.description || item.excerpt || 'Discovered open-source project')}</p>
    <div class="metrics"><span>${icon('star', 'sm')} <b>${compact(r.stars)}</b></span><span>${growth}</span><span><b>${r.mentions_7d ?? 0}</b> mentions</span></div></article>`;
}
const renderItem = item => item.community ? postCard(item, 'compact') : item.repository && item.type === 'github_repository' ? rowCard(item) : rowCard(item);

// ---------------------------------------------------------------- subreddit health
function subHealth(sub) {
  const s = sub.source;
  if (!s) return ['off', 'seen via front page / cross-posts'];
  if (s.status === 'muted' || s.status === 'disabled') return ['off', s.status];
  if (s.consecutive_failures > 0) return ['bad', s.last_error || 'failing'];
  if (!s.last_success_at) return ['warn', 'waiting for first collection'];
  return ['', `collected ${age(s.last_success_at)} ago via ${s.access || 'reddit'}`];
}
async function loadSubs(force = false) {
  if (!force && state.subs.length && Date.now() - (state.subsAt || 0) < 60000) return state.subs;
  const d = await api('/communities');
  state.subs = d.communities; state.subsAt = Date.now(); state.oauth = d.oauth;
  const fresh = state.subs.reduce((n, s) => n + (s.items_24h || 0), 0);
  $('#reddit-count').textContent = fresh ? compact(fresh) : '';
  const top = state.subs.filter(s => s.source && !s.feed && s.source.status !== 'muted').slice(0, 10);
  $('#nav-subs').hidden = !top.length;
  $('#nav-subs-list').innerHTML = top.map(s => `<a href="#/reddit/${encodeURIComponent(s.name)}" data-sub="${esc(s.name.toLowerCase())}"><span class="sub-dot" style="--h:${hue(s.name)}">r/</span><span>${esc(s.name)}</span><small>${s.items_24h || ''}</small></a>`).join('');
  return state.subs;
}
function redditNotice(subs) {
  const failing = subs.filter(s => s.source && s.source.consecutive_failures > 0);
  const waiting = subs.filter(s => s.source && !s.source.last_success_at && !s.source.consecutive_failures);
  if (failing.length) {
    return `<div class="notice">${icon('bolt')}<div><b>${plural(failing.length, 'subreddit')} failing to collect.</b> ${esc(failing.map(s => 'r/' + s.name).join(', '))} — last error: <code>${esc((failing[0].source.last_error || '').slice(0, 140))}</code><br>Radar reads Reddit's public feeds without an account; for vote counts and more reliable access add a Reddit app id/secret under <code>[reddit]</code> in config.toml.</div></div>`;
  }
  if (waiting.length && waiting.length === subs.filter(s => s.source).length) {
    return `<div class="notice">${icon('refresh')}<div><b>First Reddit collection is running.</b> New subreddits are fetched by the worker within a couple of minutes.</div></div>`;
  }
  return '';
}

// ---------------------------------------------------------------- Reddit account (private feed token)
const ROLE_LABEL = {front: 'Front page (your subscriptions)', upvoted: 'Upvoted posts → likes', saved: 'Saved posts → saved'};
function accountCard(acc) {
  if (!acc?.connected) {
    return `<div class="card account-card"><div class="card-head"><h2><span class="badge reddit" style="width:24px;height:24px">${icon('reddit', 'sm')}</span>Connect your Reddit</h2></div>
      <p class="hint" style="margin-top:0">No password or API app needed. Radar follows your subscriptions and learns from what you upvote and save.</p>
      <ol class="steps"><li>Open <a href="https://www.reddit.com/prefs/feeds" target="_blank" rel="noopener">reddit.com/prefs/feeds</a> while logged in</li><li>Copy the <b>RSS</b> link next to “your front page”</li><li>Paste it here</li></ol>
      <form class="add-form" data-act="connect-reddit"><input class="field" name="feed_url" placeholder="https://www.reddit.com/.rss?feed=…&user=…" aria-label="Private Reddit feed URL" required autocomplete="off"><button class="action reddit" type="submit">${icon('plus', 'sm')}Connect account</button></form>
      <p class="hint">The link is a read-only token; it stays on this machine and is masked everywhere in the UI.</p></div>`;
  }
  const rows = acc.feeds.map(f => {
    const [h, t] = f.last_error ? ['bad', f.last_error] : f.last_success_at ? ['', `synced ${age(f.last_success_at)} ago via ${f.access || 'rss'}`] : ['warn', 'waiting for first sync'];
    return `<div class="sub-row" title="${esc(t)}"><i class="health ${h}"></i><span>${esc(ROLE_LABEL[f.role] || f.name)}</span><small>${f.last_error ? 'error' : f.items}</small></div>`;
  }).join('');
  const failed = acc.feeds.find(f => f.last_error);
  return `<div class="card account-card"><div class="card-head"><h2><span class="badge reddit" style="width:24px;height:24px">${icon('reddit', 'sm')}</span>u/${esc(acc.user)}</h2><small class="hint" style="margin:0">connected</small></div>
    <div class="sub-list">${rows}</div>
    <p class="hint">Learned from Reddit: <b>${acc.learned?.upvoted || 0}</b> upvotes and <b>${acc.learned?.saved || 0}</b> saves feed your interest model. Already-voted posts are kept out of your feeds.</p>
    ${failed ? `<p class="hint" style="color:var(--bad)">${esc(failed.name)}: ${esc(failed.last_error.slice(0, 160))}${/403/.test(failed.last_error) ? ' — Reddit rejected the token; copy the link again while logged in.' : ''}</p>` : ''}
    <div class="toolbar" style="margin:12px 0 0"><button class="action" data-act="sync-reddit">${icon('refresh', 'sm')}Sync now</button><button class="action" data-act="disconnect-reddit">Disconnect</button></div></div>`;
}
async function accountAction(kind, el) {
  try {
    if (kind === 'connect') {
      el.querySelector('button').disabled = true; el.querySelector('button').textContent = 'Connecting & syncing…';
      const r = await post('/reddit/account', {feed_url: el.feed_url.value});
      toast(`Connected u/${r.user} — ${r.learned.upvoted} upvotes, ${r.learned.saved} saves learned`);
    } else if (kind === 'sync') {
      el.disabled = true; el.textContent = 'Syncing…';
      const r = await post('/reddit/account/sync');
      toast(`Synced — ${r.learned.upvoted} upvotes, ${r.learned.saved} saves learned`);
    } else if (kind === 'disconnect') {
      if (!confirm('Disconnect Reddit? The stored feed token is deleted; interests already learned are kept.')) return;
      await api('/reddit/account', {method: 'DELETE'});
      toast('Reddit disconnected');
    }
  } catch (err) { toast(err.message); }
  state.fresh = true; await loadSubs(true).catch(() => {}); route();
}

// ---------------------------------------------------------------- Today
const SECTION_META = {
  reddit: ['Hot on Reddit', 'Top threads from your subreddits, ranked by community heat and your interests'],
  important: ['Top stories', 'What many sources and communities are talking about'],
  for_you: ['For you', 'Matched to your interest model'],
  oss: ['Open source momentum', 'Repositories gaining stars and mentions'],
  adjacent: ['Off the beaten path', 'Adjacent topics and deliberate exploration'],
};
async function today() {
  loading('hero');
  const [d, subs, hn] = await Promise.all([
    api('/digest?period=7d&limit=48&requested_by=ui'),
    loadSubs().catch(() => []),
    api('/community?platform=hackernews&sort=hot&period=2d&limit=16').catch(() => ({items: []})),
  ]);
  const by = Object.fromEntries(d.sections.map(s => [s.key, s.items]));
  by.adjacent = [...(by.adjacent || []), ...(by.wildcard || [])];
  const shown = new Set(d.sections.flatMap(s => s.items.map(i => i.id)));
  const reddit = by.reddit || [];
  const hnItems = hn.items.filter(i => !shown.has(i.id)).slice(0, 10);
  const sourceCount = new Set(d.sections.flatMap(s => s.items.map(i => i.source?.id)).filter(Boolean)).size;
  const now = new Date();
  const kicker = `<b>${esc(now.toLocaleDateString(undefined, {weekday: 'long'}))}</b><span>${esc(now.toLocaleDateString(undefined, {day: 'numeric', month: 'long'}))}</span>`;
  const stats = `<div class="stats"><div class="stat reddit"><strong>${reddit.length}</strong><span>Reddit threads</span></div><div class="stat"><strong>${d.count - reddit.length}</strong><span>stories & projects</span></div><div class="stat"><strong>${sourceCount}</strong><span>sources</span></div></div>`;
  let html = header(kicker, 'Your radar today', 'Reddit first, then the stories, launches and projects worth your time — ranked by what you like, with room for surprises.', stats);
  html += redditNotice(subs);
  // Reddit hero
  const redditBadge = `<span class="badge reddit">${icon('reddit')}</span>`;
  const redditMore = '<a class="more" href="#/reddit">Open Reddit →</a>';
  if (reddit.length) {
    const [lead, ...rest] = reddit;
    html += `<section class="section">${sectionTitle(...SECTION_META.reddit, redditBadge, redditMore)}
      <div class="reddit-hero">${leadCard(lead)}<div class="stack">${rest.slice(0, 4).map(miniCard).join('')}</div></div>
      ${rest.length > 4 ? `<div class="grid-2">${rest.slice(4, 12).map(miniCard).join('')}</div>` : ''}</section>`;
  } else {
    html += `<section class="section">${sectionTitle(...SECTION_META.reddit, redditBadge, redditMore)}${empty('No Reddit threads yet', '<p class="hint">Add subreddits on the Reddit page; the worker collects them within a few minutes.</p><p><a class="action reddit" href="#/reddit">Set up Reddit</a></p>')}</section>`;
  }
  // Rail: HN + subreddits
  const hnList = hnItems.map((it, n) => `<a class="hn-item" href="#/item/${it.id}"><span class="n">${n + 1}</span><div><h3>${esc(it.title)}</h3><div class="meta">${it.community?.score != null ? `<span>${compact(it.community.score)} points</span><span class="dot"></span>` : ''}<span>${compact(it.community?.comments ?? 0)} comments</span>${it.link_url ? `<span class="dot"></span><span class="domain">${esc(hostOf(it.link_url))}</span>` : ''}</div></div></a>`).join('');
  const subList = subs.filter(s => s.source && !s.feed).slice(0, 9).map(s => { const [h, t] = subHealth(s); return `<a class="sub-row" href="#/reddit/${encodeURIComponent(s.name)}" title="${esc(t)}"><span class="sub-dot" style="--h:${hue(s.name)}">r/</span><span>r/${esc(s.name)}</span><small><i class="health ${h}"></i> ${s.items_24h || 0} today</small></a>`; }).join('');
  html += `<aside class="rail">
    <div class="card"><div class="card-head"><h2><span class="badge hn" style="width:24px;height:24px">${icon('hn')}</span>Hacker News</h2><a href="#/hn">All →</a></div><div class="hn-list">${hnList || '<p class="hint">Hacker News items appear after the next collection.</p>'}</div></div>
    ${subList ? `<div class="card"><div class="card-head"><h2>Your subreddits</h2><a href="#/reddit">Manage →</a></div><div class="sub-list">${subList}</div></div>` : ''}
  </aside>`;
  if (by.important?.length) html += `<section class="section">${sectionTitle(...SECTION_META.important, `<span class="badge accent">${icon('bolt')}</span>`)}<div class="grid-3">${by.important.map(newsCard).join('')}</div></section>`;
  if (by.for_you?.length) html += `<section class="section">${sectionTitle(...SECTION_META.for_you, `<span class="badge soft">${icon('heart')}</span>`)}<div class="list">${by.for_you.map(rowCard).join('')}</div></section>`;
  if (by.oss?.length) html += `<section class="section">${sectionTitle(...SECTION_META.oss, `<span class="badge soft">${icon('projects')}</span>`, '<a class="more" href="#/projects">All projects →</a>')}<div class="project-grid">${by.oss.map(projectCard).join('')}</div></section>`;
  if (by.adjacent.length) html += `<section class="section">${sectionTitle(...SECTION_META.adjacent, `<span class="badge soft">${icon('explore')}</span>`)}<div class="list">${by.adjacent.map(rowCard).join('')}</div></section>`;
  if (!d.count) html += empty('The first collection cycle has not produced a digest yet.', '<p class="hint">Run <code>radar collect --force</code> or wait for the worker.</p>');
  $('#main').innerHTML = `<div class="today">${html}</div>`;
}

// ---------------------------------------------------------------- Reddit / HN community view
const SORTS = [['hot', 'Hot'], ['top', 'Top'], ['new', 'New'], ['discussed', 'Discussed'], ['best', 'For you']];
const PERIODS = [['24h', '24h'], ['3d', '3 days'], ['7d', 'Week'], ['30d', 'Month']];
function communityParams(platform) {
  return {sort: store.get(`radar-${platform}-sort`, 'hot'), period: store.get(`radar-${platform}-period`, '3d'), view: store.get('radar-view', 'card')};
}
async function communityPage(platform, name) {
  loading('list');
  const p = communityParams(platform);
  const reddit = platform === 'reddit';
  const q = `/community?platform=${platform}&sort=${p.sort}&period=${p.period}&limit=40${name ? `&community=${encodeURIComponent(name)}` : ''}`;
  const [feed, allSubs, account] = await Promise.all([api(q), reddit ? loadSubs(true).catch(() => []) : Promise.resolve([]),
    reddit ? api('/reddit/account').catch(() => null) : Promise.resolve(null)]);
  const subs = allSubs.filter(s => !s.feed);
  state.feed = {platform, name, q, offset: feed.items.length, done: feed.items.length < 40};
  const sub = subs.find(s => s.name.toLowerCase() === (name || '').toLowerCase());
  const title = reddit ? (name ? `r/${name}` : 'Reddit') : 'Hacker News';
  const kicker = reddit ? `<span class="badge reddit" style="width:22px;height:22px">${icon('reddit', 'sm')}</span><b style="color:var(--reddit)">Reddit</b><span>${name ? 'Subreddit' : 'All your subreddits'}</span>` : `<span class="badge hn" style="width:22px;height:22px">${icon('hn', 'sm')}</span><b style="color:var(--hn)">Hacker News</b><span>top · best · search</span>`;
  const total = subs.filter(s => s.source).length;
  const sub24 = sub ? `${sub.items_24h} new today · ${sub.items} collected` : reddit ? `${plural(total, 'subreddit')} · ${subs.reduce((n, s) => n + (s.items_24h || 0), 0)} new threads today` : 'Stories and discussions from news.ycombinator.com';
  const right = `<div class="toolbar" style="margin:0">${reddit && name ? `<a class="action" href="https://www.reddit.com/r/${encodeURIComponent(name)}" target="_blank" rel="noopener">${icon('out', 'sm')}Open on Reddit</a>` : ''}<div class="segmented" role="group" aria-label="View"><button class="${p.view === 'card' ? 'active' : ''}" data-act="view" data-view="card" title="Card view">${icon('cards', 'sm')}Cards</button><button class="${p.view === 'compact' ? 'active' : ''}" data-act="view" data-view="compact" title="Compact view">${icon('list', 'sm')}Compact</button></div></div>`;
  const tabs = reddit ? `<div class="scroller" role="tablist"><a class="chip sub ${!name ? 'active' : ''}" href="#/reddit">All <small>${subs.reduce((n, s) => n + (s.items_24h || 0), 0)}</small></a>${subs.map(s => `<a class="chip sub ${s.name.toLowerCase() === (name || '').toLowerCase() ? 'active' : ''}" href="#/reddit/${encodeURIComponent(s.name)}"><span class="sub-dot" style="--h:${hue(s.name)};width:14px;height:14px;font-size:7px">r/</span>${esc(s.name)} <small>${s.items_24h || 0}</small></a>`).join('')}</div>` : '';
  const sorts = `<div class="toolbar"><div class="segmented" role="group" aria-label="Sort">${SORTS.map(([k, l]) => `<button class="${p.sort === k ? 'active' : ''}" data-act="sort" data-platform="${platform}" data-sort="${k}">${l}</button>`).join('')}</div><span class="grow"></span><select class="select" data-act="period" data-platform="${platform}" aria-label="Time range">${PERIODS.map(([k, l]) => `<option value="${k}" ${p.period === k ? 'selected' : ''}>${l}</option>`).join('')}</select></div>`;
  const posts = feed.items.map(it => postCard(it, p.view)).join('');
  const emptyMsg = reddit ? empty(name ? `Nothing from r/${name} in this time range` : 'No Reddit threads in this time range', `<p class="hint">${sub && sub.source ? esc(subHealth(sub)[1]) : 'Try a longer range, or add subreddits on the right.'}</p>`) : empty('No Hacker News stories in this time range');
  let rail = '';
  if (reddit) {
    const rows = subs.map(s => { const [h, t] = subHealth(s); return `<div class="sub-row"><span class="health ${h}" title="${esc(t)}"></span><a href="#/reddit/${encodeURIComponent(s.name)}"><span>r/${esc(s.name)}</span></a><small>${s.source ? `<button class="pill-btn" style="min-height:28px;padding:2px 6px" data-act="source-status" data-id="${s.source.id}" data-status="${s.source.status === 'muted' ? 'active' : 'muted'}" title="${s.source.status === 'muted' ? 'Resume' : 'Mute'} collection">${s.source.status === 'muted' ? 'Resume' : 'Mute'}</button>` : `${s.items}`}</small></div>`; }).join('');
    rail = `<aside class="rail">${accountCard(account)}
      <div class="card"><div class="card-head"><h2>Add subreddits</h2></div>
        <form class="add-form" data-act="add-subs"><textarea class="field" name="subs" placeholder="rust, r/golang, r/selfhosted …" aria-label="Subreddits to add"></textarea><button class="action reddit" type="submit">${icon('plus', 'sm')}Add to radar</button></form>
        <p class="hint">Names, <code>r/name</code>, subreddit links or multireddits like <code>rust+golang</code>.</p></div>
      <div class="card subs-card"><div class="card-head"><h2>Subreddits</h2><small class="hint" style="margin:0">${plural(total, 'source')}</small></div><div class="sub-list">${rows || '<p class="hint">No subreddits yet.</p>'}</div>
        <p class="hint">Access: public RSS${state.oauth ? ' + OAuth API' : ''}. ${state.oauth ? '' : 'Vote counts need a Reddit app in <code>config.toml</code>; until then posts rank by their Hot/Top position.'}</p></div>
    </aside>`;
  }
  $('#main').innerHTML = header(kicker, title, esc(sub24), right) + (reddit ? redditNotice(sub ? [sub] : subs) : '') +
    `<div class="community"><div class="community-feed">${tabs}${sorts}<div class="feed-col" id="feed-col">${posts || emptyMsg}</div><div class="load-more">${state.feed.done ? '' : '<button class="action" data-act="more">Load more</button>'}</div></div>${rail}</div>`;
  $$('#nav-subs-list a').forEach(a => a.classList.toggle('active', a.dataset.sub === (name || '').toLowerCase()));
}
async function loadMore(btn) {
  const f = state.feed; if (!f || f.done) return;
  btn.disabled = true; btn.textContent = 'Loading…';
  const data = await api(`${f.q}&offset=${f.offset}`);
  const view = communityParams(f.platform).view;
  $('#feed-col').insertAdjacentHTML('beforeend', data.items.map(it => postCard(it, view)).join(''));
  f.offset += data.items.length; f.done = data.items.length < 40;
  btn.parentElement.innerHTML = f.done ? '' : '<button class="action" data-act="more">Load more</button>';
}

// ---------------------------------------------------------------- Explore, projects, lists, search
const EXPLORE_TYPES = [['', 'All'], ['reddit_post', 'Reddit'], ['hackernews_post', 'Hacker News'], ['rss_entry', 'News'], ['article', 'Articles'], ['youtube_video', 'Video'], ['github_repository', 'Projects'], ['github_release', 'Releases']];
async function explore(params = {}) {
  loading('list');
  const offset = params.offset || 0, sort = params.sort || 'newest';
  const type = params.type ? `&content_type=${encodeURIComponent(params.type)}` : '';
  const data = await api(`/items?sort=${sort}&limit=60&offset=${offset}${type}`);
  state.explore = {...params, offset, sort};
  $('#main').innerHTML = header('<b>Manual mode</b><span>everything collected, unranked</span>', 'Explore everything', 'Browse the complete stream independently of recommendations.') +
    `<div class="scroller">${EXPLORE_TYPES.map(([k, l]) => `<button class="chip ${(params.type || '') === k ? 'active' : ''}" data-act="explore-type" data-type="${k}">${l}</button>`).join('')}</div>
     <div class="toolbar"><div class="segmented">${[['newest', 'Newest'], ['score', 'Relevance'], ['novelty', 'Novelty']].map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="explore-sort" data-sort="${k}">${l}</button>`).join('')}</div></div>
     <div class="list">${data.items.map(rowCard).join('') || empty('No collected items match this view.')}</div>
     <div class="load-more">${offset ? '<button class="action" data-act="explore-page" data-dir="-1">← Previous</button>' : ''}${data.items.length === 60 ? '<button class="action" data-act="explore-page" data-dir="1">Next 60 →</button>' : ''}</div>`;
}
async function projects(sort = 'momentum') {
  loading('list');
  const data = await api(`/projects?sort=${sort}&limit=100`);
  $('#main').innerHTML = header('<b>Open source</b><span>repositories with growth, releases and mentions</span>', 'Project discovery', 'Repositories analyzed with activity, star growth, releases, license and cross-community signals.') +
    `<div class="toolbar"><div class="segmented">${[['momentum', 'Momentum'], ['newest', 'Newest'], ['stars', 'Stars']].map(([k, l]) => `<button class="${sort === k ? 'active' : ''}" data-act="project-sort" data-sort="${k}">${l}</button>`).join('')}</div></div>
     <div class="project-grid">${data.projects.map(projectCard).join('') || empty('Projects will appear after the GitHub collectors run.')}</div>`;
}
async function listMode(mode) {
  loading('list');
  const data = await api(`/items?${mode}=true&sort=newest&limit=100`);
  const saved = mode === 'saved';
  $('#main').innerHTML = header(saved ? '<b>Library</b>' : '<b>Activity</b>', saved ? 'Saved' : 'Reading history', saved ? 'Threads and stories kept for later.' : 'Everything you opened, newest first.') +
    `<div class="list">${data.items.map(rowCard).join('') || empty(saved ? 'Nothing saved yet' : 'No reading history yet', `<p class="hint">${saved ? 'Use the bookmark on any card.' : 'Open a thread or story to see it here.'}</p>`)}</div>`;
}
async function search(query) {
  loading('list');
  const d = await api(`/search?q=${encodeURIComponent(query)}`);
  $('#search').value = query;
  const reddit = d.results.filter(isReddit), other = d.results.filter(i => !isReddit(i));
  $('#main').innerHTML = header('<b>Search</b><span>lexical + semantic</span>', `“${query}”`, `${plural(d.count, 'match', 'matches')} across everything Radar has collected.`) +
    (reddit.length ? `<section class="section">${sectionTitle('On Reddit', plural(reddit.length, 'thread'), `<span class="badge reddit">${icon('reddit')}</span>`)}<div class="feed-col">${reddit.map(it => postCard(it, 'compact')).join('')}</div></section>` : '') +
    (other.length ? `<section class="section">${sectionTitle('Stories, HN & projects', plural(other.length, 'result'), `<span class="badge soft">${icon('search')}</span>`)}<div class="list">${other.map(rowCard).join('')}</div></section>` : '') +
    (d.count ? '' : empty('No matches found', '<p class="hint">Try fewer or broader words.</p>'));
}

// ---------------------------------------------------------------- Sources & settings
const KINDS = [['reddit', 'Subreddit / Reddit feed', 'LocalLLaMA  or  https://www.reddit.com/.rss?feed=…&user=…'], ['rss', 'RSS / Atom feed', 'https://example.com/feed.xml'], ['hn_search', 'Hacker News search', 'local llm'], ['youtube', 'YouTube channel', 'UC… channel id'], ['github_search', 'GitHub search', 'topic:self-hosted stars:>50'], ['google_news', 'Google News search', '"open source" AI'], ['website_watch', 'Website change watch', 'https://example.com/changelog']];
async function sources() {
  loading('list');
  const d = await api('/sources');
  const rows = d.sources.map(s => {
    const failing = s.last_error && s.status === 'active';
    return `<tr><td><b>${esc(s.name)}</b><br><small>${esc(s.identifier)}</small>${failing ? `<span class="err" title="${esc(s.last_error)}">${esc(s.last_error)}</span>` : ''}</td><td>${esc(s.kind)}${s.access ? `<br><small>via ${esc(s.access)}</small>` : ''}</td>
      <td><span class="status ${failing ? 'failing' : esc(s.status)}">${failing ? 'failing' : esc(s.status)}</span></td><td class="num">${s.item_count}</td><td>${age(s.last_success_at) || '—'}</td>
      <td><button class="action" data-act="source-status" data-id="${s.id}" data-status="${s.status === 'active' || s.status === 'trial' ? 'muted' : 'active'}">${s.status === 'active' || s.status === 'trial' ? 'Mute' : 'Activate'}</button></td></tr>`;
  }).join('');
  $('#main').innerHTML = header('<b>Collection network</b>', 'Sources', 'Seed, manual and automatically discovered inputs with visible health.') +
    `<div class="card" style="margin-bottom:18px"><div class="card-head"><h2>Add a source</h2></div><form class="form-row" data-act="add-source">
      <select class="select" name="kind" aria-label="Source type">${KINDS.map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
      <input class="field" name="identifier" placeholder="${esc(KINDS[0][2])}" aria-label="Identifier" required><button class="action primary" type="submit">Add</button></form></div>
     <div class="table-wrap"><table class="source-table"><thead><tr><th>Source</th><th>Type</th><th>Status</th><th>Items</th><th>Last success</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
const MIX = [
  ['feed.budget', 'reddit', 'Reddit share of Today', 0, 0.6, 0.01, v => `${Math.round(v * 100)}%`],
  ['ranking.content_type_preference', 'reddit_post', 'Reddit preference', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'hackernews_post', 'Hacker News preference', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'github_repository', 'GitHub preference', 0, 1, 0.05, v => v.toFixed(2)],
  ['ranking.content_type_preference', 'default', 'News & articles', 0, 1, 0.05, v => v.toFixed(2)],
];
async function settings() {
  loading('list');
  const [ints, health, cfg, account] = await Promise.all([api('/interests'), api('/health'), api('/settings'), api('/reddit/account').catch(() => null)]);
  state.settings = cfg.settings;
  const sliders = MIX.map(([key, sub, label, min, max, step, fmt], n) => { const v = Number(cfg.settings[key]?.[sub] ?? 0); return `<label class="slider-row"><span>${esc(label)}</span><input type="range" min="${min}" max="${max}" step="${step}" value="${v}" data-act="mix" data-n="${n}"><output>${fmt(v)}</output></label>`; }).join('');
  $('#main').innerHTML = header('<b>Transparent controls</b>', 'Settings & interests', 'Radar learns from explicit actions; exploration keeps its own budget.') +
    `<section class="section">${sectionTitle('Reddit account', 'Subscriptions, upvotes and saves as taste signals', `<span class="badge reddit">${icon('reddit')}</span>`)}<div style="max-width:560px">${accountCard(account)}</div></section>
     <section class="section">${sectionTitle('Feed mix', 'How much Reddit, Hacker News and the rest weigh in ranking', `<span class="badge reddit">${icon('reddit')}</span>`)}<div class="card">${sliders}<p class="hint">Changes apply on the next Today refresh.</p></div></section>
     <section class="section">${sectionTitle('Interest model', plural(ints.interests.length, 'topic'), `<span class="badge soft">${icon('heart')}</span>`)}<div class="table-wrap"><table class="source-table"><thead><tr><th>Topic</th><th>Weight</th><th>Evidence</th><th>Origin</th></tr></thead><tbody>${ints.interests.map(x => `<tr><td><b>${esc(x.topic)}</b></td><td class="num">${x.weight.toFixed(2)}</td><td class="num">+${x.positive_evidence} / −${x.negative_evidence}</td><td>${x.seed ? 'seed' : ''}${x.learned ? ' learned' : ''}</td></tr>`).join('')}</tbody></table></div></section>
     <section class="section">${sectionTitle('Local system', '', `<span class="badge soft">${icon('sources')}</span>`)}<div class="card"><p style="margin:0;color:var(--text-2)">Database holds <b>${health.counts.items}</b> items from <b>${health.counts.sources}</b> sources, <b>${health.counts.projects}</b> projects and <b>${health.counts.clusters}</b> story clusters. Processing stays on this machine.</p></div></section>`;
}
let mixTimer;
function onMix(input) {
  const [key, sub, , , , , fmt] = MIX[Number(input.dataset.n)];
  const value = Number(input.value);
  input.nextElementSibling.textContent = fmt(value);
  state.settings[key] = {...state.settings[key], [sub]: value};
  clearTimeout(mixTimer);
  mixTimer = setTimeout(() => api(`/settings/${key}`, {method: 'PATCH', body: JSON.stringify({value: state.settings[key]})}).then(() => toast('Feed mix saved')).catch(e => toast(e.message)), 400);
}

// ---------------------------------------------------------------- detail
async function detail(id, isProject = false) {
  loading('list');
  const d = await api(isProject ? `/projects/${id}` : `/items/${id}`);
  const item = d.project || d.item, r = item.repository, c = item.community;
  if (!state.opened.has(item.id)) { state.opened.add(item.id); post(`/items/${item.id}/feedback`, {action: 'open'}).catch(() => {}); }
  const story = item.cluster_id ? await api(`/stories/${item.cluster_id}`).catch(() => null) : null;
  const reddit = c?.platform === 'reddit', hn = c?.platform === 'hackernews';
  const meta = `<div class="meta">${reddit ? subLink(c.name) : sourceMark(item)}${item.author ? `<span class="dot"></span><span>${reddit ? 'u/' : ''}${esc(item.author)}</span>` : ''}<span class="dot"></span><span title="${esc(fullDate(item.published_at))}">${age(item.published_at || item.first_seen_at)} ago</span>${c?.flair ? `<span class="flair">${esc(c.flair)}</span>` : ''}</div>`;
  const buttons = `<div class="toolbar">${c ? `<a class="action ${reddit ? 'reddit' : ''}" href="${esc(discussionUrl(item))}" target="_blank" rel="noopener">${icon('out', 'sm')}Open on ${reddit ? 'Reddit' : 'HN'}</a>` : ''}${item.link_url || !c ? `<a class="action ${c ? '' : 'primary'}" href="${esc(outboundUrl(item))}" target="_blank" rel="noopener">${icon('out', 'sm')}${c ? esc(hostOf(outboundUrl(item))) : 'Open original'}</a>` : ''}
    <button class="action" data-act="save" data-id="${item.id}">${icon('saved', 'sm')}Save</button><button class="action" data-act="like" data-id="${item.id}">${icon('heart', 'sm')}More like this</button><button class="action" data-act="dismiss" data-id="${item.id}" data-back="1">${icon('x', 'sm')}Hide</button></div>`;
  const body = item.body ? `<div class="prose">${prose(item.body)}</div>` : '';
  const content = item.content && (!item.body || item.content.length > item.body.length + 400)
    ? `<section class="section">${sectionTitle(c ? 'From the linked page' : 'Source material', c && item.link_url ? esc(hostOf(item.link_url)) : '')}<div class="panel"><div class="prose clamp" id="source-text">${prose(item.content)}</div><button class="action" data-act="expand" style="margin-top:12px">Read more</button></div></section>` : '';
  const coverage = story && story.story.sources.length > 1 ? `<section class="section">${sectionTitle('Coverage', `${story.story.source_count} sources · ${story.story.item_count} reports`)}<div class="list">${story.story.sources.map(rowCard).join('')}</div></section>` : '';
  const facts = [];
  if (c) {
    if (c.votes_known && c.score != null) facts.push([hn ? 'Points' : 'Upvotes', compact(c.score)]);
    else if (heat(c)) facts.push([`r/${c.name} ${heat(c).sort}`, `#${heat(c).rank} of ${heat(c).size || '?'}`]);
    if (c.comments != null) facts.push(['Comments', compact(c.comments)]);
    if (c.upvote_ratio) facts.push(['Upvoted', `${Math.round(c.upvote_ratio * 100)}%`]);
    if (c.kind) facts.push(['Post type', c.kind]);
  }
  if (r) facts.push(['Stars', compact(r.stars)], ['Growth / 7d', r.stars_delta_7d ?? '—'], ['License', r.license || 'unknown'], ['Language', r.language || '—']);
  if (item.score) facts.push(['Relevance', Math.round(item.score.total * 100)], ['Interest', item.score.interest.toFixed(2)]);
  facts.push(['First seen', fullDate(item.first_seen_at)]);
  const rail = `<aside class="rail"><div class="card"><div class="card-head"><h2>Signals</h2></div><div class="facts">${facts.map(([k, v]) => `<div class="fact"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div></div>
    ${item.why_it_matters ? `<div class="card"><div class="eyebrow"><b>Why it matters</b></div><p style="margin:0;color:var(--text-2);line-height:1.55">${esc(item.why_it_matters)}</p></div>` : ''}</aside>`;
  const hero = item.image_url && (c?.kind === 'media' || !c) ? `<div class="media hero-media"><img src="${esc(item.image_url)}" alt=""></div>` : '';
  const discussion = c && ['reddit', 'hackernews'].includes(c.platform) ? `<section class="section" id="discussion">${sectionTitle('Discussion', c.comments != null ? plural(c.comments, 'comment') : '', `<span class="badge ${reddit ? 'reddit' : 'hn'}">${icon('comment')}</span>`, `<button class="action" data-act="refresh-comments" data-id="${item.id}">${icon('refresh', 'sm')}Refresh</button>`)}<div id="comments">${skeleton('list', 3)}</div></section>` : '';
  $('#main').innerHTML = `<a class="back" href="javascript:history.back()">${icon('back', 'sm')}Back</a><div class="detail"><article class="detail-main">${meta}<h1>${esc(item.title)}</h1>
    ${!item.body && describe(item) ? `<p class="lede">${esc(describe(item))}</p>` : ''}${buttons}${hero}${body}${content}${coverage}${discussion}</article>${rail}</div>`;
  if (discussion) loadComments(item.id, false);
}
async function loadComments(id, refresh) {
  const box = $('#comments'); if (!box) return;
  if (refresh) box.innerHTML = skeleton('list', 2);
  try {
    const t = await api(`/items/${id}/comments${refresh ? '?refresh=true' : ''}`);
    if (!t.comments.length) { box.innerHTML = empty(t.error ? 'Could not load the discussion' : 'No comments yet', t.error ? `<p class="hint">${esc(t.error)}</p>` : ''); return; }
    box.innerHTML = `<div class="comments">${t.comments.map(cm => `<div class="comment" style="--d:${Math.min(cm.depth || 0, 6)}"><div class="c-inner" style="--h:${hue(cm.author)}"><div class="c-head"><b>${esc(cm.author || 'anonymous')}</b>${cm.score != null ? `<span class="vote">${icon('up')}${compact(cm.score)}</span>` : ''}<span title="${esc(fullDate(cm.created_at))}">${age(cm.created_at)}</span></div><div class="prose">${prose(cm.text)}</div></div></div>`).join('')}</div>
      <p class="hint">${plural(t.count, 'comment')} loaded via ${esc(t.via || 'source')}${t.stale ? ' (cached — live refresh failed)' : ''} · ${age(t.fetched_at)} ago</p>`;
  } catch (err) { box.innerHTML = empty('Could not load the discussion', `<p class="hint">${esc(err.message)}</p>`); }
}

// ---------------------------------------------------------------- delegated interactions
async function feedbackAction(btn) {
  const id = btn.dataset.id, act = btn.dataset.act;
  try {
    if (act === 'save') await post(`/items/${id}/save`);
    else await post(`/items/${id}/feedback`, {action: act});
    toast(act === 'save' ? 'Saved' : act === 'like' ? 'Got it — more like this' : 'Hidden');
    if (act === 'like') btn.classList.add('on');
    if (act === 'dismiss') { if (btn.dataset.back) history.back(); else btn.closest('[data-item]')?.remove(); }
  } catch (err) { toast(err.message); }
}
async function addSubs(form) {
  const names = form.subs.value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  if (!names.length) return;
  let added = 0; const failed = [];
  for (const n of names) {
    try { await post('/sources', {kind: 'reddit', identifier: n}); added++; } catch (err) { failed.push(`${n}: ${err.message}`); }
  }
  toast(added ? `${plural(added, 'subreddit')} added — collecting within ~2 min` : failed[0] || 'Nothing added');
  await loadSubs(true); route();
}
document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]');
  if (el && el.tagName === 'A') return;
  if (!el) {
    const nav = e.target.closest('.sidebar a, .mobile-nav a, .brand'); if (nav) state.fresh = true;
    return;
  }
  const act = el.dataset.act;
  if (['like', 'save', 'dismiss'].includes(act)) { e.preventDefault(); feedbackAction(el); }
  else if (act === 'sort') { store.set(`radar-${el.dataset.platform}-sort`, el.dataset.sort); state.fresh = true; route(); }
  else if (act === 'view') { store.set('radar-view', el.dataset.view); state.fresh = true; route(); }
  else if (act === 'more') loadMore(el).catch(err => toast(err.message));
  else if (act === 'explore-type') explore({type: el.dataset.type, sort: state.explore?.sort});
  else if (act === 'explore-sort') explore({type: state.explore?.type, sort: el.dataset.sort});
  else if (act === 'explore-page') explore({...state.explore, offset: Math.max(0, (state.explore?.offset || 0) + Number(el.dataset.dir) * 60)});
  else if (act === 'project-sort') projects(el.dataset.sort);
  else if (act === 'source-status') api(`/sources/${el.dataset.id}`, {method: 'PATCH', body: JSON.stringify({status: el.dataset.status})}).then(() => { toast('Source updated'); state.fresh = true; loadSubs(true); route(); }).catch(err => toast(err.message));
  else if (act === 'expand') { $('#source-text')?.classList.add('open'); el.remove(); }
  else if (act === 'refresh-comments') loadComments(el.dataset.id, true);
  else if (act === 'sync-reddit') accountAction('sync', el);
  else if (act === 'disconnect-reddit') accountAction('disconnect', el);
});
document.addEventListener('change', e => {
  const el = e.target.closest('[data-act="period"]');
  if (el) { store.set(`radar-${el.dataset.platform}-period`, el.value); state.fresh = true; route(); }
});
document.addEventListener('input', e => { if (e.target.dataset?.act === 'mix') onMix(e.target); });
document.addEventListener('submit', e => {
  const form = e.target.closest('form[data-act]'); if (!form) return;
  e.preventDefault();
  if (form.dataset.act === 'add-subs') addSubs(form);
  if (form.dataset.act === 'connect-reddit') accountAction('connect', form);
  if (form.dataset.act === 'add-source') post('/sources', {kind: form.kind.value, identifier: form.identifier.value}).then(() => { toast('Source added'); sources(); }).catch(err => toast(err.message));
});
document.addEventListener('change', e => { if (e.target.name === 'kind' && e.target.form?.dataset.act === 'add-source') e.target.form.identifier.placeholder = KINDS.find(k => k[0] === e.target.value)?.[2] || ''; });

// ---------------------------------------------------------------- routing with Back restore
const CACHEABLE = new Set(['today', 'reddit', 'hn', 'explore', 'projects', 'saved', 'history', 'search']);
let currentHash = null;
async function route() {
  const hash = location.hash || '#/today';
  const [name, ...rest] = hash.replace(/^#\//, '').split('/');
  const arg = decodeURIComponent(rest.join('/'));
  if (currentHash && CACHEABLE.has(currentHash.split('/')[1])) state.cache.set(currentHash, {html: $('#main').innerHTML, y: scrollY, feed: state.feed, at: Date.now()});
  const prev = currentHash; currentHash = hash;
  state.route = name || 'today';
  $$('[data-route]').forEach(a => a.classList.toggle('active', a.dataset.route === state.route));
  const cached = state.cache.get(hash);
  if (!state.fresh && cached && prev !== hash && Date.now() - cached.at < 15 * 60e3) {
    $('#main').innerHTML = cached.html; state.feed = cached.feed; requestAnimationFrame(() => scrollTo(0, cached.y)); return;
  }
  state.fresh = false;
  try {
    if (state.route === 'today') await today();
    else if (state.route === 'reddit') await communityPage('reddit', arg || null);
    else if (state.route === 'hn') await communityPage('hackernews', null);
    else if (state.route === 'explore') await explore();
    else if (state.route === 'projects') await projects();
    else if (state.route === 'saved' || state.route === 'history') await listMode(state.route);
    else if (state.route === 'sources') await sources();
    else if (state.route === 'settings') await settings();
    else if (state.route === 'item') await detail(arg);
    else if (state.route === 'project') await detail(arg, true);
    else if (state.route === 'search') await search(arg);
    else await today();
  } catch (err) {
    $('#main').innerHTML = header('<b>Connection issue</b>', 'Radar is unavailable', esc(err.message)) + empty('Check that the local API service is running.');
  }
  if (prev !== hash) scrollTo(0, 0);
}
$('#search').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.value.trim()) { state.fresh = true; location.hash = `#/search/${encodeURIComponent(e.target.value.trim())}`; } });
document.addEventListener('keydown', e => { if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) { e.preventDefault(); $('#search').focus(); } });
$('#theme').onclick = () => { const light = document.documentElement.classList.toggle('light'); store.set('radar-theme', light ? 'light' : 'dark'); };
window.addEventListener('hashchange', route);
route();
loadSubs().catch(() => {});
api('/health').then(h => { $('#system-pill').classList.add('ok'); $('#system-pill span').textContent = `Local · ${compact(h.counts.items)} items`; }).catch(() => { $('#system-pill span').textContent = 'Offline'; });
