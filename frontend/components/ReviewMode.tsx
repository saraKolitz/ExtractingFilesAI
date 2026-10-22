"use client";

import { useEffect, useRef, useState } from "react";
import { cellState, type ReviewItem, type Row } from "@/hooks/useBatch";
import type { RowConflicts } from "@/lib/crossCheck";
import { FIELDS, FIELD_VALIDATORS, type Box, type FieldKey } from "@/lib/fields";
import type { PageInfo } from "@/lib/api";
import { BADGE } from "./EditableCell";

interface Props {
  rows: Row[];
  queue: ReviewItem[];
  conflicts: Record<string, RowConflicts>;
  onEdit: (rowKey: string, field: FieldKey, value: string) => void;
  onConfirm: (rowKey: string, field: FieldKey) => void;
  onClose: () => void;
}

const MAX_VIEW_VH = 55;

// Zooms the page to the spot where the field was read: the region plus some surrounding context,
// with the exact region outlined. Without a recorded region the whole page is shown instead.
function RegionView({ page, boxes }: { page: PageInfo; boxes?: Box[] }) {
  const [index, setIndex] = useState(0);
  const [full, setFull] = useState(false);
  const box = boxes?.[Math.min(index, (boxes?.length ?? 1) - 1)];

  if (!box || full) {
    return (
      <div className="region">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="region-full" src={page.image_url} alt={`${page.file_name} - עמוד ${page.page_number}`} />
        <p className="muted">{box ? "מוצג כל העמוד." : "לא נשמר אזור מדויק לשדה הזה - מוצג כל העמוד."}</p>
        {box && (
          <button className="link-btn" onClick={() => setFull(false)}>
            חזרה להגדלה
          </button>
        )}
      </div>
    );
  }

  const [x0, y0, x1, y1] = box;
  const padX = Math.max((x1 - x0) * 0.6, 0.03);
  const padY = Math.max((y1 - y0) * 0.6, 0.03);
  const px0 = Math.max(0, x0 - padX);
  const py0 = Math.max(0, y0 - padY);
  const pw = Math.min(1, x1 + padX) - px0;
  const ph = Math.min(1, y1 + padY) - py0;
  const aspect = (pw * page.width) / (ph * page.height);

  return (
    <div className="region">
      <div
        className="region-zoom"
        role="img"
        aria-label="הגדלה של האזור בטופס"
        style={{
          aspectRatio: aspect,
          width: `min(100%, ${MAX_VIEW_VH * aspect}vh)`,
          backgroundImage: `url(${page.image_url})`,
          backgroundSize: `${100 / pw}% ${100 / ph}%`,
          backgroundPosition: `${pw < 1 ? (px0 / (1 - pw)) * 100 : 0}% ${ph < 1 ? (py0 / (1 - ph)) * 100 : 0}%`,
        }}
      >
        <span
          className="region-box"
          style={{
            left: `${((x0 - px0) / pw) * 100}%`,
            top: `${((y0 - py0) / ph) * 100}%`,
            width: `${((x1 - x0) / pw) * 100}%`,
            height: `${((y1 - y0) / ph) * 100}%`,
          }}
        />
      </div>
      <div className="region-tools">
        {boxes && boxes.length > 1 && (
          <>
            <button className="link-btn" onClick={() => setIndex((i) => (i + 1) % boxes.length)}>
              אזור הבא ({Math.min(index, boxes.length - 1) + 1}/{boxes.length})
            </button>
            <span className="muted">·</span>
          </>
        )}
        <button className="link-btn" onClick={() => setFull(true)}>
          הצג עמוד מלא
        </button>
      </div>
    </div>
  );
}

// One flagged cell at a time, next to the part of the page it came from. The list is fixed when
// the mode opens (a confirmed cell would otherwise vanish from under the reviewer).
export function ReviewMode({ rows, queue, conflicts, onEdit, onConfirm, onClose }: Props) {
  const [items] = useState(queue);
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState<string | null>(null);
  const [reviewed, setReviewed] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const item = items[index];
  const row = rows.find((r) => r.key === item?.rowKey);
  const finished = !item || !row;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [index]);

  if (finished) {
    return (
      <div className="overlay" onClick={onClose}>
        <div className="review review-done" role="dialog" aria-modal onClick={(e) => e.stopPropagation()}>
          <div className="review-done-icon" aria-hidden>
            ✓
          </div>
          <h2>סיימת לעבור על כל השדות החשודים</h2>
          <p className="muted">{reviewed} שדות נבדקו. אפשר לייצא ל-Excel.</p>
          <button className="extract-all-btn" onClick={onClose}>
            חזרה לטבלה
          </button>
        </div>
      </div>
    );
  }

  const field = FIELDS.find((f) => f.key === item.field)!;
  const state = cellState(row, item.field);
  const conflict = conflicts[row.key]?.[item.field];
  const value = draft ?? row.values[item.field];
  // The same rule as in the table: a value that breaks it can be neither confirmed nor saved.
  const error = FIELD_VALIDATORS[item.field]?.(value) ?? null;
  const rowNumber = rows.findIndex((r) => r.key === row.key) + 1;

  const go = (next: number) => {
    setDraft(null);
    setIndex(Math.max(0, Math.min(items.length, next)));
  };
  // Enter: a typed change is saved as an edit, an untouched value is confirmed as correct.
  const accept = () => {
    if (error) return;
    if (draft !== null && draft !== row.values[item.field]) onEdit(row.key, item.field, draft);
    else onConfirm(row.key, item.field);
    setReviewed((n) => n + 1);
    go(index + 1);
  };

  return (
    <div className="overlay" onClick={onClose}>
      <div className="review" role="dialog" aria-modal aria-label="בדיקה מהירה" onClick={(e) => e.stopPropagation()}>
        <div className="review-head">
          <strong>בדיקה מהירה</strong>
          <span className="muted">
            {index + 1} מתוך {items.length}
          </span>
          <button className="link-btn" onClick={onClose}>
            סגירה ✕
          </button>
        </div>
        <div className="progress-bar" role="progressbar" aria-valuenow={index} aria-valuemin={0} aria-valuemax={items.length}>
          <div className="progress-fill" style={{ width: `${(index / items.length) * 100}%` }} />
        </div>

        <div className="review-body">
          <RegionView key={`${row.key}-${item.field}`} page={row.page} boxes={row.regions[item.field]} />
          <div className="review-side">
            <p className="muted">
              שורה {rowNumber} · <span dir="ltr">{row.page.file_name}</span> · עמוד {row.page.page_number}
            </p>
            <h3>{field.label}</h3>
            {state !== "ok" && (
              <p className={`review-reason ${state}`}>
                {BADGE[state].text}
                {typeof row.confidence[item.field] === "number" && ` ${row.confidence[item.field]}%`} - {BADGE[state].hint}
              </p>
            )}
            {item.spot && (
              <p className="review-reason spot">
                בדיקת דגימה: השדה לא סומן, והוא נבחר באקראי כדי למדוד את דיוק המערכת. כדאי לבדוק אותו מול הטופס באמת.
              </p>
            )}
            {conflict && (
              <p className="review-reason conflict">
                {conflict.kind === "duplicate" ? "כפילות" : "סתירה"}: {conflict.text}
              </p>
            )}
            {item.field === "central_numbers" && row.barcodeSuggestions.map((s) => (
              <p className="review-reason review" key={s.read + s.barcode}>
                הברקוד בעמוד קרא {s.barcode}, והמספר שנקרא הוא {s.read} - ייתכן שגיאת קריאה.
              </p>
            ))}
            {error && (
              <p className="review-reason unread" role="alert">
                {error}
              </p>
            )}
            <input
              ref={inputRef}
              className="review-input"
              dir="auto"
              aria-label={field.label}
              placeholder="NULL"
              value={value}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && accept()}
            />
            {item.field === "central_numbers" && row.barcodes.length > 0 && (
              <div className="barcode-chips">
                <span className="muted">ברקודים בעמוד (לחיצה מוסיפה לשדה):</span>
                {row.barcodes.map((code) => (
                  <button
                    key={code}
                    className="chip"
                    onClick={() => {
                      const current = value.trim();
                      if (!current.split(",").map((n) => n.trim()).includes(code)) setDraft(current ? `${current}, ${code}` : code);
                      inputRef.current?.focus();
                    }}
                  >
                    {code}
                  </button>
                ))}
              </div>
            )}
            <div className="review-actions">
              <button className="extract-all-btn" disabled={!!error} onClick={accept}>
                {draft !== null && draft !== row.values[item.field] ? "שמור והבא" : "תקין, הבא"}
              </button>
              <button className="link-btn" disabled={index === 0} onClick={() => go(index - 1)}>
                הקודם
              </button>
              <button className="link-btn" onClick={() => go(index + 1)}>
                דלג
              </button>
            </div>
            <p className="muted">Enter - אישור והבא · Esc - סגירה</p>
          </div>
        </div>
      </div>
    </div>
  );
}
