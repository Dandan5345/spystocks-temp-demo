const $ = (id) => document.getElementById(id);
const ui = {
  insider: {
    eyebrow: 'INSIDER INTELLIGENCE · OFFICIAL FILINGS',
    title: 'Know the person<br/><span>behind the trade.</span>',
    copy: 'Search any SEC reporting owner. We resolve the person, rebuild their career footprint and decode Forms 3, 4 and 5 directly from EDGAR.',
    placeholder: 'Try: Jensen Huang, Lisa Su, or a random insider…',
    shortPlaceholder: 'Search an SEC insider…',
    source: 'Live from SEC EDGAR',
    quick: ['Jensen Huang', 'Lisa Su', 'Satya Nadella']
  },
  politician: {
    eyebrow: 'CONGRESSIONAL INTELLIGENCE · OFFICIAL RECORDS',
    title: 'Follow the person<br/><span>behind the policy.</span>',
    copy: 'Search current and former members of Congress. Explore verified identity, service history and legislation directly from Congress.gov.',
    placeholder: 'Try: Nancy Pelosi, Tommy Tuberville, or any member…',
    shortPlaceholder: 'Search a member of Congress…',
    source: 'Verified by Congress.gov',
    quick: ['Nancy Pelosi', 'Tommy Tuberville', 'Bernie Sanders']
  },
  institution: {
    eyebrow: 'INSTITUTIONAL INTELLIGENCE · SEC FORM 13F',
    title: 'See where the<br/><span>big money moves.</span>',
    copy: 'Search institutional investment managers and explore their latest SEC-reported holdings, portfolio changes and quarterly Form 13F history.',
    placeholder: 'Try: BlackRock, Berkshire Hathaway, or Citadel Advisors…',
    shortPlaceholder: 'Search a 13F manager…',
    source: 'Verified by SEC Form 13F',
    quick: ['BlackRock', 'Berkshire Hathaway', 'Citadel Advisors']
  }
};

let mode = 'insider';
let allTransactions = [];
let searchTimer;
let searchAbort = null;
let searchToken = 0;
let searchPatienceTimer;
const searchCache = new Map();
const narrowScreen = matchMedia('(max-width: 560px)');
let activePolitician = null;
let insiderLoadId = 0;
let pageLoadId = 0;
let activeInsiderKnowledge = null;
const responseCache = new Map();
const inFlightRequests = new Map();
const legislationState = {sponsored: {offset: 0, items: []}, cosponsored: {offset: 0, items: []}};
const institutionHoldingsState = {cik: null, offset: 0, items: [], total: 0};
const esc = (s = '') => String(s).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const initials = (name = '') => name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join('').toUpperCase() || '?';
const display = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : value;
// The interface is written in English, so dates stay in English too rather than
// following the phone's locale and producing half-translated lines.
const date = (value) => {
  if (!value) return '—';
  const parsed = new Date(value.length === 4 ? `${value}-01-01T00:00:00` : value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleDateString('en-US', {year:'numeric', month:'short', day: value.length === 4 ? undefined : 'numeric'});
};
const money = (value) => {
  if (value == null || Number.isNaN(Number(value))) return '—';
  const n = Number(value);
  if (Math.abs(n) >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (Math.abs(n) >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + n.toLocaleString(undefined, {maximumFractionDigits: 0});
};
const num = (value) => value == null ? '—' : Number(value).toLocaleString(undefined, {maximumFractionDigits: 2});

async function api(url, {signal} = {}) {
  const response = await fetch(url, {headers: {'Accept': 'application/json'}, signal});
  const data = await response.json().catch(() => ({detail: 'Unexpected server response.'}));
  if (!response.ok) throw new Error(data.detail || 'Request failed.');
  return data;
}

async function cachedApi(url, ttlMs, {session = false} = {}) {
  const now = Date.now();
  const memoryHit = responseCache.get(url);
  if (memoryHit && memoryHit.expires > now) return memoryHit.data;
  if (session) {
    try {
      const stored = JSON.parse(sessionStorage.getItem('information-check:' + url));
      if (stored && stored.expires > now) {
        responseCache.set(url, stored);
        return stored.data;
      }
    } catch (_) { /* Storage is an optional performance layer. */ }
  }
  if (inFlightRequests.has(url)) return inFlightRequests.get(url);
  const request = api(url).then(data => {
    const entry = {data, expires: Date.now() + ttlMs};
    responseCache.set(url, entry);
    if (session) {
      try { sessionStorage.setItem('information-check:' + url, JSON.stringify(entry)); } catch (_) { /* Quota/privacy mode. */ }
    }
    return data;
  }).finally(() => inFlightRequests.delete(url));
  inFlightRequests.set(url, request);
  return request;
}

function setMode(nextMode, {focus = true} = {}) {
  insiderLoadId += 1;
  pageLoadId += 1;
  activePolitician = null;
  mode = nextMode;
  const content = ui[mode];
  document.querySelectorAll('.person-tab').forEach(tab => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('.compact-tabs').forEach(node => {
    node.innerHTML = `<button data-compact-mode="insider" class="${mode === 'insider' ? 'active' : ''}">Insiders</button><button data-compact-mode="politician" class="${mode === 'politician' ? 'active' : ''}">Politicians</button><button data-compact-mode="institution" class="${mode === 'institution' ? 'active' : ''}">Institutions</button>`;
  });
  $('eyebrow').textContent = content.eyebrow;
  $('heroTitle').innerHTML = content.title;
  $('heroCopy').textContent = content.copy;
  $('searchInput').placeholder = narrowScreen.matches ? content.shortPlaceholder : content.placeholder;
  $('sourceLabel').textContent = content.source;
  $('quickSearch').innerHTML = `<span>Try</span>${content.quick.map(name => `<button data-q="${esc(name)}">${esc(name)}</button>`).join('')}`;
  $('searchInput').value = '';
  closeSearch();
  hideProfiles();
  $('hero').classList.remove('hidden');
  $('errorBox').classList.add('hidden');
  bindDynamicControls();
  if (focus) $('searchInput').focus();
}

function hideProfiles() {
  $('insiderProfile').classList.add('hidden');
  $('politicianProfile').classList.add('hidden');
  $('institutionProfile').classList.add('hidden');
  $('loading').classList.add('hidden');
}

function startLoading(label) {
  closeSearch();
  $('hero').classList.add('hidden');
  hideProfiles();
  $('errorBox').classList.add('hidden');
  $('loadingText').textContent = label;
  $('loading').classList.remove('hidden');
}

function showError(error) {
  hideProfiles();
  $('hero').classList.remove('hidden');
  $('errorBox').textContent = error.message || 'Something went wrong. Please try again.';
  $('errorBox').classList.remove('hidden');
}

const searchEndpoint = {
  politician: q => '/api/politicians/search?q=' + encodeURIComponent(q),
  institution: q => '/api/institutions/search?q=' + encodeURIComponent(q),
  insider: q => '/api/search?q=' + encodeURIComponent(q)
};

const searchKey = (targetMode, q) => targetMode + ':' + q.toLowerCase();

function closeSearch() {
  clearTimeout(searchTimer);
  clearTimeout(searchPatienceTimer);
  searchToken += 1;
  if (searchAbort) searchAbort.abort();
  searchAbort = null;
  const panel = $('searchResults');
  panel.classList.remove('open', 'is-refreshing');
}

// Official records can take several seconds to resolve, so the panel always shows
// motion while a query is in flight instead of sitting blank and looking frozen.
function showSearchPending() {
  const panel = $('searchResults');
  const hasResults = panel.classList.contains('open') && panel.querySelector('.result-item');
  if (hasResults) {
    panel.classList.add('is-refreshing');
    return;
  }
  panel.innerHTML = `<div class="search-pending" aria-live="polite">
    <div class="search-pending-row"><span class="pending-avatar shimmer"></span><span class="pending-copy"><i class="shimmer"></i><i class="shimmer short"></i></span></div>
    <div class="search-pending-row"><span class="pending-avatar shimmer"></span><span class="pending-copy"><i class="shimmer"></i><i class="shimmer short"></i></span></div>
    <p class="search-pending-note" id="searchPendingNote">Searching official records…</p>
  </div>`;
  panel.classList.remove('is-refreshing');
  panel.classList.add('open');
}

// Entry point for every search trigger. Feedback is painted on the same frame as
// the keystroke; only the network call is debounced.
function requestSearch(query, {immediate = false} = {}) {
  const q = query.trim();
  clearTimeout(searchTimer);
  if (q.length < 2) {
    closeSearch();
    return;
  }
  const cached = searchCache.get(searchKey(mode, q));
  if (cached) {
    searchToken += 1;
    if (searchAbort) searchAbort.abort();
    searchAbort = null;
    renderSearch(cached);
    return;
  }
  showSearchPending();
  if (immediate) search(q);
  else searchTimer = setTimeout(() => search(q), 150);
}

async function search(query) {
  const q = query.trim();
  const requestedMode = mode;
  if (q.length < 2) {
    closeSearch();
    return;
  }
  const key = searchKey(requestedMode, q);
  const cached = searchCache.get(key);
  if (cached) {
    renderSearch(cached);
    return;
  }
  const token = ++searchToken;
  if (searchAbort) searchAbort.abort();
  const controller = new AbortController();
  searchAbort = controller;
  showSearchPending();
  clearTimeout(searchPatienceTimer);
  searchPatienceTimer = setTimeout(() => {
    const note = $('searchPendingNote');
    if (note && token === searchToken) note.textContent = 'Still searching official records — first lookups are slower.';
  }, 2200);
  try {
    const payload = await api(searchEndpoint[requestedMode](q), {signal: controller.signal});
    const results = requestedMode === 'insider' ? payload.results : payload;
    searchCache.set(key, results);
    if (token !== searchToken || mode !== requestedMode) return;
    renderSearch(results);
  } catch (error) {
    if (error.name === 'AbortError' || token !== searchToken || mode !== requestedMode) return;
    $('searchResults').innerHTML = `<div class="search-message"><strong>${esc(error.message)}</strong><span>Check your connection and try again.</span></div>`;
    $('searchResults').classList.remove('is-refreshing');
    $('searchResults').classList.add('open');
  } finally {
    clearTimeout(searchPatienceTimer);
    if (searchAbort === controller) searchAbort = null;
  }
}

function renderSearch(results) {
  if (!results.length) {
    const hint = mode === 'politician'
      ? 'Congress.gov contains current and former House and Senate members—not every U.S. political officeholder.'
      : 'Check the spelling or try a fuller name.';
    const typeLabel = mode === 'politician' ? 'politician' : mode === 'institution' ? '13F institutional manager' : 'SEC filer';
    $('searchResults').innerHTML = `<div class="search-message"><strong>No matching ${typeLabel} found</strong><span>${hint}</span></div>`;
  } else if (mode === 'politician') {
    $('searchResults').innerHTML = results.map(person => {
      const place = [person.party, person.state, person.chamber].filter(Boolean).join(' · ');
      const district = person.district !== null && person.district !== undefined && person.chamber === 'House' ? `District ${person.district}` : '';
      const image = person.imageUrl ? `<img src="${esc(person.imageUrl)}" alt="" onerror="this.remove()" />` : `<span>${esc(initials(person.name))}</span>`;
      const years = !person.currentMember && person.termStart ? `${person.termStart}–${person.termEnd || '—'}` : '';
      const executive = ['executive', 'family'].includes(person.profileType);
      const status = person.profileType === 'family' ? 'FAMILY' : executive ? 'WHITE HOUSE' : person.currentMember ? 'CURRENT' : 'FORMER';
      return `<button class="result-item politician-result" data-person-id="${esc(person.id || person.bioguideId)}"><div class="result-avatar">${image}</div><div class="result-copy"><strong>${esc(person.name)}</strong><span>${esc(executive ? person.role : place)}</span><small>${esc(executive ? 'Official WhiteHouse.gov profile' : district || years)}</small></div><em class="member-chip ${person.currentMember || executive ? 'current' : ''}">${status}</em></button>`;
    }).join('');
  } else if (mode === 'institution') {
    $('searchResults').innerHTML = results.map(manager => `<button class="result-item" data-institution-cik="${manager.cik}"><div class="result-avatar institution-result-avatar"><span>${esc(initials(manager.name))}</span></div><div class="result-copy"><strong>${esc(manager.name)}</strong><span>CIK ${manager.cik}</span><small>Latest 13F filed ${date(manager.lastFilingDate)}</small></div><em class="member-chip current">13F</em></button>`).join('');
  } else {
    $('searchResults').innerHTML = results.map(result => `<button class="result-item" data-cik="${result.cik}"><div class="result-copy"><strong>${esc(result.name)}</strong><span>CIK ${result.cik} · ${result.score}% match</span></div><span class="result-arrow">↗</span></button>`).join('');
  }
  $('searchResults').classList.remove('is-refreshing');
  $('searchResults').classList.add('open');
  $('searchResults').querySelectorAll('[data-cik]').forEach(node => node.onclick = () => loadInsider(node.dataset.cik));
  $('searchResults').querySelectorAll('[data-person-id]').forEach(node => node.onclick = () => loadPolitician(node.dataset.personId));
  $('searchResults').querySelectorAll('[data-institution-cik]').forEach(node => node.onclick = () => loadInstitution(node.dataset.institutionCik));
}

async function loadInsider(cik, {push = true} = {}) {
  setMode('insider', {focus: false});
  const loadId = ++insiderLoadId;
  const normalizedCik = String(cik);
  startLoading('Loading SEC identity…');
  const overviewUrl = '/api/profile/' + encodeURIComponent(normalizedCik) + '/overview';
  const profileUrl = '/api/profile/' + encodeURIComponent(normalizedCik) + '?max_filings=12&include_photo=false';
  let overviewShown = false;
  let overviewReady = false;
  try {
    const overview = await cachedApi(overviewUrl, 15 * 60 * 1000, {session: true});
    if (loadId !== insiderLoadId) return;
    renderInsiderOverview(overview);
    showInsiderProfile(normalizedCik, push);
    overviewShown = true;
    overviewReady = overview.status === 'ready';
    loadInsiderKnowledge(normalizedCik, loadId);
  } catch (_) {
    // The small live fallback below can still recover a useful profile.
  }
  if (overviewReady) {
    $('insiderLoadStatus').classList.add('hidden');
    return;
  }
  // This is only a cold-start fallback for installations whose quarterly DB has not
  // been ingested yet. It never runs when the local fast path is available.
  const fullResult = await cachedApi(profileUrl, 5 * 60 * 1000).then(data => ({data}), error => ({error}));
  const result = fullResult;
  if (loadId !== insiderLoadId) return;
  if (result.data) {
    renderInsider(result.data);
    showInsiderProfile(normalizedCik, push && !overviewShown);
    $('insiderLoadStatus').classList.add('hidden');
  } else if (overviewShown) {
    $('insiderLoadStatus').classList.add('error');
    $('insiderLoadText').textContent = 'Identity loaded. Ownership history is temporarily unavailable—try again shortly.';
  } else {
    showError(result.error);
  }
}

function showInsiderProfile(cik, push) {
  $('loading').classList.add('hidden');
  $('insiderProfile').classList.remove('hidden');
  if (push) history.pushState({mode: 'insider', id: cik}, '', '/insider/' + encodeURIComponent(cik));
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function renderInsiderOverview(data) {
  const person = data.person || {}, coverage = data.coverage || {};
  $('personName').textContent = person.name || 'Unknown SEC filer';
  $('cikValue').textContent = 'CIK ' + person.cik;
  $('secProfileLink').href = 'https://www.sec.gov/edgar/browse/?CIK=' + encodeURIComponent(person.cik) + '&owner=include';
  const location = person.latest_location || {};
  $('locationValue').textContent = [location.city, location.state, location.country].filter(Boolean).join(', ') || 'Location not reported';
  const role = data.summary?.roles?.[0];
  $('headlineRole').textContent = role ? `${role.role} · ${role.company}${role.ticker ? ' (' + role.ticker + ')' : ''}` : 'SEC reporting owner';
  renderAvatar('insiderAvatar', 'insiderInitials', person.name);
  if (data.summary) {
    const summary = data.summary;
    $('statFilings').textContent = summary.stats.filings_parsed.toLocaleString();
    $('statTransactions').textContent = summary.stats.transactions.toLocaleString();
    $('statCompanies').textContent = summary.stats.companies.toLocaleString();
    $('statSold').textContent = money(summary.stats.disposed_value);
    renderRoles(summary.roles || []);
    renderCompanies(summary.companies || []);
    allTransactions = summary.transactions || [];
    renderTransactions(allTransactions);
  } else {
  ['statFilings', 'statTransactions', 'statCompanies', 'statSold'].forEach(id => { $(id).textContent = '…'; });
  $('coverageText').textContent = coverage.mode === 'quarterly_bulk'
    ? `${coverage.ownership_filings_found || 0} filings · ${coverage.first_quarter || '—'} to ${coverage.last_quarter || '—'}`
    : `${coverage.ownership_filings_found || 0} ownership filings found`;
  $('rolesCount').textContent = 'Loading…';
  $('roles').innerHTML = '<div class="section-loading shimmer"></div>';
  $('companies').innerHTML = '<div class="section-loading shimmer"></div>';
  $('transactions').innerHTML = '<div class="section-loading shimmer"></div>';
  }
  $('proxyCard').classList.add('hidden');
  $('knowledgeCard').classList.add('hidden');
  activeInsiderKnowledge = null;
  $('insiderLoadStatus').classList.remove('hidden', 'error');
  $('insiderLoadText').textContent = data.status === 'ready'
    ? 'Profile ready · loading verified biography…'
    : 'Profile ready · loading a small ownership sample and verified biography…';
}

async function loadInsiderKnowledge(cik, loadId) {
  try {
    const data = await cachedApi('/api/profile/' + encodeURIComponent(cik) + '/knowledge', 24 * 60 * 60 * 1000, {session: true});
    if (loadId !== insiderLoadId || !data.knowledge) return;
    const knowledge = data.knowledge;
    activeInsiderKnowledge = knowledge;
    $('knowledgeTitle').textContent = knowledge.title || 'About';
    $('knowledgeSummary').textContent = knowledge.summary || '';
    $('knowledgeSource').href = knowledge.url;
    const signals = knowledge.evidence_signals || [];
    $('knowledgeEvidence').innerHTML = `<strong>Identity evidence:</strong> ${esc(signals.join(' · '))}`;
    $('knowledgeCard').classList.remove('hidden');
    if (knowledge.image_url) renderAvatar('insiderAvatar', 'insiderInitials', $('personName').textContent, knowledge.image_url);
  } catch (_) {
    // Public biography is optional; verified SEC data remains the source of record.
  }
}

function renderInsider(data) {
  const person = data.person, summary = data.summary, coverage = data.coverage;
  $('personName').textContent = person.name || 'Unknown SEC filer';
  $('insiderInitials').textContent = initials(person.name);
  $('cikValue').textContent = 'CIK ' + person.cik;
  $('secProfileLink').href = 'https://www.sec.gov/edgar/browse/?CIK=' + encodeURIComponent(person.cik) + '&owner=include';
  const location = person.latest_location || {};
  $('locationValue').textContent = [location.city, location.state, location.country].filter(Boolean).join(', ') || 'Location not reported';
  const role = summary.roles?.[0];
  $('headlineRole').textContent = role ? `${role.role} · ${role.company}${role.ticker ? ' (' + role.ticker + ')' : ''}` : 'SEC reporting owner';
  renderAvatar('insiderAvatar', 'insiderInitials', person.name, person.photo?.url || activeInsiderKnowledge?.image_url);
  $('statFilings').textContent = summary.stats.filings_parsed.toLocaleString();
  $('statTransactions').textContent = summary.stats.transactions.toLocaleString();
  $('statCompanies').textContent = summary.stats.companies.toLocaleString();
  $('statSold').textContent = money(summary.stats.disposed_value);
  $('coverageText').textContent = `${coverage.ownership_filings_found} ownership filings found${coverage.oldest_loaded ? ' · back to ' + coverage.oldest_loaded.slice(0, 4) : ''}`;
  renderRoles(summary.roles || []);
  renderCompanies(summary.companies || []);
  renderProxy(data.proxy_enrichment);
  allTransactions = summary.transactions || [];
  renderTransactions(allTransactions);
}

function renderAvatar(containerId, initialsId, name, imageUrl) {
  const avatar = $(containerId), fallback = $(initialsId);
  avatar.querySelectorAll('img').forEach(image => image.remove());
  fallback.textContent = initials(name);
  fallback.style.display = 'block';
  if (imageUrl) {
    const image = document.createElement('img');
    image.src = imageUrl;
    image.alt = name;
    image.onload = () => { fallback.style.display = 'none'; };
    image.onerror = () => { image.remove(); fallback.style.display = 'block'; };
    avatar.appendChild(image);
  }
}

function renderProxy(proxy) {
  $('proxyCard').classList.toggle('hidden', !proxy?.facts?.length);
  if (!proxy?.facts?.length) return;
  $('proxySource').href = proxy.source;
  $('proxyFacts').innerHTML = `<div class="proxy-source-meta">DEF 14A · ${esc(proxy.issuer || 'connected issuer')} · filed ${date(proxy.filing_date)}</div>${proxy.facts.slice(0, 5).map(fact => `<div class="proxy-fact">${esc(fact)}</div>`).join('')}`;
}

function renderRoles(roles) {
  $('rolesCount').textContent = roles.length + ' roles';
  $('roles').innerHTML = roles.slice(0, 16).map(role => `<div class="role-row"><div class="timeline-dot"></div><div><strong>${esc(role.role)}</strong><p>${esc(role.company || 'Unknown issuer')}${role.ticker ? ' · ' + esc(role.ticker) : ''}</p></div><div class="date-range">${role.first_seen?.slice(0, 4) || '—'} → ${role.last_seen?.slice(0, 4) || '—'}</div></div>`).join('') || '<p class="empty-state">No role data parsed.</p>';
}

function renderCompanies(companies) {
  $('companies').innerHTML = companies.slice(0, 12).map(company => `<div class="company-item"><div class="company-logo">${esc((company.ticker || company.name || '?').slice(0, 4))}</div><div><strong>${esc(company.name || 'Unknown issuer')}</strong><span>${esc(company.ticker || 'No ticker')} · ${company.first_seen?.slice(0, 4) || '—'}–${company.last_seen?.slice(0, 4) || '—'}</span></div><div class="company-count">${company.filings} filings</div></div>`).join('') || '<p class="empty-state">No connected issuers found.</p>';
}

function renderTransactions(transactions) {
  const available = transactions.filter(tx => tx.code !== 'HOLDING');
  const notable = [...available].filter(tx => tx.value != null).sort((a, b) => Math.abs(Number(b.value)) - Math.abs(Number(a.value))).slice(0, 5);
  const notableKeys = new Set(notable.map(tx => `${tx.accession}:${tx.security}:${tx.date}:${tx.code}`));
  const rows = notable.concat(available.filter(tx => !notableKeys.has(`${tx.accession}:${tx.security}:${tx.date}:${tx.code}`)).slice(0, 45));
  $('transactions').innerHTML = rows.map(tx => {
    const type = tx.acquired_disposed === 'A' ? 'buy' : tx.acquired_disposed === 'D' ? 'sell' : 'other';
    const arrow = type === 'buy' ? '↙' : type === 'sell' ? '↗' : '•';
    return `<div class="tx"><div class="tx-icon ${type}">${arrow}</div><div class="tx-main"><strong>${esc(tx.company || 'Unknown')} ${tx.ticker ? '· ' + esc(tx.ticker) : ''}</strong><span>${esc(tx.security || 'Security')} · ${date(tx.date || tx.filing_date)} · code ${esc(tx.code || '—')}${tx.derivative ? ' · derivative' : ''}</span></div><div class="tx-cell shares"><strong>${money(tx.value)}</strong><span>reported transaction value</span></div><div class="tx-cell value"><strong>${tx.price != null ? money(tx.price) : '—'}</strong><span>${tx.price != null ? 'price per share' : 'price not disclosed'}</span></div><a class="text-link" href="${esc(tx.sec_url || '#')}" target="_blank" rel="noreferrer">↗</a></div>`;
  }).join('') || '<p class="empty-state">No structured transactions found in the loaded filings.</p>';
}

async function loadPolitician(personId, {push = true} = {}) {
  setMode('politician', {focus: false});
  const loadId = pageLoadId;
  const isWhiteHouse = personId.startsWith('whitehouse:');
  const identifier = isWhiteHouse ? personId.slice('whitehouse:'.length) : personId;
  startLoading(isWhiteHouse ? 'Loading official White House profile…' : 'Loading verified congressional profile…');
  try {
    const endpoint = isWhiteHouse ? '/api/politicians/whitehouse/' : '/api/politicians/';
    let profile = await cachedApi(endpoint + encodeURIComponent(identifier) + (isWhiteHouse ? '' : '/overview'), 60 * 1000);
    if (loadId !== pageLoadId) return;
    activePolitician = profile.profileType === 'legislator' ? profile.bioguideId : null;
    renderPolitician(profile);
    $('loading').classList.add('hidden');
    $('politicianProfile').classList.remove('hidden');
    const path = isWhiteHouse ? `/politician/whitehouse/${encodeURIComponent(profile.slug)}` : `/politician/${encodeURIComponent(profile.bioguideId)}`;
    if (push) history.pushState({mode: 'politician', id: profile.id}, '', path);
    window.scrollTo({top: 0, behavior: 'smooth'});
    if (!isWhiteHouse && profile.status === 'overview') {
      $('congressTimeline').innerHTML = '<p class="empty-state">Identity ready · loading congressional service history…</p>';
      try {
        profile = await cachedApi(endpoint + encodeURIComponent(identifier), 300000);
        if (loadId !== pageLoadId) return;
        renderPolitician(profile);
      } catch (_) {
        if (loadId !== pageLoadId) return;
        $('congressTimeline').innerHTML = '<p class="empty-state">Detailed service history is temporarily unavailable. The identity above comes from the saved Congress.gov directory.</p>';
      }
    }
    if (profile.profileType === 'legislator') {
      loadDisclosures(profile.bioguideId);
      loadLegislation('sponsored', true);
      loadLegislation('cosponsored', true);
    }
    if (isWhiteHouse) loadFamilyBiography(identifier, loadId);
  } catch (error) { if (loadId === pageLoadId) showError(error); }
}

function renderPolitician(profile) {
  $('familyCard')?.classList.add('hidden');
  if (['executive', 'family'].includes(profile.profileType)) {
    renderExecutiveProfile(profile);
    return;
  }
  $('verificationLabel').textContent = '✓ CONGRESS.GOV VERIFIED';
  $('politicianName').textContent = profile.name;
  $('memberStatus').textContent = profile.currentMember ? 'CURRENT MEMBER' : 'FORMER MEMBER';
  $('memberStatus').classList.toggle('former', !profile.currentMember);
  const title = profile.currentChamber === 'Senate' ? 'U.S. Senator' : profile.currentChamber === 'House' ? 'U.S. Representative' : 'Member of Congress';
  $('politicianRole').textContent = title;
  $('politicianMeta').innerHTML = [profile.currentParty, profile.currentState, profile.currentDistrict !== null && profile.currentDistrict !== undefined && profile.currentChamber === 'House' ? `District ${profile.currentDistrict}` : null].filter(Boolean).map(value => `<span>${esc(value)}</span>`).join('');
  renderAvatar('politicianAvatar', 'politicianInitials', profile.name, profile.imageUrl);
  $('imageCredit').textContent = profile.imageAttribution || '';
  $('imageCredit').classList.toggle('hidden', !profile.imageAttribution);
  $('officialWebsite').classList.toggle('hidden', !profile.officialWebsiteUrl);
  if (profile.officialWebsiteUrl) {
    $('officialWebsite').href = profile.officialWebsiteUrl;
    $('officialWebsite').textContent = 'Official Website ↗';
  }
  $('whiteHouseBioCard').classList.add('hidden');
  $('congressTimelineCard').classList.remove('hidden');
  $('disclosuresCard').classList.remove('hidden');
  $('sponsoredCard').classList.remove('hidden');
  $('cosponsoredCard').classList.remove('hidden');
  const stats = [
    ['Current chamber', profile.currentChamber], ['Party', profile.currentParty], ['State', profile.currentState],
    ['District', profile.currentChamber === 'House' ? profile.currentDistrict : null], ['Years in Congress', profile.yearsInCongress],
    ['Total terms', profile.totalTerms], ['Bioguide ID', profile.bioguideId], ['Last Congress.gov update', profile.source.updatedAt ? date(profile.source.updatedAt) : null]
  ].filter(([, value]) => value !== null && value !== undefined && value !== '');
  $('politicianStats').innerHTML = stats.map(([label, value]) => `<div class="stat card"><div class="stat-label">${esc(label)}</div><div class="stat-value compact">${esc(display(value))}</div></div>`).join('');
  renderTerms(profile.terms || [], profile.currentParty);
  renderPartyHistory(profile.partyHistory || []);
  renderPersonalDetails(profile);
  $('sourceBioguide').textContent = profile.bioguideId;
  $('sourceIdentifierRow').classList.remove('hidden');
  $('politicianSourceName').textContent = 'Congress.gov';
  $('sourceUpdated').textContent = profile.source.updatedAt ? date(profile.source.updatedAt) : 'Not reported';
  $('sourceDescription').textContent = 'Identity, congressional career and legislation are provided by the official Congress.gov API.';
  $('congressSource').href = profile.source.officialUrl;
  $('sponsoredCount').textContent = profile.sponsoredLegislation.total == null ? '' : `${profile.sponsoredLegislation.total.toLocaleString()} total`;
  $('cosponsoredCount').textContent = profile.cosponsoredLegislation.total == null ? '' : `${profile.cosponsoredLegislation.total.toLocaleString()} total`;
  for (const kind of ['sponsored', 'cosponsored']) {
    legislationState[kind] = {offset: 0, items: []};
    $(`${kind}Legislation`).innerHTML = '<div class="section-loading shimmer"></div>';
    $(`loadMore${kind[0].toUpperCase() + kind.slice(1)}`).classList.add('hidden');
  }
  $('disclosureSummary').innerHTML = '<div class="section-loading shimmer"></div>';
  $('disclosureTransactions').innerHTML = '';
  $('disclosureFilings').innerHTML = '';
  $('disclosuresSource').classList.add('hidden');
}

function renderExecutiveProfile(profile) {
  const isFamily = profile.profileType === 'family';
  $('verificationLabel').textContent = isFamily ? 'PUBLIC FAMILY RELATIONSHIP' : 'OFFICIAL WHITE HOUSE PROFILE';
  $('politicianName').textContent = profile.name;
  $('memberStatus').textContent = isFamily ? 'FAMILY MEMBER' : 'CURRENT ADMINISTRATION';
  $('memberStatus').classList.remove('former');
  $('politicianRole').textContent = profile.role;
  $('politicianMeta').innerHTML = '<span>United States</span><span>' + (isFamily ? 'Presidential family' : 'Executive Branch') + '</span>';
  renderAvatar('politicianAvatar', 'politicianInitials', profile.name, profile.imageUrl);
  $('imageCredit').textContent = profile.imageAttribution || '';
  $('imageCredit').classList.toggle('hidden', !profile.imageAttribution);
  $('officialWebsite').classList.remove('hidden');
  $('officialWebsite').href = profile.officialWebsiteUrl;
  $('officialWebsite').textContent = 'View on WhiteHouse.gov ↗';
  $('politicianStats').innerHTML = [
    ['Role / relationship', profile.role], ['Category', isFamily ? 'Presidential family' : 'Executive'], ['Source', 'WhiteHouse.gov']
  ].map(([label, value]) => `<div class="stat card"><div class="stat-label">${esc(label)}</div><div class="stat-value compact">${esc(value)}</div></div>`).join('');
  $('whiteHouseBioCard').classList.remove('hidden');
  $('whiteHouseBiography').innerHTML = (profile.biography || []).map(paragraph => `<p>${esc(paragraph)}</p>`).join('') || '<p class="empty-state">No official biography was published.</p>';
  $('congressTimelineCard').classList.add('hidden');
  $('disclosuresCard').classList.add('hidden');
  $('sponsoredCard').classList.add('hidden');
  $('cosponsoredCard').classList.add('hidden');
  $('partyHistoryCard').classList.add('hidden');
  $('personalDetailsCard').classList.remove('hidden');
  $('personalDetails').innerHTML = `<div class="prov-row"><span>Office</span><strong>${esc(profile.role)}</strong></div><div class="prov-row"><span>Branch</span><strong>Executive</strong></div>`;
  $('politicianSourceName').textContent = 'WhiteHouse.gov';
  $('sourceIdentifierRow').classList.add('hidden');
  $('sourceUpdated').textContent = profile.source.updatedAt ? date(profile.source.updatedAt) : 'Not reported';
  $('sourceDescription').textContent = 'Role, biography and portrait are provided by the official website of the White House.';
  $('congressSource').href = profile.source.officialUrl;
  renderFamily(profile.family || []);
  $('personalDetails').innerHTML = `<div class="prov-row"><span>${isFamily ? 'Relationship' : 'Role'}</span><strong>${esc(profile.role)}</strong></div>`;
  $('sourceDescription').textContent = isFamily ? 'Relationship sourced from the presidential biography. This label does not imply public office.' : 'Official biography published by the White House; statements reflect the source’s account.';
}

function disclosedRange(range) {
  return range && range.min != null ? `${money(range.min)}–${money(range.max)}` : null;
}

async function loadDisclosures(memberId) {
  try {
    const data = await api(`/api/politicians/${encodeURIComponent(memberId)}/disclosures`);
    if (activePolitician !== memberId) return;
    renderDisclosures(data);
  } catch (error) {
    if (activePolitician !== memberId) return;
    $('disclosureSummary').innerHTML = `<p class="inline-error">${esc(error.message)} <button onclick="loadDisclosures('${esc(memberId)}')">Retry</button></p>`;
  }
}

function renderDisclosures(data) {
  const source = $('disclosuresSource');
  source.href = data.source?.officialUrl || '#';
  source.textContent = `${data.source?.name || 'Official source'} ↗`;
  source.classList.toggle('hidden', !data.source?.officialUrl);
  if (!data.available) {
    $('disclosureSummary').innerHTML = `<div class="disclosure-notice"><strong>Official source temporarily unavailable</strong><span>${esc(data.message || 'The official disclosure search could not be reached.')}</span></div>`;
    $('disclosureTransactions').innerHTML = '';
    $('disclosureFilings').innerHTML = '';
    return;
  }
  const stats = data.summary || {};
  const cards = [
    ['Total trades', stats.totalTrades], ['Purchases', stats.purchases], ['Sales', stats.sales],
    ['Most recent', stats.mostRecentTrade ? date(stats.mostRecentTrade) : '—'], ['Spouse trades', stats.spouseTrades]
  ];
  const purchaseRange = disclosedRange(stats.purchaseRange);
  const saleRange = disclosedRange(stats.saleRange);
  if (purchaseRange) cards.push(['Purchase range total', purchaseRange]);
  if (saleRange) cards.push(['Sale range total', saleRange]);
  $('disclosureSummary').innerHTML = cards.map(([label, value]) => `<div class="disclosure-stat"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
  $('disclosureTransactions').innerHTML = (data.transactions || []).map(tx => {
    const kind = tx.transactionType === 'Purchase' ? 'buy' : tx.transactionType === 'Sale' ? 'sell' : 'other';
    const filed = tx.notificationDate || tx.filingDate;
    return `<article class="disclosure-row"><div class="disclosure-top-kind ${kind}">${esc(tx.transactionType || 'Other')}</div><div class="disclosure-asset"><div><strong>${esc(tx.asset || 'Unknown asset')}</strong>${tx.ticker ? `<em>${esc(tx.ticker)}</em>` : ''}</div><span>Owner: ${esc(tx.owner || 'Self')}</span></div><div class="disclosure-dates"><span>Trade <strong>${date(tx.transactionDate)}</strong></span><span>Filed / notified <strong>${date(filed)}</strong></span></div><div class="disclosure-amount"><strong>${esc(tx.amount?.label || 'Not disclosed')}</strong><span>Disclosed range</span></div><a class="official-disclosure" href="${esc(tx.source?.documentUrl || '#')}" target="_blank" rel="noreferrer"><span>${esc(tx.source?.badge || 'OFFICIAL DISCLOSURE')}</span>View filing ↗</a></article>`;
  }).join('') || '<p class="empty-state">No PTR transactions were found in the official records searched. Members are not required to file a PTR when they have no reportable transactions.</p>';
  const annualCount = (data.annualReports || []).length;
  const ptrs = (data.filings || []).filter(item => item.reportType === 'PTR');
  $('disclosureFilings').innerHTML = ptrs.length || annualCount ? `<span>${ptrs.length} official PTR filing${ptrs.length === 1 ? '' : 's'} found${annualCount ? ` · ${annualCount} annual report${annualCount === 1 ? '' : 's'} indexed for future detail support` : ''}</span>` : '';
}

async function loadInstitution(cik, {push = true} = {}) {
  setMode('institution', {focus: false});
  const loadId = pageLoadId;
  startLoading('Opening institution profile…');
  try {
    const profile = await cachedApi('/api/institutions/' + encodeURIComponent(cik) + '/overview', 60000);
    if (loadId !== pageLoadId) return;
    renderInstitutionShell(profile);
    if (profile.status === 'ready') renderInstitution(profile);
    $('loading').classList.add('hidden');
    $('institutionProfile').classList.remove('hidden');
    if (push) history.pushState({mode: 'institution', id: profile.cik}, '', '/institution/' + encodeURIComponent(profile.cik));
    window.scrollTo({top: 0, behavior: 'smooth'});
    loadOrganizationBiography(cik, loadId);
    if (profile.status !== 'ready') {
      try {
        const detail = await cachedApi('/api/institutions/' + encodeURIComponent(cik), 300000);
        if (loadId !== pageLoadId) return;
        renderInstitution(detail);
        $('institutionLoadStatus').textContent = 'Portfolio loaded from official SEC filings.';
      } catch (error) {
        if (loadId !== pageLoadId) return;
        $('institutionLoadStatus').textContent = 'Institution profile ready. Portfolio source unavailable; reload to retry.';
        $('institutionHoldings').innerHTML = `<p class="empty-state">${esc(error.message)}</p>`;
      }
    }
  } catch (error) { if (loadId === pageLoadId) showError(error); }
}

function renderInstitution(profile) {
  $('institutionName').textContent = profile.name || 'Unknown institutional manager';
  $('institutionInitials').textContent = initials(profile.name);
  $('institutionCik').textContent = 'CIK ' + profile.cik;
  $('institutionQuarter').textContent = 'Reporting period ' + date(profile.latestQuarter);
  const address = profile.address || {};
  $('institutionAddress').textContent = [address.street1, address.street2, address.city, address.stateOrCountry, address.zipCode].filter(Boolean).join(' · ');
  $('institutionSecLink').href = profile.source.officialUrl;
  $('institutionValue').textContent = money(profile.stats.totalPortfolioValue);
  $('institutionHoldingCount').textContent = profile.stats.numberOfHoldings.toLocaleString();
  $('institutionNewCount').textContent = profile.stats.newPositions.toLocaleString();
  $('institutionExitedCount').textContent = profile.stats.exitedPositions.toLocaleString();
  $('institutionSourceCik').textContent = profile.cik;
  $('institutionTableSource').href = profile.source.currentInformationTable;
  institutionHoldingsState.cik = profile.cik;
  institutionHoldingsState.offset = profile.holdings.items.length;
  institutionHoldingsState.items = profile.holdings.items;
  institutionHoldingsState.total = profile.holdings.total;
  renderInstitutionHoldings();
  renderRanking('institutionTopHoldings', profile.topHoldings, profile.stats.totalPortfolioValue, 'value');
  renderRanking('institutionAdditions', profile.biggestAdditions, profile.stats.totalPortfolioValue, 'positionChangeValue');
  renderRanking('institutionReductions', profile.biggestReductions, profile.stats.totalPortfolioValue, 'positionChangeValue');
  renderInstitutionFilings('institution13fFilings', profile.thirteenFFilings);
  renderInstitutionFilings('institutionRecentFilings', profile.recentFilings);
}

function holdingName(item) {
  return item.ticker ? `${item.ticker} · ${item.issuer}` : item.issuer;
}

function renderInstitutionHoldings() {
  const state = institutionHoldingsState;
  $('holdingsCount').textContent = `${state.total.toLocaleString()} positions incl. exits`;
  $('institutionHoldings').innerHTML = state.items.map(item => {
    const change = item.changePercent == null ? 'New' : `${item.changePercent > 0 ? '+' : ''}${item.changePercent.toFixed(1)}%`;
    const statusClass = item.status.toLowerCase().replace(/\s+/g, '-');
    const valueLabel = !item.previousValue
      ? 'Current value'
      : item.status === 'UNCHANGED'
        ? `${money(item.valueChange)} market-value move`
        : `${money(item.positionChangeValue)} estimated position change`;
    return `<div class="holding-row"><div class="holding-security"><strong>${esc(holdingName(item))}</strong><span>${esc([item.titleOfClass, 'CUSIP ' + item.cusip, item.putCall].filter(Boolean).join(' · '))}</span></div><div><strong>${num(item.shares)}</strong><span>${esc(item.shareType || '')}</span></div><div><strong>${money(item.value)}</strong><span>${valueLabel}</span></div><div class="holding-change"><em class="position-status ${statusClass}">${esc(item.status)}</em><span>${esc(change)}</span></div></div>`;
  }).join('') || '<p class="empty-state">No holdings were reported in the latest information table.</p>';
  $('loadMoreHoldings').classList.toggle('hidden', state.items.length >= state.total);
}

async function loadMoreInstitutionHoldings() {
  const state = institutionHoldingsState;
  if (!state.cik) return;
  const button = $('loadMoreHoldings');
  button.disabled = true;
  button.textContent = 'Loading…';
  try {
    const page = await api(`/api/institutions/${encodeURIComponent(state.cik)}/holdings?offset=${state.offset}&limit=100`);
    state.items = state.items.concat(page.items);
    state.offset = state.items.length;
    state.total = page.total;
    renderInstitutionHoldings();
  } catch (error) {
    button.textContent = error.message;
  } finally {
    button.disabled = false;
    if (button.textContent === 'Loading…') button.textContent = 'Load more holdings';
  }
}

function renderRanking(targetId, items, portfolioValue, valueKey) {
  $(targetId).innerHTML = (items || []).map((item, index) => {
    const value = item[valueKey] || 0;
    const width = portfolioValue ? Math.min(100, Math.abs(value) / portfolioValue * 100) : 0;
    return `<div class="ranking-row"><span class="ranking-number">${index + 1}</span><div><strong>${esc(holdingName(item))}</strong><span>${money(value)}</span><div class="ranking-bar"><i style="width:${width}%"></i></div></div></div>`;
  }).join('') || '<p class="empty-state">No comparable position changes are available.</p>';
}

function renderInstitutionFilings(targetId, filings) {
  $(targetId).innerHTML = (filings || []).map(filing => `<a class="filing-row" href="${esc(filing.sourceUrl || '#')}" target="_blank" rel="noreferrer"><span class="filing-form">${esc(filing.form || 'FILING')}</span><div><strong>${filing.reportDate ? 'Period ' + date(filing.reportDate) : 'SEC filing'}</strong><small>Filed ${date(filing.filingDate)} · ${esc(filing.accessionNumber || '')}</small></div><em>↗</em></a>`).join('') || '<p class="empty-state">No filings found.</p>';
}

function renderTerms(terms, party) {
  const sorted = [...terms].sort((a, b) => Number(b.startYear || 0) - Number(a.startYear || 0));
  $('termsCount').textContent = `${terms.length} terms`;
  $('congressTimeline').innerHTML = sorted.map(term => `<div class="role-row"><div class="timeline-dot"></div><div><strong>${esc(term.role)}</strong><p>${esc([term.state, term.district !== null && term.district !== undefined && term.chamber === 'House' ? 'District ' + term.district : null, term.party || party].filter(Boolean).join(' · '))}</p><small>${term.congress ? `${term.congress}th Congress` : ''}</small></div><div class="date-range">${display(term.startYear)} → ${term.endYear || 'Present'}</div></div>`).join('') || '<p class="empty-state">No congressional terms were reported.</p>';
}

function renderPartyHistory(history) {
  $('partyHistoryCard').classList.toggle('hidden', !history.length);
  $('partyHistory').innerHTML = history.map(item => `<div class="detail-row"><div><span class="party-dot"></span><strong>${esc(item.party || 'Unknown')}</strong></div><span>${esc(display(item.startYear))} → ${esc(display(item.endYear, 'Present'))}</span></div>`).join('');
}

function renderPersonalDetails(profile) {
  const details = [['Birth year', profile.birthYear], ['Bioguide ID', profile.bioguideId], ['State', profile.currentState], ['District', profile.currentChamber === 'House' ? profile.currentDistrict : null]]
    .filter(([, value]) => value !== null && value !== undefined && value !== '');
  $('personalDetailsCard').classList.toggle('hidden', !details.length);
  $('personalDetails').innerHTML = details.map(([label, value]) => `<div class="prov-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');
}

async function loadLegislation(kind, reset = false) {
  if (!activePolitician) return;
  const memberId = activePolitician;
  const state = legislationState[kind];
  const offset = reset ? 0 : state.offset;
  const target = $(`${kind}Legislation`);
  const button = $(`loadMore${kind[0].toUpperCase() + kind.slice(1)}`);
  button.disabled = true;
  button.textContent = 'Loading…';
  try {
    const data = await api(`/api/politicians/${encodeURIComponent(memberId)}/legislation?kind=${kind}&offset=${offset}&limit=12`);
    if (activePolitician !== memberId) return;
    state.items = reset ? data.items : state.items.concat(data.items);
    state.offset = state.items.length;
    target.innerHTML = state.items.map(bill => `<article class="bill"><div class="bill-mark">${esc(bill.type || 'BILL')}</div><div class="bill-copy"><div class="bill-meta"><strong>${esc(bill.label)}</strong><span>${bill.congress ? `${bill.congress}th Congress` : ''}${bill.policyArea ? ' · ' + esc(bill.policyArea) : ''}</span></div><h4>${esc(bill.title || 'Untitled legislation')}</h4>${bill.latestAction?.text ? `<p>${esc(bill.latestAction.text)}</p>` : ''}<div class="bill-foot"><span>${bill.latestAction?.date ? date(bill.latestAction.date) : bill.introducedDate ? 'Introduced ' + date(bill.introducedDate) : ''}</span>${bill.officialUrl ? `<a href="${esc(bill.officialUrl)}" target="_blank" rel="noreferrer">Congress.gov ↗</a>` : ''}</div></div></article>`).join('') || '<p class="empty-state">No legislation was returned by Congress.gov.</p>';
    button.classList.toggle('hidden', !data.hasMore);
  } catch (error) {
    if (reset) target.innerHTML = `<div class="inline-error">${esc(error.message)} <button data-retry="${kind}">Try again</button></div>`;
  } finally {
    button.disabled = false;
    button.textContent = 'Load more';
    target.querySelectorAll('[data-retry]').forEach(node => node.onclick = () => loadLegislation(node.dataset.retry, true));
  }
}

function bindDynamicControls() {
  document.querySelectorAll('[data-q]').forEach(button => button.onclick = event => {
    // Quick-search buttons sit outside .search-shell. Without stopping this click,
    // the document-level outside-click handler immediately closes the result panel
    // and aborts the request that this button has just started.
    event.stopPropagation();
    const input = $('searchInput');
    input.focus({preventScroll: true});
    input.value = button.dataset.q;
    requestSearch(button.dataset.q, {immediate: true});
  });
  document.querySelectorAll('[data-compact-mode]').forEach(button => button.onclick = () => {
    history.pushState({}, '', '/');
    setMode(button.dataset.compactMode);
  });
}

// A pasted or autofilled name is a complete query, so it skips the typing debounce.
$('searchInput').addEventListener('input', event => {
  const pasted = event.inputType === 'insertFromPaste' || event.inputType === 'insertReplacementText';
  requestSearch($('searchInput').value, {immediate: pasted});
});
$('searchInput').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); requestSearch($('searchInput').value, {immediate: true}); } });
$('searchInput').addEventListener('focus', () => { if ($('searchInput').value.trim().length >= 2) requestSearch($('searchInput').value, {immediate: true}); });
narrowScreen.addEventListener('change', () => { $('searchInput').placeholder = narrowScreen.matches ? ui[mode].shortPlaceholder : ui[mode].placeholder; });
document.querySelectorAll('.person-tab').forEach(tab => tab.onclick = () => setMode(tab.dataset.mode));
document.querySelectorAll('[data-back]').forEach(button => button.onclick = () => { history.pushState({}, '', '/'); setMode(mode); });
document.addEventListener('click', event => {
  if (!event.target.closest('.search-shell, #quickSearch')) closeSearch();
});
document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => {
  document.querySelectorAll('[data-filter]').forEach(node => node.classList.remove('active'));
  button.classList.add('active');
  renderTransactions(button.dataset.filter === 'all' ? allTransactions : allTransactions.filter(tx => tx.acquired_disposed === button.dataset.filter));
});
$('loadMoreSponsored').onclick = () => loadLegislation('sponsored');
$('loadMoreCosponsored').onclick = () => loadLegislation('cosponsored');
$('loadMoreHoldings').onclick = loadMoreInstitutionHoldings;
window.addEventListener('popstate', routeFromLocation);

function routeFromLocation() {
  const whitehouse = location.pathname.match(/^\/politician\/whitehouse\/([a-z0-9-]+)\/?$/);
  const politician = location.pathname.match(/^\/politician\/([A-Za-z]\d{6})\/?$/);
  const institution = location.pathname.match(/^\/institution\/(\d+)\/?$/);
  const insider = location.pathname.match(/^\/insider\/(\d+)\/?$/);
  if (whitehouse) loadPolitician('whitehouse:' + whitehouse[1], {push: false});
  else if (politician) loadPolitician(politician[1], {push: false});
  else if (institution) loadInstitution(institution[1], {push: false});
  else if (insider) loadInsider(insider[1], {push: false});
  else setMode(mode, {focus: false});
}

setMode('insider', {focus: false});
routeFromLocation();
