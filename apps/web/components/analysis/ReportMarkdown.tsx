"use client";

import { Fragment, type ReactNode } from "react";

/** Render the inline subset the report model actually emits: **bold** text. */
function renderInline(text: string, keyPrefix: string): ReactNode[] {
  return text
    .split(/\*\*([^*]+)\*\*/g)
    .map((part, index) =>
      index % 2 === 1 ? (
        <strong key={`${keyPrefix}-${index}`}>{part}</strong>
      ) : (
        <Fragment key={`${keyPrefix}-${index}`}>{part}</Fragment>
      ),
    );
}

/**
 * Minimal markdown renderer for AI analysis reports.
 *
 * Supports exactly what the report prompt asks the model to produce --
 * headings, unordered/ordered lists, bold spans and paragraphs. Deliberately
 * not a general markdown engine: no raw HTML, no links, so model output can
 * never inject markup into the page.
 */
export function ReportMarkdown({ markdown }: { markdown: string }) {
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;

  const flushList = () => {
    if (!list) return;
    const current = list;
    list = null;
    const items = current.items.map((item, index) => (
      <li key={index}>{renderInline(item, `li-${blocks.length}-${index}`)}</li>
    ));
    blocks.push(
      current.ordered ? (
        <ol key={`list-${blocks.length}`} style={{ margin: "6px 0", paddingLeft: 20 }}>
          {items}
        </ol>
      ) : (
        <ul key={`list-${blocks.length}`} style={{ margin: "6px 0", paddingLeft: 20 }}>
          {items}
        </ul>
      ),
    );
  };

  markdown.split(/\r?\n/).forEach((raw, index) => {
    const line = raw.trimEnd();
    if (!line.trim()) {
      flushList();
      return;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      blocks.push(
        <div
          key={`h-${index}`}
          style={{
            fontWeight: 600,
            fontSize: level === 1 ? 17 : level === 2 ? 14.5 : 13,
            marginTop: index ? 14 : 0,
            color: "var(--ink)",
          }}
        >
          {renderInline(heading[2], `h-${index}`)}
        </div>,
      );
      return;
    }
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      if (!list || list.ordered) {
        flushList();
        list = { ordered: false, items: [] };
      }
      list.items.push(bullet[1]);
      return;
    }
    const ordered = line.match(/^\s*\d+[.、)]\s+(.*)$/);
    if (ordered) {
      if (!list || !list.ordered) {
        flushList();
        list = { ordered: true, items: [] };
      }
      list.items.push(ordered[1]);
      return;
    }
    flushList();
    blocks.push(
      <p key={`p-${index}`} style={{ margin: "6px 0", lineHeight: 1.75 }}>
        {renderInline(line, `p-${index}`)}
      </p>,
    );
  });
  flushList();

  return <div style={{ fontSize: 13, color: "var(--ink)", overflowWrap: "anywhere" }}>{blocks}</div>;
}
