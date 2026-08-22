import { useEffect, useState } from "react";

/**
 * Phone / small-tablet breakpoint. Keep in lockstep with the
 * `@media (max-width: 720px)` block in styles.css (grep NARROW_BREAKPOINT).
 *
 * At this width the sidebar MUST be an overlay drawer, never a docked
 * 292px flex column. See isNarrowViewport / shellClassName.
 */
export const NARROW_BREAKPOINT = 720;

export const NARROW_MEDIA = `(max-width: ${NARROW_BREAKPOINT}px)`;

/** True at 375×812 (iPhone-class) and any viewport up to the breakpoint. */
export function isNarrowViewport(widthPx: number): boolean {
  return Number.isFinite(widthPx) && widthPx <= NARROW_BREAKPOINT;
}

/**
 * Class contract for the app shell.
 *
 * - desktop: `"shell"` — docked sidebar, optional `.collapsed` lives on the aside
 * - narrow, drawer closed: `"shell narrow"` — CSS media query + this class
 *   take the sidebar out of flow (`position: fixed` + off-canvas)
 * - narrow, drawer open: `"shell narrow drawer-open"`
 *
 * `drawerOpen` is ignored on desktop so a stale flag cannot restyle the
 * three-pane layout.
 */
export function shellClassName(opts: { narrow: boolean; drawerOpen: boolean }): string {
  if (!opts.narrow) return "shell";
  return opts.drawerOpen ? "shell narrow drawer-open" : "shell narrow";
}

/**
 * Insets smaller than this are treated as 0 so collapsing browser chrome
 * (URL bar show/hide) does not thrash layout. Real soft keyboards are larger.
 */
export const KEYBOARD_INSET_MIN_PX = 80;

export interface ViewportGeometry {
  height: number;
  offsetTop: number;
}

/**
 * Height (px) of the layout viewport currently obscured by the soft keyboard.
 * `inset = layoutHeight - vv.height - vv.offsetTop`.
 */
export function computeKeyboardInset(
  viewport: ViewportGeometry | null | undefined,
  layoutHeightPx: number,
): number {
  if (!viewport || !Number.isFinite(layoutHeightPx) || layoutHeightPx <= 0) {
    return 0;
  }
  const { height, offsetTop } = viewport;
  if (!Number.isFinite(height) || !Number.isFinite(offsetTop)) return 0;
  const inset = Math.round(layoutHeightPx - height - offsetTop);
  return inset >= KEYBOARD_INSET_MIN_PX ? inset : 0;
}

export function useNarrowViewport(): boolean {
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined" && window.matchMedia(NARROW_MEDIA).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(NARROW_MEDIA);
    const onChange = () => setNarrow(mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return narrow;
}

/**
 * Writes `--keyboard-inset` on :root so the shell can shrink above the
 * on-screen keyboard (iOS Safari / Android Chrome visualViewport).
 */
export function useKeyboardInset(): void {
  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      const vv = window.visualViewport;
      const inset = computeKeyboardInset(
        vv ? { height: vv.height, offsetTop: vv.offsetTop } : null,
        window.innerHeight,
      );
      root.style.setProperty("--keyboard-inset", `${inset}px`);
      if (inset > 0) window.scrollTo(0, 0);
    };
    apply();
    const vv = window.visualViewport;
    vv?.addEventListener("resize", apply);
    vv?.addEventListener("scroll", apply);
    window.addEventListener("resize", apply);
    return () => {
      vv?.removeEventListener("resize", apply);
      vv?.removeEventListener("scroll", apply);
      window.removeEventListener("resize", apply);
      root.style.removeProperty("--keyboard-inset");
    };
  }, []);
}
