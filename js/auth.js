// js/auth.js — shared auth guard
// Usage: call requireAuth(['board','selector']) at the top of any protected page

const PAGE_ROLES = {
  'dashboard.html':  ['board', 'selector', 'national_coach', 'regional_coach', 'umpire', 'ngb_paid_staff'],
  'admin.html':      ['board'],
  'selectors.html':  ['board', 'selector'],
  'players.html':    ['board', 'national_coach', 'regional_coach', 'ngb_paid_staff'],
  'matches.html':    ['board'],
  'umpires.html':    ['umpire'],
};

/**
 * Call this at the top of every protected page.
 * Pass the roles allowed for this page, e.g.:
 *   requireAuth(['board', 'selector'])
 *
 * Returns the full profile object if authorised:
 *   { id, email, role, region, full_name, ... }
 */
async function requireAuth(allowedRoles) {
  // 1. Check for an active session
  const { data: { session }, error: sessionError } = await supabase.auth.getSession();

  if (sessionError || !session) {
    redirectToLogin();
    return null;
  }

  // 2. Fetch the user's profile (includes role + region)
  const { data: profile, error: profileError } = await supabase
    .from('profiles')
    .select('*')
    .eq('id', session.user.id)
    .single();

  if (profileError || !profile) {
    console.error('Could not load profile:', profileError?.message);
    redirectToLogin();
    return null;
  }

  // 3. Check the role is allowed on this page
  if (!allowedRoles.includes(profile.role)) {
    window.location.href = 'unauthorized.html';
    return null;
  }

  // 4. Inject nav and return profile for the page to use
  injectNav(profile);
  return profile;
}

/**
 * For pages that are public but want to show different UI to logged-in users.
 * Returns profile or null — never redirects.
 */
async function getOptionalSession() {
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) return null;

  const { data: profile } = await supabase
    .from('profiles')
    .select('*')
    .eq('id', session.user.id)
    .single();

  return profile || null;
}

/**
 * Sign out and go to login page.
 */
async function signOut() {
  await supabase.auth.signOut();
  redirectToLogin();
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function redirectToLogin() {
  // Store the page they were trying to reach so we can redirect back after login
  const current = window.location.pathname + window.location.search;
  sessionStorage.setItem('redirectAfterLogin', current);
  window.location.href = 'index.html';
}

/**
 * Dynamically injects a nav bar into #nav-container (if it exists on the page).
 * Shows links based on the user's role.
 */
function injectNav(profile) {
  const container = document.getElementById('nav-container');
  if (!container) return;

  const links = [
    { href: 'dashboard.html', label: 'Dashboard', roles: ['board', 'selector', 'national_coach', 'regional_coach', 'umpire', 'ngb_paid_staff'] },
    { href: 'selectors.html', label: 'Selection',  roles: ['board', 'selector'] },
    { href: 'players.html',   label: 'Players',    roles: ['board', 'national_coach', 'regional_coach', 'ngb_paid_staff'] },
    { href: 'matches.html',   label: 'Matches',    roles: ['board'] },
    { href: 'admin.html',     label: 'Admin',      roles: ['board'] },
    { href: 'umpires.html',   label: 'My Profile', roles: ['umpire'] },
  ];

  const visibleLinks = links
    .filter(l => l.roles.includes(profile.role))
    .map(l => `<a href="${l.href}">${l.label}</a>`)
    .join('');

  const roleLabel = formatRole(profile.role);

  container.innerHTML = `
    <nav class="main-nav">
      <div class="nav-links">${visibleLinks}</div>
      <div class="nav-user">
        <span class="nav-role-badge">${roleLabel}</span>
        <span class="nav-name">${profile.full_name || profile.email}</span>
        <button onclick="signOut()" class="btn-signout">Sign out</button>
      </div>
    </nav>
  `;
}

function formatRole(role) {
  const labels = {
    board:           'Board',
    selector:        'Selector',
    national_coach:  'National coach',
    regional_coach:  'Regional coach',
    umpire:          'Umpire',
    ngb_paid_staff:  'NGB Paid Staff',
  };
  return labels[role] || role;
}