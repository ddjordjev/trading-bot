import { useCallback, useEffect, useRef, useState } from "react";

export function Summary() {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/summary-html")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((raw) => {
        const bodyMatch = raw.match(/<body[^>]*>([\s\S]*)<\/body>/i);
        const styleMatch = raw.match(/<style[^>]*>([\s\S]*?)<\/style>/gi);
        const styles = styleMatch ? styleMatch.join("\n") : "";
        const body = bodyMatch ? bodyMatch[1] : raw;
        setHtml(styles + body);
      })
      .catch((e) => setError(e.message));
  }, []);

  const handleClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const anchor = target.closest("a");
    if (!anchor) return;
    const href = anchor.getAttribute("href");
    if (!href || !href.startsWith("#")) return;
    e.preventDefault();
    const el = containerRef.current?.querySelector(href);
    if (el) el.scrollIntoView({ behavior: "smooth" });
  }, []);

  if (error) {
    return (
      <div className="empty-state">
        <p>Could not load summary: {error}</p>
      </div>
    );
  }

  if (!html) {
    return (
      <div className="empty-state">
        <p style={{ color: "var(--text-muted)" }}>Loading summary...</p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="summary-embed"
      style={{
        height: "calc(100vh - 120px)",
        overflow: "auto",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "1rem",
      }}
      onClick={handleClick}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
