/* ================================================================
   TOKENPOOL — API LAYER  (frontend/static/js/api.js)
   All fetch() calls go through here.
   Role model: USER / ADMIN  (no CONSUMER/PROVIDER split)
   ================================================================ */

const API_BASE = "/api/v1";

async function apiRequest(path, opts = {}) {
  const token = localStorage.getItem("tp_token");
  const headers = { ...(opts.headers || {}) };
  if (!(opts.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  if (token) headers["Authorization"] = "Bearer " + token;

  let res;
  try {
    res = await fetch(API_BASE + path, { ...opts, headers });
  } catch (e) {
    throw { code: "NETWORK_ERROR", message: "Cannot reach server. Is backend running?" };
  }

  if (res.status === 401) {
    localStorage.removeItem("tp_token");
    localStorage.removeItem("tp_user");
    if (!window.location.pathname.includes("/login")) window.location.href = "/login";
    return;
  }

  if (res.status === 403) {
    throw { code: "FORBIDDEN", message: "Access denied. You do not have permission to perform this action." };
  }

  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw { code: data?.error?.code || "API_ERROR", message: data?.detail || data?.error?.message || "Request failed" };
  return data;
}

/* GENERIC API CLIENT — supports Api.get(), Api.post(), etc. */
const Api = {
  get:    (path, opts = {}) => apiRequest(path, { ...opts, method: "GET" }),
  post:   (path, body, opts = {}) => apiRequest(path, { ...opts, method: "POST", body: body instanceof FormData ? body : JSON.stringify(body) }),
  put:    (path, body, opts = {}) => apiRequest(path, { ...opts, method: "PUT", body: body instanceof FormData ? body : JSON.stringify(body) }),
  patch:  (path, body, opts = {}) => apiRequest(path, { ...opts, method: "PATCH", body: body ? (body instanceof FormData ? body : JSON.stringify(body)) : undefined }),
  delete: (path, opts = {}) => apiRequest(path, { ...opts, method: "DELETE" }),
};

window.Api = Api;
window.api = Api;
window.apiRequest = apiRequest;

/* AUTH */
const AuthAPI = {
  login:       (email, pw)  => apiRequest("/auth/login",       { method:"POST", body: JSON.stringify({email, password:pw}) }),
  googleLogin: (payload)    => apiRequest("/auth/google",      { method:"POST", body: JSON.stringify(payload) }),
  register:    (payload)    => apiRequest("/auth/register",    { method:"POST", body: JSON.stringify(payload) }),
  me:          ()           => apiRequest("/auth/me"),
};
window.AuthAPI = AuthAPI;

/* CONSUMER — available to all authenticated users */
const ConsumerAPI = {
  overview:     ()               => apiRequest("/consumer/overview"),
  marketplace:  (params={})      => apiRequest("/consumer/marketplace?" + new URLSearchParams(params)),
  modelDetail:  (slug)           => apiRequest("/consumer/models/" + slug),
  wallet:       ()               => apiRequest("/consumer/wallet"),
  topup:        (amt, method)    => apiRequest("/consumer/wallet/topup", { method:"POST", body: JSON.stringify({amount:amt, payment_method:method}) }),
  transactions: (page=1,q="")   => apiRequest(`/consumer/transactions?page=${page}&search=${encodeURIComponent(q)}`),
  usage:        (range="7d")     => apiRequest(`/consumer/usage?range=${range}`),
  modelCredits: ()               => apiRequest("/consumer/model-credits"),
};

/* CHAT */
const ChatAPI = {
  conversations:      ()        => apiRequest("/chat/conversations"),
  createConversation: (payload) => apiRequest("/chat/conversations", { method:"POST", body: JSON.stringify(payload || {}) }),
  getConversation:    (id)      => apiRequest("/chat/conversations/" + id),
  messages:           (id)      => apiRequest("/chat/conversations/" + id + "/messages"),
  deleteConversation: (id)      => apiRequest("/chat/conversations/" + id, { method:"DELETE" }),
  send:               (payload) => apiRequest("/chat/messages", { method:"POST", body: JSON.stringify(payload) }),
};

/* PROVIDER — available to all authenticated users */
const ProviderAPI = {
  overview:              ()                      => apiRequest("/provider/overview"),
  aiAccounts:            ()                      => apiRequest("/provider/ai-accounts"),
  connectAiAccount:      (payload)               => apiRequest("/provider/ai-accounts/connect", { method:"POST", body: JSON.stringify(payload) }),
  declareAiCapacity:     (id, declared_capacity) => apiRequest(`/provider/ai-accounts/${id}/declare-capacity`, { method:"POST", body: JSON.stringify({ declared_capacity }) }),
  disconnectAiAccount:   (id)                    => apiRequest(`/provider/ai-accounts/${id}/disconnect`, { method:"POST" }),
  healthCheckAiAccount:  (id)                    => apiRequest(`/provider/ai-accounts/${id}/health-check`, { method:"POST" }),
  connections:           ()                      => apiRequest("/provider/connections"),
  connect:               (payload)               => apiRequest("/provider/connections", { method:"POST", body: JSON.stringify(payload) }),
  declareCapacity:       (id, declared_capacity) => apiRequest(`/provider/connections/${id}/declare-capacity`, { method:"POST", body: JSON.stringify({ declared_capacity }) }),
  revoke:                (id)                    => apiRequest(`/provider/connections/${id}/revoke`, { method:"POST" }),
  disconnect:            (id)                    => apiRequest(`/provider/connections/${id}/revoke`, { method:"POST" }),
  setDelegatedCommitment: (id, payload) => apiRequest(`/provider/ai-accounts/${id}/commitment`, { method:"POST", body: JSON.stringify(payload) }),
  listings:        ()                      => apiRequest("/provider/listings"),
  allListings:     (params={})             => apiRequest("/listings?" + new URLSearchParams(params)),
  createListing:   (payload)               => apiRequest("/provider/listings", { method:"POST", body: JSON.stringify(payload) }),
  editListing:     (id, payload)           => apiRequest(`/provider/listings/${id}`, { method:"PUT", body: JSON.stringify(payload) }),
  deleteListing:   (id)                    => apiRequest(`/provider/listings/${id}`, { method:"DELETE" }),
  toggleListing:   (id, status)            => apiRequest(`/provider/listings/${id}/status?new_status=${status}`, { method:"PATCH" }),
  earnings:        ()                      => apiRequest("/provider/earnings"),
  payouts:         ()                      => apiRequest("/provider/payouts"),
  requestPayout:   (amt, dest)             => apiRequest("/provider/payouts", { method:"POST", body: JSON.stringify({amount:amt, destination:dest}) }),
};
window.ProviderAPI = ProviderAPI;

/* ADMIN — protected by backend RBAC (403 returned for non-admins) */
const AdminAPI = {
  overview:        ()                          => apiRequest("/admin/overview"),
  users:           (params={})                 => apiRequest("/admin/users?" + new URLSearchParams(params)),
  toggleUser:      (id, status)                => apiRequest(`/admin/users/${id}/status?new_status=${status}`, { method:"PATCH" }),
  listings:        ()                          => apiRequest("/admin/listings"),
  providers:       ()                          => apiRequest("/admin/providers"),
  transactions:    (params={})                 => apiRequest("/admin/transactions?" + new URLSearchParams(params)),
  settings:        ()                          => apiRequest("/admin/settings"),
  saveSettings:    (payload)                   => apiRequest("/admin/settings", { method:"PUT", body: JSON.stringify(payload) }),
  getOpenAIDelegatedStatus: ()                 => apiRequest("/admin/integrations/openai-delegated"),
  updateOpenAIDelegatedStatus: (payload)       => apiRequest("/admin/integrations/openai-delegated/status", { method:"POST", body: JSON.stringify(payload) }),
  // AI model catalog management
  aiModels:        ()                          => apiRequest("/ai-models?active_only=false"),
  createAIModel:   (payload)                   => apiRequest("/admin/ai-models", { method:"POST", body: JSON.stringify(payload) }),
  updateAIModel:   (id, payload)               => apiRequest(`/admin/ai-models/${id}`, { method:"PUT", body: JSON.stringify(payload) }),
  toggleAIModel:   (id, is_active)             => apiRequest(`/admin/ai-models/${id}/toggle?is_active=${is_active}`, { method:"PATCH" }),
};

/* AI MODEL CATALOG — all authenticated users */
const AIModelsAPI = {
  list: (active_only=true) => apiRequest(`/ai-models?active_only=${active_only}`),
};

/* NOTIFICATIONS */
const NotifAPI = {
  list:    ()    => apiRequest("/notifications"),
  read:    (id)  => apiRequest("/notifications/" + id + "/read", { method:"PATCH" }),
  readAll: ()    => apiRequest("/notifications/read-all", { method:"PATCH" }),
};

/* SUPPORT */
const SupportAPI = {
  tickets: ()        => apiRequest("/support/tickets"),
  create:  (payload) => apiRequest("/support/tickets", { method:"POST", body: JSON.stringify(payload) }),
};

/* ORDERS & MARKETPLACE PURCHASES (BUYER) */
const OrdersAPI = {
  create:      (listing_id, quantity)       => apiRequest("/orders", { method:"POST", body: JSON.stringify({ listing_id, quantity }) }),
  list:        (status="")                  => apiRequest("/orders" + (status ? `?status=${status}` : "")),
  get:         (order_id)                   => apiRequest(`/orders/${order_id}`),
  submitProof: (order_id, formData)         => apiRequest(`/orders/${order_id}/payment-proof`, { method:"POST", body: formData }),
  dispute:     (order_id, reason, details)  => apiRequest(`/orders/${order_id}/dispute`, { method:"POST", body: JSON.stringify({ reason, details }) }),
  proofUrl:    (order_id)                   => `${API_BASE}/orders/${order_id}/payment-proof`,
};

/* PROVIDER ORDERS (SELLER) */
const ProviderOrdersAPI = {
  list:    (status="")         => apiRequest("/provider/orders" + (status ? `?status=${status}` : "")),
  get:     (order_id)          => apiRequest(`/provider/orders/${order_id}`),
  confirm: (order_id)          => apiRequest(`/provider/orders/${order_id}/confirm-payment`, { method:"POST" }),
  reject:  (order_id, reason)  => apiRequest(`/provider/orders/${order_id}/reject-payment`, { method:"POST", body: JSON.stringify({ reason }) }),
};

/* ADMIN ORDERS & DISPUTES */
const AdminOrdersAPI = {
  orders:            (status="")                 => apiRequest("/admin/orders" + (status ? `?status=${status}` : "")),
  disputes:          (status="")                 => apiRequest("/admin/disputes" + (status ? `?status=${status}` : "")),
  getDispute:        (dispute_id)                => apiRequest(`/admin/disputes/${dispute_id}`),
  approvePayment:    (order_id)                  => apiRequest(`/admin/orders/${order_id}/approve-payment`, { method:"POST" }),
  rejectPayment:     (order_id, reason)          => apiRequest(`/admin/orders/${order_id}/reject-payment`, { method:"POST", body: JSON.stringify({ reason }) }),
  requestEvidence:   (dispute_id, message)       => apiRequest(`/admin/disputes/${dispute_id}/request-evidence`, { method:"POST", body: JSON.stringify({ message }) }),
  setProviderStatus: (provider_id, status, r="") => apiRequest(`/admin/providers/${provider_id}/status`, { method:"PATCH", body: JSON.stringify({ status, reason: r }) }),
};

const Health = () => fetch("/api/health").then(r => r.json());
