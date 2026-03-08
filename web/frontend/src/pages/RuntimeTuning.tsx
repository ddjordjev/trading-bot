import { useEffect, useMemo, useState } from "react";
import { get, postBody } from "../api/client";

interface RuntimeTuningPayload {
  bot_id: string;
  revision: string;
  values: Record<string, number>;
}

interface RuntimeTuningKeysPayload {
  keys: string[];
  types: Record<string, string>;
}

interface BotProfile {
  id: string;
}

export function RuntimeTuning() {
  const [selectedBotId, setSelectedBotId] = useState<string>("");
  const [globalValues, setGlobalValues] = useState<Record<string, number>>({});
  const [globalRevision, setGlobalRevision] = useState<string>("");
  const [botEffectiveValues, setBotEffectiveValues] = useState<Record<string, number>>({});
  const [botEffectiveRevision, setBotEffectiveRevision] = useState<string>("");
  const [botOverrideValues, setBotOverrideValues] = useState<Record<string, number>>({});
  const [botOverrideRevision, setBotOverrideRevision] = useState<string>("");
  const [keys, setKeys] = useState<string[]>([]);
  const [types, setTypes] = useState<Record<string, string>>({});
  const [botIds, setBotIds] = useState<string[]>([]);
  const [globalDrafts, setGlobalDrafts] = useState<Record<string, string>>({});
  const [botDrafts, setBotDrafts] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState<boolean>(true);
  const [savingGlobal, setSavingGlobal] = useState<boolean>(false);
  const [savingBot, setSavingBot] = useState<boolean>(false);
  const [globalExpanded, setGlobalExpanded] = useState<boolean>(false);
  const [botExpanded, setBotExpanded] = useState<boolean>(true);
  const [error, setError] = useState<string>("");

  const sortedKeys = useMemo(() => [...keys].sort(), [keys]);

  const loadGlobal = async () => {
    const payload = await get<RuntimeTuningPayload>("/api/runtime-tuning");
    const values = payload.values || {};
    setGlobalValues(values);
    setGlobalRevision(payload.revision || "");
    const nextDrafts: Record<string, string> = {};
    Object.entries(values).forEach(([key, value]) => {
      nextDrafts[key] = String(value);
    });
    setGlobalDrafts(nextDrafts);
  };

  const loadBot = async (botId: string) => {
    if (!botId) {
      setBotEffectiveValues({});
      setBotEffectiveRevision("");
      setBotOverrideValues({});
      setBotOverrideRevision("");
      setBotDrafts({});
      return;
    }
    const [effectivePayload, overridesPayload] = await Promise.all([
      get<RuntimeTuningPayload>(`/api/runtime-tuning/${botId}`),
      get<RuntimeTuningPayload>(`/api/runtime-tuning-overrides/${botId}`),
    ]);
    const effective = effectivePayload.values || {};
    const overrides = overridesPayload.values || {};
    setBotEffectiveValues(effective);
    setBotEffectiveRevision(effectivePayload.revision || "");
    setBotOverrideValues(overrides);
    setBotOverrideRevision(overridesPayload.revision || "");
    const nextDrafts: Record<string, string> = {};
    Object.entries(effective).forEach(([key, value]) => {
      nextDrafts[key] = String(value);
    });
    setBotDrafts(nextDrafts);
  };

  const bootstrap = async () => {
    setLoading(true);
    setError("");
    try {
      const [keysPayload, profiles] = await Promise.all([
        get<RuntimeTuningKeysPayload>("/api/runtime-tuning-keys"),
        get<BotProfile[]>("/api/bot-profiles"),
      ]);
      const sortedBotIds = (profiles || []).map((p) => p.id).sort();
      setKeys(keysPayload.keys || []);
      setTypes(keysPayload.types || {});
      setBotIds(sortedBotIds);
      const firstBot = sortedBotIds[0] || "";
      setSelectedBotId(firstBot);
      await loadGlobal();
      await loadBot(firstBot);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load runtime tuning");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    bootstrap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const parseDraftValue = (rawValue: string, key: string): number | null => {
    const raw = String(rawValue ?? "").trim();
    if (!raw) {
      return null;
    }
    const kind = types[key] || "float";
    if (kind === "int") {
      const val = Number.parseInt(raw, 10);
      return Number.isFinite(val) ? val : null;
    }
    const val = Number.parseFloat(raw);
    return Number.isFinite(val) ? val : null;
  };

  const isSameValue = (a: number, b: number, key: string): boolean => {
    if ((types[key] || "float") === "int") {
      return Math.trunc(a) === Math.trunc(b);
    }
    return Math.abs(a - b) < 1e-12;
  };

  const globalChangedKeys = useMemo(() => {
    return sortedKeys.filter((key) => {
      const draft = parseDraftValue(globalDrafts[key] ?? "", key);
      if (draft === null) return false;
      const current = Number(globalValues[key]);
      return Number.isFinite(current) && !isSameValue(draft, current, key);
    });
  }, [sortedKeys, globalDrafts, globalValues, types]);

  const botChangedKeys = useMemo(() => {
    return sortedKeys.filter((key) => {
      if (!selectedBotId) return false;
      const draft = parseDraftValue(botDrafts[key] ?? "", key);
      if (draft === null) return false;
      const current = Number(botEffectiveValues[key]);
      return Number.isFinite(current) && !isSameValue(draft, current, key);
    });
  }, [sortedKeys, selectedBotId, botDrafts, botEffectiveValues, types]);

  const hasInvalidGlobalDrafts = useMemo(() => {
    return sortedKeys.some((key) => {
      const raw = String(globalDrafts[key] ?? "").trim();
      return raw.length > 0 && parseDraftValue(raw, key) === null;
    });
  }, [sortedKeys, globalDrafts, types]);

  const hasInvalidBotDrafts = useMemo(() => {
    return sortedKeys.some((key) => {
      const raw = String(botDrafts[key] ?? "").trim();
      return raw.length > 0 && parseDraftValue(raw, key) === null;
    });
  }, [sortedKeys, botDrafts, types]);

  const canSaveGlobal = globalChangedKeys.length > 0 && !hasInvalidGlobalDrafts && !savingGlobal;
  const canSaveBot = !!selectedBotId && botChangedKeys.length > 0 && !hasInvalidBotDrafts && !savingBot;

  const saveGlobalChanges = async () => {
    if (!canSaveGlobal) return;
    setSavingGlobal(true);
    setError("");
    try {
      for (const key of globalChangedKeys) {
        const parsed = parseDraftValue(globalDrafts[key] ?? "", key);
        if (parsed === null) continue;
        await postBody<{ success: boolean; message: string }>("/api/runtime-tuning", {
          key,
          value: parsed,
          bot_id: "*",
        });
      }
      await loadGlobal();
      await loadBot(selectedBotId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save runtime tuning");
    } finally {
      setSavingGlobal(false);
    }
  };

  const saveBotChanges = async () => {
    if (!selectedBotId || !canSaveBot) return;
    setSavingBot(true);
    setError("");
    try {
      // Save selected bot snapshot from current visible values.
      for (const key of sortedKeys) {
        const parsed = parseDraftValue(botDrafts[key] ?? "", key);
        if (parsed === null) continue;
        const globalVal = Number(globalValues[key]);
        const shouldClearOverride = Number.isFinite(globalVal) && isSameValue(parsed, globalVal, key);
        await postBody<{ success: boolean; message: string }>("/api/runtime-tuning", {
          key,
          value: shouldClearOverride ? null : parsed,
          bot_id: selectedBotId,
        });
      }
      await loadBot(selectedBotId);
      await loadGlobal();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save runtime tuning");
    } finally {
      setSavingBot(false);
    }
  };

  const onBotChange = async (nextBot: string) => {
    setSelectedBotId(nextBot);
    setLoading(true);
    setError("");
    try {
      await loadBot(nextBot);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to refresh tuning");
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return <div className="empty-state">Loading...</div>;
  }

  return (
    <div>
      <h3 style={{ color: "var(--heading)", marginBottom: "0.25rem" }}>Runtime Tuning</h3>
      <p style={{ color: "var(--text-muted)", fontSize: "0.8rem", marginBottom: "1rem" }}>
        UI and DB use the same whitelist only. No hidden runtime keys are accepted outside this list.
      </p>

      {error && (
        <div style={{ color: "var(--red)", fontSize: "0.8rem", marginBottom: "0.75rem" }}>
          {error}
        </div>
      )}

      <div style={{ color: "var(--text-muted)", fontSize: "0.78rem", marginBottom: "0.75rem" }}>
        Changed values are wrapped in orange.
      </div>

      <div style={{ marginBottom: "1rem", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "0.6rem 0.75rem" }}>
        <div
          style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
          onClick={() => setGlobalExpanded((prev) => !prev)}
        >
          <h4 style={{ color: "var(--heading)", margin: 0 }}>Global Runtime Config</h4>
          <span style={{ color: "var(--text-muted)", fontSize: "0.82rem" }}>{globalExpanded ? "hide" : "show"}</span>
        </div>
        <div style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginTop: "0.45rem" }}>
          global hash: {globalRevision || "none"}
        </div>
        {globalExpanded && (
          <>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "0.5rem", marginBottom: "0.5rem" }}>
              <button
                className="btn-primary"
                disabled={!canSaveGlobal}
                onClick={saveGlobalChanges}
                style={{ fontSize: "0.78rem", padding: "0.35rem 0.65rem" }}
              >
                {savingGlobal ? "Saving..." : "Save Global"}
              </button>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Key</th>
                  <th>Type</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {sortedKeys.map((key) => {
                  const isChanged = globalChangedKeys.includes(key);
                  return (
                    <tr key={`global-${key}`}>
                      <td style={{ fontFamily: "monospace", fontSize: "0.8rem" }}>{key}</td>
                      <td>{types[key] || "float"}</td>
                      <td>
                        <input
                          value={globalDrafts[key] ?? ""}
                          onChange={(e) => setGlobalDrafts((prev) => ({ ...prev, [key]: e.target.value }))}
                          placeholder="Enter value"
                          style={{
                            width: "100%",
                            minWidth: 160,
                            padding: "0.35rem 0.5rem",
                            border: isChanged ? "1px solid #d29922" : "1px solid var(--border)",
                            boxShadow: isChanged ? "0 0 0 1px rgba(210, 153, 34, 0.45) inset" : "none",
                            borderRadius: "var(--radius)",
                            background: isChanged ? "rgba(210, 153, 34, 0.12)" : undefined,
                          }}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </div>

      <div style={{ border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "0.6rem 0.75rem" }}>
        <div
          style={{ display: "flex", justifyContent: "space-between", alignItems: "center", cursor: "pointer" }}
          onClick={() => setBotExpanded((prev) => !prev)}
        >
          <h4 style={{ color: "var(--heading)", margin: 0 }}>Selected Bot Runtime Config</h4>
          <span style={{ color: "var(--text-muted)", fontSize: "0.82rem" }}>{botExpanded ? "hide" : "show"}</span>
        </div>
        <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", marginTop: "0.6rem", marginBottom: "0.5rem" }}>
          <label style={{ fontSize: "0.8rem", color: "var(--text-muted)" }}>Bot</label>
          <select
            value={selectedBotId}
            onChange={(e) => onBotChange(e.target.value)}
            style={{ padding: "0.35rem 0.55rem", borderRadius: "var(--radius)", border: "1px solid var(--border)" }}
          >
            {botIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </div>
        <div style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginBottom: "0.35rem" }}>
          bot effective hash: {botEffectiveRevision || "none"}
        </div>
        <div style={{ color: "var(--text-muted)", fontSize: "0.75rem", marginBottom: "0.5rem" }}>
          bot override hash: {botOverrideRevision || "none"}
        </div>
        {botExpanded && (
          <>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "0.2rem", marginBottom: "0.5rem" }}>
              <button
                className="btn-primary"
                disabled={!canSaveBot}
                onClick={saveBotChanges}
                style={{ fontSize: "0.78rem", padding: "0.35rem 0.65rem" }}
              >
                {savingBot ? "Saving..." : `Save ${selectedBotId || "Bot"}`}
              </button>
            </div>
            <table>
              <thead>
                <tr>
                  <th>Key</th>
                  <th>Type</th>
                  <th>Value</th>
                </tr>
              </thead>
              <tbody>
                {sortedKeys.map((key) => {
                  const isChanged = botChangedKeys.includes(key);
                  return (
                    <tr key={`bot-${key}`}>
                      <td style={{ fontFamily: "monospace", fontSize: "0.8rem" }}>{key}</td>
                      <td>{types[key] || "float"}</td>
                      <td>
                        <input
                          value={botDrafts[key] ?? ""}
                          onChange={(e) => setBotDrafts((prev) => ({ ...prev, [key]: e.target.value }))}
                          placeholder="Enter value"
                          style={{
                            width: "100%",
                            minWidth: 160,
                            padding: "0.35rem 0.5rem",
                            border: isChanged ? "1px solid #d29922" : "1px solid var(--border)",
                            boxShadow: isChanged ? "0 0 0 1px rgba(210, 153, 34, 0.45) inset" : "none",
                            borderRadius: "var(--radius)",
                            background: isChanged ? "rgba(210, 153, 34, 0.12)" : undefined,
                          }}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}
