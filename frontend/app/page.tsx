"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { extractPage, uploadFiles, type PageInfo, type UploadError } from "@/lib/api";

type Extraction =
  | { status: "loading" }
  | { status: "done"; text: string }
  | { status: "error"; message: string };

const pageKey = (p: PageInfo) => `${p.file_id}-${p.page_number}`;

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
                    {extraction?.status === "done" && (
                      <div className="extract-text" dir="auto">
                        {extraction.text || "לא הוחזר טקסט"}
                      </div>
                    )}
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
