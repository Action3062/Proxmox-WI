import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api.js";
import SoftwareSelector from "./SoftwareSelector.jsx";

const INITIAL = {
  type: "lxc",
  node: "",
  os: "debian",
  template: "",
  hostname: "",
  description: "",
  cores: 2,
  memory_mb: 1024,
  disk_gb: 8,
  storage: "local-zfs",
  bridge: "vmbr0",
  ip_config: "dhcp",
  ip_address: "",
  gateway: "",
  username: "admin",
  password: "",
  ssh_key: "",
  autostart: false,
  software: [],
};

export default function CreateContainerForm({ onCreated }) {
  const [form, setForm] = useState(INITIAL);
  const [catalog, setCatalog] = useState([]);
  const [nodes, setNodes] = useState([]);
  const [storages, setStorages] = useState([]);
  const [bridges, setBridges] = useState([]);
  const [templates, setTemplates] = useState([]);
  const [vmTemplates, setVmTemplates] = useState([]);
  const [metaError, setMetaError] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const set = (key, value) => setForm((f) => ({ ...f, [key]: value }));

  // Load the software catalog and preselect the default (base) packages.
  useEffect(() => {
    (async () => {
      try {
        const pkgs = await api.software();
        setCatalog(pkgs);
        setForm((f) => ({
          ...f,
          software: pkgs.filter((p) => p.default).map((p) => p.id),
        }));
      } catch (err) {
        setMetaError(err.message);
      }
    })();
  }, []);

  // Load Proxmox metadata for the dropdowns. Failures are non-fatal: the user
  // can still type values manually (storage/bridge are free-text with hints).
  useEffect(() => {
    (async () => {
      try {
        const [nodeList, storageList, bridgeList, templateList, vmTemplateList, defaults] =
          await Promise.all([
            api.nodes().catch(() => []),
            api.storages().catch(() => []),
            api.bridges().catch(() => []),
            api.templates().catch(() => []),
            api.vmTemplates().catch(() => []),
            api.defaults().catch(() => null),
          ]);
        setNodes(nodeList);
        setStorages(storageList);
        setBridges(bridgeList);
        setTemplates(templateList);
        setVmTemplates(vmTemplateList);
        // Pre-fill storage/bridge from the server-side configured defaults.
        if (defaults) {
          setForm((f) => ({
            ...f,
            storage: defaults.storage || f.storage,
            bridge: defaults.bridge || f.bridge,
          }));
        }
        if (!templateList.length && !storageList.length) {
          setMetaError(
            "Proxmox-Metadaten konnten nicht geladen werden. Werte können manuell eingegeben werden."
          );
        }
      } catch (err) {
        setMetaError(err.message);
      }
    })();
  }, []);

  // Template options depend on the type: LXC uses ostemplate volids, VM uses
  // the VMID of a cloud-init template to clone. Both filtered by the chosen OS.
  const templateOptions = useMemo(() => {
    if (form.type === "vm") {
      return vmTemplates
        .filter((t) => t.os === form.os || t.os === "other")
        .map((t) => ({ value: String(t.vmid), label: `${t.name} (VMID ${t.vmid})` }));
    }
    return templates
      .filter((t) => t.os === form.os)
      .map((t) => ({ value: t.volid, label: `${t.label} (${t.filename})` }));
  }, [form.type, form.os, templates, vmTemplates]);

  // Keep the selected template valid for the current type/OS.
  useEffect(() => {
    if (templateOptions.length) {
      const stillValid = templateOptions.some((o) => o.value === form.template);
      if (!stillValid) set("template", templateOptions[0].value);
    } else {
      set("template", "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateOptions]);

  const onSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!form.template) {
      setError(
        form.type === "vm"
          ? "Bitte ein VM-Template auswählen (oder VMID eingeben)."
          : "Bitte ein Template auswählen (oder Template-Volid eingeben)."
      );
      return;
    }
    setBusy(true);
    try {
      const payload = {
        ...form,
        node: form.node || undefined,
        cores: Number(form.cores),
        memory_mb: Number(form.memory_mb),
        disk_gb: Number(form.disk_gb),
        ip_address: form.ip_config === "static" ? form.ip_address : undefined,
        gateway: form.ip_config === "static" ? form.gateway || undefined : undefined,
        password: form.password || undefined,
        ssh_key: form.ssh_key || undefined,
      };
      const job = await api.createContainer(payload);
      onCreated?.(job);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card form" onSubmit={onSubmit}>
      <h2>{form.type === "vm" ? "Neue VM erstellen" : "Neuen LXC-Container erstellen"}</h2>
      {metaError && <div className="alert alert-warn">{metaError}</div>}
      {error && <div className="alert alert-error">{error}</div>}

      <fieldset>
        <legend>Typ &amp; Betriebssystem</legend>
        <div className="grid">
          <label>
            Typ
            <select value={form.type} onChange={(e) => set("type", e.target.value)}>
              <option value="lxc">LXC-Container</option>
              <option value="vm">Virtuelle Maschine</option>
            </select>
          </label>
          <label>
            Betriebssystem
            <select value={form.os} onChange={(e) => set("os", e.target.value)}>
              <option value="debian">Debian</option>
              <option value="ubuntu">Ubuntu</option>
            </select>
          </label>
          <label>
            Version / Template
            {templateOptions.length ? (
              <select value={form.template} onChange={(e) => set("template", e.target.value)}>
                {templateOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                placeholder={
                  form.type === "vm"
                    ? "VMID eines Cloud-Init-Templates, z. B. 9000"
                    : "z. B. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
                }
                value={form.template}
                onChange={(e) => set("template", e.target.value)}
              />
            )}
          </label>
        </div>
        {form.type === "vm" && (
          <p className="muted small">
            VMs werden aus einem Cloud-Init-Template geklont. Im Template muss
            <code> qemu-guest-agent</code> installiert sein.
          </p>
        )}
      </fieldset>

      <fieldset>
        <legend>Identität</legend>
        <div className="grid">
          <label>
            Hostname
            <input
              type="text"
              required
              placeholder="z. B. web01"
              value={form.hostname}
              onChange={(e) => set("hostname", e.target.value)}
            />
          </label>
          {nodes.length > 1 && (
            <label>
              Node
              <select value={form.node} onChange={(e) => set("node", e.target.value)}>
                <option value="">Standard</option>
                {nodes.map((n) => (
                  <option key={n.node} value={n.node}>
                    {n.node}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="full">
            Beschreibung / Notiz (optional)
            <input
              type="text"
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
            />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Ressourcen</legend>
        <div className="grid">
          <label>
            CPU-Kerne
            <input
              type="number"
              min="1"
              max="128"
              value={form.cores}
              onChange={(e) => set("cores", e.target.value)}
            />
          </label>
          <label>
            RAM (MB)
            <input
              type="number"
              min="128"
              step="128"
              value={form.memory_mb}
              onChange={(e) => set("memory_mb", e.target.value)}
            />
          </label>
          <label>
            Speicher (GB)
            <input
              type="number"
              min="1"
              value={form.disk_gb}
              onChange={(e) => set("disk_gb", e.target.value)}
            />
          </label>
          <label>
            Storage
            <input
              type="text"
              list="storage-list"
              value={form.storage}
              onChange={(e) => set("storage", e.target.value)}
            />
            <datalist id="storage-list">
              {storages.map((s) => (
                <option key={s.storage} value={s.storage}>
                  {s.type}
                </option>
              ))}
            </datalist>
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Netzwerk</legend>
        <div className="grid">
          <label>
            Bridge
            <input
              type="text"
              list="bridge-list"
              value={form.bridge}
              onChange={(e) => set("bridge", e.target.value)}
            />
            <datalist id="bridge-list">
              {bridges.map((b) => (
                <option key={b.name} value={b.name} />
              ))}
            </datalist>
          </label>
          <label>
            IP-Konfiguration
            <select value={form.ip_config} onChange={(e) => set("ip_config", e.target.value)}>
              <option value="dhcp">DHCP</option>
              <option value="static">Statische IP</option>
            </select>
          </label>
          {form.ip_config === "static" && (
            <>
              <label>
                IP-Adresse (CIDR)
                <input
                  type="text"
                  placeholder="192.168.1.50/24"
                  value={form.ip_address}
                  onChange={(e) => set("ip_address", e.target.value)}
                />
              </label>
              <label>
                Gateway
                <input
                  type="text"
                  placeholder="192.168.1.1"
                  value={form.gateway}
                  onChange={(e) => set("gateway", e.target.value)}
                />
              </label>
            </>
          )}
        </div>
      </fieldset>

      <fieldset>
        <legend>Zugangsdaten</legend>
        <p className="muted small">Mindestens Passwort oder SSH-Key angeben.</p>
        <div className="grid">
          <label>
            Benutzername
            <input
              type="text"
              required
              value={form.username}
              onChange={(e) => set("username", e.target.value)}
            />
          </label>
          <label>
            Passwort
            <input
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(e) => set("password", e.target.value)}
            />
          </label>
          <label className="full">
            SSH Public Key (optional)
            <textarea
              rows="3"
              placeholder="ssh-ed25519 AAAA..."
              value={form.ssh_key}
              onChange={(e) => set("ssh_key", e.target.value)}
            />
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>Optionen</legend>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.autostart}
            onChange={(e) => set("autostart", e.target.checked)}
          />
          Autostart aktivieren
        </label>
      </fieldset>

      <fieldset>
        <legend>Software</legend>
        <SoftwareSelector
          catalog={catalog}
          selected={form.software}
          onChange={(ids) => set("software", ids)}
        />
      </fieldset>

      <div className="actions">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy
            ? "Wird erstellt …"
            : form.type === "vm"
            ? "VM erstellen"
            : "Container erstellen"}
        </button>
      </div>
    </form>
  );
}
