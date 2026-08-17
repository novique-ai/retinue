import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";
import {
  AgentIdentity,
  AgentMeta,
  AgentPatch,
  api,
  AuthRequiredError,
  AuthStatus,
  ProviderAuth,
  getApiKey,
  workspaceFileUrl,
  IdeFolderListing,
  ModelPreset,
  Persona,
  Principal,
  ProjectMeta,
  ReauthSession,
  RoomMeta,
  RoomMsg,
  RoomWorkspace,
  Itinerary,
  ItineraryItem,
  ItineraryStatus,
  RoutineMeta,
  setApiKey,
  SidebarItem,
  SidebarLayout,
  SidebarTeam,
  VoiceStatus,
  WorkspaceStatus,
} from "./api";
import { LOGO_SRC, YOU_SRC, agentIcon } from "./icons";
import { remainingThinkers, remainingThinkersAfter } from "./thinking";

/** Shared outer ring/tooltip/working-pulse frame for every avatar shape. */
function AvatarFrame({
  children,
  label,
  working = false,
}: {
  children: ReactNode;
  label?: string;
  working?: boolean;
}) {
  const title = working && label ? `${label} is working` : label;
  return (
    <span className={working ? "avatar-wrap working" : "avatar-wrap"} title={title}>
      {children}
    </span>
  );
}

/**
 * CSS colour for a palette key — reads the `--palette-<key>` custom property
 * (styles.css) so every surface agrees. `color` is always a key the backend
 * resolved (CONTRACT-identity.md), never re-derived here.
 */
function identityColor(color: string | null | undefined): string {
  return color ? `var(--palette-${color}, var(--text))` : "var(--text)";
}

/** Gap (each side) between an illustrated avatar's frame and the picture
 * inside it — the frame's own background peeks through as a colour ring. */
function ringPad(size: number): number {
  return Math.max(2, Math.round(size * 0.07));
}

/**
 * An agent's avatar. Resolution order (CONTRACT-identity.md + operator
 * correction 2026-08-17):
 *   1. `identity.emoji` — an explicit per-agent override always wins.
 *   2. bespoke artwork, if `slug` is one of the six named retainers
 *      (icons.ts) — the palette colour still shows as a ring/backing behind
 *      the illustration, so an agent's colour stays consistent across every
 *      surface even when its glyph is a picture, not a letter.
 *   3. `identity.initial` over the resolved palette colour — everyone else.
 * Never re-derive a colour or initial here — always read `identity`.
 * `fallback` only covers `identity` being entirely missing (e.g. a room
 * member slug that no longer resolves to a live agent). `slug` is omitted
 * for previews of an agent that doesn't exist yet (hire form) — no artwork
 * lookup is attempted without one.
 */
function AgentAvatar({
  identity,
  slug,
  fallback,
  label,
  size = 28,
  working = false,
}: {
  identity: AgentIdentity | null | undefined;
  slug?: string;
  fallback: string;
  label?: string;
  size?: number;
  working?: boolean;
}) {
  const initial = identity?.initial || (fallback.trim().charAt(0) || "?").toUpperCase();
  const color = identity?.color;
  const ring = color ? `var(--palette-${color}, var(--bg-input))` : "var(--bg-input)";
  const art = identity?.emoji ? null : slug ? agentIcon(slug) : null;
  const pad = ringPad(size);
  return (
    <AvatarFrame label={label} working={working}>
      <span
        className="avatar avatar-badge"
        style={{
          width: size,
          height: size,
          fontSize: Math.round(size * 0.46),
          background: ring,
          color: color ? "var(--accent-text)" : "var(--text)",
        }}
        aria-hidden={label ? true : undefined}
        role={label ? undefined : "img"}
        aria-label={label ? undefined : "agent"}
      >
        {art ? (
          <img
            className="avatar-illustration"
            src={art}
            alt=""
            width={size - pad * 2}
            height={size - pad * 2}
            draggable={false}
          />
        ) : (
          identity?.emoji || initial
        )}
      </span>
    </AvatarFrame>
  );
}

/**
 * The human's avatar — deliberately a different shape (rounded square, not
 * a circle), its own accent ring, and a different colour family (a neutral,
 * not one of the twelve agent palette keys) so the one line in a transcript
 * that is *yours* reads as a categorically different kind of participant,
 * not "one more agent." `you.png` is the same illustrated character family
 * as the named-agent artwork (navy hood, gold star) — it is a PLACEHOLDER
 * pending a bespoke glasses-and-vest render in that same style; the
 * shape/frame/colour distinction here is what carries "different kind of
 * participant" today, not the art itself.
 */
function UserAvatar({
  label,
  size = 28,
  working = false,
}: {
  label?: string;
  size?: number;
  working?: boolean;
}) {
  const pad = ringPad(size);
  return (
    <AvatarFrame label={label} working={working}>
      <span
        className="avatar avatar-badge avatar-user"
        style={{ width: size, height: size }}
        aria-hidden={label ? true : undefined}
        role={label ? undefined : "img"}
        aria-label={label ? undefined : "you"}
      >
        <img
          className="avatar-illustration"
          src={YOU_SRC}
          alt=""
          width={size - pad * 2}
          height={size - pad * 2}
          draggable={false}
        />
      </span>
    </AvatarFrame>
  );
}

/**
 * Generic 7-hue hash for entities the backend has no derived identity for:
 * rooms (collapsed rail) and team-affiliation accents in the sidebar.
 * Agents have a real backend-derived colour in `identity.color` — never call
 * this for an agent (see `identityColor` / `AgentAvatar` above).
 */
const CHIP_COLORS = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#f7768e", "#73daca"];

function chipColor(name: string): string {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return CHIP_COLORS[h % CHIP_COLORS.length];
}

const MENTION_TOKEN = /^[A-Za-z0-9_][A-Za-z0-9_-]*$/;

function mentionHandle(slug: string, agents: AgentMeta[], members: string[]): string {
  const bySlug = Object.fromEntries(agents.map((a) => [a.slug, a]));
  const displayOf = (s: string) => (bySlug[s]?.display_name || s).trim() || s;
  const raw = displayOf(slug);
  if (MENTION_TOKEN.test(raw)) {
    const owners = members.filter((m) => displayOf(m).toLowerCase() === raw.toLowerCase());
    if (owners.length === 1 && owners[0] === slug) return raw;
  }
  const first = raw.split(/\s+/)[0] || slug;
  if (!MENTION_TOKEN.test(first)) return slug;
  const owners = members.filter((m) => {
    const f = displayOf(m).split(/\s+/)[0] || m;
    return f.toLowerCase() === first.toLowerCase();
  });
  return owners.length === 1 && owners[0] === slug ? first : slug;
}

function blankFences(text: string): string {
  const out: string[] = [];
  let i = 0;
  let fence: string | null = null;
  while (i < text.length) {
    if (fence === null) {
      if (text.startsWith("```", i) || text.startsWith("~~~", i)) {
        fence = text.slice(i, i + 3);
        out.push("   ");
        i += 3;
      } else {
        out.push(text[i]);
        i += 1;
      }
    } else if (text.startsWith(fence, i)) {
      out.push("   ");
      i += fence.length;
      fence = null;
    } else {
      out.push(" ");
      i += 1;
    }
  }
  return out.join("");
}

function resolveTypedMention(
  token: string,
  members: string[],
  handleOf: (slug: string) => string,
): string | null {
  const key = token.toLowerCase();
  const aliases: Array<[string, string]> = [];
  for (const slug of members) {
    aliases.push([slug.toLowerCase(), slug]);
    aliases.push([handleOf(slug).toLowerCase(), slug]);
  }
  const exact = aliases.find(([alias]) => alias === key);
  if (exact) return exact[1];
  const hits = [...new Set(aliases.filter(([alias]) => alias.startsWith(key)).map(([, slug]) => slug))];
  return hits.length === 1 ? hits[0] : null;
}

function mentionPartial(text: string, caret: number): { start: number; query: string } | null {
  const head = text.slice(0, caret);
  const match = /(?:^|[\s([{])@([A-Za-z0-9_-]*)$/.exec(head);
  if (!match) return null;
  return { start: caret - match[1].length - 1, query: match[1] };
}

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const n = samples.length;
  const buffer = new ArrayBuffer(44 + n * 2);
  const view = new DataView(buffer);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + n * 2, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, n * 2, true);
  let off = 44;
  for (let i = 0; i < n; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}

async function startMic(): Promise<{ stop: () => Promise<Blob> }> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const mute = ctx.createGain();
  mute.gain.value = 0;
  const parts: Float32Array[] = [];
  proc.onaudioprocess = (ev) => {
    parts.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
  };
  source.connect(proc);
  proc.connect(mute);
  mute.connect(ctx.destination);
  const sampleRate = ctx.sampleRate;
  return {
    stop: async () => {
      proc.disconnect();
      source.disconnect();
      mute.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      await ctx.close();
      const total = parts.reduce((n, p) => n + p.length, 0);
      const merged = new Float32Array(total);
      let o = 0;
      for (const p of parts) {
        merged.set(p, o);
        o += p.length;
      }
      return encodeWav(merged, sampleRate);
    },
  };
}

const SPEAK_KEY = "retinue.speakReplies";
const ARCHIVED_KEY = "retinue.showArchived";
const DRAG_MIME = "application/x-retinue";
const SIDEBAR_COLLAPSED_KEY = "retinue.sidebarCollapsed";
// Persisted as the real project id, or "" for the Unfiled pseudo-project
// (slugify() never mints an empty id, so "" is unambiguous).
const SELECTED_PROJECT_KEY = "retinue.selectedProject";

function teamSlug(label: string): string {
  return (
    (label || "team")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 24) || "team"
  );
}

function nextTeamId(label: string, items: SidebarItem[], keep?: string): string {
  const slug = teamSlug(label);
  const taken = new Set(
    items.filter((i): i is SidebarTeam => i.kind === "team" && i.id !== keep).map((i) => i.id),
  );
  if (!taken.has(slug)) return slug;
  return `${slug}-${Math.random().toString(16).slice(2, 6)}`;
}

function itemKey(item: SidebarItem): string {
  return item.kind === "team" ? `team:${item.id}` : `agent:${item.slug}`;
}

function relocate<T>(list: T[], from: number, insertAt: number): T[] {
  if (from < 0 || from >= list.length) return list.slice();
  const next = list.slice();
  const [picked] = next.splice(from, 1);
  let at = insertAt > from ? insertAt - 1 : insertAt;
  if (at < 0) at = 0;
  if (at > next.length) at = next.length;
  next.splice(at, 0, picked);
  return next;
}

function fallbackLayout(rooms: RoomMeta[], agents: AgentMeta[]): SidebarLayout {
  return {
    rooms: rooms.map((r) => r.id),
    items: agents.map((a) => ({ kind: "agent" as const, slug: a.slug })),
  };
}

function RowMenu({
  actions,
}: {
  actions: { label: string; danger?: boolean; onClick: () => void }[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="row-menu-wrap" ref={ref}>
      <button
        className="row-menu-btn"
        title="More"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ⋯
      </button>
      {open && (
        <div className="row-menu">
          {actions.map((a) => (
            <button
              key={a.label}
              className={a.danger ? "danger" : undefined}
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                a.onClick();
              }}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Chip-level "…" menu for a room member. A naked × on a 24px avatar is too
 * easy to miss-hit, so removal is two clicks: open the menu, then confirm.
 */
function MemberMenu({
  onRemove,
  disabledReason,
}: {
  onRemove: () => void;
  disabledReason: string | null;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="row-menu-wrap" ref={ref}>
      <button
        type="button"
        className="mini"
        title="Member options"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        ⋯
      </button>
      {open && (
        <div className="row-menu">
          {disabledReason ? (
            <span className="row-menu-note">{disabledReason}</span>
          ) : (
            <button
              type="button"
              className="danger"
              onClick={(e) => {
                e.stopPropagation();
                setOpen(false);
                onRemove();
              }}
            >
              Remove from room
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Room-header invite control. Only agents not already in the room are
 * offered. The 20-message window is stated up front so the newcomer's
 * blind spot isn't a surprise.
 */
function InviteMenu({
  candidates,
  onInvite,
}: {
  candidates: AgentMeta[];
  onInvite: (slug: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  return (
    <div className="invite-wrap" ref={ref}>
      <button
        type="button"
        className="mini wide"
        title="Invite an agent into this room"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
      >
        + invite
      </button>
      {open && (
        <div className="invite-menu">
          <p className="invite-note">
            New members see roughly the last 20 messages, not the full transcript.
          </p>
          {candidates.length === 0 ? (
            <p className="invite-empty">Everyone available is already here.</p>
          ) : (
            candidates.map((a) => (
              <button
                key={a.slug}
                type="button"
                className="invite-opt"
                onClick={(e) => {
                  e.stopPropagation();
                  setOpen(false);
                  onInvite(a.slug);
                }}
              >
                <AgentAvatar
                  identity={a.identity}
                  slug={a.slug}
                  fallback={a.slug}
                  label={a.display_name || a.slug}
                  size={20}
                />
                @{a.slug}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/** Keyboard-accessible move-up / move-down for sidebar rows (pairs with drag). */
function ReorderButtons({
  what,
  canUp,
  canDown,
  onUp,
  onDown,
}: {
  what: string;
  canUp: boolean;
  canDown: boolean;
  onUp: () => void;
  onDown: () => void;
}) {
  return (
    <div className="reorder-btns">
      <button
        type="button"
        className="reorder-btn"
        aria-label={`Move ${what} up`}
        title={`Move ${what} up`}
        disabled={!canUp}
        onClick={(e) => {
          e.stopPropagation();
          onUp();
        }}
      >
        ▲
      </button>
      <button
        type="button"
        className="reorder-btn"
        aria-label={`Move ${what} down`}
        title={`Move ${what} down`}
        disabled={!canDown}
        onClick={(e) => {
          e.stopPropagation();
          onDown();
        }}
      >
        ▼
      </button>
    </div>
  );
}

/**
 * Collapsed-sidebar rail entry for one room — an initial today, structured so
 * a richer per-agent avatar badge can drop in later without a reshape.
 */
function RailButton({
  room,
  active,
  onClick,
}: {
  room: RoomMeta;
  active: boolean;
  onClick: () => void;
}) {
  const initial = (room.name.trim()[0] || "?").toUpperCase();
  return (
    <button
      type="button"
      className={`rail-item avatar-badge${active ? " active" : ""}${room.archived ? " archived" : ""}`}
      style={{ background: chipColor(room.id) }}
      aria-label={room.name}
      aria-current={active ? "true" : undefined}
      title={room.name}
      onClick={onClick}
    >
      {initial}
    </button>
  );
}

// ── message row ──────────────────────────────────────────────────────────

function MentionBody({
  text,
  members,
  handleOf,
  agentsBySlug,
}: {
  text: string;
  members: string[];
  handleOf: (slug: string) => string;
  agentsBySlug: Record<string, AgentMeta>;
}) {
  const unfenced = blankFences(text);
  const re = /@([A-Za-z0-9_][A-Za-z0-9_-]*)/g;
  const parts: ReactNode[] = [];
  let last = 0;
  let match: RegExpExecArray | null;
  while ((match = re.exec(unfenced))) {
    const slug = resolveTypedMention(match[1], members, handleOf);
    if (!slug) continue;
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(
      <span
        key={match.index}
        className="live-mention"
        style={{ color: identityColor(agentsBySlug[slug]?.identity.color) }}
        title={`@${handleOf(slug)}`}
      >
        {text.slice(match.index, match.index + match[0].length)}
      </span>,
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

const WORKSPACE_FILE = /\/workspace\/[A-Za-z0-9._+/-]+/g;
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg)$/i;

function workspacePaths(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const match of text.matchAll(WORKSPACE_FILE)) {
    if (!seen.has(match[0])) {
      seen.add(match[0]);
      out.push(match[0]);
    }
  }
  return out;
}

function WorkspaceStrip({ roomId, text }: { roomId: string; text: string }) {
  const paths = workspacePaths(text);
  if (!paths.length) return null;
  return (
    <div className="workspace-strip">
      {paths.map((path) => {
        const href = workspaceFileUrl(roomId, path);
        if (IMAGE_EXT.test(path)) {
          return (
            <a key={path} className="workspace-image" href={href} target="_blank" rel="noreferrer">
              <img src={href} alt={path} />
              <span className="nav-sub">{path}</span>
            </a>
          );
        }
        return (
          <a key={path} className="workspace-file" href={href} target="_blank" rel="noreferrer">
            {path}
          </a>
        );
      })}
    </div>
  );
}

function MessageRow({
  msg,
  userName,
  members,
  handleOf,
  roomId,
  agentsBySlug,
}: {
  msg: RoomMsg;
  userName: string;
  members: string[];
  handleOf: (slug: string) => string;
  roomId: string;
  agentsBySlug: Record<string, AgentMeta>;
}) {
  if (msg.kind === "system") {
    return <div className="msg-system">— {msg.text} —</div>;
  }
  const mine = msg.kind === "user";
  return (
    <div className={mine ? "msg-row mine" : "msg-row"}>
      {mine ? (
        <UserAvatar label={userName} size={32} />
      ) : (
        <AgentAvatar
          identity={agentsBySlug[msg.speaker]?.identity}
          slug={msg.speaker}
          fallback={msg.speaker}
          label={msg.speaker}
          size={32}
          working={!!agentsBySlug[msg.speaker]?.busy}
        />
      )}
      <div className={mine ? "bubble mine" : "bubble"}>
        {!mine && (
          <span className="chip" style={{ color: identityColor(agentsBySlug[msg.speaker]?.identity.color) }}>
            {handleOf(msg.speaker) !== msg.speaker ? handleOf(msg.speaker) : msg.speaker}
          </span>
        )}
        <div className="msg-text">
          <MentionBody text={msg.text} members={members} handleOf={handleOf} agentsBySlug={agentsBySlug} />
        </div>
        <WorkspaceStrip roomId={roomId} text={msg.text} />
      </div>
    </div>
  );
}

// ── room chat view ───────────────────────────────────────────────────────

function RoomView({
  room,
  userName,
  agents,
  onEdit,
  onArchive,
  onDelete,
  onRoomUpdate,
  sidebarCollapsed,
  onToggleSidebar,
}: {
  room: RoomMeta;
  userName: string;
  agents: AgentMeta[];
  onEdit: () => void;
  onArchive: () => void;
  onDelete: () => void;
  onRoomUpdate: (updated: RoomMeta) => void;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}) {
  const [messages, setMessages] = useState<RoomMsg[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [thinking, setThinking] = useState<string[]>([]);
  const [holding, setHolding] = useState(false);
  const [voiceNote, setVoiceNote] = useState("");
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [speakReplies, setSpeakReplies] = useState(
    () => localStorage.getItem(SPEAK_KEY) !== "0",
  );
  const roomRef = useRef(room.id);
  roomRef.current = room.id;
  const sinceRef = useRef(0);
  const messagesRef = useRef<RoomMsg[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const micRef = useRef<{ stop: () => Promise<Blob> } | null>(null);
  const [caret, setCaret] = useState(0);
  const [mentionPick, setMentionPick] = useState(0);
  const [mentionOff, setMentionOff] = useState(false);
  const [roomRoutines, setRoomRoutines] = useState<RoutineMeta[]>([]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [itineraryOpen, setItineraryOpen] = useState(() => {
    try {
      return localStorage.getItem(`retinue:itinerary:${room.id}`) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      setItineraryOpen(localStorage.getItem(`retinue:itinerary:${room.id}`) === "1");
    } catch {
      setItineraryOpen(false);
    }
  }, [room.id]);
  const handleOf = useCallback(
    (slug: string) => mentionHandle(slug, agents, room.members),
    [agents, room.members],
  );
  const agentsBySlug = useMemo(
    () => Object.fromEntries(agents.map((a) => [a.slug, a])),
    [agents],
  );
  const mentionQuery = mentionOff ? null : mentionPartial(draft, caret);
  const mentionChoices = mentionQuery
    ? room.members.filter((m) => {
        const q = mentionQuery.query.toLowerCase();
        const handle = handleOf(m).toLowerCase();
        return handle.startsWith(q) || m.toLowerCase().startsWith(q);
      })
    : [];
  useEffect(() => {
    if (mentionPick >= mentionChoices.length) setMentionPick(0);
  }, [mentionChoices.length, mentionPick]);
  const playQ = useRef(Promise.resolve());
  const spokenRef = useRef<Set<number>>(new Set());
  const openedAtRef = useRef(Date.now() / 1000);

  useEffect(() => {
    api
      .voiceStatus()
      .then(setVoice)
      .catch(() => setVoice(null));
  }, []);

  const refreshRoomRoutines = useCallback(() => {
    api
      .listRoomRoutines(room.id)
      .then((r) => setRoomRoutines(r.routines))
      .catch(() => setRoomRoutines([]));
  }, [room.id]);
  useEffect(() => {
    refreshRoomRoutines();
  }, [refreshRoomRoutines]);

  // SSE transcript stream; long-poll is the fallback (see api.watchTranscript).
  useEffect(() => {
    sinceRef.current = 0;
    setMessages([]);
    messagesRef.current = [];
    setThinking([]);
    setSending(false);
    spokenRef.current = new Set();
    openedAtRef.current = Date.now() / 1000;
    const ctl = new AbortController();
    api.watchTranscript(
      room.id,
      0,
      (fresh) => {
        sinceRef.current = Math.max(sinceRef.current, ...fresh.map((m) => m.seq));
        setMessages((prev) => {
          const next = [...prev, ...fresh];
          messagesRef.current = next;
          return next;
        });
        setThinking((waiting) => remainingThinkers(waiting, fresh));
      },
      ctl.signal,
    );
    return () => ctl.abort();
  }, [room.id]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, thinking]);

  // Self-heal: a no-reply system line (or a cycle abort) must drop the
  // bubble even if it arrived before setThinking(planned), or if we only
  // filtered on kind===agent (the previous bug).
  useEffect(() => {
    setThinking((waiting) => remainingThinkersAfter(waiting, messages));
  }, [messages]);

  useEffect(() => {
    if (!speakReplies) return;
    for (const msg of messages) {
      if (msg.kind !== "agent" || spokenRef.current.has(msg.seq)) continue;
      if (msg.ts && msg.ts < openedAtRef.current - 1) continue;
      spokenRef.current.add(msg.seq);
      playQ.current = playQ.current
        .then(async () => {
          const blob = await api.speak(msg.text, msg.speaker);
          const url = URL.createObjectURL(blob);
          try {
            await new Promise<void>((resolve, reject) => {
              const audio = new Audio(url);
              audio.onended = () => resolve();
              audio.onerror = () => reject(new Error("playback failed"));
              void audio.play().catch(reject);
            });
          } finally {
            URL.revokeObjectURL(url);
          }
        })
        .catch((e) => {
          setVoiceNote(String(e));
        });
    }
  }, [messages, speakReplies]);

  const inviteCandidates = useMemo(
    () => agents.filter((a) => !a.archived && !room.members.includes(a.slug)),
    [agents, room.members],
  );

  const inviteMember = useCallback(
    async (slug: string) => {
      try {
        onRoomUpdate(await api.inviteMember(room.id, slug));
      } catch (e) {
        alert(String(e));
      }
    },
    [room.id, onRoomUpdate],
  );

  const removeMember = useCallback(
    async (slug: string) => {
      try {
        onRoomUpdate(await api.removeMember(room.id, slug));
      } catch (e) {
        alert(String(e));
      }
    },
    [room.id, onRoomUpdate],
  );

  const send = useCallback(async () => {
    const text = draft.trim();
    if ((!text && pendingFiles.length === 0) || sending) return;
    setSending(true);
    try {
      const paths: string[] = [];
      for (const file of pendingFiles) {
        const uploaded = await api.uploadAttachment(room.id, file);
        paths.push(uploaded.path);
      }
      const body = [text, ...paths].filter(Boolean).join("\n");
      const { planned } = await api.send(room.id, body, userName);
      if (roomRef.current !== room.id) return;
      setThinking(remainingThinkersAfter(planned, messagesRef.current));
      setDraft("");
      setPendingFiles([]);
    } catch (e) {
      alert(String(e));
    } finally {
      setSending(false);
    }
  }, [draft, sending, pendingFiles, room.id, userName]);

  const beginTalk = useCallback(async () => {
    if (sending || holding) return;
    setVoiceNote("");
    try {
      micRef.current = await startMic();
      setHolding(true);
    } catch (e) {
      setVoiceNote(String(e));
    }
  }, [sending, holding]);

  const endTalk = useCallback(async () => {
    const rec = micRef.current;
    micRef.current = null;
    setHolding(false);
    if (!rec) return;
    setSending(true);
    setVoiceNote("transcribing…");
    try {
      const blob = await rec.stop();
      const { planned, text } = await api.sendAudio(room.id, blob, userName);
      if (roomRef.current !== room.id) return;
      setThinking(remainingThinkersAfter(planned, messagesRef.current));
      setVoiceNote(text ? `Heard: ${text}` : "");
    } catch (e) {
      setVoiceNote(String(e));
    } finally {
      setSending(false);
    }
  }, [room.id, userName]);

  return (
    <div className="room-view">
      <header className="room-header">
        <div className="room-header-main">
          <h2>
            {room.name}
            {room.archived ? " (archived)" : ""}
            {(room.workspace ?? "sandbox") === "ide" && (
              <span className="mode-badge" title={room.ide_path || ""}>
                {ideFolderLabel(room.ide_path)}
              </span>
            )}
          </h2>
          <div className="room-members">
            {room.members.map((m) => (
              <span key={m} className="member-chip">
                <AgentAvatar
                  identity={agentsBySlug[m]?.identity}
                  slug={m}
                  fallback={m}
                  label={handleOf(m)}
                  size={24}
                  working={!!agentsBySlug[m]?.busy}
                />
                <span className="chip" style={{ color: identityColor(agentsBySlug[m]?.identity.color) }}>
                  @{handleOf(m)}
                  {room.lead === m ? " ★" : ""}
                </span>
                <MemberMenu
                  disabledReason={
                    room.members.length <= 1 ? "A room needs at least one member" : null
                  }
                  onRemove={() => void removeMember(m)}
                />
              </span>
            ))}
            <InviteMenu candidates={inviteCandidates} onInvite={(slug) => void inviteMember(slug)} />
          </div>
        </div>
        <div className="room-header-actions">
          <button
            type="button"
            className="mini wide"
            aria-expanded={!sidebarCollapsed}
            aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={`${sidebarCollapsed ? "Expand" : "Collapse"} sidebar (Ctrl+B)`}
            onClick={onToggleSidebar}
          >
            {sidebarCollapsed ? "»" : "«"}
          </button>
          <button className="mini wide" onClick={onEdit}>
            Edit
          </button>
          <button className="mini wide" onClick={onArchive}>
            {room.archived ? "Unarchive" : "Archive"}
          </button>
          <button className="mini wide danger-btn" onClick={onDelete}>
            Delete
          </button>
          <button
            className={`mini wide${itineraryOpen ? " picked" : ""}`}
            aria-pressed={itineraryOpen}
            onClick={() => {
              const next = !itineraryOpen;
              setItineraryOpen(next);
              try {
                localStorage.setItem(`retinue:itinerary:${room.id}`, next ? "1" : "0");
              } catch {
                /* ignore */
              }
            }}
          >
            Itinerary
          </button>
          <button
            className="mini wide"
            onClick={async () => {
              const name = window.prompt("Save this room's user prompts as a routine named:");
              if (!name) return;
              try {
                const created = await api.saveRoutine(name, room.id);
                refreshRoomRoutines();
                alert(`Saved routine "${created.name}" (${created.messages.length} steps)`);
              } catch (e) {
                alert(String(e));
              }
            }}
          >
            Save routine
          </button>
        </div>
      </header>
      {roomRoutines.length > 0 && (
        <div className="room-routines" aria-label="Routines for this room">
          {roomRoutines.map((rt) => (
            <span key={rt.slug} className="routine-chip">
              {rt.name}
              <span className="nav-sub">
                {" "}
                · {rt.messages.length} step{rt.messages.length === 1 ? "" : "s"}
              </span>
              <button
                className="mini"
                onClick={async () => {
                  try {
                    await api.runRoutine(rt.slug, room.id);
                  } catch (e) {
                    alert(String(e));
                  }
                }}
              >
                Run
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="room-body">
      <div className="room-chat">
      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-hint">
            Say something — no @mention goes to the lead{room.lead ? ` (@${handleOf(room.lead)})` : ""}.
          </div>
        )}
        {messages.map((m) => (
          <MessageRow
            key={m.seq}
            msg={m}
            userName={userName}
            members={room.members}
            handleOf={handleOf}
            roomId={room.id}
            agentsBySlug={agentsBySlug}
          />
        ))}
        {thinking[0] && (
          <div key={thinking[0]} className="msg-row">
            <AgentAvatar
              identity={agentsBySlug[thinking[0]]?.identity}
              slug={thinking[0]}
              fallback={thinking[0]}
              label={handleOf(thinking[0])}
              size={32}
              working
            />
            <div className="bubble thinking">
              <span
                className="chip"
                style={{ color: identityColor(agentsBySlug[thinking[0]]?.identity.color) }}
              >
                {handleOf(thinking[0])}
              </span>
              <div className="msg-text dots">thinking</div>
            </div>
          </div>
        )}
        {thinking.length > 1 && (
          <div className="queued-hint">
            Up next: {thinking.slice(1).map((w) => `@${handleOf(w)}`).join(" → ")}
          </div>
        )}
      </div>
      <div
        className="composer"
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDrop={(e) => {
          e.preventDefault();
          const files = Array.from(e.dataTransfer.files || []);
          if (files.length) setPendingFiles((cur) => [...cur, ...files]);
        }}
      >
        <div className="mention-bar">
          {room.members.map((m) => (
            <button
              key={m}
              className="mention-btn"
              title={`@${m}`}
              onClick={() => {
                const token = `@${handleOf(m)} `;
                setDraft((d) => `${d}${token}`);
                setCaret((c) => c + token.length);
              }}
            >
              <AgentAvatar identity={agentsBySlug[m]?.identity} slug={m} fallback={m} label={handleOf(m)} size={18} />
              @{handleOf(m)}
            </button>
          ))}
        </div>
        {pendingFiles.length > 0 && (
          <div className="attach-chips">
            {pendingFiles.map((file, i) => (
              <span key={`${file.name}-${i}`} className="attach-chip">
                {file.name}
                <button
                  type="button"
                  className="mini"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => setPendingFiles((cur) => cur.filter((_, j) => j !== i))}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="composer-row">
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              const files = Array.from(e.target.files || []);
              if (files.length) setPendingFiles((cur) => [...cur, ...files]);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="attach-btn"
            title="Attach a file or image"
            aria-label="Attach a file or image"
            onClick={() => fileRef.current?.click()}
          >
            +
          </button>
          <div className="composer-input">
            {mentionChoices.length > 0 && (
              <div className="mention-picker" role="listbox" aria-label="Mention a member">
                {mentionChoices.map((m, i) => (
                  <button
                    key={m}
                    role="option"
                    aria-selected={i === mentionPick}
                    className={i === mentionPick ? "mention-opt active" : "mention-opt"}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      const q = mentionQuery;
                      if (!q) return;
                      const token = `@${handleOf(m)} `;
                      const next = draft.slice(0, q.start) + token + draft.slice(caret);
                      setDraft(next);
                      setCaret(q.start + token.length);
                      setMentionPick(0);
                    }}
                  >
                    <AgentAvatar identity={agentsBySlug[m]?.identity} slug={m} fallback={m} label={handleOf(m)} size={16} />
                    @{handleOf(m)}
                    {handleOf(m) !== m ? <span className="nav-sub"> @{m}</span> : null}
                  </button>
                ))}
              </div>
            )}
            <textarea
              ref={draftRef}
              value={draft}
              placeholder={`Message ${room.name}…  Type @ to mention`}
              onChange={(e) => {
                setDraft(e.target.value);
                setCaret(e.target.selectionStart);
                setMentionOff(false);
              }}
              onClick={(e) => setCaret((e.target as HTMLTextAreaElement).selectionStart)}
              onKeyUp={(e) => setCaret((e.target as HTMLTextAreaElement).selectionStart)}
              onKeyDown={(e) => {
                if (mentionChoices.length > 0) {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setMentionPick((i) => (i + 1) % mentionChoices.length);
                    return;
                  }
                  if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setMentionPick((i) => (i - 1 + mentionChoices.length) % mentionChoices.length);
                    return;
                  }
                  if (e.key === "Tab" || e.key === "Enter") {
                    e.preventDefault();
                    const m = mentionChoices[mentionPick] || mentionChoices[0];
                    const q = mentionQuery;
                    if (!q || !m) return;
                    const token = `@${handleOf(m)} `;
                    const next = draft.slice(0, q.start) + token + draft.slice(caret);
                    setDraft(next);
                    setCaret(q.start + token.length);
                    setMentionPick(0);
                    return;
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    setMentionOff(true);
                    return;
                  }
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={2}
            />
          </div>
          <button
            className={holding ? "talk-btn holding" : "talk-btn"}
            disabled={sending}
            title={
              voice && !voice.ready
                ? `Voice not ready (${voice.backend}): ${voice.detail}`
                : "Hold to talk"
            }
            onPointerDown={(e) => {
              e.preventDefault();
              (e.currentTarget as HTMLButtonElement).setPointerCapture(e.pointerId);
              void beginTalk();
            }}
            onPointerUp={() => void endTalk()}
            onPointerCancel={() => void endTalk()}
          >
            {holding ? "Listening…" : "Hold to talk"}
          </button>
          <button
            className="send-btn"
            disabled={sending || (!draft.trim() && pendingFiles.length === 0)}
            onClick={() => void send()}
          >
            Send
          </button>
        </div>
        <div className="voice-bar">
          <label className="voice-toggle">
            <input
              type="checkbox"
              checked={speakReplies}
              onChange={(e) => {
                const on = e.target.checked;
                setSpeakReplies(on);
                localStorage.setItem(SPEAK_KEY, on ? "1" : "0");
              }}
            />
            Speak replies
          </label>
          <span className="voice-backend">
            {voice
              ? `${voice.backend}${voice.ready ? "" : " (not ready)"}`
              : "voice…"}
          </span>
          {voiceNote && <span className="voice-note">{voiceNote}</span>}
        </div>
      </div>
      </div>
      {itineraryOpen && (
        <ItineraryPane roomId={room.id} lead={room.lead} handleOf={handleOf} />
      )}
      </div>
    </div>
  );
}

const STATUS_CYCLE: ItineraryStatus[] = ["todo", "doing", "done"];

function nextStatus(status: ItineraryStatus): ItineraryStatus {
  const i = STATUS_CYCLE.indexOf(status);
  return STATUS_CYCLE[(i + 1) % STATUS_CYCLE.length];
}

function ItineraryPane({
  roomId,
  lead,
  handleOf,
}: {
  roomId: string;
  lead: string | null;
  handleOf: (slug: string) => string;
}) {
  const [plan, setPlan] = useState<Itinerary | null>(null);
  const [draftItem, setDraftItem] = useState("");
  const [note, setNote] = useState("");
  const saveTimer = useRef<number | null>(null);
  const dirtyRef = useRef(false);

  const persist = useCallback(
    async (next: Itinerary) => {
      dirtyRef.current = true;
      setPlan(next);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => {
        const snapshot = next;
        void api
          .putItinerary(roomId, {
            title: snapshot.title,
            summary: snapshot.summary,
            items: snapshot.items,
            updated_by: "user",
          })
          .then((saved) => {
            setPlan((cur) => {
              if (!cur) return saved;
              const same =
                cur.title === snapshot.title &&
                cur.summary === snapshot.summary &&
                JSON.stringify(cur.items) === JSON.stringify(snapshot.items);
              dirtyRef.current = !same;
              return same ? saved : cur;
            });
          })
          .catch((e) => setNote(String(e)));
      }, 280);
    },
    [roomId],
  );

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      if (dirtyRef.current) return;
      api
        .getItinerary(roomId)
        .then((got) => {
          if (!cancelled && !dirtyRef.current) setPlan(got);
        })
        .catch((e) => {
          if (!cancelled) setNote(String(e));
        });
    };
    load();
    const tick = window.setInterval(load, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(tick);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
  }, [roomId]);

  if (!plan) {
    return (
      <aside className="itinerary-pane" aria-label="Room itinerary">
        <h3>Itinerary</h3>
        <p className="note">{note || "Loading…"}</p>
      </aside>
    );
  }

  const addItem = () => {
    const text = draftItem.trim();
    if (!text) return;
    const item: ItineraryItem = {
      id: `n${Date.now().toString(36)}`,
      text,
      status: "todo",
    };
    setDraftItem("");
    void persist({ ...plan, items: [...plan.items, item] });
  };

  return (
    <aside className="itinerary-pane" aria-label="Room itinerary">
      <header className="itinerary-head">
        <h3>Itinerary</h3>
        {lead ? (
          <p className="note">
            Lead @{handleOf(lead)} keeps this current. You can edit it too.
          </p>
        ) : (
          <p className="note">Living outline for this room — not the transcript.</p>
        )}
      </header>
      <label className="itinerary-field">
        Title
        <input
          value={plan.title}
          placeholder="What is this room doing?"
          onChange={(e) => void persist({ ...plan, title: e.target.value })}
        />
      </label>
      <label className="itinerary-field">
        Where we are
        <textarea
          value={plan.summary}
          rows={3}
          placeholder="One or two sentences on current progress."
          onChange={(e) => void persist({ ...plan, summary: e.target.value })}
        />
      </label>
      <ul className="itinerary-items">
        {plan.items.map((item) => (
          <li key={item.id} className={`itinerary-item ${item.status}`}>
            <button
              type="button"
              className={`itin-status ${item.status}`}
              title="Cycle todo → doing → done"
              onClick={() =>
                void persist({
                  ...plan,
                  items: plan.items.map((it) =>
                    it.id === item.id ? { ...it, status: nextStatus(it.status) } : it,
                  ),
                })
              }
            >
              {item.status}
            </button>
            <input
              value={item.text}
              onChange={(e) =>
                void persist({
                  ...plan,
                  items: plan.items.map((it) =>
                    it.id === item.id ? { ...it, text: e.target.value } : it,
                  ),
                })
              }
            />
            <button
              type="button"
              className="mini danger-btn"
              aria-label={`Remove ${item.text}`}
              onClick={() =>
                void persist({
                  ...plan,
                  items: plan.items.filter((it) => it.id !== item.id),
                })
              }
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="itinerary-add">
        <input
          value={draftItem}
          placeholder="Add a step…"
          onChange={(e) => setDraftItem(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addItem();
            }
          }}
        />
        <button type="button" className="mini" disabled={!draftItem.trim()} onClick={addItem}>
          Add
        </button>
      </div>
      {note && <p className="note">{note}</p>}
    </aside>
  );
}

// ── forms ────────────────────────────────────────────────────────────────

const PERSONA_DEFAULT: Persona = { warmth: "balanced", verbosity: "balanced", formality: "balanced" };

const PERSONA_DIAL_OPTIONS: {
  dial: keyof Persona;
  options: { value: string; label: string }[];
}[] = [
  {
    dial: "warmth",
    options: [
      { value: "terse", label: "Terse" },
      { value: "balanced", label: "Balanced" },
      { value: "warm", label: "Warm" },
    ],
  },
  {
    dial: "verbosity",
    options: [
      { value: "brief", label: "Brief" },
      { value: "balanced", label: "Balanced" },
      { value: "thorough", label: "Thorough" },
    ],
  },
  {
    dial: "formality",
    options: [
      { value: "casual", label: "Casual" },
      { value: "balanced", label: "Balanced" },
      { value: "formal", label: "Formal" },
    ],
  },
];

function personaIsDefault(persona: Persona): boolean {
  return (
    persona.warmth === PERSONA_DEFAULT.warmth &&
    persona.verbosity === PERSONA_DEFAULT.verbosity &&
    persona.formality === PERSONA_DEFAULT.formality
  );
}

/**
 * The three persona dials as segmented choices, not free text — "how it
 * should work" already covers free-form instruction; the value here is that
 * two agents differ *predictably*. Leaving every dial on "Balanced" changes
 * nothing about the agent (see the hint line).
 */
function PersonaDials({ persona, onChange }: { persona: Persona; onChange: (next: Persona) => void }) {
  return (
    <div className="persona-dials">
      <span className="field-label">
        Persona
        <span className="nav-sub">
          {personaIsDefault(persona)
            ? "Balanced on all three — leaving it here changes nothing about this agent."
            : "Adjusted from balanced."}
        </span>
      </span>
      {PERSONA_DIAL_OPTIONS.map(({ dial, options }) => (
        <div className="persona-row" key={dial} role="group" aria-label={dial}>
          <span className="persona-dial-label">{dial}</span>
          <div className="segmented">
            {options.map((opt) => (
              <button
                type="button"
                key={opt.value}
                className={persona[dial] === opt.value ? "seg-opt picked" : "seg-opt"}
                aria-pressed={persona[dial] === opt.value}
                onClick={() => onChange({ ...persona, [dial]: opt.value } as Persona)}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function voiceLabel(id: string, voices: Record<string, string>): string {
  return voices[id] || id;
}

function ModelPicker({ model, models, onChange }: { model: string; models: ModelPreset[]; onChange: (v: string) => void }) {
  if (models.length === 0) return null;
  return (
    <label>
      Model
      <select value={model} onChange={(e) => onChange(e.target.value)}>
        <option value="">Workspace default</option>
        {models.some((m) => !m.local) && (
          <optgroup label="Cloud">
            {models
              .filter((m) => !m.local)
              .map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name} ({m.summary})
                </option>
              ))}
          </optgroup>
        )}
        {models.some((m) => m.local) && (
          <optgroup label="Local">
            {models
              .filter((m) => m.local)
              .map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name} ({m.summary})
                </option>
              ))}
          </optgroup>
        )}
      </select>
    </label>
  );
}

function HirePanel({ onDone }: { onDone: (created?: AgentMeta) => void }) {
  const [name, setName] = useState("");
  const [job, setJob] = useState("");
  const [how, setHow] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [avatarEmoji, setAvatarEmoji] = useState("");
  const [avatarColor, setAvatarColor] = useState("");
  const [voice, setVoice] = useState("");
  const [persona, setPersona] = useState<Persona>(PERSONA_DEFAULT);
  const [palette, setPalette] = useState<string[]>([]);
  const [voices, setVoices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  useEffect(() => {
    api
      .listModels()
      .then((r) => setModels(r.models))
      .catch(() => setModels([]));
    api
      .getPalette()
      .then((r) => setPalette(r.colors))
      .catch(() => setPalette([]));
    api
      .voiceStatus()
      .then((v) => setVoices(v.voices))
      .catch(() => setVoices({}));
  }, []);
  const trimmedEmoji = avatarEmoji.trim();
  const previewIdentity =
    trimmedEmoji || avatarColor
      ? {
          emoji: trimmedEmoji || null,
          initial: (name.trim().charAt(0) || "?").toUpperCase(),
          color: avatarColor,
          color_source: (avatarColor ? "override" : "derived") as "override" | "derived",
        }
      : undefined;
  return (
    <div className="panel">
      <h3>Hire an agent</h3>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Scout" />
      </label>
      <label>
        Primary job
        <input
          value={job}
          onChange={(e) => setJob(e.target.value)}
          placeholder="Find and verify facts fast"
        />
      </label>
      <label>
        How it should work
        <textarea
          value={how}
          onChange={(e) => setHow(e.target.value)}
          rows={4}
          placeholder="Check sources before answering. Keep replies short. Hand writing questions to the editor."
        />
      </label>
      <ModelPicker model={model} models={models} onChange={setModel} />
      <div className="identity-pick">
        <span className="field-label">Look</span>
        <div className="identity-pick-row">
          <AgentAvatar identity={previewIdentity} fallback={name || "?"} size={40} />
          <input
            value={avatarEmoji}
            onChange={(e) => setAvatarEmoji(e.target.value)}
            placeholder="Emoji (optional)"
            maxLength={4}
            aria-label="Avatar emoji"
          />
          <select value={avatarColor} onChange={(e) => setAvatarColor(e.target.value)} aria-label="Avatar colour">
            <option value="">Auto (assigned from the name)</option>
            {palette.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
      </div>
      <label>
        Voice
        <select value={voice} onChange={(e) => setVoice(e.target.value)}>
          <option value="">Auto (assigned from the name)</option>
          {Object.entries(voices).map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <PersonaDials persona={persona} onChange={setPersona} />
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button onClick={() => onDone()}>Cancel</button>
        <button
          className="primary"
          disabled={busy || !name.trim() || !job.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              const created = await api.hire(name, job, how, model, {
                avatar_emoji: trimmedEmoji || undefined,
                avatar_color: avatarColor || undefined,
                voice: voice || undefined,
                persona,
              });
              setNote(
                created.online
                  ? `${created.display_name} is hired and ready.`
                  : created.activation ?? "",
              );
              onDone(created);
            } catch (e) {
              setNote(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          Hire
        </button>
      </div>
    </div>
  );
}

function ideFolderLabel(path?: string | null): string {
  if (!path) return "IDE";
  const bits = path.split("/").filter(Boolean);
  return bits.length ? `IDE · ${bits[bits.length - 1]}` : "IDE";
}

function WorkspacePicker({
  value,
  onChange,
  ideRoot,
  idePath,
  onIdePath,
  confirm,
  onConfirm,
  switching,
}: {
  value: RoomWorkspace;
  onChange: (next: RoomWorkspace) => void;
  ideRoot?: string | null;
  idePath: string;
  onIdePath: (next: string) => void;
  confirm: boolean;
  onConfirm: (ok: boolean) => void;
  switching?: boolean;
}) {
  const [listing, setListing] = useState<IdeFolderListing | null>(null);
  const [browseNote, setBrowseNote] = useState("");
  const browse = useCallback(
    async (path?: string) => {
      try {
        setBrowseNote("");
        setListing(await api.listIdeFolders(path));
      } catch (e) {
        setListing(null);
        setBrowseNote(String(e));
      }
    },
    [],
  );
  useEffect(() => {
    if (value !== "ide") {
      setListing(null);
      setBrowseNote("");
      return;
    }
    void browse(idePath || ideRoot || undefined);
    // Only when the mode flips — typing the path browses on blur.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, browse]);
  return (
    <div className="workspace-pick">
      <span className="field-label">Where should this room work?</span>
      <div className="mode-row">
        <button
          type="button"
          className={value === "sandbox" ? "pick picked" : "pick"}
          onClick={() => {
            onChange("sandbox");
            onConfirm(false);
          }}
        >
          Isolated container
        </button>
        <button
          type="button"
          className={value === "ide" ? "pick picked" : "pick"}
          onClick={() => {
            onChange("ide");
            if (!idePath && ideRoot) onIdePath(ideRoot);
          }}
        >
          This machine&apos;s IDE
        </button>
      </div>
      {value === "ide" && (
        <div className="ide-warn">
          <p>
            Agents in this room work in a container that <strong>bind-mounts a
            folder on this machine</strong> at <code>/workspace</code>. They can
            read and write that tree. Casual rooms should stay isolated.
          </p>
          <label>
            Folder on this machine
            <input
              value={idePath}
              onChange={(e) => onIdePath(e.target.value)}
              onBlur={() => {
                if (idePath.trim()) void browse(idePath.trim());
              }}
              placeholder={ideRoot || "/absolute/path/to/project"}
            />
          </label>
          {listing && (
            <div className="ide-browse">
              <div className="ide-browse-bar">
                {listing.parent && (
                  <button
                    type="button"
                    className="mini"
                    onClick={() => {
                      const up = listing.parent || "";
                      onIdePath(up);
                      void browse(up);
                    }}
                  >
                    Up
                  </button>
                )}
                <code className="ide-browse-path">{listing.path}</code>
              </div>
              <div className="ide-browse-list">
                {listing.folders.map((f) => (
                  <button
                    key={f.path}
                    type="button"
                    className={f.path === idePath ? "pick picked" : "pick"}
                    onClick={() => {
                      onIdePath(f.path);
                      void browse(f.path);
                    }}
                  >
                    {f.name}
                  </button>
                ))}
                {listing.folders.length === 0 && (
                  <span className="nav-sub">No subfolders</span>
                )}
              </div>
            </div>
          )}
          {browseNote && <p className="note">{browseNote}</p>}
          <label className="ide-confirm">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => onConfirm(e.target.checked)}
            />
            {switching
              ? "Mount this folder into this room"
              : "Create an IDE-attached room"}
          </label>
        </div>
      )}
    </div>
  );
}

function NewRoomPanel({
  agents,
  projects,
  projectId,
  ideRoot,
  onDone,
}: {
  agents: AgentMeta[];
  /** Project this room inherits — the sidebar's current selection. */
  projects: ProjectMeta[];
  projectId: string | null;
  ideRoot?: string | null;
  onDone: (created?: RoomMeta) => void;
}) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [lead, setLead] = useState<string>("");
  const [workspace, setWorkspace] = useState<RoomWorkspace>("sandbox");
  const [idePath, setIdePath] = useState(ideRoot || "");
  const [ideOk, setIdeOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const toggle = (slug: string) =>
    setPicked((p) => (p.includes(slug) ? p.filter((x) => x !== slug) : [...p, slug]));
  const ideBlocked = workspace === "ide" && !ideOk;
  return (
    <div className="panel">
      <h3>New room</h3>
      <p className="note">
        In project: {projectId ? (projects.find((p) => p.id === projectId)?.name ?? "?") : "Unfiled"}
      </p>
      <label>
        Room name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ops room" />
      </label>
      <WorkspacePicker
        value={workspace}
        onChange={setWorkspace}
        ideRoot={ideRoot}
        idePath={idePath}
        onIdePath={setIdePath}
        confirm={ideOk}
        onConfirm={setIdeOk}
      />
      <div className="member-pick">
        {agents
          .filter((a) => !a.archived)
          .map((a) => (
            <button
              key={a.slug}
              className={picked.includes(a.slug) ? "pick picked" : "pick"}
              onClick={() => toggle(a.slug)}
            >
              <AgentAvatar identity={a.identity} slug={a.slug} fallback={a.slug} label={a.slug} size={18} />
              @{a.slug}
            </button>
          ))}
        {agents.filter((a) => !a.archived).length === 0 && (
          <p className="note">No agents yet — hire one first.</p>
        )}
      </div>
      {picked.length > 0 && (
        <label>
          Lead (answers when nobody is mentioned)
          <select value={lead} onChange={(e) => setLead(e.target.value)}>
            <option value="">first member</option>
            {picked.map((m) => (
              <option key={m} value={m}>
                @{m}
              </option>
            ))}
          </select>
        </label>
      )}
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button onClick={() => onDone()}>Cancel</button>
        <button
          className="primary"
          disabled={busy || !name.trim() || picked.length === 0 || ideBlocked}
          onClick={async () => {
            setBusy(true);
            try {
              onDone(
                await api.createRoom(name, picked, lead || null, {
                  workspace,
                  ide_path: workspace === "ide" ? idePath.trim() || null : null,
                  project_id: projectId,
                }),
              );
            } catch (e) {
              setNote(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          {workspace === "ide" ? "Create IDE room" : "Create"}
        </button>
      </div>
    </div>
  );
}

function EditRoomPanel({
  room,
  agents,
  projects,
  ideRoot,
  onDone,
}: {
  room: RoomMeta;
  agents: AgentMeta[];
  projects: ProjectMeta[];
  ideRoot?: string | null;
  onDone: (updated?: RoomMeta) => void;
}) {
  const [name, setName] = useState(room.name);
  const [picked, setPicked] = useState<string[]>(room.members);
  const [lead, setLead] = useState<string>(room.lead ?? "");
  const [projectId, setProjectId] = useState(room.project_id ?? "");
  const [workspace, setWorkspace] = useState<RoomWorkspace>(room.workspace ?? "sandbox");
  const [idePath, setIdePath] = useState(room.ide_path || ideRoot || "");
  const [ideOk, setIdeOk] = useState((room.workspace ?? "sandbox") === "ide");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const switchingToIde = workspace === "ide" && (room.workspace ?? "sandbox") !== "ide";
  const ideBlocked = switchingToIde && !ideOk;
  const visible = agents.filter((a) => !a.archived || picked.includes(a.slug));
  const toggle = (slug: string) =>
    setPicked((p) => (p.includes(slug) ? p.filter((x) => x !== slug) : [...p, slug]));
  return (
    <div className="panel">
      <h3>Edit room</h3>
      <label>
        Room name
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <div className="member-pick">
        {visible.map((a) => (
          <button
            key={a.slug}
            className={picked.includes(a.slug) ? "pick picked" : "pick"}
            onClick={() => toggle(a.slug)}
          >
            <AgentAvatar identity={a.identity} slug={a.slug} fallback={a.slug} label={a.slug} size={18} />
            @{a.slug}
          </button>
        ))}
      </div>
      {picked.length > 0 && (
        <label>
          Lead (answers when nobody is mentioned)
          <select value={lead} onChange={(e) => setLead(e.target.value)}>
            <option value="">first member</option>
            {picked.map((m) => (
              <option key={m} value={m}>
                @{m}
              </option>
            ))}
          </select>
        </label>
      )}
      <label>
        Project
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">Unfiled</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <WorkspacePicker
        value={workspace}
        onChange={setWorkspace}
        ideRoot={ideRoot}
        idePath={idePath}
        onIdePath={setIdePath}
        confirm={ideOk}
        onConfirm={setIdeOk}
        switching
      />
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button onClick={() => onDone()}>Cancel</button>
        <button
          className="primary"
          disabled={busy || !name.trim() || picked.length === 0 || ideBlocked}
          onClick={async () => {
            setBusy(true);
            try {
              onDone(
                await api.patchRoom(room.id, {
                  name: name.trim(),
                  members: picked,
                  lead: lead || null,
                  workspace,
                  ide_path: workspace === "ide" ? idePath.trim() || null : null,
                  project_id: projectId || null,
                }),
              );
            } catch (e) {
              setNote(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function EditAgentPanel({
  agent,
  models,
  palette,
  voices,
  onDone,
}: {
  agent: AgentMeta;
  models: ModelPreset[];
  palette: string[];
  voices: Record<string, string>;
  onDone: (updated?: AgentMeta) => void;
}) {
  const [name, setName] = useState(agent.display_name || agent.slug);
  const [job, setJob] = useState(agent.job || "");
  const [how, setHow] = useState(agent.how || "");
  const [model, setModel] = useState(agent.model_preset || "");
  // Populate from the stored overrides directly (`avatar_emoji`,
  // `avatar_color`, `voice`) rather than inferring them from the resolved
  // values — the backend emits both, and the stored one is ground truth for
  // "is this actually overridden." null/"" means no override, i.e. Auto.
  const baseEmoji = agent.avatar_emoji ?? "";
  const baseColor = agent.avatar_color ?? "";
  const baseVoice = (agent.voice ?? "").toLowerCase();
  const [avatarEmoji, setAvatarEmoji] = useState(baseEmoji);
  const [avatarColor, setAvatarColor] = useState(baseColor);
  const [voice, setVoice] = useState(baseVoice);
  const [persona, setPersona] = useState<Persona>(agent.persona);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  return (
    <div className="panel">
      <h3>Edit @{agent.slug}</h3>
      <p className="note">Rewrites this profile&apos;s SOUL — the slug stays @{agent.slug}.</p>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Primary job
        <input value={job} onChange={(e) => setJob(e.target.value)} />
      </label>
      <label>
        How it should work
        <textarea value={how} onChange={(e) => setHow(e.target.value)} rows={4} />
      </label>
      {models.length > 0 && (
        <label>
          Model
          <select
            value={model}
            disabled={!!agent.busy}
            title={agent.busy ? "working — switch after this turn" : undefined}
            onChange={(e) => setModel(e.target.value)}
          >
            {!model && <option value="">current model</option>}
            {models.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name} ({m.summary})
              </option>
            ))}
          </select>
        </label>
      )}
      <div className="identity-pick">
        <span className="field-label">Look</span>
        <div className="identity-pick-row">
          <AgentAvatar
            identity={{
              emoji: avatarEmoji.trim() || null,
              initial: agent.identity.initial,
              color: avatarColor || agent.identity.color,
              color_source: avatarColor ? "override" : "derived",
            }}
            slug={agent.slug}
            fallback={name || agent.slug}
            size={40}
          />
          <input
            value={avatarEmoji}
            onChange={(e) => setAvatarEmoji(e.target.value)}
            placeholder="Emoji (optional)"
            maxLength={4}
            aria-label="Avatar emoji"
          />
          <select value={avatarColor} onChange={(e) => setAvatarColor(e.target.value)} aria-label="Avatar colour">
            <option value="">{`Auto (currently ${agent.identity.color})`}</option>
            {palette.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>
      </div>
      <label>
        Voice
        <select value={voice} onChange={(e) => setVoice(e.target.value)}>
          <option value="">{`Auto (currently sounds like ${voiceLabel(agent.voice_resolved, voices)})`}</option>
          {Object.entries(voices).map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <PersonaDials persona={persona} onChange={setPersona} />
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button onClick={() => onDone()}>Cancel</button>
        <button
          className="primary"
          disabled={busy || !name.trim() || !job.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              const body: AgentPatch = { name: name.trim(), job: job.trim(), how };
              if (model && model !== agent.model_preset) body.model = model;
              const trimmedEmoji = avatarEmoji.trim();
              if (trimmedEmoji !== baseEmoji) body.avatar_emoji = trimmedEmoji || null;
              if (avatarColor !== baseColor) body.avatar_color = avatarColor || null;
              if (voice !== baseVoice) body.voice = voice || null;
              if (
                persona.warmth !== agent.persona.warmth ||
                persona.verbosity !== agent.persona.verbosity ||
                persona.formality !== agent.persona.formality
              ) {
                body.persona = persona;
              }
              onDone(await api.patchAgent(agent.slug, body));
            } catch (e) {
              setNote(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}

function providerLabel(id: string | null | undefined): string {
  if (!id) return "provider";
  if (id === "xai-oauth" || id === "xai") return "Grok / xAI";
  if (id === "anthropic") return "Claude / Anthropic";
  if (id === "openai-codex") return "Codex / OpenAI";
  return id;
}

function needsReauth(status?: AuthStatus): boolean {
  return status === "relogin_required" || status === "missing";
}

function ReauthPanel({
  provider,
  onDone,
}: {
  provider: string;
  onDone: (ok: boolean) => void;
}) {
  const [session, setSession] = useState<ReauthSession | null>(null);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(true);
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  const start = useCallback(async () => {
    setStarting(true);
    setError("");
    try {
      setSession(await api.startReauth(provider));
    } catch (e) {
      setError(String(e));
      setSession(null);
    } finally {
      setStarting(false);
    }
  }, [provider]);

  useEffect(() => {
    void start();
  }, [start]);

  const sessionId = session?.session_id;
  const sessionStatus = session?.status;
  useEffect(() => {
    if (!sessionId || sessionStatus !== "pending") return;
    let cancelled = false;
    const tick = () => {
      api
        .reauthSession(sessionId)
        .then((next) => {
          if (cancelled) return;
          setSession(next);
          if (next.status === "approved") onDoneRef.current(true);
        })
        .catch((e) => {
          if (!cancelled) setError(String(e));
        });
    };
    tick();
    const t = window.setInterval(tick, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(t);
    };
  }, [sessionId, sessionStatus]);

  const url = session?.verification_url || session?.verification_uri || "";
  return (
    <div className="panel">
      <h3>Sign in with {providerLabel(provider)}</h3>
      <p className="note">
        This workspace needs its own {providerLabel(provider)} grant. Approve the
        device code in the browser — the gateway does not restart.
      </p>
      {starting && <p className="note">Starting sign-in…</p>}
      {session?.user_code && (
        <>
          <p className="reauth-code">{session.user_code}</p>
          {url && (
            <p>
              <a href={url} target="_blank" rel="noreferrer">
                Open {url.replace(/^https?:\/\//, "").split("/")[0]} to approve
              </a>
            </p>
          )}
          <p className="note">
            Waiting for approval
            {session.expires_in != null ? ` · ${session.expires_in}s left` : ""}.
          </p>
        </>
      )}
      {(error || session?.status === "error" || session?.status === "expired") && (
        <p className="auth-error">{error || session?.error || "Sign-in failed."}</p>
      )}
      {session?.status === "approved" && <p className="note">Signed in. Updating agents…</p>}
      <div className="panel-actions">
        <button onClick={() => onDone(false)}>Cancel</button>
        <button className="primary" onClick={() => void start()} disabled={starting}>
          {session?.status === "pending" ? "Restart" : "Sign in"}
        </button>
      </div>
    </div>
  );
}

function SettingsPanel({
  onReauth,
  onDone,
  onPrincipal,
  onDirtyChange,
}: {
  onReauth: (provider: string) => void;
  onDone: () => void;
  onPrincipal?: (p: Principal) => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const [accounts, setAccounts] = useState<Array<ProviderAuth & { login?: string }>>([]);
  const [voice, setVoice] = useState<VoiceStatus | null>(null);
  const [workspace, setWorkspace] = useState<WorkspaceStatus | null>(null);
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [youName, setYouName] = useState("");
  const [youAbout, setYouAbout] = useState("");
  // What the server has stored — the dirty comparison baseline. Updated on
  // load and again after a successful save.
  const [baseName, setBaseName] = useState("");
  const [baseAbout, setBaseAbout] = useState("");
  const [claudeKey, setClaudeKey] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    api
      .authStatus()
      .then((s) => setAccounts(s.accounts || s.providers || []))
      .catch(() => setAccounts([]));
    api.voiceStatus().then(setVoice).catch(() => setVoice(null));
    api.workspace().then(setWorkspace).catch(() => setWorkspace(null));
    api.listModels().then((r) => setModels(r.models)).catch(() => setModels([]));
    api
      .getPrincipal()
      .then((p) => {
        // An unset principal's display_name comes back as the literal "You" —
        // prefilling that is indistinguishable from a name someone chose, so
        // treat it as empty and let the placeholder carry it instead. Empty
        // is also the dirty baseline, so an untouched panel isn't flagged.
        const name = p.display_name === "You" ? "" : p.display_name || "";
        const about = p.about || "";
        setYouName(name);
        setYouAbout(about);
        setBaseName(name);
        setBaseAbout(about);
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    reload();
  }, [reload]);

  const dirty = youName !== baseName || youAbout !== baseAbout;
  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  const saveYou = async () => {
    const trimmed = youName.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      const saved = await api.savePrincipal({ display_name: trimmed, about: youAbout });
      onPrincipal?.(saved);
      setYouName(trimmed);
      setBaseName(trimmed);
      setBaseAbout(youAbout);
      setNote("Saved your name for this workspace.");
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  const confirmDiscard = () =>
    !dirty || window.confirm("Discard unsaved changes to your name and about you?");

  const handleDone = () => {
    if (confirmDiscard()) onDone();
  };

  return (
    <div className="panel settings-panel">
      <h3>Settings</h3>
      <p className="note">
        You, cloud logins, local workspace, and voice. Hired agents inherit the workspace grant.
      </p>
      <label>
        Your name
        <input
          value={youName}
          onChange={(e) => setYouName(e.target.value)}
          placeholder="Clayton"
        />
      </label>
      <label>
        About you
        <textarea
          value={youAbout}
          onChange={(e) => setYouAbout(e.target.value)}
          rows={3}
          placeholder="How retainers should address you, what you care about."
        />
      </label>
      <p className="note">This is you, not a hire. Retainers will use this name. You do not take turns.</p>
      <div className="settings-save-row">
        <button
          className={dirty ? "mini wide primary" : "mini wide"}
          disabled={!dirty || !youName.trim() || busy}
          onClick={saveYou}
        >
          Save you
        </button>
        {dirty && <span className="unsaved-badge">Unsaved changes</span>}
      </div>
      {accounts.map((acct) => (
        <div key={acct.id} className="settings-row">
          <div>
            <strong>{providerLabel(acct.id)}</strong>
            <div className="nav-sub">
              {acct.status}
              {acct.error ? ` · ${acct.error}` : ""}
            </div>
          </div>
          {acct.login === "api_key" || acct.id === "anthropic" ? (
            <div className="settings-key">
              <input
                type="password"
                placeholder="Anthropic API key"
                value={claudeKey}
                onChange={(e) => setClaudeKey(e.target.value)}
              />
              <button
                className="mini"
                disabled={!claudeKey.trim()}
                onClick={async () => {
                  try {
                    await api.saveApiKey("anthropic", claudeKey.trim());
                    setClaudeKey("");
                    setNote("Claude key saved in this workspace.");
                    reload();
                  } catch (e) {
                    setNote(String(e));
                  }
                }}
              >
                Save
              </button>
            </div>
          ) : (
            <button
              className="mini"
              onClick={() => {
                if (confirmDiscard()) onReauth(acct.id);
              }}
            >
              Sign in
            </button>
          )}
        </div>
      ))}
      <div className="settings-row">
        <div>
          <strong>Voice</strong>
          <div className="nav-sub">
            {voice ? `${voice.backend}${voice.ready ? "" : " (not ready)"}` : "…"}
            {voice?.detail ? ` · ${voice.detail}` : ""}
          </div>
        </div>
      </div>
      <div className="settings-row">
        <div>
          <strong>Workspace computer</strong>
          <div className="nav-sub">
            {workspace
              ? workspace.enabled
                ? `${workspace.running ? "up" : "idle"}${workspace.ide_root ? ` · IDE ${workspace.ide_root}` : ""}`
                : workspace.detail || "not enabled"
              : "…"}
          </div>
        </div>
      </div>
      <div className="settings-row">
        <div>
          <strong>Hire presets</strong>
          <div className="nav-sub">
            {models.length ? models.map((m) => m.name).join(", ") : "none yet"}
          </div>
        </div>
      </div>
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button className="primary" onClick={handleDone}>
          Done
        </button>
      </div>
    </div>
  );
}

function KeyPanel({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState(getApiKey());
  return (
    <div className="panel">
      <h3>API key required</h3>
      <p className="note">This Retinue server requires a bearer key (RETINUE_ROOMS_API_KEY).</p>
      <label>
        API key
        <input type="password" value={key} onChange={(e) => setKey(e.target.value)} />
      </label>
      <div className="panel-actions">
        <button
          className="primary"
          onClick={() => {
            setApiKey(key);
            onDone();
          }}
        >
          Save
        </button>
      </div>
    </div>
  );
}

// ── shell ────────────────────────────────────────────────────────────────

type Modal = "hire" | "room" | "key" | "edit-room" | "edit-agent" | "reauth" | "settings" | null;
type DragPayload = { list: "rooms" | "items"; id: string };
type DropHint = { list: "rooms" | "items"; id: string; place: "before" | "after" } | null;

export default function App() {
  const [rooms, setRooms] = useState<RoomMeta[]>([]);
  const [projects, setProjects] = useState<ProjectMeta[]>([]);
  // Distinguishes "haven't fetched projects yet" from "workspace genuinely
  // has none" — the reconciliation effect below must not treat the former
  // as proof the stored selection is stale (it would reset every reload).
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  // "" means the Unfiled pseudo-project. Sticky across reloads; validated
  // against the live project list once projects have loaded.
  const [selectedProjectId, setSelectedProjectId] = useState(
    () => localStorage.getItem(SELECTED_PROJECT_KEY) ?? "",
  );
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [palette, setPalette] = useState<string[]>([]);
  const [voices, setVoices] = useState<Record<string, string>>({});
  const [routineList, setRoutineList] = useState<RoutineMeta[]>([]);
  const [workspaceInfo, setWorkspaceInfo] = useState<WorkspaceStatus | null>(null);
  const [layout, setLayout] = useState<SidebarLayout>({ rooms: [], items: [] });
  const [current, setCurrent] = useState<RoomMeta | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [editingRoom, setEditingRoom] = useState<RoomMeta | null>(null);
  const [editingAgent, setEditingAgent] = useState<AgentMeta | null>(null);
  const [showArchived, setShowArchived] = useState(
    () => localStorage.getItem(ARCHIVED_KEY) === "1",
  );
  const [dropHint, setDropHint] = useState<DropHint>(null);
  const [userName, setUserName] = useState(
    () => localStorage.getItem("retinue.userName") ?? "You",
  );
  const [reauthProvider, setReauthProvider] = useState("xai-oauth");
  const [gitSha, setGitSha] = useState<string | null>(null);
  // Per-viewer display preference, not workspace data — lives in
  // localStorage, never synced through /sidebar (that's item order only).
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1",
  );
  const [settingsDirty, setSettingsDirty] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [r, p, a, m, rt, ws] = await Promise.all([
        api.listRooms(),
        api.listProjects(),
        api.listAgents(),
        api.listModels(),
        api.listRoutines(),
        api.workspace(),
      ]);
      setRooms(r.rooms);
      setProjects(p.projects);
      setProjectsLoaded(true);
      setAgents(a.agents);
      setModels(m.models);
      setRoutineList(rt.routines);
      setWorkspaceInfo(ws);
      // Rarely change; harmless to refresh alongside everything else, and
      // keeps the hire/edit panels' pickers off a running gateway rather
      // than a second hardcoded copy (CONTRACT-identity.md).
      api.getPalette().then((p) => setPalette(p.colors)).catch(() => {});
      api.voiceStatus().then((v) => setVoices(v.voices)).catch(() => {});
      try {
        setLayout(await api.getSidebar());
      } catch {
        setLayout(fallbackLayout(r.rooms, a.agents));
      }
      setCurrent((cur) => (cur ? (r.rooms.find((x) => x.id === cur.id) ?? null) : null));
    } catch (e) {
      if (e instanceof AuthRequiredError) setModal("key");
      else console.error(e);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    api
      .health()
      .then((h) => setGitSha(h.git_sha || "unknown"))
      .catch(() => setGitSha("unknown"));
  }, []);

  useEffect(() => {
    api
      .getPrincipal()
      .then((p) => {
        if (!p.display_name) return;
        setUserName(p.display_name);
        try {
          localStorage.setItem("retinue.userName", p.display_name);
        } catch {
          /* ignore */
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const t = window.setInterval(() => {
      api
        .listAgents()
        .then((d) => setAgents(d.agents))
        .catch(() => {});
    }, 2000);
    return () => window.clearInterval(t);
  }, []);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  // Ctrl+B / Cmd+B, the conventional sidebar toggle — but not while the user
  // is typing somewhere.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key.toLowerCase() !== "b" || !(e.ctrlKey || e.metaKey)) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) return;
      e.preventDefault();
      toggleSidebar();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleSidebar]);

  const persistLayout = useCallback(
    async (next: SidebarLayout) => {
      setLayout(next);
      try {
        setLayout(await api.putSidebar(next));
        await refresh();
      } catch (e) {
        alert(String(e));
        await refresh();
      }
    },
    [refresh],
  );

  // If the stored selection points at a project that no longer exists
  // (deleted elsewhere), fall back to Unfiled rather than showing an empty,
  // unselectable room list. Gated on projectsLoaded: `projects` starts as
  // `[]` before the first refresh() resolves, and running this check
  // against that placeholder would wipe a valid sticky selection on every
  // page load, before the real list ever arrives.
  useEffect(() => {
    if (projectsLoaded && selectedProjectId && !projects.some((p) => p.id === selectedProjectId)) {
      selectProject("");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectsLoaded, projects, selectedProjectId]);

  const selectProject = (id: string) => {
    setSelectedProjectId(id);
    try {
      localStorage.setItem(SELECTED_PROJECT_KEY, id);
    } catch {
      /* ignore */
    }
  };

  const visibleProjects = projects.filter((p) => showArchived || !p.archived);
  const visibleRooms = rooms
    .filter((r) => showArchived || !r.archived)
    .filter((r) => (r.project_id ?? "") === selectedProjectId);
  const agentsBySlug = Object.fromEntries(agents.map((a) => [a.slug, a]));
  const visibleCast = agents.filter((a) => !a.archived);

  const archiveRoom = async (room: RoomMeta) => {
    try {
      const updated = await api.patchRoom(room.id, { archived: !room.archived });
      if (current?.id === room.id) setCurrent(updated);
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const deleteRoom = async (room: RoomMeta) => {
    if (
      !window.confirm(
        `Delete room "${room.name}"? This removes the transcript. Archive instead if you want to keep it.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteRoom(room.id);
      if (current?.id === room.id) setCurrent(null);
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const addProject = async () => {
    const name = (window.prompt("Project name") || "").trim();
    if (!name) return;
    try {
      const created = await api.createProject(name);
      await refresh();
      selectProject(created.id);
    } catch (e) {
      alert(String(e));
    }
  };

  const renameProject = async (project: ProjectMeta) => {
    const name = (window.prompt("Rename project", project.name) || "").trim();
    if (!name || name === project.name) return;
    try {
      await api.patchProject(project.id, { name });
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const archiveProject = async (project: ProjectMeta) => {
    try {
      await api.patchProject(project.id, { archived: !project.archived });
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const deleteProject = async (project: ProjectMeta) => {
    if (
      !window.confirm(
        `Delete project "${project.name}"? Its rooms move to Unfiled — transcripts stay.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteProject(project.id);
      if (selectedProjectId === project.id) selectProject("");
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const archiveAgent = async (agent: AgentMeta) => {
    try {
      await api.patchAgent(agent.slug, { archived: !agent.archived });
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const deleteAgent = async (agent: AgentMeta) => {
    if (agent.slug === "default") {
      alert("The default profile cannot be deleted.");
      return;
    }
    if (
      !window.confirm(
        `Delete @${agent.slug}? This removes profiles/${agent.slug}/ and cannot be undone.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteAgent(agent.slug);
      await refresh();
    } catch (e) {
      alert(String(e));
    }
  };

  const addTeam = () => {
    const label = (window.prompt("Team name", "Development") || "").trim();
    if (!label) return;
    void persistLayout({
      ...layout,
      items: [...layout.items, { kind: "team", id: nextTeamId(label, layout.items), label }],
    });
  };

  const renameTeam = (id: string, currentLabel: string) => {
    const label = (window.prompt("Rename team", currentLabel) || "").trim();
    if (!label) return;
    void persistLayout({
      ...layout,
      items: layout.items.map((item) =>
        item.kind === "team" && item.id === id
          ? { ...item, id: nextTeamId(label, layout.items, id), label }
          : item,
      ),
    });
  };

  const deleteTeam = (id: string, label: string) => {
    if (!window.confirm(`Remove the "${label}" separator? Bots stay; they just ungroup.`)) return;
    void persistLayout({
      ...layout,
      items: layout.items.filter((item) => !(item.kind === "team" && item.id === id)),
    });
  };

  const parseDrag = (e: DragEvent): DragPayload | null => {
    const raw = e.dataTransfer.getData(DRAG_MIME) || e.dataTransfer.getData("text/plain");
    if (!raw) return null;
    try {
      return JSON.parse(raw) as DragPayload;
    } catch {
      return null;
    }
  };

  const placeFromEvent = (e: DragEvent): "before" | "after" => {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    return e.clientY > rect.top + rect.height / 2 ? "after" : "before";
  };

  const startDrag = (e: DragEvent, payload: DragPayload) => {
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(payload));
    e.dataTransfer.setData("text/plain", JSON.stringify(payload));
    e.dataTransfer.effectAllowed = "move";
  };

  const dropOnRoom = (e: DragEvent, targetId: string) => {
    e.preventDefault();
    setDropHint(null);
    const drag = parseDrag(e);
    if (!drag || drag.list !== "rooms" || drag.id === targetId) return;
    const from = layout.rooms.indexOf(drag.id);
    const to = layout.rooms.indexOf(targetId);
    if (from < 0 || to < 0) return;
    const insertAt = placeFromEvent(e) === "after" ? to + 1 : to;
    void persistLayout({ ...layout, rooms: relocate(layout.rooms, from, insertAt) });
  };

  const dropOnItem = (e: DragEvent, target: SidebarItem) => {
    e.preventDefault();
    setDropHint(null);
    const drag = parseDrag(e);
    if (!drag || drag.list !== "items") return;
    const from = layout.items.findIndex((item) => itemKey(item) === drag.id);
    const to = layout.items.findIndex((item) => itemKey(item) === itemKey(target));
    if (from < 0 || to < 0 || from === to) return;
    // Dropping a bot onto a team separator assigns it to that team.
    const ontoTeam = target.kind === "team" && drag.id.startsWith("agent:");
    const insertAt = ontoTeam || placeFromEvent(e) === "after" ? to + 1 : to;
    void persistLayout({ ...layout, items: relocate(layout.items, from, insertAt) });
  };

  // Keyboard reorder steps over what the user can SEE. Archived rooms and
  // agents keep their slot in the stored layout but render nothing, so
  // stepping through the stored order would swap a row past a hidden one and
  // paint no change — a control that looks actionable and does nothing, which
  // is the failure these buttons exist to remove. Swap with the nearest
  // VISIBLE neighbour instead, and derive the disabled ends from that same
  // list so "no neighbour" and "button disabled" can never disagree.
  const visibleRoomIds = visibleRooms.map((r) => r.id);
  const visibleItemKeys = layout.items
    .filter((item) => {
      if (item.kind === "team") return true;
      const a = agentsBySlug[item.slug];
      return !!a && (showArchived || !a.archived);
    })
    .map(itemKey);

  /** Swap *id* with its neighbour in *visible*, expressed in stored order. */
  const stepped = (order: string[], visible: string[], id: string, dir: -1 | 1) => {
    const target = visible[visible.indexOf(id) + dir];
    if (!visible.includes(id) || target === undefined) return null;
    const a = order.indexOf(id);
    const b = order.indexOf(target);
    if (a < 0 || b < 0) return null;
    const next = order.slice();
    next[a] = target;
    next[b] = id;
    return next;
  };

  const moveRoom = (roomId: string, dir: -1 | 1) => {
    const next = stepped(layout.rooms, visibleRoomIds, roomId, dir);
    if (next) void persistLayout({ ...layout, rooms: next });
  };

  const moveItem = (key: string, dir: -1 | 1) => {
    const keys = layout.items.map(itemKey);
    const next = stepped(keys, visibleItemKeys, key, dir);
    if (!next) return;
    const byKey = new Map(layout.items.map((item) => [itemKey(item), item]));
    void persistLayout({
      ...layout,
      items: next.map((k) => byKey.get(k)!).filter(Boolean),
    });
  };

  const broken = agents.filter((a) => !a.archived && needsReauth(a.auth_status));
  const openReauth = (provider = "xai-oauth") => {
    setReauthProvider(provider || "xai-oauth");
    setModal("reauth");
  };

  return (
    <div className="app-root">
      {broken.length > 0 && (
        <div className="auth-banner" role="status">
          <span>
            {providerLabel(broken[0].auth_provider)} sign-in required
            {broken.length > 1 ? ` · ${broken.length} agents` : ` · @${broken[0].slug}`}.
            Local members still work.
          </span>
          <button className="primary" onClick={() => openReauth(broken[0].auth_provider || "xai-oauth")}>
            Reauth
          </button>
        </div>
      )}
      <div className="shell">
      <aside className={sidebarCollapsed ? "sidebar collapsed" : "sidebar"}>
        {sidebarCollapsed ? (
          <nav className="rail" aria-label="Rooms">
            <button
              type="button"
              className="rail-toggle"
              aria-expanded={false}
              aria-label="Expand sidebar"
              title="Expand sidebar (Ctrl+B)"
              onClick={toggleSidebar}
            >
              <img className="rail-logo" src={LOGO_SRC} alt="" />
            </button>
            {visibleRooms.map((r) => (
              <RailButton
                key={r.id}
                room={r}
                active={current?.id === r.id}
                onClick={() => setCurrent(r)}
              />
            ))}
          </nav>
        ) : (
          <>
        <div className="brand">
          <img className="brand-logo" src={LOGO_SRC} alt="" />
          Retinue
          <button className="mini" style={{ marginLeft: "auto" }} onClick={() => setModal("settings")}>
            Settings
          </button>
        </div>
        <div className="section">
          <div className="section-head">
            <span>Projects</span>
            <button className="mini" onClick={() => void addProject()} title="New project">
              +
            </button>
          </div>
          <div className="nav-row">
            <button
              className={selectedProjectId === "" ? "nav-item active" : "nav-item"}
              onClick={() => selectProject("")}
            >
              Unfiled
            </button>
          </div>
          {visibleProjects.map((p) => (
            <div key={p.id} className={`nav-row${p.archived ? " archived" : ""}`}>
              <button
                className={selectedProjectId === p.id ? "nav-item active" : "nav-item"}
                onClick={() => selectProject(p.id)}
              >
                {p.name}
                {p.archived ? " (archived)" : ""}
              </button>
              <RowMenu
                actions={[
                  { label: "Rename", onClick: () => void renameProject(p) },
                  {
                    label: p.archived ? "Unarchive" : "Archive",
                    onClick: () => void archiveProject(p),
                  },
                  { label: "Delete", danger: true, onClick: () => void deleteProject(p) },
                ]}
              />
            </div>
          ))}
        </div>
        <div className="section">
          <div className="section-head">
            <span>Rooms</span>
            <button className="mini" onClick={() => setModal("room")} title="New room">
              +
            </button>
          </div>
          {visibleRooms.map((r) => {
            const hint = dropHint?.list === "rooms" && dropHint.id === r.id ? dropHint.place : "";
            const roomIdx = visibleRoomIds.indexOf(r.id);
            return (
              <div
                key={r.id}
                className={`nav-row${r.archived ? " archived" : ""}${hint ? ` drop-${hint}` : ""}`}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDropHint({ list: "rooms", id: r.id, place: placeFromEvent(e) });
                }}
                onDragLeave={() => setDropHint((h) => (h?.id === r.id ? null : h))}
                onDrop={(e) => dropOnRoom(e, r.id)}
              >
                <span
                  className="drag-handle"
                  title="Drag to reorder"
                  draggable
                  onDragStart={(e) => startDrag(e, { list: "rooms", id: r.id })}
                >
                  ⋮⋮
                </span>
                <ReorderButtons
                  what={`room ${r.name}`}
                  canUp={roomIdx > 0}
                  canDown={roomIdx >= 0 && roomIdx < visibleRoomIds.length - 1}
                  onUp={() => moveRoom(r.id, -1)}
                  onDown={() => moveRoom(r.id, 1)}
                />
                <button
                  className={current?.id === r.id ? "nav-item active" : "nav-item"}
                  onClick={() => setCurrent(r)}
                >
                  {r.name}
                  {r.archived ? " (archived)" : ""}
                  {(r.workspace ?? "sandbox") === "ide" && (
                    <span className="mode-badge" title={r.ide_path || ""}>
                      {ideFolderLabel(r.ide_path)}
                    </span>
                  )}
                  <span className="nav-sub nav-faces">
                    {r.members.map((m) => (
                      <AgentAvatar
                        key={m}
                        identity={agentsBySlug[m]?.identity}
                        slug={m}
                        fallback={m}
                        label={m}
                        size={18}
                        working={!!agentsBySlug[m]?.busy}
                      />
                    ))}
                  </span>
                </button>
                <RowMenu
                  actions={[
                    {
                      label: "Edit",
                      onClick: () => {
                        setEditingRoom(r);
                        setModal("edit-room");
                      },
                    },
                    {
                      label: r.archived ? "Unarchive" : "Archive",
                      onClick: () => void archiveRoom(r),
                    },
                    { label: "Delete", danger: true, onClick: () => void deleteRoom(r) },
                  ]}
                />
              </div>
            );
          })}
          {visibleRooms.length === 0 && (
            <p className="note pad">
              No rooms in{" "}
              {selectedProjectId
                ? (projects.find((p) => p.id === selectedProjectId)?.name ?? "this project")
                : "Unfiled"}
              .
            </p>
          )}
        </div>
        <div className="section">
          <div className="section-head">
            <span>Agents</span>
            <div className="head-actions">
              <button className="mini wide" onClick={addTeam} title="Add team separator">
                team
              </button>
              <button className="mini" onClick={() => setModal("hire")} title="Hire">
                +
              </button>
            </div>
          </div>
          {(() => {
            // Team affiliation follows sidebar position: an agent belongs to
            // the most recent team separator above it (or no team, before
            // the first one). Reuses `chipColor` — teams have no backend
            // identity of their own, only agents do.
            let openTeam: SidebarTeam | null = null;
            const teamOfSlug = new Map<string, SidebarTeam | null>();
            for (const it of layout.items) {
              if (it.kind === "team") {
                openTeam = it;
              } else {
                teamOfSlug.set(it.slug, openTeam);
              }
            }
            return layout.items.map((item) => {
            const visIdx = visibleItemKeys.indexOf(itemKey(item));
            if (item.kind === "team") {
              const hint =
                dropHint?.list === "items" && dropHint.id === itemKey(item) ? dropHint.place : "";
              return (
                <div
                  key={itemKey(item)}
                  className={`team-bar${hint ? ` drop-${hint}` : ""}`}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDropHint({
                      list: "items",
                      id: itemKey(item),
                      place: placeFromEvent(e),
                    });
                  }}
                  onDrop={(e) => dropOnItem(e, item)}
                >
                  <span
                    className="drag-handle"
                    title="Drag to reorder"
                    draggable
                    onDragStart={(e) => startDrag(e, { list: "items", id: itemKey(item) })}
                  >
                    ⋮⋮
                  </span>
                  <ReorderButtons
                    what={`team ${item.label}`}
                    canUp={visIdx > 0}
                    canDown={visIdx >= 0 && visIdx < visibleItemKeys.length - 1}
                    onUp={() => moveItem(itemKey(item), -1)}
                    onDown={() => moveItem(itemKey(item), 1)}
                  />
                  <span className="team-label">{item.label}</span>
                  <RowMenu
                    actions={[
                      { label: "Rename", onClick: () => renameTeam(item.id, item.label) },
                      {
                        label: "Remove",
                        danger: true,
                        onClick: () => deleteTeam(item.id, item.label),
                      },
                    ]}
                  />
                </div>
              );
            }
            const a = agentsBySlug[item.slug];
            if (!a || (!showArchived && a.archived)) return null;
            const hint =
              dropHint?.list === "items" && dropHint.id === itemKey(item) ? dropHint.place : "";
            const team = teamOfSlug.get(item.slug) ?? null;
            return (
              <div
                key={item.slug}
                className={`agent-row${a.archived ? " archived" : ""}${hint ? ` drop-${hint}` : ""}`}
                style={team ? { borderLeftColor: chipColor(team.id) } : undefined}
                title={team ? `Team: ${team.label}` : undefined}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDropHint({
                    list: "items",
                    id: itemKey(item),
                    place: placeFromEvent(e),
                  });
                }}
                onDrop={(e) => dropOnItem(e, item)}
              >
                <span
                  className="drag-handle"
                  title="Drag to reorder"
                  draggable
                  onDragStart={(e) => startDrag(e, { list: "items", id: itemKey(item) })}
                >
                  ⋮⋮
                </span>
                <ReorderButtons
                  what={`agent @${a.slug}`}
                  canUp={visIdx > 0}
                  canDown={visIdx >= 0 && visIdx < visibleItemKeys.length - 1}
                  onUp={() => moveItem(itemKey(item), -1)}
                  onDown={() => moveItem(itemKey(item), 1)}
                />
                <div className={`agent-item${needsReauth(a.auth_status) ? " needs-auth" : ""}`}>
                  <AgentAvatar
                    identity={a.identity}
                    slug={a.slug}
                    fallback={a.slug}
                    label={a.display_name || a.slug}
                    size={32}
                    working={!!a.busy}
                  />
                  <div className="agent-copy">
                    <span className="chip" style={{ color: identityColor(a.identity.color) }}>
                      @{a.slug}
                    </span>
                    <span className="nav-sub">
                      {a.job || "hand-made profile"}
                      {a.archived ? " · archived" : ""}
                      {a.model_preset
                        ? ` · ${a.model_preset}`
                        : a.model_summary
                          ? ` · ${a.model_summary}`
                          : ""}
                      {a.local_llm
                        ? ` · local · ${Math.round((a.turn_timeout ?? 1800) / 60)}m`
                        : a.turn_timeout
                          ? ` · cloud · ${Math.round(a.turn_timeout / 60)}m`
                          : ""}
                      {needsReauth(a.auth_status) ? " · reauth needed" : ""}
                    </span>
                    {models.length > 0 && (
                      <select
                        className="agent-model"
                        value={a.model_preset || ""}
                        disabled={!!a.busy}
                        title={
                          a.busy
                            ? "working — switch after this turn"
                            : a.model_summary || "switch model"
                        }
                        onChange={async (e) => {
                          const next = e.target.value;
                          if (!next || next === a.model_preset) return;
                          try {
                            await api.switchModel(a.slug, next);
                            await refresh();
                          } catch (err) {
                            alert(String(err));
                          }
                        }}
                      >
                        {!a.model_preset && <option value="">current model</option>}
                        {models.map((m) => (
                          <option key={m.name} value={m.name}>
                            {m.name}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                </div>
                <RowMenu
                  actions={[
                    ...(needsReauth(a.auth_status)
                      ? [
                          {
                            label: `Reauth ${providerLabel(a.auth_provider)}`,
                            onClick: () => openReauth(a.auth_provider || "xai-oauth"),
                          },
                        ]
                      : []),
                    {
                      label: "Edit",
                      onClick: () => {
                        setEditingAgent(a);
                        setModal("edit-agent");
                      },
                    },
                    {
                      label: a.archived ? "Unarchive" : "Archive",
                      onClick: () => void archiveAgent(a),
                    },
                    { label: "Delete", danger: true, onClick: () => void deleteAgent(a) },
                  ]}
                />
              </div>
            );
            });
          })()}
        </div>
        <label className="section-toggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => {
              const on = e.target.checked;
              setShowArchived(on);
              localStorage.setItem(ARCHIVED_KEY, on ? "1" : "0");
            }}
          />
          Show archived
        </label>
        <div className="section">
          <div className="section-head">
            <span>Routines</span>
          </div>
          {routineList.map((rt) => (
            <div key={rt.slug} className="agent-item">
              <span className="nav-sub">
                {rt.name} · {rt.messages.length} step{rt.messages.length === 1 ? "" : "s"}
              </span>
              <button
                className="mini"
                disabled={!current}
                onClick={async () => {
                  if (!current) return;
                  try {
                    await api.runRoutine(rt.slug, current.id);
                  } catch (e) {
                    alert(String(e));
                  }
                }}
              >
                Run
              </button>
            </div>
          ))}
          {routineList.length === 0 && (
            <p className="note pad">Save a room&apos;s prompts as a routine, then replay.</p>
          )}
        </div>
        <footer className="foot">
          {workspaceInfo?.enabled ? (
            <span>
              workspace {workspaceInfo.running ? "up" : "idle"}
              {workspaceInfo.container?.name ? ` · ${workspaceInfo.container.name}` : ""}
              {workspaceInfo.attach ? ` · ${workspaceInfo.attach}` : ""}
            </span>
          ) : (
            <span>self-hosted AI teammates</span>
          )}
          {" · "}
          <a href="https://github.com/novique-ai/retinue" target="_blank" rel="noreferrer">
            github
          </a>
          {gitSha && (
            <>
              {" · "}
              <span className="build-id" title="Retinue commit">
                {gitSha}
              </span>
            </>
          )}
        </footer>
          </>
        )}
      </aside>
      <main className="main">
        {current ? (
          <RoomView
            key={current.id}
            room={current}
            userName={userName}
            agents={agents}
            onEdit={() => {
              setEditingRoom(current);
              setModal("edit-room");
            }}
            onArchive={() => void archiveRoom(current)}
            onDelete={() => void deleteRoom(current)}
            onRoomUpdate={(updated) => {
              setCurrent(updated);
              void refresh();
            }}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={toggleSidebar}
          />
        ) : visibleCast.length === 0 ? (
          <div className="welcome empty-state">
            <img className="welcome-logo" src={LOGO_SRC} alt="Retinue" />
            <h1>Hire your first retainer</h1>
            <p className="note empty-state-copy">
              Give them a name, a job, and how they should work — that three-field brief is the
              whole hire.
            </p>
            <button className="primary empty-state-cta" onClick={() => setModal("hire")}>
              Hire an agent
            </button>
          </div>
        ) : visibleRooms.length === 0 ? (
          <div className="welcome empty-state">
            <img className="welcome-logo" src={LOGO_SRC} alt="Retinue" />
            <h1>Open a room</h1>
            <p className="note empty-state-copy">
              Put your retainers in a room, pick a lead, and start talking.
            </p>
            <div className="retinue-cast">
              <div className="cast-member principal">
                <UserAvatar label={userName} size={72} />
                <span>{userName}</span>
              </div>
              {visibleCast.map((a) => (
                <div key={a.slug} className="cast-member">
                  <AgentAvatar
                    identity={a.identity}
                    slug={a.slug}
                    fallback={a.slug}
                    label={a.display_name || a.slug}
                    size={72}
                    working={!!a.busy}
                  />
                  <span>@{a.slug}</span>
                </div>
              ))}
            </div>
            <button className="primary empty-state-cta" onClick={() => setModal("room")}>
              Create a room
            </button>
          </div>
        ) : (
          <div className="welcome">
            <img className="welcome-logo" src={LOGO_SRC} alt="Retinue" />
            <h1>Retinue</h1>
            <p>A suite of retainers in your service.</p>
            <p className="note">
              Hire them, put them in a room, and talk — they answer you and each other.
            </p>
            <div className="retinue-cast">
              <div className="cast-member principal">
                <UserAvatar label={userName} size={72} />
                <span>{userName}</span>
              </div>
              {visibleCast.map((a) => (
                <div key={a.slug} className="cast-member">
                  <AgentAvatar
                    identity={a.identity}
                    slug={a.slug}
                    fallback={a.slug}
                    label={a.display_name || a.slug}
                    size={72}
                    working={!!a.busy}
                  />
                  <span>@{a.slug}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </main>
      {modal && (
        <div
          className="overlay"
          onClick={() => {
            if (
              modal === "settings" &&
              settingsDirty &&
              !window.confirm("Discard unsaved changes to your name and about you?")
            ) {
              return;
            }
            setModal(null);
            setEditingRoom(null);
            setEditingAgent(null);
            setSettingsDirty(false);
          }}
        >
          <div onClick={(e) => e.stopPropagation()}>
            {modal === "hire" && (
              <HirePanel
                onDone={(created) => {
                  if (created) void refresh();
                  setModal(null);
                  if (created && created.online === false && created.activation) {
                    alert(created.activation);
                  }
                }}
              />
            )}
            {modal === "room" && (
              <NewRoomPanel
                agents={agents}
                projects={projects}
                projectId={selectedProjectId || null}
                ideRoot={workspaceInfo?.ide_root}
                onDone={(created) => {
                  setModal(null);
                  if (created) {
                    void refresh();
                    setCurrent(created);
                  }
                }}
              />
            )}
            {modal === "edit-room" && editingRoom && (
              <EditRoomPanel
                room={editingRoom}
                agents={agents}
                projects={projects}
                ideRoot={workspaceInfo?.ide_root}
                onDone={(updated) => {
                  setModal(null);
                  setEditingRoom(null);
                  if (updated) {
                    setCurrent(updated);
                    void refresh();
                  }
                }}
              />
            )}
            {modal === "edit-agent" && editingAgent && (
              <EditAgentPanel
                agent={editingAgent}
                models={models}
                palette={palette}
                voices={voices}
                onDone={(updated) => {
                  setModal(null);
                  setEditingAgent(null);
                  if (updated) void refresh();
                }}
              />
            )}
            {modal === "key" && (
              <KeyPanel
                onDone={() => {
                  setModal(null);
                  void refresh();
                }}
              />
            )}
            {modal === "reauth" && (
              <ReauthPanel
                provider={reauthProvider}
                onDone={(ok) => {
                  setModal(null);
                  if (ok) void refresh();
                }}
              />
            )}
            {modal === "settings" && (
              <SettingsPanel
                onReauth={(provider) => {
                  setReauthProvider(provider);
                  setModal("reauth");
                }}
                onPrincipal={(p) => {
                  setUserName(p.display_name);
                  try {
                    localStorage.setItem("retinue.userName", p.display_name);
                  } catch {
                    /* ignore */
                  }
                }}
                onDirtyChange={setSettingsDirty}
                onDone={() => {
                  setModal(null);
                  setSettingsDirty(false);
                  void refresh();
                }}
              />
            )}
          </div>
        </div>
      )}
      </div>
    </div>
  );
}
