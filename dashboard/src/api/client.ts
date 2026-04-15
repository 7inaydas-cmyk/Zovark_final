export const API_BASE_URL = '/api/v1';

// Access token stored in memory only (never localStorage) — XSS-safe
let jwtToken: string | null = null;
let currentUser: any = null;

// Restore token and user from sessionStorage (survives page navigation, cleared on tab close)
const savedToken = sessionStorage.getItem('zovark_token');
if (savedToken) {
    jwtToken = savedToken;
}
const savedUser = sessionStorage.getItem('zovark_user');
if (savedUser) {
    try { currentUser = JSON.parse(savedUser); } catch { /* ignore */ }
}

export const setToken = (token: string, user: any) => {
    jwtToken = token;
    currentUser = user;
    // Store in sessionStorage so token survives page navigation (cleared on tab close)
    sessionStorage.setItem('zovark_token', token);
    sessionStorage.setItem('zovark_user', JSON.stringify(user));
};

export const clearToken = () => {
    jwtToken = null;
    currentUser = null;
    sessionStorage.removeItem('zovark_token');
    sessionStorage.removeItem('zovark_user');
    // Also clear any legacy localStorage tokens
    localStorage.removeItem('zovark_token');
    localStorage.removeItem('zovark_user');
};

export const getUser = () => currentUser;

/** Latest `X-Zovark-Trace-ID` from an API response (Ticket 10). */
let lastApiTraceId: string | null = null;

export function captureApiTraceId(response: Response): void {
    const t = response.headers.get('X-Zovark-Trace-ID');
    if (t) {
        lastApiTraceId = t.trim();
    }
}

export function getLastApiTraceId(): string | null {
    return lastApiTraceId;
}

/** For EventSource (SSE) which cannot send Authorization headers. */
export const getAccessToken = () => jwtToken;

// Flag to prevent concurrent refresh attempts
let isRefreshing = false;
let refreshPromise: Promise<boolean> | null = null;

// Attempt to refresh the access token using the httpOnly refresh cookie
const refreshAccessToken = async (): Promise<boolean> => {
    if (isRefreshing && refreshPromise) {
        return refreshPromise;
    }
    isRefreshing = true;
    refreshPromise = (async () => {
        try {
            const resp = await fetch(`${API_BASE_URL}/auth/refresh`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
            });
            captureApiTraceId(resp);
            if (!resp.ok) return false;
            const data = await resp.json();
            if (data.token) {
                setToken(data.token, data.user || currentUser);
                return true;
            }
            return false;
        } catch {
            return false;
        } finally {
            isRefreshing = false;
            refreshPromise = null;
        }
    })();
    return refreshPromise;
};

const getHeaders = () => {
    const headers: Record<string, string> = {
        'Content-Type': 'application/json',
    };
    if (jwtToken) {
        headers['Authorization'] = `Bearer ${jwtToken}`;
    }
    return headers;
};

// Audit 4.5: default per-request timeout. `AbortSignal.timeout` is native in
// modern Chromium/Firefox/Safari; fall back to manual AbortController on old
// targets (Vite target is ES2020 which includes AbortController).
const DEFAULT_TIMEOUT_MS = 15_000;

function withTimeoutSignal(options: RequestInit, ms: number = DEFAULT_TIMEOUT_MS): RequestInit {
    // If caller already passed a signal, respect it and do NOT override —
    // effect hooks thread their own AbortController through for cancellation.
    if (options.signal) {
        return options;
    }
    try {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const sig = (AbortSignal as any).timeout?.(ms);
        if (sig) {
            return { ...options, signal: sig };
        }
    } catch {
        /* fall through to manual controller */
    }
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return { ...options, signal: controller.signal };
}

// Wrapper that automatically retries on 401 by refreshing the token
const fetchWithRefresh = async (url: string, options: RequestInit = {}): Promise<Response> => {
    // Ensure cookies are sent for refresh token
    options.credentials = 'include';

    // Audit 4.5: enforce a default request timeout so a hung API doesn't
    // leave the UI spinner forever.
    options = withTimeoutSignal(options);

    let response = await fetch(url, options);
    captureApiTraceId(response);
    if (response.status === 401 && !isRefreshing) {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
            // Retry with new token
            const newHeaders = { ...options.headers } as Record<string, string>;
            if (jwtToken) {
                newHeaders['Authorization'] = `Bearer ${jwtToken}`;
            }
            // Reset the timeout signal for the retry so we don't inherit an
            // already-aborted controller from the original attempt.
            const retryOpts: RequestInit = {
                ...options,
                headers: newHeaders,
                signal: undefined,
            };
            response = await fetch(url, withTimeoutSignal(retryOpts));
            captureApiTraceId(response);
        }
    }
    return response;
};

export interface Task {
    id: string;
    status: 'pending' | 'executing' | 'completed' | 'failed' | 'awaiting_approval' | 'rejected';
    task_type: string;
    created_at: string;
    execution_ms?: number;
    severity?: 'critical' | 'high' | 'medium' | 'low' | 'informational';
    prompt?: string | null;
    verdict?: string | null;
    risk_score?: number | null;
    path_taken?: string | null;
}

export interface TaskDetail extends Task {
    input: {
        prompt: string;
    };
    output?: {
        code?: string;
        stdout?: string;
        step_count?: number;
        model_used?: string;
    };
    tokens_used_input?: number;
    tokens_used_output?: number;
    step_count?: number;
    current_step?: number;
    approval_status?: string;
    pending_approval_id?: string;
    approval_risk_level?: string;
    approval_reason?: string;
}

export interface ApprovalRequest {
    id: string;
    task_id: string;
    step_number: number;
    requested_at: string;
    status: string;
    risk_level: string;
    action_summary: string;
    generated_code: string;
    task_type: string;
    prompt: string;
    severity?: string;
}

export interface InvestigationStep {
    id: string;
    step_number: number;
    step_type: string;
    prompt: string;
    generated_code?: string;
    output?: string;
    status: string;
    tokens_used_input: number;
    tokens_used_output: number;
    execution_ms?: number;
    created_at: string;
    completed_at?: string;
    execution_mode?: string;
    parameters_used?: any;
}

export interface AuditEntry {
    action: string;
    timestamp: string;
    details: any;
}

export interface Playbook {
    id: string;
    tenant_id?: string;
    name: string;
    description?: string;
    icon: string;
    task_type: string;
    is_template: boolean;
    system_prompt_override?: string;
    steps: string[];
    created_at: string;
}

export interface Stats {
    total_tasks: number;
    completed: number;
    failed: number;
    pending: number;
    executing: number;
    total_tokens_input: number;
    total_tokens_output: number;
    type_distribution: Record<string, number>;
    siem_alerts_total: number;
    siem_alerts_new: number;
    siem_alerts_investigating: number;
    recent_activity: Array<{ id: string; status: string; task_type: string; created_at: string; prompt?: string }>;
}

export interface TaskListResponse {
    tasks: Task[];
    total: number;
    page: number;
    limit: number;
    pages: number;
    /** Opaque cursor for the next page when sorting by created_at (keyset pagination). */
    next_cursor?: string | null;
    /** API ignored legacy ?page= when keyset mode is active. */
    deprecated_page_ignored?: boolean;
}

export const fetchTasks = async (params?: Record<string, string>): Promise<TaskListResponse> => {
    let url = `${API_BASE_URL}/tasks`;
    if (params) {
        const qs = new URLSearchParams();
        for (const [k, v] of Object.entries(params)) {
            if (v) qs.set(k, v);
        }
        const str = qs.toString();
        if (str) url += `?${str}`;
    }
    const response = await fetchWithRefresh(url, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch tasks');
    return response.json();
};

export const fetchTaskDetail = async (id: string): Promise<TaskDetail> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks/${id}`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        throw new Error('Failed to fetch task details');
    }
    const data = await response.json();
    // Re-map the API's task_id to id for the frontend
    if (data.task_id && !data.id) {
        data.id = data.task_id;
    }
    return data;
};

export const fetchTaskSteps = async (id: string): Promise<InvestigationStep[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks/${id}/steps`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        throw new Error('Failed to fetch task steps');
    }
    const data = await response.json();
    return data.steps || [];
};

export interface TimelineEvent {
    id: string;
    timestamp: string;
    type: string;
    icon: string;
    description: string;
    duration_ms?: number;
}

export const fetchTaskTimeline = async (id: string): Promise<TimelineEvent[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks/${id}/timeline`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) return [];
    try {
        const data = await response.json();
        return data.timeline || [];
    } catch {
        return [];
    }
};

export const fetchPendingApprovals = async (): Promise<{ approvals: ApprovalRequest[], count: number }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/approvals/pending`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch approvals');
    return response.json();
};

export const decideApproval = async (approvalId: string, approved: boolean, comment: string): Promise<any> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/approvals/${approvalId}/decide`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ approved, comment })
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to submit approval decision');
    return response.json();
};

export const createTask = async (prompt: string, taskType: string = 'Log Analysis', playbookId?: string): Promise<{ task_id: string }> => {
    const payload: any = {
        task_type: taskType,
        input: { prompt }
    };
    if (playbookId) {
        payload.input.playbook_id = playbookId;
    }
    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(payload),
    });

    if (!response.ok) {
        throw new Error('Failed to create task');
    }
    return response.json();
};

export const uploadTask = async (
    file: File,
    taskType: string = 'Log Analysis',
    prompt: string = 'Analyze this log file for security anomalies and threats'
): Promise<{ task_id: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    formData.append('task_type', taskType);
    formData.append('prompt', prompt);

    const headers: Record<string, string> = {};
    if (jwtToken) {
        headers['Authorization'] = `Bearer ${jwtToken}`;
    }

    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks/upload`, {
        method: 'POST',
        headers,
        body: formData,
    });

    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || 'Failed to upload file');
    }
    return response.json();
};

export const fetchTaskAudit = async (id: string): Promise<AuditEntry[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tasks/${id}/audit`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        throw new Error('Failed to fetch task audit log');
    }
    const data = await response.json();
    return data.audit_trail || [];
};

export const fetchStats = async (): Promise<Stats> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/stats`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        throw new Error('Failed to fetch stats');
    }
    return response.json();
};

export const login = async (email: string, password: string): Promise<any> => {
    const response = await fetch(`${API_BASE_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email, password }),
    });
    captureApiTraceId(response);

    if (!response.ok) {
        throw new Error('Invalid credentials');
    }

    const data = await response.json();
    setToken(data.token, data.user);
    return data.user;
};

export const logout = async (): Promise<void> => {
    try {
        const r = await fetch(`${API_BASE_URL}/auth/logout`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
        });
        captureApiTraceId(r);
    } catch { /* ignore logout errors */ }
    clearToken();
};

export const register = async (email: string, password: string, display_name: string, tenant_id: string): Promise<any> => {
    const response = await fetch(`${API_BASE_URL}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, display_name, tenant_id }),
    });
    captureApiTraceId(response);

    if (!response.ok) {
        throw new Error('Failed to register');
    }

    return response.json();
};

export const fetchPlaybooks = async (): Promise<Playbook[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/playbooks`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch playbooks');
    return response.json();
};

export const createPlaybook = async (data: any): Promise<Playbook> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/playbooks`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to create playbook');
    return response.json();
};

export const updatePlaybook = async (id: string, data: any): Promise<any> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/playbooks/${id}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to update playbook');
    return response.json();
};

export const deletePlaybook = async (id: string): Promise<any> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/playbooks/${id}`, {
        method: 'DELETE',
        headers: getHeaders()
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to delete playbook');
    return response.json();
};

export interface Skill {
    id: string;
    skill_name: string;
    skill_slug: string;
    threat_types: string[];
    mitre_tactics: string[];
    mitre_techniques: string[];
    severity_default: string;
    investigation_methodology: string;
    detection_patterns: string;
    example_prompt: string;
    times_used: number;
    version: number;
    is_community: boolean;
    has_template?: boolean;
}

export const fetchSkills = async (): Promise<{ skills: Skill[], count: number }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/skills`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch skills');
    return response.json();
};

export interface LogSource {
    id: string;
    name: string;
    source_type: string;
    connection_config: Record<string, any>;
    is_active: boolean;
    last_event_at?: string;
    event_count: number;
    created_at: string;
    webhook_url: string;
}

export interface SIEMAlert {
    id: string;
    log_source_id: string;
    task_id?: string;
    alert_name: string;
    severity?: string;
    source_ip?: string;
    dest_ip?: string;
    rule_name?: string;
    status: string;
    auto_investigate: boolean;
    created_at: string;
}

export const fetchLogSources = async (): Promise<{ sources: LogSource[], count: number }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/log-sources`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch log sources');
    return response.json();
};

export const createLogSource = async (data: any): Promise<any> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/log-sources`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to create log source');
    return response.json();
};

export const fetchSIEMAlerts = async (status?: string, sourceId?: string): Promise<{ alerts: SIEMAlert[], count: number }> => {
    let url = `${API_BASE_URL}/siem-alerts`;
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (sourceId) params.set('source_id', sourceId);
    const qs = params.toString();
    if (qs) url += `?${qs}`;

    const response = await fetchWithRefresh(url, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch SIEM alerts');
    return response.json();
};

export const investigateAlert = async (alertId: string): Promise<any> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/siem-alerts/${alertId}/investigate`, {
        method: 'POST',
        headers: getHeaders()
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to investigate alert');
    return response.json();
};

export interface Notification {
    id: string;
    type: 'task_completed' | 'approval_requested' | 'siem_alert';
    message: string;
    task_id?: string;
    timestamp: string;
}

// --- Tenant Management ---
export interface Tenant {
    id: string;
    name: string;
    slug: string;
    tier: string;
    status: string;
    created_at: string;
    user_count?: number;
}

export interface TenantUser {
    id: string;
    email: string;
    display_name: string;
    role: string;
    created_at: string;
}

export const fetchTenants = async (): Promise<Tenant[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tenants`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch tenants');
    const data = await response.json();
    return data.tenants || data || [];
};

export const createTenant = async (data: { name: string; slug: string; tier: string }): Promise<Tenant> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tenants`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to create tenant');
    return response.json();
};

export const updateTenant = async (id: string, data: { name?: string; tier?: string; status?: string }): Promise<Tenant> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tenants/${id}`, {
        method: 'PUT',
        headers: getHeaders(),
        body: JSON.stringify(data)
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to update tenant');
    return response.json();
};

export const fetchTenantUsers = async (tenantId: string): Promise<TenantUser[]> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/tenants/${tenantId}/users`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to fetch tenant users');
    const data = await response.json();
    return data.users || data || [];
};

// --- Cost Tracking ---
export interface CostData {
    total_cost: number;
    cost_by_model: Record<string, number>;
    cost_by_tenant: Record<string, number>;
    daily_costs: Array<{ date: string; cost: number }>;
    weekly_costs: Array<{ week: string; cost: number }>;
    monthly_costs: Array<{ month: string; cost: number }>;
    total_tokens_input: number;
    total_tokens_output: number;
    total_requests: number;
}

export const fetchCosts = async (period?: string): Promise<CostData> => {
    let url = `${API_BASE_URL}/costs`;
    if (period) url += `?period=${period}`;
    const response = await fetchWithRefresh(url, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        // Fallback to stats endpoint if /costs doesn't exist
        return {
            total_cost: 0,
            cost_by_model: {},
            cost_by_tenant: {},
            daily_costs: [],
            weekly_costs: [],
            monthly_costs: [],
            total_tokens_input: 0,
            total_tokens_output: 0,
            total_requests: 0,
        };
    }
    return response.json();
};

// --- Entity Graph ---
export interface Entity {
    id: string;
    entity_type: string;
    value: string;
    first_seen: string;
    last_seen: string;
    investigation_count: number;
    risk_score?: number;
    metadata?: Record<string, unknown>;
}

export interface EntityEdge {
    id: string;
    source_id: string;
    target_id: string;
    relationship: string;
    confidence: number;
    first_seen: string;
}

export interface EntityGraphData {
    entities: Entity[];
    edges: EntityEdge[];
}

export const fetchEntities = async (entityType?: string, limit = 500): Promise<EntityGraphData> => {
    let url = `${API_BASE_URL}/entities?limit=${limit}`;
    if (entityType) url += `&type=${encodeURIComponent(entityType)}`;
    const response = await fetchWithRefresh(url, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        // Return placeholder data if endpoint doesn't exist yet
        return generatePlaceholderEntities();
    }
    return response.json();
};

export const fetchEntityNeighborhood = async (entityId: string): Promise<EntityGraphData> => {
    const response = await fetchWithRefresh(
        `${API_BASE_URL}/entities/${encodeURIComponent(entityId)}/neighborhood`,
        { headers: getHeaders(), cache: 'no-store' }
    );
    if (response.status === 401 || response.status === 403) throw new Error('Unauthorized');
    if (!response.ok) throw new Error('Failed to load neighborhood');
    return response.json();
};

export interface McpKeyRow {
    id: string;
    name: string;
    created_at: string;
    active: boolean;
    last_used_at?: string;
    revoked_at?: string;
}

export const listMcpKeys = async (): Promise<McpKeyRow[]> => {
    const r = await fetchWithRefresh(`${API_BASE_URL}/mcp-keys`, { headers: getHeaders() });
    if (!r.ok) throw new Error('Failed to list MCP keys');
    const d = await r.json();
    return d.items || [];
};

export const createMcpKey = async (name: string): Promise<{ id: string; name: string; key: string; created_at: string }> => {
    const r = await fetchWithRefresh(`${API_BASE_URL}/mcp-keys`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error('Failed to create MCP key');
    return r.json();
};

export const revokeMcpKey = async (id: string): Promise<void> => {
    const r = await fetchWithRefresh(`${API_BASE_URL}/mcp-keys/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        headers: getHeaders(),
    });
    if (!r.ok) throw new Error('Failed to revoke MCP key');
};

function generatePlaceholderEntities(): EntityGraphData {
    const entities: Entity[] = [
        { id: 'e1', entity_type: 'ip', value: '192.168.1.100', first_seen: '2026-03-10T08:00:00Z', last_seen: '2026-03-12T10:00:00Z', investigation_count: 5, risk_score: 72 },
        { id: 'e2', entity_type: 'ip', value: '10.0.0.55', first_seen: '2026-03-11T14:00:00Z', last_seen: '2026-03-12T09:00:00Z', investigation_count: 3, risk_score: 45 },
        { id: 'e3', entity_type: 'domain', value: 'evil-c2.example.com', first_seen: '2026-03-10T12:00:00Z', last_seen: '2026-03-12T11:00:00Z', investigation_count: 8, risk_score: 95 },
        { id: 'e4', entity_type: 'user', value: 'admin@corp.local', first_seen: '2026-03-09T08:00:00Z', last_seen: '2026-03-12T08:00:00Z', investigation_count: 12, risk_score: 60 },
        { id: 'e5', entity_type: 'hash', value: 'a1b2c3d4e5f6...', first_seen: '2026-03-11T16:00:00Z', last_seen: '2026-03-11T16:00:00Z', investigation_count: 2, risk_score: 88 },
        { id: 'e6', entity_type: 'ip', value: '203.0.113.42', first_seen: '2026-03-10T09:00:00Z', last_seen: '2026-03-12T07:00:00Z', investigation_count: 4, risk_score: 82 },
        { id: 'e7', entity_type: 'domain', value: 'phishing-site.xyz', first_seen: '2026-03-11T10:00:00Z', last_seen: '2026-03-12T06:00:00Z', investigation_count: 6, risk_score: 90 },
        { id: 'e8', entity_type: 'user', value: 'svc-backup@corp.local', first_seen: '2026-03-10T06:00:00Z', last_seen: '2026-03-12T05:00:00Z', investigation_count: 3, risk_score: 35 },
        { id: 'e9', entity_type: 'ip', value: '172.16.0.10', first_seen: '2026-03-11T08:00:00Z', last_seen: '2026-03-12T04:00:00Z', investigation_count: 2, risk_score: 20 },
        { id: 'e10', entity_type: 'hash', value: 'f7e8d9c0b1a2...', first_seen: '2026-03-12T02:00:00Z', last_seen: '2026-03-12T02:00:00Z', investigation_count: 1, risk_score: 75 },
    ];
    const edges: EntityEdge[] = [
        { id: 'ed1', source_id: 'e1', target_id: 'e3', relationship: 'connected_to', confidence: 0.92, first_seen: '2026-03-10T12:00:00Z' },
        { id: 'ed2', source_id: 'e4', target_id: 'e1', relationship: 'logged_into', confidence: 0.85, first_seen: '2026-03-10T08:00:00Z' },
        { id: 'ed3', source_id: 'e5', target_id: 'e3', relationship: 'downloaded_from', confidence: 0.95, first_seen: '2026-03-11T16:00:00Z' },
        { id: 'ed4', source_id: 'e6', target_id: 'e7', relationship: 'resolved_to', confidence: 0.88, first_seen: '2026-03-10T09:00:00Z' },
        { id: 'ed5', source_id: 'e4', target_id: 'e2', relationship: 'logged_into', confidence: 0.78, first_seen: '2026-03-11T14:00:00Z' },
        { id: 'ed6', source_id: 'e2', target_id: 'e3', relationship: 'connected_to', confidence: 0.70, first_seen: '2026-03-11T15:00:00Z' },
        { id: 'ed7', source_id: 'e8', target_id: 'e9', relationship: 'logged_into', confidence: 0.65, first_seen: '2026-03-11T08:00:00Z' },
        { id: 'ed8', source_id: 'e10', target_id: 'e7', relationship: 'downloaded_from', confidence: 0.91, first_seen: '2026-03-12T02:00:00Z' },
        { id: 'ed9', source_id: 'e1', target_id: 'e6', relationship: 'scanned_by', confidence: 0.60, first_seen: '2026-03-10T10:00:00Z' },
    ];
    return { entities, edges };
}

// --- Bulk SIEM Alert Investigation ---
export const bulkInvestigateAlerts = async (alertIds: string[]): Promise<any> => {
    const results = await Promise.allSettled(
        alertIds.map(id => investigateAlert(id))
    );
    return results;
};

export const fetchNotifications = async (since?: string): Promise<Notification[]> => {
    let url = `${API_BASE_URL}/notifications`;
    if (since) {
        url += `?since=${encodeURIComponent(since)}`;
    }
    const response = await fetchWithRefresh(url, { headers: getHeaders(), cache: 'no-store' });
    if (!response.ok) return [];
    try {
        const data = await response.json();
        return data.notifications || [];
    } catch (e) {
        return [];
    }
};

// --- Template Promotion API ---

export interface PromotionItem {
    task_id: string;
    task_type: string;
    path_taken: string;
    risk_score: number;
    verdict: string;
    findings: any[];
    iocs: any[];
    mitre_attack: string[];
    siem_event: any;
    created_at: string;
    execution_ms: number;
    generated_code?: string;
}

export interface AutoTemplate {
    slug: string;
    name: string;
    task_types: string[];
    auto_promoted: boolean;
    source_task_id: string;
    promoted_at: string;
    promoted_by: string;
    status: string;
}

export interface DashboardStats {
    total_investigations: number;
    avg_response_ms: number;
    detection_rate: number;
    false_positive_rate: number;
    auto_templates_count: number;
    awaiting_review: number;
    path_distribution: Record<string, number>;
    analyst_hours_saved: number;
}

export const fetchPromotionQueue = async (): Promise<{ items: PromotionItem[], total: number, awaiting_review: number }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/promotion-queue`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        // Return empty if endpoint not yet implemented
        return { items: [], total: 0, awaiting_review: 0 };
    }
    return response.json();
};

export const submitAnalystFeedback = async (feedback: {
    task_id: string;
    analyst_verdict: string;
    analyst_risk_score?: number;
    analyst_notes?: string;
    promote?: boolean;
}): Promise<{ status: string; promoted_slug?: string; approvals?: number; required?: number }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/analyst-feedback`, {
        method: 'POST',
        headers: getHeaders(),
        body: JSON.stringify(feedback),
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (response.status === 409) throw new Error('You have already approved this promotion');
    // 202 = pending second approval (quorum not yet met)
    if (response.status === 202 || response.ok) return response.json();
    throw new Error('Failed to submit analyst feedback');
};

export const fetchAutoTemplates = async (): Promise<{ items: AutoTemplate[] }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/auto-templates`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        // Return empty if endpoint not yet implemented
        return { items: [] };
    }
    return response.json();
};

export const disableAutoTemplate = async (slug: string): Promise<{ status: string }> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/auto-templates/${slug}`, {
        method: 'DELETE',
        headers: getHeaders(),
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) throw new Error('Failed to disable auto template');
    return response.json();
};

export const fetchDashboardStats = async (): Promise<DashboardStats> => {
    const response = await fetchWithRefresh(`${API_BASE_URL}/dashboard-stats`, {
        headers: getHeaders(),
        cache: 'no-store'
    });
    if (response.status === 401 || response.status === 403) throw new Error("Unauthorized");
    if (!response.ok) {
        // Return defaults if endpoint not yet implemented
        return {
            total_investigations: 0,
            avg_response_ms: 0,
            detection_rate: 0,
            false_positive_rate: 0,
            auto_templates_count: 0,
            awaiting_review: 0,
            path_distribution: {},
            analyst_hours_saved: 0,
        };
    }
    return response.json();
};
