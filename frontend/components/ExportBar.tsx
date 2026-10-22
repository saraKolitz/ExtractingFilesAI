interface Props {
  stats: { total: number; done: number; needReview: number; pending: number; awaiting: number; failed: number; secondsSaved: number };
  exporting: boolean;
  onExport: () => void;
  onExtract: () => void;
}

function formatSaved(seconds: number) {
  if (seconds < 60) return `${seconds} שנ׳`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} דק׳` : `${Math.floor(minutes / 60)} שע׳ ${minutes % 60} דק׳`;
}

export function ExportBar({ stats, exporting, onExport, onExtract }: Props) {
  const metrics = [
    { label: "טפסים שעובדו", value: `${stats.done}/${stats.total}`, tone: "" },
    { label: "דורשים בדיקה", value: String(stats.needReview), tone: stats.needReview ? "warn" : "" },
    { label: "זמן שנחסך (הערכה)", value: formatSaved(stats.secondsSaved), tone: "" },
  ];
  return (
    <div className="export-bar">
      <div className="metrics">
        {metrics.map((m) => (
          <div className={`metric ${m.tone}`} key={m.label}>
            <span className="metric-value">{m.value}</span>
            <span className="metric-label">{m.label}</span>
          </div>
        ))}
      </div>
      <div className="actions">
        <button className="extract-all-btn" disabled={stats.awaiting === 0} onClick={onExtract}>
          {stats.awaiting > 0 ? `חלץ נתונים מכל הקבצים (${stats.awaiting})` : "חלץ נתונים מכל הקבצים"}
        </button>
        <button className="export-btn" disabled={exporting || stats.done === 0} onClick={onExport}>
          {exporting ? "מייצא..." : "ייצוא ל-Excel"}
        </button>
      </div>
    </div>
  );
}
