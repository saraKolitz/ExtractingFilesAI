"use client";

import { useState } from "react";
import type { CellState } from "@/lib/fields";

interface Props {
  label: string;
  value: string;
  state: CellState;
  score?: number;
  onCommit: (value: string) => void;
}

const BADGE: Record<Exclude<CellState, "ok">, { text: string; hint: string }> = {
  review: { text: "לבדיקה", hint: "הערך נקרא אך אינו וודאי - יש לבדוק מול הטופס המקורי" },
  unread: { text: "לא נקרא", hint: "הביטחון נמוך מדי - השדה נשאר ריק, יש להקליד לפי הטופס המקורי" },
};

// The input is the cell: click and type. The draft is kept locally and committed on blur/Enter,
// so a half-typed value never reaches the table state or the export.
export function EditableCell({ label, value, state, score, onCommit }: Props) {
  const [draft, setDraft] = useState<string | null>(null);
  const badge = state === "ok" ? null : BADGE[state];
  const commit = () => {
    if (draft !== null && draft !== value) onCommit(draft);
    setDraft(null);
  };

  return (
    <td className={`cell cell-${state}`}>
      <input
        className="cell-input"
        dir="auto"
        aria-label={label}
        // Nothing was recognised: show NULL. It is only a placeholder, never a value, so the
        // field is still empty underneath (and in the export).
        placeholder="NULL"
        value={draft ?? value}
        title={badge?.hint}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") {
            setDraft(null);
            e.currentTarget.blur();
          }
        }}
      />
      {badge && (
        <span className="badge" title={badge.hint}>
          {badge.text}
          {typeof score === "number" && ` ${score}%`}
        </span>
      )}
    </td>
  );
}
