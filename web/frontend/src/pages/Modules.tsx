import { useEffect, useState } from "react";
import { get, post } from "../api/client";

interface Module {
  name: string;
  enabled: boolean;
  display_name: string;
  description: string;
  stats: Record<string, unknown>;
}

export function Modules() {
  const [modules, setModules] = useState<Module[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    get<Module[]>("/api/modules").then(setModules).catch(() => {}).finally(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
  }, []);

  const toggle = async (name: string) => {
    await post(`/api/module/${name}/toggle`);
    refresh();
  };

  if (loading) return <div className="empty-state">Loading...</div>;

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "1rem" }}>Modules</h3>
      <div className="module-grid">
        {modules.map((m) => (
          <div className="module-card" key={m.name}>
            <div className="module-header">
              <h3>{m.display_name}</h3>
              <button
                className={`toggle ${m.enabled ? "on" : "off"}`}
                onClick={() => toggle(m.name)}
                title={m.description || (m.enabled ? "Disable this module" : "Enable this module")}
              />
            </div>
            <p>{m.description}</p>
            {Object.entries(m.stats).length > 0 && (
              <div style={{ fontSize: "0.8rem" }}>
                {Object.entries(m.stats).map(([k, v]) => (
                  <div key={k} style={{ display: "flex", justifyContent: "space-between", padding: "0.15rem 0" }}>
                    <span style={{ color: "var(--text-muted)" }}>{k.replace(/_/g, " ")}</span>
                    <span style={{ color: "var(--heading)" }}>{String(v)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
