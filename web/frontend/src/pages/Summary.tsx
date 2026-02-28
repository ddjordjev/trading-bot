import { useEffect, useRef, useState } from "react";

type LoadState = "loading" | "ready" | "error";

function buildShadowDocument(rawHtml: string): string {
  const parser = new DOMParser();
  const parsed = parser.parseFromString(rawHtml, "text/html");
  const styles = Array.from(parsed.head.querySelectorAll("style"))
    .map((tag) => tag.outerHTML)
    .join("\n");
  const bodyContent = parsed.body?.innerHTML || "<p>Summary is empty.</p>";

  return `
    <style>
      :host { display: block; }
      .summary-root {
        color: var(--text);
      }
    </style>
    ${styles}
    <div class="summary-root">${bodyContent}</div>
  `;
}

export function Summary() {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [showBackToTop, setShowBackToTop] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const host = hostRef.current;
    if (!host) return undefined;

    const shadow = host.shadowRoot ?? host.attachShadow({ mode: "open" });

    const onClick = (event: Event) => {
      const target = event.target as HTMLElement | null;
      const anchor = target?.closest("a");
      if (!anchor) return;

      const href = anchor.getAttribute("href") || "";
      if (!href) return;

      if (href.startsWith("#")) {
        event.preventDefault();
        const id = href.slice(1);
        const el = shadow.getElementById(id);
        if (el) {
          el.scrollIntoView({ behavior: "smooth", block: "start" });
        }
        return;
      }

      // Keep external navigation out of the SPA shell.
      if (/^https?:\/\//i.test(href)) {
        event.preventDefault();
        window.open(href, "_blank", "noopener,noreferrer");
      }
    };

    shadow.addEventListener("click", onClick);

    const load = async () => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 8000);
      try {
        setState("loading");
        const response = await fetch("/api/summary-html", { signal: controller.signal });
        if (!response.ok) throw new Error("Summary not found");
        const html = await response.text();
        if (cancelled) return;
        shadow.innerHTML = buildShadowDocument(html);
        setState("ready");
      } catch {
        if (!cancelled) {
          setState("error");
        }
      } finally {
        window.clearTimeout(timeout);
      }
    };

    void load();

    return () => {
      cancelled = true;
      shadow.removeEventListener("click", onClick);
    };
  }, []);

  useEffect(() => {
    const onScroll = () => {
      setShowBackToTop(window.scrollY > 420);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  if (state === "error") {
    return (
      <div className="empty-state">
        <p style={{ color: "var(--red)" }}>Could not load summary content.</p>
      </div>
    );
  }

  return (
    <div>
      {state === "loading" && (
        <p style={{ color: "var(--text-muted)", marginBottom: "0.75rem" }}>Loading summary...</p>
      )}
      <div ref={hostRef} />
      {state === "ready" && showBackToTop && (
        <button
          type="button"
          aria-label="Back to top"
          title="Back to top"
          onClick={() => window.scrollTo({ top: 0, left: 0, behavior: "smooth" })}
          style={{
            position: "fixed",
            right: "1.25rem",
            bottom: "1.25rem",
            width: 40,
            height: 40,
            borderRadius: "999px",
            border: "1px solid var(--border)",
            background: "var(--surface)",
            color: "var(--heading)",
            fontSize: "1.2rem",
            lineHeight: 1,
            cursor: "pointer",
            boxShadow: "0 6px 18px rgba(0, 0, 0, 0.35)",
            zIndex: 20,
          }}
        >
          ↑
        </button>
      )}
    </div>
  );
}
