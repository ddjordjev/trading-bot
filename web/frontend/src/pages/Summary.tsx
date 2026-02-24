import { useEffect, useState } from "react";

export function Summary() {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/summary-html")
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then((raw) => {
        setHtml(raw);
      })
      .catch((e) => setError(e.message));
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
    <iframe
      className="summary-embed"
      title="Trade Borg Summary"
      srcDoc={html}
      style={{
        width: "100%",
        height: "calc(100vh - 120px)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--bg)",
      }}
    />
  );
}
