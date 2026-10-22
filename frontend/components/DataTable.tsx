"use client";

import { cellState, type Row } from "@/hooks/useBatch";
import type { RowConflicts } from "@/lib/crossCheck";
import { FIELDS, FIELD_VALIDATORS, type FieldKey } from "@/lib/fields";
import { EditableCell } from "./EditableCell";

interface Props {
  rows: Row[];
  conflicts: Record<string, RowConflicts>;
  onEdit: (rowKey: string, field: FieldKey, value: string) => void;
  onRetry: (rowKey: string) => void;
  onRemove: (rowKey: string) => void;
  onView: (row: Row) => void;
}

function RowStatusCell({ row, onRetry }: { row: Row; onRetry: (key: string) => void }) {
  if (row.status === "error") {
    return (
      <td className="status-cell" colSpan={FIELDS.length}>
        <span className="error" dir="auto">
          {row.error}
        </span>
        <button className="link-btn" onClick={() => onRetry(row.key)}>
          נסו שוב
        </button>
      </td>
    );
  }
  return (
    <td className="status-cell" colSpan={FIELDS.length}>
      {row.status !== "pending" && <span className="spinner" aria-hidden />}
      <span className="muted">{row.status === "pending" ? "ממתין להפעלת החילוץ" : row.status === "queued" ? "ממתין בתור..." : "מחלץ נתונים..."}</span>
    </td>
  );
}

export function DataTable({ rows, conflicts, onEdit, onRetry, onRemove, onView }: Props) {
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th className="col-num">#</th>
            <th className="col-doc">מסמך</th>
            {FIELDS.map((f) => (
              <th key={f.key}>{f.label}</th>
            ))}
            <th className="col-act" aria-label="פעולות" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={row.key}>
              <td className="col-num">{i + 1}</td>
              <td className="col-doc">
                <button className="doc-btn" onClick={() => onView(row)} title="הצגת הטופס המקורי">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={row.page.thumbnail_url} alt="" loading="lazy" />
                  <span dir="ltr" className="file-name">
                    {row.page.file_name} · {row.page.page_number}
                  </span>
                </button>
              </td>
              {row.status === "done" ? (
                FIELDS.map((f) => (
                  <EditableCell
                    key={f.key}
                    label={f.label}
                    value={row.values[f.key]}
                    state={cellState(row, f.key)}
                    score={row.confidence[f.key]}
                    conflict={conflicts[row.key]?.[f.key]}
                    verified={row.barcodeConfirmed.includes(f.key)}
                    validate={FIELD_VALIDATORS[f.key]}
                    onCommit={(v) => onEdit(row.key, f.key, v)}
                  />
                ))
              ) : (
                <RowStatusCell row={row} onRetry={onRetry} />
              )}
              <td className="col-act">
                <button className="icon-btn" onClick={() => onRemove(row.key)} title="הסרת השורה" aria-label="הסרת השורה">
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
