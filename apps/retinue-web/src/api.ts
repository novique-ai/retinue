export interface RoomMeta {
  id: string;
  name: string;
  members: string[];
  lead: string | null;
  max_agent_turns: number;
}

export interface RoomMsg {
  seq: number;
  ts: number;
  kind: "user" | "agent" | "system";
  speaker: string;
  text: string;
}

export interface AgentMeta {
  display_name: string;
  slug: string;
  job: string;
  how: string;
  has_soul?: boolean;
  activation?: string;
}

const KEY_STORAGE = "retinue.apiKey";

export class AuthRequiredError extends Error {}

export function getApiKey(): string {
  return localStorage.getItem(KEY_STORAGE) ?? "";
}

export function setApiKey(key: string) {
  localStorage.setItem(KEY_STORAGE, key);
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

export const api = {
  listRooms: () => req<{ rooms: RoomMeta[] }>("GET", "/rooms"),
  createRoom: (name: string, members: string[], lead: string | null) =>
    req<RoomMeta>("POST", "/rooms", { name, members, lead }),
  transcript: (id: string, since: number, wait: number, signal?: AbortSignal) =>
    req<{ messages: RoomMsg[] }>(
      "GET",
      `/rooms/${id}/transcript?since=${since}&wait=${wait}`,
      undefined,
      signal,
    ),
  send: (id: string, text: string, from: string) =>
    req<{ seq: number; planned: string[] }>("POST", `/rooms/${id}/messages`, { text, from }),
  listAgents: () => req<{ agents: AgentMeta[] }>("GET", "/agents"),
  hire: (name: string, job: string, how: string) =>
    req<AgentMeta>("POST", "/agents", { name, job, how }),
};
