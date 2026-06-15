import StatusBadge from "./StatusBadge.jsx";

export default function JobList({ jobs, selectedId, onSelect, onRefresh }) {
  return (
    <div className="card job-list">
      <div className="job-list-head">
        <h3>Aufträge</h3>
        <button className="btn btn-ghost btn-sm" onClick={onRefresh}>
          Aktualisieren
        </button>
      </div>
      {!jobs.length && <p className="muted">Noch keine Aufträge.</p>}
      <ul>
        {jobs.map((job) => (
          <li
            key={job.id}
            className={job.id === selectedId ? "selected" : ""}
            onClick={() => onSelect(job.id)}
          >
            <div className="job-row">
              <span className="job-host">{job.hostname || job.id.slice(0, 8)}</span>
              <StatusBadge status={job.status} />
            </div>
            <div className="muted small">
              {job.vmid ? `VMID ${job.vmid}` : "—"} · {job.step}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
