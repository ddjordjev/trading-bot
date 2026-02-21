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
      .then(setHtml)
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

  const patched = html.replace(
    /<head>/i,
    "<head><base target='_self'>"
  );

  return (
    <div
      style={{
        height: "calc(100vh - 120px)",
        overflow: "auto",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
      }}
    >
      <iframe
        srcDoc={patched}
        sandbox="allow-same-origin"
        style={{
          width: "100%",
          height: "100%",
          border: "none",
          display: "block",
        }}
        title="Requirements Summary"
      />
    </div>
  );
}
