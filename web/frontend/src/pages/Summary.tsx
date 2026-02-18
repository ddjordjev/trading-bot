export function Summary() {
  return (
    <div style={{ height: "calc(100vh - 120px)" }}>
      <iframe
        src="/docs/summary"
        style={{
          width: "100%",
          height: "100%",
          border: "1px solid var(--border)",
          borderRadius: "var(--radius)",
          background: "var(--bg)",
        }}
        title="Requirements Summary"
      />
    </div>
  );
}
