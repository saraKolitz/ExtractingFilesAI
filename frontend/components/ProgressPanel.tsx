import type { Row, UploadPhase } from "@/hooks/useBatch";

interface Props {
  rows: Row[];
  phase: UploadPhase;
}

interface FileProgress {
  name: string;
  pages: number;
  done: number;
  failed: number;
  pending: number;
  entities: string[];
}

function groupByFile(rows: Row[]): FileProgress[] {
  const files = new Map<string, FileProgress>();
  for (const r of rows) {
    const f = files.get(r.page.file_id) ?? { name: r.page.file_name, pages: 0, done: 0, failed: 0, pending: 0, entities: [] };
    f.pages += 1;
    if (r.status === "pending") f.pending += 1;
    if (r.status === "done") f.done += 1;
    if (r.status === "error") f.failed += 1;
    const entity = r.values.sending_entity;
    if (r.status === "done" && entity && !f.entities.includes(entity)) f.entities.push(entity);
    files.set(r.page.file_id, f);
  }
  return [...files.values()];
}

// Three stages per file: split into pages (done as soon as the pages exist), identify the sender,
// extract the fields. The sender comes out of the same extraction call, so the last two advance together.
export function ProgressPanel({ rows, phase }: Props) {
  const files = groupByFile(rows);
  if (files.length === 0 && phase === "idle") return null;
  return (
    <section className="panel" aria-label="התקדמות עיבוד">
      <h2>התקדמות עיבוד</h2>
      {phase === "uploading" && <p className="muted">מפצל קבצים לעמודים...</p>}
      <ul className="progress-list">
        {files.map((f) => {
          const pct = Math.round(((f.done + f.failed) / f.pages) * 100);
          const finished = f.done + f.failed === f.pages;
          return (
            <li key={f.name + f.pages} className="progress-item">
              <div className="progress-head">
                <span className="file-name" dir="ltr" title={f.name}>
                  {f.name}
                </span>
                <span className="muted">
                  {f.done}/{f.pages} עמודים
                </span>
              </div>
              <div className="progress-bar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
                <div className={`progress-fill${f.failed ? " has-error" : ""}`} style={{ width: `${pct}%` }} />
              </div>
              <div className="stages">
                <span className="stage done">✓ פוצל ל-{f.pages} עמודים</span>
                <span className={`stage${f.entities.length ? " done" : ""}`}>
                  {f.entities.length
                    ? `✓ זוהה: ${f.entities.join(", ")}`
                    : finished
                      ? "גורם שולח לא זוהה"
                      : f.pending === f.pages
                        ? "ממתין לחילוץ"
                        : "מזהה גורם שולח..."}
                </span>
                <span className={`stage${finished && !f.failed ? " done" : ""}`}>
                  {finished
                    ? f.failed
                      ? `${f.failed} עמודים נכשלו`
                      : "✓ החילוץ הושלם"
                    : f.pending === f.pages
                      ? "ממתין לחילוץ"
                      : "מחלץ שדות..."}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
