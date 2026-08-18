import { api } from "./api";
import type { CronJobRow } from "./api";
import type { ReactElement } from "react";

export type CronScheduleMode = "once" | "every" | "cron";

export type CronFormState = {
  jobId: string | null;
  owner: string;
  name: string;
  room: string;
  skill: string;
  prompt: string;
  enabled: boolean;
  mode: CronScheduleMode;
  at: string;
  every: string;
  unit: "m" | "h" | "d";
  expr: string;
};

export function scheduleFromForm(form: CronFormState): string {
  if (form.mode === "once") return form.at;
  if (form.mode === "every") return `every ${form.every}${form.unit}`;
  return form.expr;
}

export function formFromRow(row: CronJobRow, fallbackOwner: string): CronFormState {
  const kind = String(row.schedule?.kind || "");
  return {
    jobId: row.id,
    owner: row.owner || fallbackOwner,
    name: row.name,
    room: row.room ?? "",
    skill: row.skill ?? "",
    prompt: row.prompt ?? "",
    enabled: row.enabled,
    mode: kind === "interval" ? "every" : kind === "cron" ? "cron" : "once",
    at: kind === "once" ? row.schedule_input : "",
    every: kind === "interval" ? String(row.schedule.minutes ?? "") : "1",
    unit: "m",
    expr: kind === "cron" ? row.schedule_input : "",
  };
}

export function blankCronForm(owner: string): CronFormState {
  return {
    jobId: null,
    owner,
    name: "",
    room: "",
    skill: "",
    prompt: "",
    enabled: true,
    mode: "every",
    at: "",
    every: "1",
    unit: "d",
    expr: "0 9 * * *",
  };
}

export function fmtRunAt(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export type CronAction = {
  key: "pause" | "resume" | "run" | "delete";
  label: string;
  call: (id: string) => Promise<unknown>;
  confirm?: string;
};

export const CRON_ACTIONS: CronAction[] = [
  { key: "pause", label: "Pause", call: (id) => api.pauseCronJob(id) },
  { key: "resume", label: "Resume", call: (id) => api.resumeCronJob(id) },
  { key: "run", label: "Run now", call: (id) => api.runCronJob(id) },
  {
    key: "delete",
    label: "Delete",
    call: (id) => api.deleteCronJob(id),
    confirm: "Delete this scheduled job?",
  },
];

const CRON_ACTION_TESTIDS: Record<CronAction["key"], string> = {
  pause: "cron-action-pause",
  resume: "cron-action-resume",
  run: "cron-action-run",
  delete: "cron-action-delete",
};

export function actionsForJob(job: CronJobRow): CronAction[] {
  return CRON_ACTIONS.filter((action) => {
    if (action.key === "pause") return job.state !== "paused";
    if (action.key === "resume") return job.state === "paused";
    return true;
  });
}

export async function submitCronJob(f: CronFormState): Promise<CronJobRow> {
  const body: Record<string, unknown> = {
    name: f.name,
    skill: f.skill,
    prompt: f.prompt,
    schedule: scheduleFromForm(f),
    enabled: f.enabled,
  };
  if (f.room) body.room = f.room;
  if (f.jobId) return api.patchCronJob(f.jobId, body);
  return api.createCronJob({ ...body, owner: f.owner, room: f.room });
}

export type SaveRoutineFormState = {
  name: string;
  room: string;
  owner: string;
  scheduled: boolean;
  mode: CronScheduleMode;
  at: string;
  every: string;
  unit: "m" | "h" | "d";
  expr: string;
};

export async function submitSaveRoutine(form: SaveRoutineFormState) {
  return api.saveRoutine(form.name, form.room, {
    owner: form.owner,
    schedule: form.scheduled
      ? scheduleFromForm(form as unknown as CronFormState)
      : undefined,
  });
}

export function ScheduledSection(props: {
  jobs: CronJobRow[];
  owners: string[];
  rooms: { id: string; name: string }[];
  timezone: string;
  filterOwner: string;
  filterRoom: string;
  onFilterOwner: (value: string) => void;
  onFilterRoom: (value: string) => void;
  onEdit: (job: CronJobRow | null) => void;
  onChanged: () => void;
}): ReactElement {
  const visible = props.jobs.filter(
    (job) =>
      (!props.filterOwner || job.owner === props.filterOwner) &&
      (!props.filterRoom || job.room === props.filterRoom),
  );
  return (
    <div className="section cron-section" data-testid="cron-section">
      <div className="section-head">
        <span>Scheduled</span>
        <button className="mini" data-testid="cron-new" onClick={() => props.onEdit(null)}>
          New scheduled job
        </button>
      </div>
      <div className="cron-filters">
        <select
          data-testid="cron-filter-owner"
          value={props.filterOwner}
          onChange={(event) => props.onFilterOwner(event.target.value)}
        >
          <option value="">All retainers</option>
          {props.owners.map((owner) => (
            <option key={owner} value={owner}>{owner}</option>
          ))}
        </select>
        <select
          data-testid="cron-filter-room"
          value={props.filterRoom}
          onChange={(event) => props.onFilterRoom(event.target.value)}
        >
          <option value="">All rooms</option>
          {props.rooms.map((room) => (
            <option key={room.id} value={room.id}>{room.name}</option>
          ))}
        </select>
      </div>
      {visible.map((job) => {
        const error = job.last_error || job.last_delivery_error || job.registration_error;
        return (
          <div className="cron-row" data-testid="cron-row" data-job-id={job.id} key={`${job.owner}:${job.id}`}>
            <div className="cron-row-head">
              <strong data-testid="cron-name">{job.name}</strong>
              <span data-testid="cron-kind" className="cron-kind">{job.kind}</span>
            </div>
            <div className="nav-sub">
              <span data-testid="cron-owner">{job.owner}</span>
              {" · "}<span data-testid="cron-room">{job.room_name || job.room || "—"}</span>
              {" · "}<span data-testid="cron-state">{job.state}</span>
            </div>
            <div className="nav-sub" data-testid="cron-schedule">{job.schedule_display}</div>
            <div className="nav-sub">Next: <span data-testid="cron-next-run">{fmtRunAt(job.next_run_at)}</span></div>
            <div className="nav-sub">Last: <span data-testid="cron-last-run">{fmtRunAt(job.last_run_at)}</span></div>
            {error && <div className="note cron-error" data-testid="cron-error">{error}</div>}
            <div className="cron-actions">
              <button className="mini" data-testid="cron-edit" onClick={() => props.onEdit(job)}>Edit</button>
              {actionsForJob(job).map((action) => (
                <button
                  className="mini"
                  data-testid={CRON_ACTION_TESTIDS[action.key]}
                  key={action.key}
                  onClick={async () => {
                    if (action.confirm && !window.confirm(action.confirm)) return;
                    try { await action.call(job.id); } catch (error) { alert(String(error)); }
                    props.onChanged();
                  }}
                >
                  {action.label}
                </button>
              ))}
            </div>
          </div>
        );
      })}
      {visible.length === 0 && <p className="note pad">No scheduled jobs yet.</p>}
      <p className="note pad" data-testid="cron-timezone">
        Times are in {props.timezone || "local"}. Run now queues the job for the next tick.
      </p>
    </div>
  );
}

function ScheduleFields(props: {
  mode: CronScheduleMode;
  at: string;
  every: string;
  unit: "m" | "h" | "d";
  expr: string;
  onChange: (values: Partial<Pick<CronFormState, "mode" | "at" | "every" | "unit" | "expr">>) => void;
}) {
  return (
    <>
      <label>
        Schedule type
        <select data-testid="cron-form-mode" value={props.mode} onChange={(event) => props.onChange({ mode: event.target.value as CronScheduleMode })}>
          <option value="once">Once</option>
          <option value="every">Every</option>
          <option value="cron">Cron</option>
        </select>
      </label>
      {props.mode === "once" && <label>Run at<input type="datetime-local" value={props.at} onChange={(event) => props.onChange({ at: event.target.value })} /></label>}
      {props.mode === "every" && (
        <label>Interval<span className="cron-interval"><input type="number" min="1" value={props.every} onChange={(event) => props.onChange({ every: event.target.value })} /><select value={props.unit} onChange={(event) => props.onChange({ unit: event.target.value as "m" | "h" | "d" })}><option value="m">minutes</option><option value="h">hours</option><option value="d">days</option></select></span></label>
      )}
      {props.mode === "cron" && <label>Cron expression<input value={props.expr} onChange={(event) => props.onChange({ expr: event.target.value })} /></label>}
    </>
  );
}

export function CronJobModal(props: {
  form: CronFormState;
  owners: string[];
  rooms: { id: string; name: string }[];
  timezone: string;
  onChange: (form: CronFormState) => void;
  onClose: () => void;
  onSaved: () => void;
}): ReactElement {
  const submitDisabled =
    props.form.jobId === null &&
    (!props.form.room || (!props.form.skill && !props.form.prompt));
  return (
    <form
      className="panel cron-panel"
      data-testid="cron-form"
      onSubmit={async (event) => {
        event.preventDefault();
        try { await submitCronJob(props.form); } catch (error) { alert(String(error)); return; }
        props.onSaved();
        props.onClose();
      }}
    >
      <h3>{props.form.jobId ? "Edit scheduled job" : "New scheduled job"}</h3>
      <label>Retainer<select data-testid="cron-form-owner" disabled={props.form.jobId !== null} value={props.form.owner} onChange={(event) => props.onChange({ ...props.form, owner: event.target.value })}>{props.owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}</select></label>
      <label>Name<input value={props.form.name} onChange={(event) => props.onChange({ ...props.form, name: event.target.value })} /></label>
      <ScheduleFields {...props.form} onChange={(values) => props.onChange({ ...props.form, ...values })} />
      <label>Destination room<select data-testid="cron-form-room" value={props.form.room} onChange={(event) => props.onChange({ ...props.form, room: event.target.value })}><option value="">— none (leave the destination unchanged) —</option>{props.rooms.map((room) => <option key={room.id} value={room.id}>{room.name}</option>)}</select></label>
      <label>Skill<input value={props.form.skill} onChange={(event) => props.onChange({ ...props.form, skill: event.target.value })} /></label>
      <label>Prompt<textarea value={props.form.prompt} onChange={(event) => props.onChange({ ...props.form, prompt: event.target.value })} /></label>
      <label className="section-toggle"><input type="checkbox" checked={props.form.enabled} onChange={(event) => props.onChange({ ...props.form, enabled: event.target.checked })} />Enabled</label>
      <p className="note">Times are in {props.timezone || "local"}.</p>
      <p className="note">Editing a routine does not rewrite its SKILL.md draft.</p>
      <p className="note">A job created outside Retinue has no room — leave the destination unset to keep it that way.</p>
      <p className="note">Clearing the prompt or the skill clears it on the job.</p>
      <div className="panel-actions"><button type="button" onClick={props.onClose}>Cancel</button><button className="primary" type="submit" data-testid="cron-form-submit" disabled={submitDisabled}>Save</button></div>
    </form>
  );
}

export function SaveRoutineModal(props: {
  form: SaveRoutineFormState;
  owners: string[];
  timezone: string;
  onChange: (form: SaveRoutineFormState) => void;
  onClose: () => void;
  onSaved: () => void;
}): ReactElement {
  return (
    <form
      className="panel cron-panel"
      data-testid="save-routine-form"
      onSubmit={async (event) => {
        event.preventDefault();
        try { await submitSaveRoutine(props.form); } catch (error) { alert(String(error)); return; }
        props.onSaved();
        props.onClose();
      }}
    >
      <h3>Save routine</h3>
      <label>Name<input required value={props.form.name} onChange={(event) => props.onChange({ ...props.form, name: event.target.value })} /></label>
      <label>Retainer<select data-testid="save-routine-owner" value={props.form.owner} onChange={(event) => props.onChange({ ...props.form, owner: event.target.value })}>{props.owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}</select></label>
      <label className="section-toggle"><input data-testid="save-routine-scheduled" type="checkbox" checked={props.form.scheduled} onChange={(event) => props.onChange({ ...props.form, scheduled: event.target.checked })} />Schedule this routine</label>
      {props.form.scheduled && <ScheduleFields {...props.form} onChange={(values) => props.onChange({ ...props.form, ...values })} />}
      <p className="note">Leave scheduling off to save the skill draft without a clock. Times are in {props.timezone || "local"}.</p>
      <div className="panel-actions"><button type="button" onClick={props.onClose}>Cancel</button><button className="primary" type="submit">Save routine</button></div>
    </form>
  );
}
