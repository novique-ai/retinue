import type { PushToTalkBind } from "./usePushToTalk";

/**
 * Physical-radio PTT surface. Styling lives on `.talk-btn` in `styles.css`
 * (`touch-action`, user-select, touch-callout). Do not drop those rules
 * when restyling — they stop Android from treating a hold as a long-press.
 */
export function PushToTalkButton({
  active,
  disabled,
  title,
  bind,
}: {
  active: boolean;
  disabled?: boolean;
  title?: string;
  bind: PushToTalkBind;
}) {
  return (
    <button
      type="button"
      className={active ? "talk-btn holding" : "talk-btn"}
      disabled={disabled}
      title={title}
      aria-pressed={active}
      aria-label="Hold to talk"
      draggable={false}
      {...bind}
    >
      {active ? "Listening…" : "Hold to talk"}
    </button>
  );
}
