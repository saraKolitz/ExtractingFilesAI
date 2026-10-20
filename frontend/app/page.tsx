"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { extractPage, uploadFiles, type PageInfo, type UploadError } from "@/lib/api";

type Extraction =
  | { status: "loading" }
  | { status: "done"; text: string }
  | { status: "error"; message: string };

const pageKey = (p: PageInfo) => `${p.file_id}-${p.page_number}`;

// The extracted fields, in the order of the data model (CLAUDE.md section 4).
const FIELDS: [key: string, label: string][] = [
  ["first_name", "שם פרטי"],
  ["last_name", "שם משפחה"],
  ["id_number", "תעודת זהות"],
  ["central_numbers", "מספר מרכזי"],
  ["received_date", "תאריך קבלה"],
  ["sending_entity", "גורם שולח"],
  ["commitment_numbers", "מספר התחייבות"],
];

interface ExtractedData {
  confidence?: Record<string, number>;
  fields_requiring_review?: string[];
  // Sent by the backend so the colour bands follow the thresholds in .env, not a copy of them.
  thresholds?: { empty_below?: number; ok_above?: number };
  [key: string]: unknown;
}

function parseExtraction(text: string): ExtractedData | null {
  try {
    const data: unknown = JSON.parse(text);
    return data && typeof data === "object" && !Array.isArray(data) ? (data as ExtractedData) : null;
  } catch {
    return null;
  }
}

const fieldValue = (value: unknown) =>
  Array.isArray(value) ? value.join(", ") : typeof value === "string" ? value : "";

// A field the model was not certain about is shown with its confidence score, in one of two
// shades: red only for a score under the "empty below" threshold - nothing was really read there
// - and orange for a value that was read but still has to be checked against the image.
function ExtractedFields({ text }: { text: string }) {
  const data = parseExtraction(text);
  // Whatever we could not parse still has to reach the reviewer - show it exactly as it came.
  if (!data) {
    return (
      <div className="extract-text" dir="auto">
        {text || "לא הוחזר טקסט"}
      </div>
    );
  }
  const flagged = new Set(
    Array.isArray(data.fields_requiring_review) ? data.fields_requiring_review : [],
  );
  const emptyBelow = data.thresholds?.empty_below ?? 50;
  return (
    <div className="fields" dir="rtl">
      {FIELDS.map(([key, label]) => {
        const value = fieldValue(data[key]);
        const score = data.confidence?.[key];
        const needsReview = flagged.has(key);
        // Red is reserved for "not read": a score under the threshold (the value was dropped).
        // A field flagged without a score was read, so it stays orange.
        const unread = typeof score === "number" && score < emptyBelow;
        return (
          <div className={`field${needsReview ? " flagged" : ""}${unread ? " unread" : ""}`} key={key}>
            <span className="field-label">{label}</span>
            {/* Nothing was read (or it scored below the threshold and was dropped): say so with
                the literal null, not a blank, so an unread field can't look like a blank form. */}
            <span className={`field-value${value ? "" : " field-null"}`} dir="auto">
              {value || "null"}
            </span>
            {needsReview && typeof score === "number" && <span className="field-score">{score}%</span>}
          </div>
        );
      })}
      <details>
        <summary>JSON מלא</summary>
        <div className="extract-text" dir="auto">
          {text}
        </div>
      </details>
    </div>
  );
}

const EXTRACT_LABEL = {
  idle: "חלץ נתונים",
  loading: "טוען...",
  done: "חלץ שוב",
  error: "נסו שוב",
};

export default function Home() {
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [errors, setErrors] = useState<UploadError[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [selected, setSelected] = useState<PageInfo | null>(null);
  const [extractions, setExtractions] = useState<Record<string, Extraction>>({});
  const inputRef = useRef<HTMLInputElement>(null);

  const handleExtract = useCallback(async (page: PageInfo) => {
    const key = pageKey(page);
    setExtractions((prev) => ({ ...prev, [key]: { status: "loading" } }));
    try {
      const text = await extractPage(page.file_id, page.page_number);
      setExtractions((prev) => ({ ...prev, [key]: { status: "done", text } }));
    } catch (e) {
      const message = e instanceof Error ? e.message : "החילוץ נכשל";
      setExtractions((prev) => ({ ...prev, [key]: { status: "error", message } }));
    }
  }, []);

  const handleFiles = useCallback(async (list: FileList | File[]) => {
    const files = Array.from(list);
    if (files.length === 0) return;
    setLoading(true);
    setMessage(null);
    try {
      const result = await uploadFiles(files);
      setPages((prev) => [...prev, ...result.pages]);
      setErrors(result.errors);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "ההעלאה נכשלה");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!selected) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelected(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected]);

  return (
    <main className="container">
      <h1>העלאת טפסי התחייבות</h1>

      <div
        className={`dropzone${dragging ? " dragging" : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void handleFiles(e.dataTransfer.files);
        }}
      >
        <p>{loading ? "מעלה ומעבד קבצים..." : "גררו לכאן קובצי PDF או תמונות, או לחצו לבחירה"}</p>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,image/*"
          hidden
          onChange={(e) => {
            if (e.target.files) void handleFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {message && <p className="error">{message}</p>}
      {errors.map((err, i) => (
        <p className="error" key={i}>
          {err.file_name}: {err.detail}
        </p>
      ))}

      {pages.length > 0 && (
        <>
          <h2>{pages.length} עמודים</h2>
          <div className="grid">
            {pages.map((p) => {
              const extraction = extractions[pageKey(p)];
              const status = extraction?.status ?? "idle";
              return (
                <div key={pageKey(p)} className="card">
                  <button className="card-open" onClick={() => setSelected(p)}>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={p.thumbnail_url} alt={`${p.file_name} - עמוד ${p.page_number}`} loading="lazy" />
                    <span className="name" title={p.file_name}>
                      {p.file_name}
                    </span>
                    <span>עמוד {p.page_number}</span>
                  </button>
                  <button
                    className="extract-btn"
                    disabled={status === "loading"}
                    onClick={() => void handleExtract(p)}
                  >
                    {EXTRACT_LABEL[status]}
                  </button>
                  <div aria-live="polite">
                    {extraction?.status === "error" && (
                      <p className="error extract-error" dir="auto">
                        {extraction.message}
                      </p>
                    )}
                    {extraction?.status === "done" && <ExtractedFields text={extraction.text} />}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}

      {selected && (
        <div className="overlay" onClick={() => setSelected(null)}>
          <div className="viewer" onClick={(e) => e.stopPropagation()}>
            <div className="viewer-bar">
              <span>
                {selected.file_name} - עמוד {selected.page_number}
              </span>
              <button onClick={() => setSelected(null)}>סגירה ✕</button>
            </div>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={selected.image_url} alt={`${selected.file_name} - עמוד ${selected.page_number}`} />
          </div>
        </div>
      )}
    </main>
  );
}
