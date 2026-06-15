import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import StatusBadge from "./StatusBadge.jsx";

const ACTIVE = new Set([
  "pending",
  "creating",
  "starting",
  "installing",
  "checking_updates",
  "installing_updates",
]);

export default function JobStatus({ jobId, onChange }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef(null);

  const load = useCallback(async () => {
    if (!jobId) return;
    try {
      const data = await api.job(jobId);
      setJob(data);
      onChange?.();
      return data;
    } catch (err) {
      setError(err.message);
    }
  }, [jobId, onChange]);

  // Poll while the job is active; stop once it is done or has failed.
  useEffect(() => {
    setJob(null);
    setError(null);
    if (!jobId) return;
    let cancelled = false;
    const tick = async () => {
      const data = await load();
      if (cancelled) return;
      if (data && ACTIVE.has(data.status)) {
        timer.current = setTimeout(tick, 2000);
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [jobId, load]);

  const installUpdates = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.installUpdates(jobId);
      // Resume polling immediately.
      const data = await load();
      if (data && ACTIVE.has(data.status)) {
        timer.current = setTimeout(async function tick() {
          const d = await load();
          if (d && ACTIVE.has(d.status)) timer.current = setTimeout(tick, 2000);
        }, 2000);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!jobId) {
    return (
      <div className="card job-detail">
        <p className="muted">Wähle links einen Auftrag aus, um Details zu sehen.</p>
      </div>
    );
  }

  if (!job) {
    return (
      <div className="card job-detail">
        {error ? <div className="alert alert-error">{error}</div> : <p className="muted">Lädt …</p>}
      </div>
    );
  }

  const updatesActive = job.status === "installing_updates";

  return (
    <div className="card job-detail">
      <div className="job-detail-head">
        <h3>{job.hostname || "Container"}</h3>
        <StatusBadge status={job.status} />
      </div>

      <div className="progress">
        <div
          className={`progress-bar ${job.status === "error" ? "error" : ""}`}
          style={{ width: `${job.progress}%` }}
        />
      </div>
      <p className="step">{job.step}</p>

      <dl className="meta">
        {job.vmid && (
          <>
            <dt>VMID</dt>
            <dd>{job.vmid}</dd>
          </>
        )}
        {job.node && (
          <>
            <dt>Node</dt>
            <dd>{job.node}</dd>
          </>
        )}
      </dl>

      {error && <div className="alert alert-error">{error}</div>}
      {job.error && <div className="alert alert-error">{job.error}</div>}

      {job.updates_checked && (
        <div className="updates">
          <div className="updates-head">
            <h4>Verfügbare Updates ({job.updates.length})</h4>
            {job.updates.length > 0 && (
              <button
                className="btn btn-primary btn-sm"
                onClick={installUpdates}
                disabled={busy || updatesActive}
              >
                {updatesActive ? "Wird installiert …" : "Updates installieren"}
              </button>
            )}
          </div>
          {job.updates.length === 0 ? (
            <p className="muted">System ist aktuell.</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Paket</th>
                  <th>Aktuell</th>
                  <th>Verfügbar</th>
                </tr>
              </thead>
              <tbody>
                {job.updates.map((u) => (
                  <tr key={u.name}>
                    <td>{u.name}</td>
                    <td>{u.current || "—"}</td>
                    <td>{u.candidate || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <details className="logs-block" open>
        <summary>Protokoll ({job.logs.length})</summary>
        <pre className="log-output">
          {job.logs
            .map((l) => `[${new Date(l.timestamp).toLocaleTimeString()}] ${l.message}`)
            .join("\n") || "Keine Einträge."}
        </pre>
      </details>
    </div>
  );
}
