import { useCallback, useEffect, useState } from "react";
import { GraphCanvas } from "./GraphCanvas";
import { useWebSocket } from "./useWebSocket";
import type {
  BlastResult,
  GraphResponse,
  NodeVisualState,
  StatusResponse,
  WsEvent,
} from "./types";

const LEGEND: { state: NodeVisualState; label: string }[] = [
  { state: "default", label: "unchanged" },
  { state: "modified", label: "modified" },
  { state: "querying", label: "querying / running" },
  { state: "passed", label: "passed" },
  { state: "failed", label: "failed" },
];

export function App() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [visualState, setVisualState] = useState<Record<string, NodeVisualState>>(
    {},
  );
  const [surface, setSurface] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<BlastResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  const loadStatus = useCallback(() => {
    fetch("/api/status")
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => undefined);
  }, []);

  const loadGraph = useCallback(() => {
    fetch("/api/graph")
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json()).error ?? `HTTP ${r.status}`);
        return r.json() as Promise<GraphResponse>;
      })
      .then((g) => {
        setGraph(g);
        setGraphError(null);
        setSurface(new Set(g.import_surface));
      })
      .catch((e: Error) => setGraphError(e.message));
  }, []);

  useEffect(() => {
    loadStatus();
    loadGraph();
  }, [loadStatus, loadGraph]);

  const handleEvent = useCallback((event: WsEvent) => {
    switch (event.type) {
      case "blast_started":
        setResult(null);
        setBanner(null);
        setSurface(new Set(event.modified_files));
        setVisualState(
          Object.fromEntries(
            event.modified_files.map((id) => [id, "modified" as NodeVisualState]),
          ),
        );
        break;
      case "surface_detected":
        setSurface(new Set(event.import_surface));
        break;
      case "db_query":
        if (event.status === "complete") {
          setVisualState((prev) => {
            const next = { ...prev };
            event.affected_tests.forEach((id) => {
              next[id] = "querying";
            });
            return next;
          });
        } else {
          setVisualState((prev) => {
            const next = { ...prev };
            surface.forEach((id) => {
              if (next[id] !== "modified") next[id] = "querying";
            });
            return next;
          });
        }
        break;
      case "test_result": {
        setResult(event.result);
        const executed = new Set(event.result.target_tests_executed);
        setVisualState((prev) => {
          const next = { ...prev };
          Object.keys(next).forEach((id) => {
            if (next[id] === "querying" && !executed.has(id)) next[id] = "default";
          });
          executed.forEach((id) => {
            next[id] =
              event.result.status === "PASSED"
                ? "passed"
                : event.result.status === "FAILED"
                  ? "failed"
                  : "default";
          });
          return next;
        });
        loadStatus();
        break;
      }
      case "error":
        setBanner(event.error);
        break;
    }
  }, [surface, loadStatus]);

  const { connected } = useWebSocket(handleEvent);

  const blast = useCallback(() => {
    setBusy(true);
    setBanner(null);
    fetch("/api/blast", { method: "POST" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) setBanner(body.error ?? `HTTP ${r.status}`);
      })
      .catch((e: Error) => setBanner(e.message))
      .finally(() => setBusy(false));
  }, []);

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Git-Blast Live</h1>
          <p className="subtitle">
            detect what changed → trace what depends on it → test only that
          </p>
        </div>
        <div className="header-right">
          <span className={`ws-dot ${connected ? "on" : "off"}`} />
          <span className="repo-id">{status?.repo_id ?? "…"}</span>
          <button className="blast-btn" onClick={blast} disabled={busy}>
            {busy ? "Blasting…" : "Blast"}
          </button>
        </div>
      </header>

      <div className="status-bar">
        {status ? (
          <>
            <span>repo: {status.repo_root}</span>
            <span>mappings: {status.mappings ?? "—"}</span>
            <span>backend: {status.db_backend ?? "—"}</span>
            <span>connections: {status.connections}</span>
            <span>entire: {status.entire_available ? "yes" : "no"}</span>
          </>
        ) : (
          <span>loading status…</span>
        )}
        <span className="legend">
          {LEGEND.map((l) => (
            <span key={l.state} className="legend-item">
              <span className={`swatch state-${l.state}`} />
              {l.label}
            </span>
          ))}
        </span>
      </div>

      {banner ? <div className="banner error">{banner}</div> : null}

      <main className="app-main">
        {graphError ? (
          <div className="banner error">graph unavailable: {graphError}</div>
        ) : graph ? (
          <GraphCanvas
            nodes={graph.nodes}
            edges={graph.edges}
            visualState={visualState}
            surface={surface}
          />
        ) : (
          <div className="loading">loading graph…</div>
        )}

        {result ? (
          <aside className={`result-panel ${result.status.toLowerCase()}`}>
            <h2>{result.status}</h2>
            <dl>
              <dt>affected tests</dt>
              <dd>{result.affected_tests.length}</dd>
              <dt>executed</dt>
              <dd>{result.target_tests_executed.join(", ") || "—"}</dd>
              <dt>test source</dt>
              <dd>{result.test_source ?? "—"}</dd>
              <dt>time</dt>
              <dd>{result.execution_time_seconds.toFixed(2)}s</dd>
            </dl>
            <p className="result-summary">{result.summary}</p>
            {result.failure_summary ? (
              <pre className="failure">{result.failure_summary}</pre>
            ) : null}
          </aside>
        ) : null}
      </main>
    </div>
  );
}
