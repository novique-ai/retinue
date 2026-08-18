/**
 * Cross-platform push-to-talk input controller.
 *
 * The special pointer handling here is intentional. Android Chrome and
 * Samsung Internet treat a long-press on a button as a *page* gesture and
 * open the browser menu (Back / Forward / Reload / Download) unless this
 * control opts out of selection, touch-callout, and contextmenu, and unless
 * we capture the pointer so a slide-off still delivers the release.
 *
 * Do not remove:
 *   - Pointer Events (`pointerdown` / `pointerup` / `pointercancel` /
 *     `lostpointercapture`) as the canonical input path
 *   - `setPointerCapture` on engage
 *   - `contextmenu` preventDefault on the PTT surface only
 *   - `touch-action: none`, `user-select: none`, `-webkit-user-select`,
 *     `-webkit-touch-callout` on the PTT surface only
 *
 * Those are the Android long-press fix, not polish. Keyboard Space (when
 * not typing) is a second trigger on the same session, not a parallel
 * audio path. Future triggers (configurable key, headset, gamepad) should
 * call `engageFrom` / `releaseFrom` with a new source name.
 */

export type PttKeyBinding = {
  /** `KeyboardEvent.code`, e.g. `"Space"`. Prefer `code` over `key` so layouts stay stable. */
  code: string;
};

/** Default PTT key. Swap this (or pass a binding into the hook) to rebind later. */
export const DEFAULT_PTT_KEY: PttKeyBinding = { code: "Space" };

export type PointerLike = {
  pointerId: number;
  pointerType?: string;
  button: number;
};

export type KeyboardLike = {
  code: string;
  repeat: boolean;
  target?: EventTarget | null;
};

export type PttReleaseInfo = {
  /**
   * True when audio `start()` finished and we were transmitting.
   * False when the gesture ended before the mic was ready (discard).
   */
  committed: boolean;
};

export type PushToTalkHooks = {
  /** Begin audio capture. May be async (`getUserMedia`). */
  start: () => Promise<void> | void;
  /** End audio capture. `committed` means a full session, not an aborted start. */
  stop: (info: PttReleaseInfo) => Promise<void> | void;
  /** When true, a new engage is refused (e.g. a send already in flight). */
  isBusy?: () => boolean;
  onActiveChange?: (active: boolean) => void;
  onError?: (error: unknown) => void;
};

export type PttGestureResult = "start" | "stop" | "consume" | "ignore";

const TYPING_SELECTOR =
  "input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox']";

/**
 * True when Space (or another PTT key) must be left to the focused field.
 * Duck-typed so unit tests can pass a fake target without a DOM.
 */
export function isTypingTarget(target: EventTarget | null | undefined): boolean {
  if (!target || typeof target !== "object") return false;
  const el = target as Partial<Element> & {
    isContentEditable?: boolean;
    tagName?: string;
  };
  if (typeof el.closest === "function") {
    try {
      return Boolean(el.closest(TYPING_SELECTOR));
    } catch {
      // ignore malformed fakes
    }
  }
  const tag = (el.tagName || "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (el.isContentEditable) return true;
  return false;
}

/** Left-button / primary contact only. Right- and middle-click must not start PTT. */
export function isPrimaryPointerDown(event: PointerLike): boolean {
  return event.button === 0;
}

export function matchesPttKey(event: KeyboardLike, binding: PttKeyBinding): boolean {
  return event.code === binding.code;
}

export class PushToTalkController {
  private readonly hooks: PushToTalkHooks;
  private binding: PttKeyBinding;
  private sources = new Set<string>();
  private pointerId: number | null = null;
  private engaging = false;
  private active = false;
  private runId = 0;

  constructor(hooks: PushToTalkHooks, binding: PttKeyBinding = DEFAULT_PTT_KEY) {
    this.hooks = hooks;
    this.binding = binding;
  }

  setKeyBinding(binding: PttKeyBinding): void {
    this.binding = binding;
  }

  isActive(): boolean {
    return this.active || this.engaging;
  }

  /** Named source (pointer, keyboard, or a future hardware trigger). */
  engageFrom(source: string): PttGestureResult {
    if (this.hooks.isBusy?.()) return "ignore";
    const first = this.sources.size === 0;
    this.sources.add(source);
    if (first) void this.begin();
    return first ? "start" : "consume";
  }

  releaseFrom(source: string): PttGestureResult {
    if (!this.sources.has(source)) return "ignore";
    this.sources.delete(source);
    if (source === "pointer") this.pointerId = null;
    if (this.sources.size === 0) {
      void this.finish();
      return "stop";
    }
    return "consume";
  }

  /**
   * Fail-safe: drop every source and stop. Used for pointercancel,
   * lostpointercapture, blur, hidden document, and unmount.
   */
  abort(): PttGestureResult {
    if (!this.engaging && !this.active && this.sources.size === 0) return "ignore";
    this.sources.clear();
    this.pointerId = null;
    void this.finish();
    return "stop";
  }

  handlePointerDown(event: PointerLike): PttGestureResult {
    if (!isPrimaryPointerDown(event)) return "ignore";
    if (this.pointerId !== null) return "ignore";
    if (this.hooks.isBusy?.()) return "ignore";
    this.pointerId = event.pointerId;
    return this.engageFrom("pointer");
  }

  handlePointerUp(event: PointerLike): PttGestureResult {
    if (this.pointerId !== event.pointerId) return "ignore";
    return this.releaseFrom("pointer");
  }

  handlePointerCancel(event: PointerLike): PttGestureResult {
    if (this.pointerId !== null && this.pointerId !== event.pointerId) return "ignore";
    return this.abort();
  }

  handleLostPointerCapture(event: PointerLike): PttGestureResult {
    if (this.pointerId !== null && this.pointerId !== event.pointerId) return "ignore";
    return this.abort();
  }

  handleKeyDown(event: KeyboardLike): PttGestureResult {
    if (!matchesPttKey(event, this.binding)) return "ignore";
    if (isTypingTarget(event.target)) return "ignore";
    if (event.repeat) {
      return this.sources.has("keyboard") ? "consume" : "ignore";
    }
    if (this.hooks.isBusy?.()) return "ignore";
    if (this.sources.has("keyboard")) return "consume";
    return this.engageFrom("keyboard");
  }

  handleKeyUp(event: KeyboardLike): PttGestureResult {
    if (!matchesPttKey(event, this.binding)) return "ignore";
    return this.releaseFrom("keyboard");
  }

  handleBlur(): PttGestureResult {
    return this.abort();
  }

  handleVisibilityHidden(): PttGestureResult {
    return this.abort();
  }

  dispose(): void {
    this.abort();
  }

  private async begin(): Promise<void> {
    if (this.engaging || this.active) return;
    this.engaging = true;
    this.hooks.onActiveChange?.(true);
    const id = ++this.runId;
    try {
      await this.hooks.start();
      if (id !== this.runId) {
        try {
          await this.hooks.stop({ committed: false });
        } catch (error) {
          this.hooks.onError?.(error);
        }
        return;
      }
      this.engaging = false;
      this.active = true;
    } catch (error) {
      if (id !== this.runId) return;
      this.engaging = false;
      this.active = false;
      this.sources.clear();
      this.pointerId = null;
      this.hooks.onActiveChange?.(false);
      this.hooks.onError?.(error);
    }
  }

  private async finish(): Promise<void> {
    if (!this.engaging && !this.active) return;
    const committed = this.active;
    this.engaging = false;
    this.active = false;
    this.sources.clear();
    this.pointerId = null;
    this.runId += 1;
    this.hooks.onActiveChange?.(false);
    if (!committed) return;
    try {
      await this.hooks.stop({ committed: true });
    } catch (error) {
      this.hooks.onError?.(error);
    }
  }
}
