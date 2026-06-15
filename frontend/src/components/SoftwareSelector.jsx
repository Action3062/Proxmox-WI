import { useMemo } from "react";

const CATEGORY_LABELS = {
  base: "Standardpakete",
  container: "Container",
  web: "Webserver",
  database: "Datenbanken",
  runtime: "Laufzeitumgebungen",
  security: "Sicherheit",
  management: "Verwaltung",
};

export default function SoftwareSelector({ catalog, selected, onChange }) {
  const groups = useMemo(() => {
    const byCategory = {};
    for (const pkg of catalog) {
      (byCategory[pkg.category] ||= []).push(pkg);
    }
    // Base packages first, then the rest alphabetically by label.
    const order = Object.keys(byCategory).sort((a, b) =>
      a === "base" ? -1 : b === "base" ? 1 : a.localeCompare(b)
    );
    return order.map((cat) => [cat, byCategory[cat]]);
  }, [catalog]);

  const toggle = (id) => {
    if (selected.includes(id)) onChange(selected.filter((s) => s !== id));
    else onChange([...selected, id]);
  };

  if (!catalog.length) {
    return <p className="muted">Softwarekatalog wird geladen …</p>;
  }

  return (
    <div className="software">
      {groups.map(([category, pkgs]) => (
        <div key={category} className="software-group">
          <h4>{CATEGORY_LABELS[category] || category}</h4>
          <div className="software-items">
            {pkgs.map((pkg) => (
              <label key={pkg.id} className="software-item" title={pkg.description}>
                <input
                  type="checkbox"
                  checked={selected.includes(pkg.id)}
                  onChange={() => toggle(pkg.id)}
                />
                <span>{pkg.label}</span>
              </label>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
