import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function CommunityScripts({ onCreated }) {
  const [suggestions, setSuggestions] = useState([]);
  const [slug, setSlug] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.communityScripts().then(setSuggestions).catch(() => {});
  }, []);

  const run = async (value) => {
    const target = (value || slug).trim().toLowerCase();
    if (!target) return;
    if (
      !window.confirm(
        `Community-Script "${target}" auf dem Proxmox-Host ausführen?\n\n` +
          "Achtung: Es wird Code als root aus dem Internet geladen und ausgeführt; " +
          "das Skript erstellt einen eigenen Container."
      )
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const job = await api.runCommunityScript(target);
      onCreated?.(job); // jump to the Aufträge tab and follow the job log
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <h2>Community-Scripts</h2>
      <div className="alert alert-warn">
        Experimentell: Das gewählte Skript von community-scripts.org wird per SSH
        auf dem Proxmox-Host ausgeführt und erstellt einen eigenen App-Container.
        Es lädt und startet Code als <strong>root</strong> aus dem Internet.
        Interaktive Abfragen werden best-effort mit Standardeinstellungen
        beantwortet; der Verlauf erscheint im Tab „Aufträge“.
      </div>
      {error && <div className="alert alert-error">{error}</div>}

      <label>
        Script-Name (Slug von community-scripts.org)
        <input
          type="text"
          list="cs-suggestions"
          placeholder="z. B. jellyfin"
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
        />
        <datalist id="cs-suggestions">
          {suggestions.map((s) => (
            <option key={s.slug} value={s.slug}>
              {s.name}
            </option>
          ))}
        </datalist>
      </label>

      <div className="actions">
        <button className="btn btn-primary" disabled={busy} onClick={() => run()}>
          {busy ? "Startet …" : "Ausführen"}
        </button>
      </div>

      <h4>Vorschläge</h4>
      <div className="software-items">
        {suggestions.map((s) => (
          <button
            key={s.slug}
            className="btn btn-ghost btn-sm"
            disabled={busy}
            onClick={() => run(s.slug)}
            title={s.slug}
          >
            {s.name}
          </button>
        ))}
      </div>
    </div>
  );
}
