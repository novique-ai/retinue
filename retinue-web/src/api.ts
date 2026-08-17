export type RoomWorkspace = "sandbox" | "ide";

export interface RoomMeta {
  id: string;
  name: string;
  members: string[];
  lead: string | null;
  max_agent_turns: number;
  archived?: boolean;
  workspace?: RoomWorkspace;
  ide_path?: string | null;
  /** Which project this room is filed under. null = Unfiled. */
  project_id?: string | null;
}

/** Projects group rooms (separate from team separators, which group agents). */
export interface ProjectMeta {
  id: string;
  name: string;
  archived: boolean;
}

export interface RoomMsg {
  seq: number;
  ts: number;
  kind: "user" | "agent" | "system";
  speaker: string;
  text: string;
}

export type AuthStatus = "ok" | "relogin_required" | "missing" | "not_required";

export interface ProviderAuth {
  id: string;
  status: AuthStatus;
  error?: string | null;
}

export interface HealthInfo {
  ok: boolean;
  rooms: number;
  /** Short Retinue commit SHA from the gateway, or `"unknown"`. */
  git_sha?: string;
  auth?: { providers: ProviderAuth[] };
}

export interface ReauthSession {
  session_id: string;
  provider: string;
  status: "pending" | "approved" | "error" | "expired";
  flow?: string;
  user_code?: string;
  verification_url?: string;
  verification_uri?: string;
  expires_in?: number;
  poll_interval?: number;
  error?: string;
  evicted?: number;
}

/**
 * Backend-computed per-agent visual identity (CONTRACT-identity.md). Render
 * from these fields directly — the derivation hash lives on the backend
 * only, so the frontend must never recompute `color` or a colour-source
 * itself, only display what the server already resolved.
 */
export interface AgentIdentity {
  /** Override emoji, or null — fall back to `initial` when null. */
  emoji: string | null;
  /** First character of display_name, uppercased. Always non-empty. */
  initial: string;
  /** A palette key (see `api.getPalette`), never a CSS colour. */
  color: string;
  color_source: "override" | "derived";
}

export type PersonaWarmth = "terse" | "balanced" | "warm";
export type PersonaVerbosity = "brief" | "balanced" | "thorough";
export type PersonaFormality = "casual" | "balanced" | "formal";

/** The three persona dials. Always present on an agent; default "balanced". */
export interface Persona {
  warmth: PersonaWarmth;
  verbosity: PersonaVerbosity;
  formality: PersonaFormality;
}

export interface AgentMeta {
  display_name: string;
  slug: string;
  job: string;
  how: string;
  has_soul?: boolean;
  activation?: string;
  online?: boolean;
  model_preset?: string | null;
  model_summary?: string;
  local_llm?: boolean;
  turn_timeout?: number;
  archived?: boolean;
  team?: string | null;
  busy?: boolean;
  auth_status?: AuthStatus;
  auth_provider?: string | null;
  auth_error?: string | null;
  identity: AgentIdentity;
  /** The override voice id, or the one derived from the slug. */
  voice_resolved: string;
  /**
   * The stored override, or null when there is none (voice is derived).
   * Backend lowercases on write — compare case-insensitively against
   * `GET /voice` ids.
   */
  voice: string | null;
  /** Stored override — same value as `identity.emoji`, included at the top
   * level too so a form can populate from it directly. */
  avatar_emoji: string | null;
  /** Stored override, or null when `identity.color_source === "derived"`. */
  avatar_color: string | null;
  persona: Persona;
}

export interface SidebarTeam {
  kind: "team";
  id: string;
  label: string;
}

export interface SidebarAgentItem {
  kind: "agent";
  slug: string;
}

export type SidebarItem = SidebarTeam | SidebarAgentItem;

export interface SidebarLayout {
  rooms: string[];
  items: SidebarItem[];
}

export interface RoomPatch {
  name?: string;
  members?: string[];
  lead?: string | null;
  archived?: boolean;
  max_agent_turns?: number;
  workspace?: RoomWorkspace;
  ide_path?: string | null;
  /** Move the room to a project, or null to unfile it. */
  project_id?: string | null;
}

export interface AgentPatch {
  name?: string;
  job?: string;
  how?: string;
  model?: string;
  archived?: boolean;
  /** null clears the override and falls back to the initial. */
  avatar_emoji?: string | null;
  /** A palette key, or null to fall back to the derived colour. */
  avatar_color?: string | null;
  /** A voice id, or null to fall back to the derived voice. */
  voice?: string | null;
  persona?: Persona;
}

export interface Principal {
  display_name: string;
  about: string;
}

export interface ModelPreset {
  name: string;
  summary: string;
  provider?: string;
  model?: string;
  local?: boolean;
}

export type ItineraryStatus = "todo" | "doing" | "done";

export interface ItineraryItem {
  id: string;
  text: string;
  status: ItineraryStatus;
}

export interface Itinerary {
  room_id: string;
  title: string;
  summary: string;
  items: ItineraryItem[];
  updated_at?: number;
  updated_by?: string;
}

export interface RoutineMeta {
  name: string;
  slug: string;
  source_room?: string;
  messages: string[];
  created_at?: number;
}

export interface IdeFolder {
  name: string;
  path: string;
}

export interface IdeFolderListing {
  path: string;
  parent: string | null;
  root: string | null;
  folders: IdeFolder[];
}

export interface WorkspaceStatus {
  enabled: boolean;
  key: string | null;
  runtime: string | null;
  running: boolean;
  attach: string | null;
  detail: string | null;
  ide_root?: string | null;
  container?: { id: string; name: string; status: string; image: string } | null;
}

export interface VoiceStatus {
  backend: string;
  ready: boolean;
  detail: string;
  voices: Record<string, string>;
}

const KEY_STORAGE = "retinue.apiKey";

export class AuthRequiredError extends Error {}

export function getApiKey(): string {
  return localStorage.getItem(KEY_STORAGE) ?? "";
}

export function setApiKey(key: string) {
  localStorage.setItem(KEY_STORAGE, key);
}

export function workspaceFileUrl(roomId: string, path: string): string {
  const q = new URLSearchParams({ path });
  const key = getApiKey();
  if (key) q.set("access_token", key);
  return `/rooms/${encodeURIComponent(roomId)}/files?${q.toString()}`;
}

async function req<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const key = getApiKey();
  if (key) headers["Authorization"] = `Bearer ${key}`;
  const resp = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (resp.status === 401) throw new AuthRequiredError("API key required");
  const data = await resp.json();
  if (!resp.ok) throw new Error(data?.error ?? `HTTP ${resp.status}`);
  return data as T;
}

async function longPollLoop(
  id: string,
  since: number,
  onMessages: (messages: RoomMsg[]) => void,
  signal?: AbortSignal,
): Promise<void> {
  let cursor = since;
  while (!signal?.aborted) {
    try {
      const { messages } = await req<{ messages: RoomMsg[] }>(
        "GET",
        `/rooms/${id}/transcript?since=${cursor}&wait=25`,
        undefined,
        signal,
      );
      if (messages.length) {
        cursor = Math.max(cursor, ...messages.map((m) => m.seq));
        onMessages(messages);
      }
    } catch (e) {
      if (signal?.aborted || (e instanceof DOMException && e.name === "AbortError")) return;
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
}

function watchTranscript(
  id: string,
  since: number,
  onMessages: (messages: RoomMsg[]) => void,
  signal?: AbortSignal,
): void {
  const params = new URLSearchParams({ since: String(since) });
  const key = getApiKey();
  if (key) params.set("access_token", key);
  let cursor = since;
  let sawEvent = false;
  const es = new EventSource(`/rooms/${id}/stream?${params.toString()}`);
  const apply = (ev: MessageEvent<string>) => {
    try {
      const payload = JSON.parse(ev.data) as { messages?: RoomMsg[] };
      const messages = payload.messages ?? [];
      if (!messages.length) return;
      sawEvent = true;
      cursor = Math.max(cursor, ...messages.map((m) => m.seq));
      onMessages(messages);
    } catch {
      /* ignore a malformed frame */
    }
  };
  es.addEventListener("messages", apply);
  es.onmessage = apply;
  es.onerror = () => {
    es.close();
    if (signal?.aborted) return;
    if (!sawEvent) {
      void longPollLoop(id, cursor, onMessages, signal);
    } else {
      // Dropped mid-stream — reconnect via SSE from the last seq.
      watchTranscript(id, cursor, onMessages, signal);
    }
  };
  signal?.addEventListener("abort", () => es.close(), { once: true });
}

export const api = {
  health: () => req<HealthInfo>("GET", "/health"),
  getPrincipal: () => req<Principal>("GET", "/principal"),
  savePrincipal: (body: { display_name: string; about?: string }) =>
    req<Principal>("PUT", "/principal", body),
  authStatus: () =>
    req<{
      providers: ProviderAuth[];
      accounts?: Array<ProviderAuth & { login?: string }>;
      session: ReauthSession | null;
    }>("GET", "/auth"),
  startReauth: (provider = "xai-oauth") =>
    req<ReauthSession>("POST", "/auth/reauth", { provider }),
  saveApiKey: (provider: string, api_key: string) =>
    req<ProviderAuth>("POST", "/auth/apikey", { provider, api_key }),
  reauthSession: (sessionId: string) =>
    req<ReauthSession>("GET", `/auth/reauth?session=${encodeURIComponent(sessionId)}`),
  listRooms: () => req<{ rooms: RoomMeta[] }>("GET", "/rooms"),
  createRoom: (
    name: string,
    members: string[],
    lead: string | null,
    extra?: {
      workspace?: RoomWorkspace;
      ide_path?: string | null;
      project_id?: string | null;
    },
  ) => req<RoomMeta>("POST", "/rooms", { name, members, lead, ...extra }),
  patchRoom: (id: string, body: RoomPatch) => req<RoomMeta>("PATCH", `/rooms/${id}`, body),
  deleteRoom: (id: string) => req<{ deleted: string }>("DELETE", `/rooms/${id}`),
  /**
   * Incremental invite — seeds `last_seen` to the last 20 messages rather
   * than the full transcript. Never use `patchRoom({ members })` for this;
   * that clobbers the member list instead of adding one.
   */
  inviteMember: (id: string, member: string) =>
    req<RoomMeta>("POST", `/rooms/${id}/members`, { member }),
  /** `last_seen` survives removal, so re-inviting resumes where they left off. */
  removeMember: (id: string, slug: string) =>
    req<RoomMeta>("DELETE", `/rooms/${id}/members/${encodeURIComponent(slug)}`),
  transcript: (id: string, since: number, wait: number, signal?: AbortSignal) =>
    req<{ messages: RoomMsg[] }>(
      "GET",
      `/rooms/${id}/transcript?since=${since}&wait=${wait}`,
      undefined,
      signal,
    ),
  /**
   * Prefer SSE (`GET /rooms/{id}/stream`). Falls back to the long-poll
   * transcript route if EventSource errors before the first event (old
   * gateway, proxy, etc.). Long-poll remains the CLI path.
   */
  watchTranscript: (
    id: string,
    since: number,
    onMessages: (messages: RoomMsg[]) => void,
    signal?: AbortSignal,
  ) => watchTranscript(id, since, onMessages, signal),
  send: (id: string, text: string, from: string) =>
    req<{ seq: number; planned: string[] }>("POST", `/rooms/${id}/messages`, { text, from }),
  listAgents: () => req<{ agents: AgentMeta[] }>("GET", "/agents"),
  listModels: () => req<{ models: ModelPreset[] }>("GET", "/models"),
  /**
   * The twelve palette keys, in the backend's stable order (do not
   * hardcode a second copy — see CONTRACT-identity.md).
   */
  getPalette: () => req<{ colors: string[] }>("GET", "/identity/palette"),
  hire: (
    name: string,
    job: string,
    how: string,
    model?: string,
    identity?: {
      avatar_emoji?: string;
      avatar_color?: string;
      voice?: string;
      persona?: Persona;
    },
  ) =>
    req<AgentMeta>("POST", "/agents", {
      name,
      job,
      how,
      model: model || undefined,
      ...identity,
    }),
  switchModel: (slug: string, model: string) =>
    req<AgentMeta>("PATCH", `/agents/${slug}`, { model }),
  patchAgent: (slug: string, body: AgentPatch) =>
    req<AgentMeta>("PATCH", `/agents/${slug}`, body),
  deleteAgent: (slug: string) =>
    req<{ deleted: string }>("DELETE", `/agents/${slug}`),
  getSidebar: () => req<SidebarLayout>("GET", "/sidebar"),
  putSidebar: (layout: SidebarLayout) => req<SidebarLayout>("PUT", "/sidebar", layout),
  listProjects: () => req<{ projects: ProjectMeta[] }>("GET", "/projects"),
  createProject: (name: string) => req<ProjectMeta>("POST", "/projects", { name }),
  patchProject: (id: string, body: { name?: string; archived?: boolean }) =>
    req<ProjectMeta>("PATCH", `/projects/${encodeURIComponent(id)}`, body),
  deleteProject: (id: string) =>
    req<{ deleted: string; unfiled_rooms: string[] }>(
      "DELETE",
      `/projects/${encodeURIComponent(id)}`,
    ),
  getItinerary: (roomId: string) =>
    req<Itinerary>("GET", `/rooms/${roomId}/itinerary`),
  putItinerary: (roomId: string, body: Partial<Itinerary> & { updated_by?: string }) =>
    req<Itinerary>("PUT", `/rooms/${roomId}/itinerary`, body),
  listRoutines: () => req<{ routines: RoutineMeta[] }>("GET", "/routines"),
  listRoomRoutines: (roomId: string) =>
    req<{ routines: RoutineMeta[] }>("GET", `/rooms/${roomId}/routines`),
  saveRoutine: (name: string, room: string) =>
    req<RoutineMeta>("POST", "/routines", { name, room }),
  runRoutine: (slug: string, room: string) =>
    req<{ slug: string; room: string; steps: unknown[] }>(
      "POST",
      `/routines/${slug}/run`,
      { room },
    ),
  deleteRoutine: (slug: string) => req<{ deleted: string }>("DELETE", `/routines/${slug}`),
  workspace: () => req<WorkspaceStatus>("GET", "/workspace"),
  listIdeFolders: (path?: string) =>
    req<IdeFolderListing>(
      "GET",
      `/workspace/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  voiceStatus: () => req<VoiceStatus>("GET", "/voice"),
  uploadAttachment: async (id: string, file: File) => {
    const headers: Record<string, string> = {
      "Content-Type": file.type || "application/octet-stream",
    };
    const key = getApiKey();
    if (key) headers.Authorization = `Bearer ${key}`;
    const params = new URLSearchParams({ filename: file.name || "file" });
    const resp = await fetch(`/rooms/${encodeURIComponent(id)}/attachments?${params}`, {
      method: "POST",
      headers,
      body: file,
    });
    if (resp.status === 401) throw new AuthRequiredError("API key required");
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data as { name: string; path: string; bytes: number; image: boolean };
  },
  sendAudio: async (id: string, blob: Blob, from: string, filename = "speech.wav") => {
    const headers: Record<string, string> = {
      "Content-Type": blob.type || "audio/wav",
    };
    const key = getApiKey();
    if (key) headers.Authorization = `Bearer ${key}`;
    const params = new URLSearchParams({ from, filename });
    const resp = await fetch(`/rooms/${id}/audio?${params.toString()}`, {
      method: "POST",
      headers,
      body: blob,
    });
    if (resp.status === 401) throw new AuthRequiredError("API key required");
    const data = await resp.json();
    if (!resp.ok) throw new Error(data?.error ?? `HTTP ${resp.status}`);
    return data as { seq: number; planned: string[]; text: string };
  },
  speak: async (text: string, speaker: string) => {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const key = getApiKey();
    if (key) headers.Authorization = `Bearer ${key}`;
    const resp = await fetch("/tts", {
      method: "POST",
      headers,
      body: JSON.stringify({ text, speaker }),
    });
    if (resp.status === 401) throw new AuthRequiredError("API key required");
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`;
      try {
        const data = await resp.json();
        detail = data?.error ?? detail;
      } catch {
        /* not json */
      }
      throw new Error(detail);
    }
    return resp.blob();
  },
};
