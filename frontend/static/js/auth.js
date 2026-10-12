/* ================================================================
   TOKENPOOL — AUTH  (frontend/static/js/auth.js)
   Role model: USER (consumer + provider features) | ADMIN
   ================================================================ */

function getToken()   { return localStorage.getItem("tp_token"); }
function getUser()    { try { return JSON.parse(localStorage.getItem("tp_user") || "null"); } catch { return null; } }
function isLoggedIn() { return !!getToken(); }
function getRole()    { return getUser()?.role || null; }
function isAdmin()    { return getRole() === "ADMIN"; }

function setSession(token, user) {
  localStorage.setItem("tp_token", token);
  localStorage.setItem("tp_user", JSON.stringify(user));
}

function clearSession() {
  localStorage.removeItem("tp_token");
  localStorage.removeItem("tp_user");
}

function requireAuth() {
  if (!isLoggedIn()) { window.location.href = "/login"; return false; }
  return true;
}

function requireAdmin() {
  if (!isLoggedIn() || !isAdmin()) {
    window.location.href = isLoggedIn() ? "/dashboard" : "/login";
    return false;
  }
  return true;
}

/* After login: ADMINs go to /admin, all other users go to /dashboard */
function redirectIfLoggedIn() {
  if (!isLoggedIn()) return;
  if (isAdmin()) window.location.href = "/admin";
  else window.location.href = "/dashboard";
}

async function doLogin(email, password) {
  const data = await AuthAPI.login(email, password);
  setSession(data.access_token, data.user);
  return data.user;
}

async function doGoogleLogin(email, full_name, google_sub_id) {
  const data = await AuthAPI.googleLogin({ email, full_name, google_sub_id });
  setSession(data.access_token, data.user);
  return data.user;
}

function doLogout() {
  clearSession();
  window.location.href = "/login";
}

function applyUser() {
  const user = getUser();
  if (!user) return;
  document.querySelectorAll("[data-user-name]").forEach(e => e.textContent = user.full_name);
  document.querySelectorAll("[data-user-email]").forEach(e => e.textContent = user.email);
  document.querySelectorAll("[data-user-initials]").forEach(e => {
    e.textContent = user.avatar_initials ||
      (user.full_name || "TP").split(" ").map(s => s[0]).join("").slice(0, 2).toUpperCase();
  });
}
