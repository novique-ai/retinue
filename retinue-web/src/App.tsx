import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentMeta,
  api,
  AuthRequiredError,
  getApiKey,
  ModelPreset,
  RoomMeta,
  RoomMsg,
  RoutineMeta,
  setApiKey,
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

// ── message row ──────────────────────────────────────────────────────────

function MessageRow({ msg, userName }: { msg: RoomMsg; userName: string }) {
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
            {msg.speaker}
          </span>
        )}
        <div className="msg-text">{msg.text}</div>
      </div>
    </div>
  );
}

// ── room chat view ───────────────────────────────────────────────────────

function RoomView({ room, userName }: { room: RoomMeta; userName: string }) {
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
  const micRef = useRef<{ stop: () => Promise<Blob> } | null>(null);
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
        <h2>{room.name}</h2>
        <div className="room-members">
          {room.members.map((m) => (
            <span key={m} className="member-chip">
              <Avatar src={agentIcon(m)} label={m} size={24} />
              <span className="chip" style={{ color: chipColor(m) }}>
                @{m}
                {room.lead === m ? " ★" : ""}
              </span>
            </span>
          ))}
          <button
            className="mini"
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
      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && (
          <div className="empty-hint">
            Say something — no @mention goes to the lead{room.lead ? ` (@${room.lead})` : ""}.
          </div>
        )}
        {messages.map((m) => (
          <MessageRow key={m.seq} msg={m} userName={userName} />
        ))}
        {thinking.map((w) => (
          <div key={w} className="msg-row">
            <Avatar src={agentIcon(w)} label={w} size={32} />
            <div className="bubble thinking">
              <span className="chip" style={{ color: chipColor(w) }}>
                {w}
              </span>
              <div className="msg-text dots">thinking</div>
            </div>
          </div>
        ))}
      </div>
      <div className="composer">
        <div className="mention-bar">
          {room.members.map((m) => (
            <button key={m} className="mention-btn" onClick={() => setDraft((d) => `${d}@${m} `)}>
              <Avatar src={agentIcon(m)} label={m} size={18} />
              @{m}
            </button>
          ))}
        </div>
        <div className="composer-row">
          <textarea
            value={draft}
            placeholder={`Message ${room.name}…`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={2}
          />
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

function NewRoomPanel({
  agents,
  onDone,
}: {
  agents: AgentMeta[];
  onDone: (created?: RoomMeta) => void;
}) {
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [lead, setLead] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const toggle = (slug: string) =>
    setPicked((p) => (p.includes(slug) ? p.filter((x) => x !== slug) : [...p, slug]));
  return (
    <div className="panel">
      <h3>New room</h3>
      <label>
        Room name
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Ops room" />
      </label>
      <div className="member-pick">
        {agents.map((a) => (
          <button
            key={a.slug}
            className={picked.includes(a.slug) ? "pick picked" : "pick"}
            onClick={() => toggle(a.slug)}
          >
            <Avatar src={agentIcon(a.slug)} label={a.slug} size={18} />
            @{a.slug}
          </button>
        ))}
        {agents.length === 0 && <p className="note">No agents yet — hire one first.</p>}
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
          disabled={busy || !name.trim() || picked.length === 0}
          onClick={async () => {
            setBusy(true);
            try {
              onDone(await api.createRoom(name, picked, lead || null));
            } catch (e) {
              setNote(String(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          Create
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

type Modal = "hire" | "room" | "key" | null;

export default function App() {
  const [rooms, setRooms] = useState<RoomMeta[]>([]);
  const [agents, setAgents] = useState<AgentMeta[]>([]);
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [routineList, setRoutineList] = useState<RoutineMeta[]>([]);
  const [workspaceInfo, setWorkspaceInfo] = useState<WorkspaceStatus | null>(null);
  const [current, setCurrent] = useState<RoomMeta | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [userName] = useState(() => localStorage.getItem("retinue.userName") ?? "You");

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
    } catch (e) {
      if (e instanceof AuthRequiredError) setModal("key");
      else console.error(e);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-logo" src={LOGO_SRC} alt="" />
          Retinue
        </div>
        <div className="section">
          <div className="section-head">
            <span>Rooms</span>
            <button className="mini" onClick={() => setModal("room")}>
              +
            </button>
          </div>
          {rooms.map((r) => (
            <button
              key={r.id}
              className={current?.id === r.id ? "nav-item active" : "nav-item"}
              onClick={() => setCurrent(r)}
            >
              {r.name}
              <span className="nav-sub nav-faces">
                {r.members.map((m) => (
                  <Avatar key={m} src={agentIcon(m)} label={m} size={18} />
                ))}
              </span>
            </button>
          ))}
          {rooms.length === 0 && <p className="note pad">No rooms yet.</p>}
        </div>
        <div className="section">
          <div className="section-head">
            <span>Agents</span>
            <button className="mini" onClick={() => setModal("hire")}>
              +
            </button>
          </div>
          {agents.map((a) => (
            <div key={a.slug} className="agent-item">
              <Avatar src={agentIcon(a.slug)} label={a.display_name || a.slug} size={32} />
              <div className="agent-copy">
                <span className="chip" style={{ color: chipColor(a.slug) }}>
                  @{a.slug}
                </span>
                <span className="nav-sub">
                  {a.job || "hand-made profile"}
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
                </span>
                {models.length > 0 && (
                  <select
                    className="agent-model"
                    value={a.model_preset || ""}
                    title={a.model_summary || "switch model"}
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
          ))}
        </div>
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
          <RoomView room={current} userName={userName} />
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
              {agents.map((a) => (
                <div key={a.slug} className="cast-member">
                  <Avatar src={agentIcon(a.slug)} label={a.display_name || a.slug} size={72} />
                  <span>@{a.slug}</span>
                </div>
              ))}
            </div>
            {agents.length === 0 && (
              <p className="note">No retainers hired yet — use + beside Agents.</p>
            )}
          </div>
        )}
      </main>
      {modal && (
        <div className="overlay" onClick={() => setModal(null)}>
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
                onDone={(created) => {
                  setModal(null);
                  if (created) {
                    void refresh();
                    setCurrent(created);
                  }
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
          </div>
        </div>
      )}
    </div>
  );
}
