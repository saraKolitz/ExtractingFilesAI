"use client";

import { useRef, useState } from "react";
import type { Conflict } from "@/lib/crossCheck";
import type { CellState } from "@/lib/fields";

interface Props {
  label: string;
  value: string;
  state: CellState;
  score?: number;
  /** A duplicate or contradiction with another row (advisory, never changes the value). */
  conflict?: Conflict;
  /** A barcode on the page gave the same number - a second source. */
  verified?: boolean;
  /** Returns an error message if the typed value may not be saved. */
  validate?: (value: string) => string | null;
  onCommit: (value: string) => void;
}

export const BADGE: Record<Exclude<CellState, "ok">, { text: string; hint: string }> = {
  review: { text: "לבדיקה", hint: "הערך נקרא אך אינו וודאי - יש לבדוק מול הטופס המקורי" },
  unread: { text: "לא נקרא", hint: "הביטחון נמוך מדי - השדה נשאר ריק, יש להקליד לפי הטופס המקורי" },
};

// The input is the cell: click and type. The draft is kept locally and committed on blur/Enter,
// so a half-typed value never reaches the table state or the export.
export function EditableCell({ label, value, state, score, conflict, verified, validate, onCommit }: Props) {
  const [draft, setDraft] = useState<string | null>(null);
  const badge = state === "ok" ? null : BADGE[state];
  const cancelled = useRef(false);
  const error = draft !== null ? (validate?.(draft) ?? null) : null;
  const commit = () => {
    if (cancelled.current) {
      cancelled.current = false;
      setDraft(null);
      return;
    }
    if (draft === null) return;
    // A value that breaks the rule is not saved: the draft stays in the cell, with the message.
    if (error) return;
    if (draft !== value) onCommit(draft);
    setDraft(null);
  };

  return (
    <td className={`cell cell-${state}${conflict ? " cell-conflict" : ""}${error ? " cell-invalid" : ""}`}>
      <input
        className="cell-input"
        dir="auto"
        aria-label={label}
        aria-invalid={error ? true : undefined}
        // Nothing was recognised: show NULL. It is only a placeholder, never a value, so the
        // field is still empty underneath (and in the export).
        placeholder="NULL"
        value={draft ?? value}
        title={[badge?.hint, conflict?.text].filter(Boolean).join(" · ") || undefined}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !error) e.currentTarget.blur();
          if (e.key === "Escape") {
            // blur() runs onBlur with the draft still in this render, so mark it as a cancel.
            cancelled.current = true;
            e.currentTarget.blur();
          }
        }}
      />
      {error && (
        <span className="cell-error" role="alert">
          {error}
        </span>
      )}
      {verified && state === "ok" && (
        <span className="badge badge-verified" title="הברקוד בעמוד זהה למספר שנקרא">
          ברקוד ✓
        </span>
      )}
      {conflict && (
        <span className="badge badge-conflict" title={conflict.text}>
          {conflict.kind === "duplicate" ? "כפילות" : "סתירה"}
        </span>
      )}
      {badge && (
        <span className="badge" title={badge.hint}>
          {badge.text}
          {typeof score === "number" && ` ${score}%`}
        </span>
      )}
    </td>
  );
}
