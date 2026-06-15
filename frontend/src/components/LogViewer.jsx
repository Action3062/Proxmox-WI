import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function LogViewer() {
  const [lines, setLines] = useState([]);
  const [error, setError] = useState(null);
  const [count, setCount] = useState(200);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api.logs(count);
      setLines(data.lines || []);
    } catch (err) {
      setError(err.message);
    }
  }, [count]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="card">
      <div className="job-list-head">
        <h3>Server-Logs</h3>
        <div className="row">
          <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
            <option value={100}>100 Zeilen</option>
            <option value={200}>200 Zeilen</option>
            <option value={500}>500 Zeilen</option>
          </select>
          <button className="btn btn-ghost btn-sm" onClick={load}>
            Aktualisieren
          </button>
        </div>
      </div>
      {error && <div className="alert alert-error">{error}</div>}
      <pre className="log-output tall">{lines.join("\n") || "Keine Logs vorhanden."}</pre>
    </div>
  );
}
