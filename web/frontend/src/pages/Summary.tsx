export function Summary() {
  return (
    <div className="empty-state">
      <p style={{ color: "var(--text-muted)", marginBottom: "0.75rem" }}>
        Summary opens in a separate tab.
      </p>
      <a
        href="/api/summary-html"
        target="_blank"
        rel="noopener noreferrer"
        style={{
          display: "inline-block",
          padding: "0.5rem 0.9rem",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          color: "var(--text)",
          textDecoration: "none",
          background: "var(--surface)",
        }}
      >
        Open Summary
      </a>
    </div>
  );
}
