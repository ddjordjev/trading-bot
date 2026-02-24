import { useEffect, useState } from "react";

function normalizeSummaryHtml(raw: string): string {
  const parser = new DOMParser();
  const doc = parser.parseFromString(raw, "text/html");

  // Keep all summary links from hijacking the iframe context.
  for (const anchor of Array.from(doc.querySelectorAll("a[href]"))) {
    const href = (anchor.getAttribute("href") ?? "").trim();
    if (!href || href.startsWith("#")) continue;
    anchor.setAttribute("target", "_blank");
    anchor.setAttribute("rel", "noopener noreferrer");
  }

  return `<!doctype html>\n${doc.documentElement.outerHTML}`;
}

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
        setHtml(normalizeSummaryHtml(raw));
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
