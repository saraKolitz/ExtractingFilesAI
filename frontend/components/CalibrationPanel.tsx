"use client";

import { useMemo } from "react";
import type { Row } from "@/hooks/useBatch";
import { MIN_SAMPLES, TARGET_ACCURACY, calibrate, type Suggestion } from "@/lib/calibration";
import { FIELDS } from "@/lib/fields";

const FIELD_KEYS = FIELDS.map((f) => f.key);
const pct = (x: number) => `${Math.round(x * 100)}%`;

function SuggestionText({ s }: { s: Suggestion }) {
  switch (s.kind) {
    case "need-data":
      return (
        <>
          נבדקו עד כה {s.have} שדות. כדי להמליץ על סף אמין צריך לפחות {s.need}: אשרו או תקנו עוד שדות בבדיקה המהירה.
        </>
      );
    case "none":
      return <>אין ציון שבו המודל מדויק ב-{pct(TARGET_ACCURACY)} לפחות, ולכן כדאי לבדוק את כל השדות ולא להסתמך על הציון.</>;
    case "keep":
      return (
        <>
          הסף הנוכחי מתאים לנתונים: שדות בציון {s.cutoff} ומעלה היו נכונים ב-{pct(s.accuracy)} ({s.count} שדות שנבדקו).
        </>
      );
    case "lower":
      return (
        <>
          אפשר לבדוק פחות: שדות בציון {s.cutoff} ומעלה היו נכונים ב-{pct(s.accuracy)} ({s.count} שדות שנבדקו), אבל כיום רק ציון {s.current} ומעלה עובר
          בלי בדיקה. בטפסים האלה זה היה חוסך בדיקה ידנית של {s.saved} שדות. לשינוי: <code>CONF_OK_ABOVE={s.cutoff - 1}</code> ב-.env.
        </>
      );
    case "raise":
      return (
        <>
          הסף נמוך מדי: שדות בציון {s.current} עד {s.cutoff - 1} לא היו מדויקים מספיק. מדיוק של {pct(TARGET_ACCURACY)} ומעלה התחיל רק בציון {s.cutoff} (
          {pct(s.accuracy)}, {s.count} שדות). לשינוי: <code>CONF_OK_ABOVE={s.cutoff - 1}</code> ב-.env.
        </>
      );
  }
}

// Shows how far the model's own confidence scores can be trusted, measured from what the
// technicians confirmed and corrected. It recommends a threshold; it never changes one.
export function CalibrationPanel({ rows }: { rows: Row[] }) {
  const c = useMemo(() => calibrate(rows, FIELD_KEYS), [rows]);
  const labelOf = (key: string) => FIELDS.find((f) => f.key === key)?.label ?? key;

  return (
    <details className="panel calibration" open={c.reviewed > 0}>
      <summary>
        <strong>כיול אמון</strong>
        <span className="muted"> - כמה אפשר לסמוך על הציונים של המודל</span>
      </summary>

      {c.reviewed === 0 ? (
        <p className="muted">
          עוד לא נבדק אף שדה. כל אישור או תיקון בטבלה או בבדיקה המהירה מלמד את המערכת אם המודל צדק, ומכאן יוצג כאן כמה אפשר לסמוך עליו (נדרשים {MIN_SAMPLES} שדות
          להמלצה).
        </p>
      ) : (
        <div className="calibration-body">
          <div className="calibration-headline">
            <span className="calibration-score">{pct(c.accuracy ?? 0)}</span>
            <span>
              מהשדות שבדקתם המודל צדק בהם ({c.correct} מתוך {c.reviewed})
            </span>
          </div>

          <div className="bands" role="table" aria-label="דיוק לפי ציון ביטחון">
            {c.bands.map((b) => (
              <div className="band" role="row" key={b.from}>
                <span className="band-label" role="cell">
                  ציון {b.from}–{b.to}
                </span>
                <span className="band-bar" role="cell" aria-hidden>
                  {b.accuracy !== null && (
                    <span className={`band-fill${b.accuracy >= TARGET_ACCURACY ? " good" : b.accuracy >= 0.8 ? " mid" : " low"}`} style={{ width: `${b.accuracy * 100}%` }} />
                  )}
                </span>
                <span className="band-value" role="cell">
                  {b.accuracy === null ? "אין נתונים" : `${pct(b.accuracy)} (${b.correct}/${b.reviewed})`}
                </span>
              </div>
            ))}
          </div>

          <div className="calibration-suggestion">
            <SuggestionText s={c.suggestion} />
          </div>

          <div className="field-stats">
            {c.fields
              .filter((f) => f.reviewed > 0)
              .map((f) => (
                <span className="chip" key={f.field}>
                  {labelOf(f.field)}: {f.correct}/{f.reviewed}
                </span>
              ))}
          </div>
          <p className="muted">
            מבוסס רק על שדות שנבדקו ידנית. הבדיקה המהירה כוללת לבד גם דגימה אקראית של שדות שלא סומנו, כדי שגם הציונים הגבוהים יימדדו. שדה שדולג עליו לא נספר.
          </p>
        </div>
      )}
    </details>
  );
}
