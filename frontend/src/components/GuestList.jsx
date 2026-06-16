import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";

function formatMem(bytes) {
  if (!bytes) return "—";
  const mb = bytes / 1024 / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

function formatUptime(seconds) {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

export default function GuestList() {
  const [guests, setGuests] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null); // "<type>-<vmid>" currently acting on
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      setGuests(await api.guests());
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Poll so status changes (after start/stop) become visible.
  useEffect(() => {
    load();
    timer.current = setInterval(load, 5000);
    return () => clearInterval(timer.current);
  }, [load]);

  const runAction = async (guest, action) => {
    setBusy(`${guest.type}-${guest.vmid}`);
    setError(null);
    try {
      await api.guestAction(guest.type, guest.vmid, action, guest.node);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const backup = async (guest) => {
    const label = guest.name || `VMID ${guest.vmid}`;
    if (!window.confirm(`Backup von "${label}" jetzt erstellen?`)) return;
    setBusy(`${guest.type}-${guest.vmid}`);
    setError(null);
    try {
      await api.guestBackup(guest.type, guest.vmid, guest.node);
      // The backup runs as a Proxmox task in the background.
      window.alert("Backup wurde gestartet (läuft im Hintergrund auf Proxmox).");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (guest) => {
    const label = guest.name || `VMID ${guest.vmid}`;
    if (
      !window.confirm(
        `Gast "${label}" (VMID ${guest.vmid}) wirklich löschen? ` +
          "Das kann nicht rückgängig gemacht werden."
      )
    ) {
      return;
    }
    setBusy(`${guest.type}-${guest.vmid}`);
    setError(null);
    try {
      await api.guestDelete(guest.type, guest.vmid, guest.node);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="card">
      <div className="job-list-head">
        <h3>Gäste ({guests.length})</h3>
        <button className="btn btn-ghost btn-sm" onClick={load}>
          Aktualisieren
        </button>
      </div>
      {error && <div className="alert alert-error">{error}</div>}

      {loading ? (
        <p className="muted">Lädt …</p>
      ) : !guests.length ? (
        <p className="muted">Keine Gäste gefunden.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Typ</th>
              <th>VMID</th>
              <th>Status</th>
              <th>IP</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>Uptime</th>
              <th>Aktionen</th>
            </tr>
          </thead>
          <tbody>
            {guests.map((g) => {
              const id = `${g.type}-${g.vmid}`;
              const isBusy = busy === id;
              const running = g.status === "running";
              return (
                <tr key={id}>
                  <td>{g.name || "—"}</td>
                  <td>{g.type === "vm" ? "VM" : "LXC"}</td>
                  <td>{g.vmid}</td>
                  <td>
                    <span className={`badge ${running ? "badge-success" : "badge-info"}`}>
                      {g.status || "—"}
                    </span>
                  </td>
                  <td>{g.ip || "—"}</td>
                  <td>{g.cpus ?? "—"}</td>
                  <td>
                    {running
                      ? `${formatMem(g.mem)} / ${formatMem(g.maxmem)}`
                      : formatMem(g.maxmem)}
                  </td>
                  <td>{formatUptime(g.uptime)}</td>
                  <td className="row">
                    {running ? (
                      <>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={isBusy}
                          onClick={() => runAction(g, "shutdown")}
                        >
                          Herunterfahren
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={isBusy}
                          onClick={() => runAction(g, "reboot")}
                        >
                          Neustart
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          className="btn btn-primary btn-sm"
                          disabled={isBusy}
                          onClick={() => runAction(g, "start")}
                        >
                          Start
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={isBusy}
                          onClick={() => remove(g)}
                        >
                          Löschen
                        </button>
                      </>
                    )}
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={isBusy}
                      onClick={() => backup(g)}
                    >
                      Backup
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
