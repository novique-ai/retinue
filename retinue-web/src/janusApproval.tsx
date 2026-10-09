import { useEffect, useState, type ReactNode } from "react";
import { api, type JanusApprovalDetail, type RoomMsg } from "./api";

const PREFIX = "Janus approval request ";
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{3,127}$/;

/** Gateway-origin Janus line. A spoofed speaker or a different prefix is not a card. */
export function janusApprovalId(msg: Pick<RoomMsg, "kind" | "speaker" | "text">): string | null {
  if (msg.kind !== "agent" || msg.speaker !== "janus") return null;
  if (!msg.text.startsWith(PREFIX)) return null;
  const rest = msg.text.slice(PREFIX.length);
  const dot = rest.indexOf(".");
  const id = (dot === -1 ? rest : rest.slice(0, dot)).trim();
  return ID_RE.test(id) ? id : null;
}

export function janusApprovalElement(msg: RoomMsg, roomId: string): ReactNode {
  const approvalId = janusApprovalId(msg);
  if (!approvalId) return null;
  return <JanusApprovalPanel roomId={roomId} approvalId={approvalId} />;
}

function exactText(value: unknown): string {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return "";
  }
}

/** Present-but-unreadable expiry fails closed. Absent expiry trusts status. */
export function expiryPast(value: unknown, now = Date.now()): boolean {
  if (value == null || value === "") return false;
  if (typeof value === "boolean") return true;
  if (typeof value === "number") {
    const ms = value > 10_000_000_000 ? value : value * 1000;
    return ms <= now;
  }
  const text = String(value).trim();
  if (!text) return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return expiryPast(Number(text), now);
  const parsed = Date.parse(text);
  if (Number.isNaN(parsed)) return true;
  return parsed <= now;
}

function actionable(detail: JanusApprovalDetail): boolean {
  const status = (detail.status || "").toLowerCase();
  return (status === "pending" || status === "needs_confirmation") && !expiryPast(detail.expires_at);
}

export function JanusApprovalCard({
  detail,
  busy = false,
  error = null,
  onDecide,
}: {
  detail: JanusApprovalDetail;
  busy?: boolean;
  error?: string | null;
  onDecide: (decision: "approve" | "deny") => void;
}) {
  const status = (detail.status || "").toLowerCase();
  const open = actionable(detail);
  const approved = status === "approved" || status === "approve";
  const denied = status === "denied" || status === "deny";
  return (
    <div className="janus-approval" data-testid="janus-approval">
      <div className="mini wide">Janus confirmation</div>
      <div>Status: {status}</div>
      <div>Capability: {detail.capability_id ?? ""}</div>
      {detail.identity ? <div>Identity: {detail.identity}</div> : null}
      <div>Reason: {detail.reason ?? ""}</div>
      <div>Env:</div>
      <pre className="janus-exact">{exactText(detail.env)}</pre>
      <div>Arguments:</div>
      <pre className="janus-exact">{exactText(detail.arguments)}</pre>
      <div>Expires: {detail.expires_at == null ? "" : String(detail.expires_at)}</div>
      <p>Approving does not run the call. It runs only when the agent retries it after you approve.</p>
      {approved ? <p>Approved. The call does not run until the agent retries it.</p> : null}
      {denied ? <p>Denied. The call stays blocked.</p> : null}
      <div className="janus-actions">
        <button
          type="button"
          className="mini wide"
          data-testid="janus-approve"
          disabled={!open || busy}
          onClick={() => onDecide("approve")}
        >
          Approve
        </button>
        <button
          type="button"
          className="mini wide danger-btn"
          data-testid="janus-deny"
          disabled={!open || busy}
          onClick={() => onDecide("deny")}
        >
          Deny
        </button>
      </div>
      {error ? <div data-testid="janus-error">{error}</div> : null}
    </div>
  );
}

export function JanusApprovalPanel({ roomId, approvalId }: { roomId: string; approvalId: string }) {
  const [detail, setDetail] = useState<JanusApprovalDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.getJanusApproval(roomId, approvalId).then(
      (next) => {
        if (!cancelled) setDetail(next);
      },
      (err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "could not load approval");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [roomId, approvalId]);

  async function onDecide(decision: "approve" | "deny") {
    setBusy(true);
    setError(null);
    try {
      const result = await api.decideJanusApproval(roomId, approvalId, decision);
      setDetail((prev) => (prev ? { ...prev, status: result.status } : prev));
    } catch (err) {
      setError(err instanceof Error ? err.message : "decision failed");
    } finally {
      setBusy(false);
    }
  }

  if (!detail) {
    return (
      <div className="janus-approval" data-testid="janus-approval">
        {error ?? "Loading approval…"}
      </div>
    );
  }
  return <JanusApprovalCard detail={detail} busy={busy} error={error} onDecide={(d) => void onDecide(d)} />;
}
