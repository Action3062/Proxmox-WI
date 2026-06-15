// Maps a job status to a coloured badge.
const VARIANTS = {
  pending: "badge-info",
  creating: "badge-info",
  starting: "badge-info",
  installing: "badge-info",
  checking_updates: "badge-info",
  installing_updates: "badge-info",
  done: "badge-success",
  error: "badge-error",
};

const LABELS = {
  pending: "In Warteschlange",
  creating: "Erstellung",
  starting: "Start",
  installing: "Installation",
  checking_updates: "Update-Prüfung",
  installing_updates: "Update-Installation",
  done: "Fertig",
  error: "Fehler",
};

export default function StatusBadge({ status }) {
  return (
    <span className={`badge ${VARIANTS[status] || "badge-info"}`}>
      {LABELS[status] || status}
    </span>
  );
}
