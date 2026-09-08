// Presentational extensions using the existing page structure.
const themeControl = document.createElement('label');
themeControl.className = 'theme-control';
themeControl.innerHTML = 'Appearance <select id="themeSelect" aria-label="Color theme"><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option></select>';
document.querySelector('.topbar').append(themeControl);
const themeSelect = document.getElementById('themeSelect');
try { themeSelect.value = localStorage.getItem('information-check-theme') || 'system'; } catch (_) {}
function applyTheme() {
  const choice = themeSelect.value;
  document.documentElement.dataset.theme = choice === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : choice;
  try { localStorage.setItem('information-check-theme', choice); } catch (_) {}
}
themeSelect.onchange = applyTheme;
matchMedia('(prefers-color-scheme: light)').addEventListener('change', applyTheme);
applyTheme();
const skip = document.createElement('a');
skip.href = '#hero'; skip.className = 'skip-link'; skip.textContent = 'Skip to content';
document.body.prepend(skip);

const familyCard = document.createElement('section');
familyCard.className = 'card section-card hidden'; familyCard.id = 'familyCard';
familyCard.innerHTML = '<div class="section-title"><div><span class="kicker">People & relationships</span><h3>Family</h3></div></div><div id="familyList"></div><p class="family-note">Relationships from the cited public biographies. Family membership does not imply government office.</p>';
document.querySelector('#politicianProfile .right-col').prepend(familyCard);

const organizationCard = document.createElement('section');
organizationCard.className = 'card section-card'; organizationCard.id = 'organizationCard';
organizationCard.innerHTML = '<div class="section-title"><div><span class="kicker">Institution overview</span><h3>About the organization</h3></div></div><p id="organizationSummary" class="knowledge-summary"></p><div id="organizationSource" class="knowledge-evidence"></div>';
document.querySelector('#institutionProfile .left-col').prepend(organizationCard);
const institutionStatus = document.createElement('p');
institutionStatus.id = 'institutionLoadStatus'; institutionStatus.className = 'profile-progress';
document.querySelector('#institutionProfile .profile-head').after(institutionStatus);

function renderFamily(family) {
  document.getElementById('familyCard').classList.toggle('hidden', !family.length);
  document.getElementById('familyList').innerHTML = family.map(person => `<div class="family-row"><div>${person.id ? `<button class="family-link" data-family-id="${esc(person.id)}">${esc(person.name)} ↗</button>` : `<strong>${esc(person.name)}</strong>`}<span>${esc(person.relationship)}</span></div><a class="text-link" href="${esc(person.sourceUrl)}" target="_blank" rel="noreferrer">Source ↗</a></div>`).join('');
  document.querySelectorAll('[data-family-id]').forEach(button => button.onclick = () => loadPolitician(button.dataset.familyId));
}

async function loadFamilyBiography(slug, loadId) {
  if (!['donald-trump-jr', 'ivanka-trump', 'eric-trump', 'tiffany-trump', 'barron-trump'].includes(slug)) return;
  try {
    const data = await cachedApi(`/api/politicians/whitehouse/${encodeURIComponent(slug)}/knowledge`, 3600000);
    if (pageLoadId !== loadId || !data.knowledge) return;
    const knowledge = data.knowledge;
    document.getElementById('whiteHouseBiography').innerHTML += `<p>${esc(knowledge.summary)}</p><p class="knowledge-evidence"><a href="${esc(knowledge.url)}" target="_blank" rel="noreferrer">Wikipedia contributors ↗</a> · CC BY-SA 4.0</p>`;
    if (knowledge.image_url) renderAvatar('politicianAvatar', 'politicianInitials', knowledge.title, knowledge.image_url);
  } catch (_) { /* Official relationship remains visible. */ }
}

function renderInstitutionShell(profile) {
  document.getElementById('institutionName').textContent = profile.name;
  document.getElementById('institutionInitials').textContent = initials(profile.name);
  document.getElementById('institutionCik').textContent = 'CIK ' + profile.cik;
  document.getElementById('institutionQuarter').textContent = profile.latestQuarter ? 'Reporting period ' + date(profile.latestQuarter) : 'SEC 13F reporting manager';
  document.getElementById('institutionAddress').textContent = '';
  document.getElementById('institutionSecLink').href = profile.source.officialUrl;
  document.getElementById('institutionSourceCik').textContent = profile.cik;
  document.getElementById('institutionTableSource').removeAttribute('href');
  document.getElementById('organizationSummary').textContent = `${profile.name} is an institutional investment manager identified in SEC Form 13F filings. These reports disclose qualifying securities holdings at quarter end; they do not represent all assets managed by the organization.`;
  document.getElementById('organizationSource').innerHTML = `<a href="${esc(profile.source.officialUrl)}" target="_blank" rel="noreferrer">SEC reporting record ↗</a>`;
  document.getElementById('institutionLoadStatus').textContent = profile.status === 'ready' ? `Portfolio snapshot · reporting period ${date(profile.latestQuarter)}${profile.fetchedAt ? ' · retrieved ' + date(new Date(profile.fetchedAt * 1000).toISOString()) : ''}` : 'Organization ready · retrieving the official portfolio in the background…';
  for (const id of ['institutionValue', 'institutionHoldingCount', 'institutionNewCount', 'institutionExitedCount']) document.getElementById(id).textContent = '—';
  for (const id of ['institutionHoldings', 'institutionTopHoldings', 'institutionAdditions', 'institutionReductions', 'institution13fFilings', 'institutionRecentFilings']) document.getElementById(id).innerHTML = '<p class="empty-state">Portfolio details are loading…</p>';
  document.getElementById('holdingsCount').textContent = '';
  document.getElementById('loadMoreHoldings').classList.add('hidden');
}

async function loadOrganizationBiography(cik, loadId) {
  try {
    const data = await cachedApi(`/api/institutions/${encodeURIComponent(cik)}/knowledge`, 3600000);
    if (pageLoadId !== loadId || !data.knowledge?.summary) return;
    document.getElementById('organizationSummary').textContent = data.knowledge.summary;
    document.getElementById('organizationSource').innerHTML = `<a href="${esc(data.knowledge.url)}" target="_blank" rel="noreferrer">Wikipedia ↗</a> · ${esc(data.knowledge.attribution)} · <a href="${esc(data.knowledge.officialWebsite)}" target="_blank" rel="noreferrer">Organization website ↗</a>`;
  } catch (_) { /* Source-backed SEC description remains available. */ }
}
