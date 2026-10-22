"use client";

import { cellState, type Row } from "@/hooks/useBatch";
import { commitmentLines } from "@/lib/commitments";
import type { RowConflicts } from "@/lib/crossCheck";
import { FIELDS, FIELD_VALIDATORS, type FieldKey } from "@/lib/fields";
import { EditableCell } from "./EditableCell";

interface Props {
  rows: Row[];
  conflicts: Record<string, RowConflicts>;
  onEdit: (rowKey: string, field: FieldKey, value: string) => void;
  /** A commitment cell edit: `index` is the line of that form, since each line holds one number. */
  onEditCommitment: (rowKey: string, index: number, value: string) => void;
  onRetry: (rowKey: string) => void;
  onRemove: (rowKey: string, index: number) => void;
  onView: (row: Row) => void;
}

// One line of the table. A form with several commitment numbers has one line per number, and each
// line shows the number alone with the form's other details repeated.
interface Line {
  row: Row;
  index: number;
  commitment: string;
}

const toLines = (rows: Row[]): Line[] =>
  rows.flatMap((row) =>
    row.status === "done"
      ? commitmentLines(row.values.commitment_numbers).map((commitment, index) => ({ row, index, commitment }))
      : [{ row, index: 0, commitment: "" }],
  );

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

export function DataTable({ rows, conflicts, onEdit, onEditCommitment, onRetry, onRemove, onView }: Props) {
  const lines = toLines(rows);
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
          {lines.map(({ row, index, commitment }, i) => (
            <tr key={`${row.key}#${index}`} className={index > 0 ? "line-extra" : undefined}>
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
                    value={f.key === "commitment_numbers" ? commitment : row.values[f.key]}
                    state={cellState(row, f.key)}
                    score={row.confidence[f.key]}
                    conflict={conflicts[row.key]?.[f.key]}
                    verified={row.barcodeConfirmed.includes(f.key)}
                    validate={FIELD_VALIDATORS[f.key]}
                    onCommit={(v) =>
                      f.key === "commitment_numbers" ? onEditCommitment(row.key, index, v) : onEdit(row.key, f.key, v)
                    }
                  />
                ))
              ) : (
                <RowStatusCell row={row} onRetry={onRetry} />
              )}
              <td className="col-act">
                <button className="icon-btn" onClick={() => onRemove(row.key, index)} title="הסרת השורה" aria-label="הסרת השורה">
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
