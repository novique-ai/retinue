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
  const sinceRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  // SSE transcript stream; long-poll is the fallback (see api.watchTranscript).
  useEffect(() => {
    sinceRef.current = 0;
    setMessages([]);
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
          <button className="send-btn" disabled={sending || !draft.trim()} onClick={() => void send()}>
            Send
          </button>
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
  const [routineList, setRoutineList] = useState<RoutineMeta[]>([]);
  const [workspaceInfo, setWorkspaceInfo] = useState<WorkspaceStatus | null>(null);
  const [current, setCurrent] = useState<RoomMeta | null>(null);
  const [modal, setModal] = useState<Modal>(null);
  const [userName] = useState(() => localStorage.getItem("retinue.userName") ?? "You");

  const refresh = useCallback(async () => {
    try {
      const [r, a, rt, ws] = await Promise.all([
        api.listRooms(),
        api.listAgents(),
        api.listRoutines(),
        api.workspace(),
      ]);
      setRooms(r.rooms);
      setAgents(a.agents);
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
                  {a.local_llm
                    ? ` · local · ${Math.round((a.turn_timeout ?? 1800) / 60)}m`
                    : a.turn_timeout
                      ? ` · cloud · ${Math.round(a.turn_timeout / 60)}m`
                      : ""}
                </span>
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
