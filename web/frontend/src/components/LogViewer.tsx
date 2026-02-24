import { useEffect, useRef, useState } from "react";
import type { LogEntry } from "../hooks/useWebSocket";

const LEVEL_COLORS: Record<string, string> = {
  TRACE:    "#6c757d",
  DEBUG:    "#6ea8fe",
  INFO:     "#75b798",
  SUCCESS:  "#20c997",
  WARNING:  "#ffda6a",
  ERROR:    "#ea868f",
  CRITICAL: "#ff6b6b",
};

const LEVEL_BADGE_BG: Record<string, string> = {
  TRACE:    "#6c757d22",
  DEBUG:    "#6ea8fe22",
  INFO:     "#75b79822",
  SUCCESS:  "#20c99722",
  WARNING:  "#ffda6a22",
  ERROR:    "#ea868f33",
  CRITICAL: "#ff6b6b44",
};

function levelColor(level: string): string {
  return LEVEL_COLORS[level] || "#adb5bd";
}

function levelBg(level: string): string {
  return LEVEL_BADGE_BG[level] || "transparent";
}

interface Props {
  logs: LogEntry[];
  maxLines?: number;
}

function safeText(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function LogViewer({ logs, maxLines = 100 }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState("ALL");

  const visible = logs
    .filter((l) => filter === "ALL" || l.level === filter)
    .slice(-maxLines);

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      const el = containerRef.current;
      el.scrollTop = el.scrollHeight;
    }
  }, [visible.length, autoScroll]);

  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setAutoScroll(atBottom);
  };

  const levels = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"];

  return (
    <div style={{
      background: "#0d1117",
      borderRadius: 8,
      border: "1px solid #21262d",
      overflow: "hidden",
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      fontSize: "0.78rem",
    }}>
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "6px 12px",
        background: "#161b22",
        borderBottom: "1px solid #21262d",
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: "#f85149", fontSize: 10 }}>●</span>
          <span style={{ color: "#d29922", fontSize: 10 }}>●</span>
          <span style={{ color: "#3fb950", fontSize: 10 }}>●</span>
          <span style={{ color: "#8b949e", fontSize: "0.75rem", marginLeft: 8 }}>
            Live Logs
          </span>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {levels.map((lvl) => (
            <button
              key={lvl}
              onClick={() => setFilter(lvl)}
              style={{
                background: filter === lvl ? "#30363d" : "transparent",
                color: lvl === "ALL" ? "#c9d1d9" : levelColor(lvl),
                border: "1px solid " + (filter === lvl ? "#484f58" : "transparent"),
                borderRadius: 4,
                padding: "2px 8px",
                fontSize: "0.7rem",
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{
          height: 320,
          overflowY: "auto",
          padding: "8px 12px",
          scrollbarWidth: "thin",
          scrollbarColor: "#30363d #0d1117",
        }}
      >
        {visible.length === 0 ? (
          <div style={{ color: "#484f58", textAlign: "center", padding: 40 }}>
            Waiting for logs...
          </div>
        ) : (
          visible.map((log, i) => (
            <div
              key={i}
              style={{
                display: "flex",
                gap: 8,
                padding: "2px 0",
                lineHeight: 1.5,
                borderLeft: `2px solid ${levelColor(log.level)}33`,
                paddingLeft: 8,
                marginBottom: 1,
              }}
            >
              <span style={{ color: "#484f58", flexShrink: 0 }}>
                {safeText(log.ts)}
              </span>
              <span
                style={{
                  color: levelColor(log.level),
                  background: levelBg(log.level),
                  borderRadius: 3,
                  padding: "0 5px",
                  flexShrink: 0,
                  fontWeight: 600,
                  fontSize: "0.72rem",
                  minWidth: 56,
                  textAlign: "center",
                }}
              >
                {safeText(log.level).slice(0, 5)}
              </span>
              <span style={{ color: "#c9d1d9", wordBreak: "break-word" }}>
                {safeText(log.msg)}
              </span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>

      {!autoScroll && (
        <div
          onClick={() => {
            setAutoScroll(true);
            const el = containerRef.current;
            if (el) {
              el.scrollTop = el.scrollHeight;
            }
          }}
          style={{
            textAlign: "center",
            padding: "4px 0",
            background: "#161b22",
            borderTop: "1px solid #21262d",
            color: "#58a6ff",
            fontSize: "0.72rem",
            cursor: "pointer",
          }}
        >
          ↓ Scroll to bottom (auto-scroll paused)
        </div>
      )}
    </div>
  );
}
