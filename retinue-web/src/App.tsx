import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type ReactNode,
} from "react";
import {
  AgentMeta,
  AgentPatch,
  api,
  AuthRequiredError,
  AuthStatus,
  getApiKey,
  workspaceFileUrl,
  ModelPreset,
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
  VoiceStatus,
  WorkspaceStatus,
} from "./api";
import { LOGO_SRC, YOU_SRC, agentIcon, speakerIcon } from "./icons";

function Avatar({
  src,
  label,
  size = 28,
}: {
  src: string;
  label?: string;
  size?: number;
}) {
  return (
    <img
      className="avatar"
      src={src}
      alt={label ?? ""}
      title={label}
      width={size}
      height={size}
      draggable={false}
    />
  );
}

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

// ── message row ──────────────────────────────────────────────────────────

function MentionBody({
  text,
  members,
  handleOf,
}: {
  text: string;
  members: string[];
  handleOf: (slug: string) => string;
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
        style={{ color: chipColor(slug) }}
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
}: {
  msg: RoomMsg;
  userName: string;
  members: string[];
  handleOf: (slug: string) => string;
  roomId: string;
}) {
  if (msg.kind === "system") {
    return <div className="msg-system">— {msg.text} —</div>;
  }
  const mine = msg.kind === "user";
  return (
    <div className={mine ? "msg-row mine" : "msg-row"}>
      <Avatar
        src={speakerIcon(msg.speaker, userName)}
        label={mine ? userName : msg.speaker}
        size={32}
      />
      <div className={mine ? "bubble mine" : "bubble"}>
        {!mine && (
          <span className="chip" style={{ color: chipColor(msg.speaker) }}>
            {handleOf(msg.speaker) !== msg.speaker ? handleOf(msg.speaker) : msg.speaker}
          </span>
        )}
        <div className="msg-text">
          <MentionBody text={msg.text} members={members} handleOf={handleOf} />
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
}: {
  room: RoomMeta;
  userName: string;
  agents: AgentMeta[];
  onEdit: () => void;
  onArchive: () => void;
  onDelete: () => void;
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
  const sinceRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const micRef = useRef<{ stop: () => Promise<Blob> } | null>(null);
  const [caret, setCaret] = useState(0);
  const [mentionPick, setMentionPick] = useState(0);
  const [mentionOff, setMentionOff] = useState(false);
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

  // SSE transcript stream; long-poll is the fallback (see api.watchTranscript).
  useEffect(() => {
    sinceRef.current = 0;
    setMessages([]);
    spokenRef.current = new Set();
    openedAtRef.current = Date.now() / 1000;
    const ctl = new AbortController();
    api.watchTranscript(
      room.id,
      0,
      (fresh) => {
        sinceRef.current = Math.max(sinceRef.current, ...fresh.map((m) => m.seq));
        setMessages((prev) => [...prev, ...fresh]);
        setThinking((waiting) =>
          waiting.filter((w) => !fresh.some((m) => m.kind === "agent" && m.speaker === w)),
        );
      },
      ctl.signal,
    );
    return () => ctl.abort();
  }, [room.id]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, thinking]);

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

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const { planned } = await api.send(room.id, text, userName);
      setThinking(planned);
      setDraft("");
    } catch (e) {
      alert(String(e));
    } finally {
      setSending(false);
    }
  }, [draft, sending, room.id, userName]);

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
      setThinking(planned);
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
              <span className="mode-badge">IDE</span>
            )}
          </h2>
          <div className="room-members">
            {room.members.map((m) => (
              <span key={m} className="member-chip">
                <Avatar src={agentIcon(m)} label={handleOf(m)} size={24} />
                <span className="chip" style={{ color: chipColor(m) }}>
                  @{handleOf(m)}
                  {room.lead === m ? " ★" : ""}
                </span>
              </span>
            ))}
          </div>
        </div>
        <div className="room-header-actions">
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
          />
        ))}
        {thinking[0] && (
          <div key={thinking[0]} className="msg-row">
            <Avatar src={agentIcon(thinking[0])} label={handleOf(thinking[0])} size={32} />
            <div className="bubble thinking">
              <span className="chip" style={{ color: chipColor(thinking[0]) }}>
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
      <div className="composer">
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
              <Avatar src={agentIcon(m)} label={handleOf(m)} size={18} />
              @{handleOf(m)}
            </button>
          ))}
        </div>
        <div className="composer-row">
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
                    <Avatar src={agentIcon(m)} label={handleOf(m)} size={16} />
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
          <button className="send-btn" disabled={sending || !draft.trim()} onClick={() => void send()}>
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

  const persist = useCallback(
    async (next: Itinerary) => {
      setPlan(next);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => {
        void api
          .putItinerary(roomId, {
            title: next.title,
            summary: next.summary,
            items: next.items,
            updated_by: "user",
          })
          .then((saved) => setPlan(saved))
          .catch((e) => setNote(String(e)));
      }, 280);
    },
    [roomId],
  );

  useEffect(() => {
    let cancelled = false;
    api
      .getItinerary(roomId)
      .then((got) => {
        if (!cancelled) setPlan(got);
      })
      .catch((e) => {
        if (!cancelled) setNote(String(e));
      });
    return () => {
      cancelled = true;
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

function HirePanel({ onDone }: { onDone: (created?: AgentMeta) => void }) {
  const [name, setName] = useState("");
  const [job, setJob] = useState("");
  const [how, setHow] = useState("");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  useEffect(() => {
    api
      .listModels()
      .then((r) => setModels(r.models))
      .catch(() => setModels([]));
  }, []);
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
      {models.length > 0 && (
        <label>
          Model
          <select value={model} onChange={(e) => setModel(e.target.value)}>
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
      )}
      {note && <p className="note">{note}</p>}
      <div className="panel-actions">
        <button onClick={() => onDone()}>Cancel</button>
        <button
          className="primary"
          disabled={busy || !name.trim() || !job.trim()}
          onClick={async () => {
            setBusy(true);
            try {
              const created = await api.hire(name, job, how, model);
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

function WorkspacePicker({
  value,
  onChange,
  ideRoot,
  confirm,
  onConfirm,
  switching,
}: {
  value: RoomWorkspace;
  onChange: (next: RoomWorkspace) => void;
  ideRoot?: string | null;
  confirm: boolean;
  onConfirm: (ok: boolean) => void;
  switching?: boolean;
}) {
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
          onClick={() => onChange("ide")}
        >
          This machine&apos;s IDE
        </button>
      </div>
      {value === "ide" && (
        <div className="ide-warn">
          <p>
            Agents in this room work in a container that <strong>bind-mounts this
            machine&apos;s IDE</strong>
            {ideRoot ? (
              <>
                {" "}
                (<code>{ideRoot}</code>)
              </>
            ) : (
              <>
                {" "}
                (set <code>RETINUE_IDE_ROOT</code> on the gateway)
              </>
            )}
            . They can read and write that tree. Casual rooms should stay isolated.
          </p>
          <label className="ide-confirm">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => onConfirm(e.target.checked)}
            />
            {switching
              ? "Mount this machine’s IDE into this room"
              : "Create an IDE-attached room"}
          </label>
        </div>
      )}
    </div>
  );
}

function NewRoomPanel({
  agents,
  ideRoot,
  onDone,
}: {
  agents: AgentMeta[];
  ideRoot?: string | null;
  onDone: (created?: RoomMeta) => void;
}) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [lead, setLead] = useState<string>("");
  const [workspace, setWorkspace] = useState<RoomWorkspace>("sandbox");
  const [ideOk, setIdeOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const toggle = (slug: string) =>
    setPicked((p) => (p.includes(slug) ? p.filter((x) => x !== slug) : [...p, slug]));
  const ideBlocked = workspace === "ide" && !ideOk;
  return (
    <div className="panel">
      <h3>New room</h3>
      <label>
        Room name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ops room" />
      </label>
      <WorkspacePicker
        value={workspace}
        onChange={setWorkspace}
        ideRoot={ideRoot}
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
              <Avatar src={agentIcon(a.slug)} label={a.slug} size={18} />
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
                await api.createRoom(name, picked, lead || null, { workspace }),
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
  ideRoot,
  onDone,
}: {
  room: RoomMeta;
  agents: AgentMeta[];
  ideRoot?: string | null;
  onDone: (updated?: RoomMeta) => void;
}) {
  const [name, setName] = useState(room.name);
  const [picked, setPicked] = useState<string[]>(room.members);
  const [lead, setLead] = useState<string>(room.lead ?? "");
  const [workspace, setWorkspace] = useState<RoomWorkspace>(room.workspace ?? "sandbox");
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
            <Avatar src={agentIcon(a.slug)} label={a.slug} size={18} />
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
      <WorkspacePicker
        value={workspace}
        onChange={setWorkspace}
        ideRoot={ideRoot ?? room.ide_path}
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
  onDone,
}: {
  agent: AgentMeta;
  models: ModelPreset[];
  onDone: (updated?: AgentMeta) => void;
}) {
  const [name, setName] = useState(agent.display_name || agent.slug);
  const [job, setJob] = useState(agent.job || "");
  const [how, setHow] = useState(agent.how || "");
  const [model, setModel] = useState(agent.model_preset || "");
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
  if (id === "xai-oauth" || id === "xai") return "xAI";
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

type Modal = "hire" | "room" | "key" | "edit-room" | "edit-agent" | "reauth" | null;
type DragPayload = { list: "rooms" | "items"; id: string };
type DropHint = { list: "rooms" | "items"; id: string; place: "before" | "after" } | null;

export default function App() {
  const [rooms, setRooms] = useState<RoomMeta[]>([]);
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [models, setModels] = useState<ModelPreset[]>([]);
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
  const [userName] = useState(() => localStorage.getItem("retinue.userName") ?? "You");
  const [reauthProvider, setReauthProvider] = useState("xai-oauth");

  const refresh = useCallback(async () => {
    try {
      const [r, a, m, rt, ws] = await Promise.all([
        api.listRooms(),
        api.listAgents(),
        api.listModels(),
        api.listRoutines(),
        api.workspace(),
      ]);
      setRooms(r.rooms);
      setAgents(a.agents);
      setModels(m.models);
      setRoutineList(rt.routines);
      setWorkspaceInfo(ws);
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
    const t = window.setInterval(() => {
      api
        .listAgents()
        .then((d) => setAgents(d.agents))
        .catch(() => {});
    }, 2000);
    return () => window.clearInterval(t);
  }, []);

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

  const visibleRooms = rooms.filter((r) => showArchived || !r.archived);
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
    const label = (window.prompt("Team name", "Cloud staff") || "").trim();
    if (!label) return;
    void persistLayout({
      ...layout,
      items: [...layout.items, { kind: "team", id: `team-${Date.now()}`, label }],
    });
  };

  const renameTeam = (id: string, currentLabel: string) => {
    const label = (window.prompt("Rename team", currentLabel) || "").trim();
    if (!label) return;
    void persistLayout({
      ...layout,
      items: layout.items.map((item) =>
        item.kind === "team" && item.id === id ? { ...item, label } : item,
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
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-logo" src={LOGO_SRC} alt="" />
          Retinue
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
                <button
                  className={current?.id === r.id ? "nav-item active" : "nav-item"}
                  onClick={() => setCurrent(r)}
                >
                  {r.name}
                  {r.archived ? " (archived)" : ""}
                  {(r.workspace ?? "sandbox") === "ide" && (
                    <span className="mode-badge">IDE</span>
                  )}
                  <span className="nav-sub nav-faces">
                    {r.members.map((m) => (
                      <Avatar key={m} src={agentIcon(m)} label={m} size={18} />
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
          {visibleRooms.length === 0 && <p className="note pad">No rooms yet.</p>}
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
          {layout.items.map((item) => {
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
            return (
              <div
                key={item.slug}
                className={`agent-row${a.archived ? " archived" : ""}${hint ? ` drop-${hint}` : ""}`}
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
                <div className={`agent-item${needsReauth(a.auth_status) ? " needs-auth" : ""}`}>
                  <Avatar src={agentIcon(a.slug)} label={a.display_name || a.slug} size={32} />
                  <div className="agent-copy">
                    <span className="chip" style={{ color: chipColor(a.slug) }}>
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
          })}
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
        </footer>
      </aside>
      <main className="main">
        {current ? (
          <RoomView
            room={current}
            userName={userName}
            agents={agents}
            onEdit={() => {
              setEditingRoom(current);
              setModal("edit-room");
            }}
            onArchive={() => void archiveRoom(current)}
            onDelete={() => void deleteRoom(current)}
          />
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
                <Avatar src={YOU_SRC} label="You" size={72} />
                <span>You</span>
              </div>
              {visibleCast.map((a) => (
                <div key={a.slug} className="cast-member">
                  <Avatar src={agentIcon(a.slug)} label={a.display_name || a.slug} size={72} />
                  <span>@{a.slug}</span>
                </div>
              ))}
            </div>
            {visibleCast.length === 0 && (
              <p className="note">No retainers hired yet — use + beside Agents.</p>
            )}
          </div>
        )}
      </main>
      {modal && (
        <div
          className="overlay"
          onClick={() => {
            setModal(null);
            setEditingRoom(null);
            setEditingAgent(null);
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
          </div>
        </div>
      )}
      </div>
    </div>
  );
}
