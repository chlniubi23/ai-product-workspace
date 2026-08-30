"use client";

import { useEffect, useRef } from "react";
import type { ECharts, EChartsOption } from "echarts";

type ChartRendererProps = {
  option: Record<string, unknown>;
  title: string;
  height?: number;
};

export function ChartRenderer({ option, title, height = 300 }: ChartRendererProps) {
  const elementRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = elementRef.current;
    if (!element) return;
    let chart: ECharts | undefined;
    let observer: ResizeObserver | undefined;
    let disposed = false;
    const resize = () => chart?.resize();

    void import("echarts").then((echarts) => {
      if (disposed) return;
      chart = echarts.init(element, undefined, { renderer: "canvas" });
      chart.setOption(option as EChartsOption, true);
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(resize);
        observer.observe(element);
      }
      window.addEventListener("resize", resize);
    });

    return () => {
      disposed = true;
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      chart?.dispose();
    };
  }, [option]);

  return (
    <div
      ref={elementRef}
      role="img"
      aria-label={title}
      style={{ width: "100%", height, minHeight: height }}
    />
  );
}
