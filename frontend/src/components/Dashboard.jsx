import { useCallback, useEffect, useState } from "react";
import { useAuth } from "../lib/auth.jsx";
import { api } from "../lib/api.js";
import CommunityScripts from "./CommunityScripts.jsx";
import CreateContainerForm from "./CreateContainerForm.jsx";
import GuestList from "./GuestList.jsx";
import JobList from "./JobList.jsx";
import JobStatus from "./JobStatus.jsx";
import LogViewer from "./LogViewer.jsx";

const TABS = [
  { id: "create", label: "Erstellen" },
  { id: "guests", label: "Gäste" },
  { id: "community", label: "Community-Scripts" },
  { id: "jobs", label: "Aufträge" },
  { id: "logs", label: "Server-Logs" },
];

export default function Dashboard() {
  const { user, logout } = useAuth();
  const [tab, setTab] = useState("create");
  const [jobs, setJobs] = useState([]);
  const [selectedJobId, setSelectedJobId] = useState(null);

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.jobs());
    } catch {
      /* surfaced elsewhere; keep the dashboard responsive */
    }
  }, []);

  useEffect(() => {
    refreshJobs();
  }, [refreshJobs]);

  // When a deployment is started, jump to the Aufträge tab and follow it.
  const onCreated = (job) => {
    setSelectedJobId(job.id);
    setTab("jobs");
    refreshJobs();
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Proxmox Web Interface</div>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? "active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="user">
          <span className="muted">{user?.username}</span>
          <button className="btn btn-ghost" onClick={logout}>
            Abmelden
          </button>
        </div>
      </header>

      <main className="content">
        {tab === "create" && <CreateContainerForm onCreated={onCreated} />}

        {tab === "guests" && <GuestList />}

        {tab === "community" && <CommunityScripts onCreated={onCreated} />}

        {tab === "jobs" && (
          <div className="jobs-layout">
            <JobList
              jobs={jobs}
              selectedId={selectedJobId}
              onSelect={setSelectedJobId}
              onRefresh={refreshJobs}
            />
            <JobStatus
              jobId={selectedJobId}
              onChange={refreshJobs}
            />
          </div>
        )}

        {tab === "logs" && <LogViewer />}
      </main>
    </div>
  );
}
