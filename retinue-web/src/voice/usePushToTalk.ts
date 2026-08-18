import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import {
  DEFAULT_PTT_KEY,
  PushToTalkController,
  type PttGestureResult,
  type PttKeyBinding,
  type PttReleaseInfo,
} from "./ptt";

export type UsePushToTalkOptions = {
  disabled?: boolean;
  /** Override the PTT key later without rewriting the gesture layer. Default: Space. */
  pttKey?: PttKeyBinding;
  onEngage: () => Promise<void> | void;
  onRelease: (info: PttReleaseInfo) => Promise<void> | void;
  onError?: (error: unknown) => void;
};

export type PushToTalkBind = {
  onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onPointerCancel: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onLostPointerCapture: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onContextMenu: (event: React.MouseEvent<HTMLButtonElement>) => void;
  onDragStart: (event: React.DragEvent<HTMLButtonElement>) => void;
};

function shouldPreventDefault(result: PttGestureResult): boolean {
  return result === "start" || result === "stop" || result === "consume";
}

/**
 * Pointer + keyboard wiring for {@link PushToTalkController}.
 *
 * Capture, contextmenu suppression, and the PTT-only CSS in `styles.css`
 * exist so Android does not steal a hold as a browser long-press. Keep
 * them if you restyle the button.
 */
export function usePushToTalk(options: UsePushToTalkOptions): {
  active: boolean;
  bind: PushToTalkBind;
} {
  const [active, setActive] = useState(false);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const controllerRef = useRef<PushToTalkController | null>(null);
  if (controllerRef.current === null) {
    controllerRef.current = new PushToTalkController(
      {
        start: () => optionsRef.current.onEngage(),
        stop: (info) => optionsRef.current.onRelease(info),
        isBusy: () => optionsRef.current.disabled === true,
        onActiveChange: setActive,
        onError: (error) => optionsRef.current.onError?.(error),
      },
      options.pttKey ?? DEFAULT_PTT_KEY,
    );
  }
  const controller = controllerRef.current;
  controller.setKeyBinding(options.pttKey ?? DEFAULT_PTT_KEY);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const result = controller.handleKeyDown(event);
      if (shouldPreventDefault(result)) event.preventDefault();
    };
    const onKeyUp = (event: KeyboardEvent) => {
      const result = controller.handleKeyUp(event);
      if (shouldPreventDefault(result)) event.preventDefault();
    };
    const onBlur = () => {
      controller.handleBlur();
    };
    const onVisibility = () => {
      if (document.hidden) controller.handleVisibilityHidden();
    };
    // Window-level pointer fallback if setPointerCapture is missing or throws.
    const onWinPointerUp = (event: PointerEvent) => {
      controller.handlePointerUp(event);
    };
    const onWinPointerCancel = (event: PointerEvent) => {
      controller.handlePointerCancel(event);
    };

    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pointerup", onWinPointerUp);
    window.addEventListener("pointercancel", onWinPointerCancel);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      window.removeEventListener("keyup", onKeyUp, true);
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pointerup", onWinPointerUp);
      window.removeEventListener("pointercancel", onWinPointerCancel);
      controller.dispose();
    };
  }, [controller]);

  const onPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      const result = controller.handlePointerDown(event);
      if (result === "ignore") return;
      event.preventDefault();
      event.stopPropagation();
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        // Capture is best-effort. Window pointerup / pointercancel still stop us.
      }
    },
    [controller],
  );

  const onPointerUp = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      controller.handlePointerUp(event);
    },
    [controller],
  );

  const onPointerCancel = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      controller.handlePointerCancel(event);
    },
    [controller],
  );

  const onLostPointerCapture = useCallback(
    (event: ReactPointerEvent<HTMLButtonElement>) => {
      controller.handleLostPointerCapture(event);
    },
    [controller],
  );

  const onContextMenu = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    // Button-only: the rest of Retinue keeps the normal browser menu.
    event.preventDefault();
  }, []);

  const onDragStart = useCallback((event: React.DragEvent<HTMLButtonElement>) => {
    event.preventDefault();
  }, []);

  return {
    active,
    bind: {
      onPointerDown,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture,
      onContextMenu,
      onDragStart,
    },
  };
}
