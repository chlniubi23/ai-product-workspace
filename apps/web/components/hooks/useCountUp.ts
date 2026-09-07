"use client";

/**
 * Batch 27: pure-display count-up for numeric UI values.
 *
 * requestAnimationFrame + ease-out cubic; re-targets from the currently shown
 * value when the target changes. Honors prefers-reduced-motion by returning
 * the target directly (no raf loop at all).
 */

import { useEffect, useRef, useState } from "react";

const easeOutCubic = (t: number): number => 1 - Math.pow(1 - t, 3);

export function useCountUp(target: number, duration = 600): number {
  const [value, setValue] = useState(target);
  const fromRef = useRef(target);

  useEffect(() => {
    if (!Number.isFinite(target)) return;
    const from = fromRef.current;
    if (from === target) return;
    if (typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      fromRef.current = target;
      setValue(target);
      return;
    }
    let frame = 0;
    const startedAt = performance.now();
    const tick = (now: number) => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const next = from + (target - from) * easeOutCubic(progress);
      setValue(next);
      if (progress < 1) {
        frame = requestAnimationFrame(tick);
      } else {
        fromRef.current = target;
      }
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);

  return value;
}
