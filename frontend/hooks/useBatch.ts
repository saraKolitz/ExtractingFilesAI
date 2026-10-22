"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { exportExcel, extractPage, uploadFiles, type PageInfo, type UploadError } from "@/lib/api";
import { FIELDS, emptyValues, parseExtraction, type CellState, type FieldKey, type FieldValues } from "@/lib/fields";

// "pending": uploaded, waiting for the user to press extract. "queued": waiting for a free slot.
export type RowStatus = "pending" | "queued" | "extracting" | "done" | "error";

export interface Row {
  key: string;
  page: PageInfo;
  status: RowStatus;
  error?: string;
  values: FieldValues;
  confidence: Partial<Record<FieldKey, number>>;
  states: Record<FieldKey, CellState>;
  /** Cells a technician typed into. An edit is a human check, so it clears the flag. */
  edited: Partial<Record<FieldKey, true>>;
}

export type UploadPhase = "idle" | "uploading";

const EXTRACT_CONCURRENCY = 2;
// A rough, adjustable estimate of typing one form by hand - only used for the "time saved" badge.
export const MANUAL_SECONDS_PER_FORM = 90;

const pageKey = (p: PageInfo) => `${p.file_id}-${p.page_number}`;
const okStates = () => Object.fromEntries(FIELDS.map((f) => [f.key, "ok"])) as Record<FieldKey, CellState>;

const newRow = (page: PageInfo): Row => ({
  key: pageKey(page),
  page,
  status: "pending",
  values: emptyValues(),
  confidence: {},
  states: okStates(),
  edited: {},
});

export const cellState = (row: Row, key: FieldKey): CellState => (row.edited[key] ? "ok" : row.states[key]);

export function useBatch() {
  const [rows, setRows] = useState<Row[]>([]);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [uploadErrors, setUploadErrors] = useState<UploadError[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // Pages waiting for an extraction slot, and how many slots are busy.
  const queue = useRef<PageInfo[]>([]);
  const running = useRef(0);

  const patchRow = useCallback((key: string, patch: (row: Row) => Row) => {
    setRows((prev) => prev.map((r) => (r.key === key ? patch(r) : r)));
  }, []);

  const runOne = useCallback(
    async (page: PageInfo) => {
      const key = pageKey(page);
      patchRow(key, (r) => ({ ...r, status: "extracting", error: undefined }));
      try {
        const text = await extractPage(page.file_id, page.page_number);
        const parsed = parseExtraction(text);
        if (!parsed) throw new Error("תשובת החילוץ אינה בפורמט צפוי");
        // A re-extraction replaces the model's values, but keeps nothing the technician typed
        // on the old result - the new values are a fresh read of the page.
        patchRow(key, (r) => ({ ...r, ...parsed, status: "done", edited: {} }));
      } catch (e) {
        const error = e instanceof Error ? e.message : "החילוץ נכשל";
        patchRow(key, (r) => ({ ...r, status: "error", error }));
      }
    },
    [patchRow],
  );

  // Up to EXTRACT_CONCURRENCY workers drain the queue. A worker exits in the same tick it finds
  // the queue empty, so a page enqueued later always finds a free slot.
  const pump = useCallback(() => {
    const worker = async () => {
      for (let page = queue.current.shift(); page; page = queue.current.shift()) await runOne(page);
      running.current -= 1;
    };
    while (running.current < EXTRACT_CONCURRENCY && queue.current.length > 0) {
      running.current += 1;
      void worker();
    }
  }, [runOne]);

  const enqueue = useCallback(
    (pages: PageInfo[]) => {
      queue.current.push(...pages);
      pump();
    },
    [pump],
  );

  const addFiles = useCallback(
    async (list: FileList | File[]) => {
      const files = Array.from(list);
      if (files.length === 0) return;
      setPhase("uploading");
      setMessage(null);
      try {
        const result = await uploadFiles(files);
        setUploadErrors(result.errors);
        setRows((prev) => {
          const known = new Set(prev.map((r) => r.key));
          return [...prev, ...result.pages.filter((p) => !known.has(pageKey(p))).map(newRow)];
        });
      } catch (e) {
        setMessage(e instanceof Error ? e.message : "ההעלאה נכשלה");
      } finally {
        setPhase("idle");
      }
    },
    [],
  );

  /** Starts extraction for every uploaded page that has not been extracted yet. */
  const extractAll = useCallback(() => {
    const pages = rows.filter((r) => r.status === "pending").map((r) => r.page);
    if (pages.length === 0) return;
    const keys = new Set(pages.map(pageKey));
    setRows((prev) => prev.map((r) => (keys.has(r.key) ? { ...r, status: "queued" } : r)));
    enqueue(pages);
  }, [rows, enqueue]);

  const retry = useCallback(
    (key: string) => {
      const row = rows.find((r) => r.key === key);
      if (!row || row.status === "extracting") return;
      patchRow(key, (r) => ({ ...r, status: "queued", error: undefined }));
      enqueue([row.page]);
    },
    [rows, patchRow, enqueue],
  );

  const editCell = useCallback(
    (key: string, field: FieldKey, value: string) => {
      patchRow(key, (r) =>
        r.values[field] === value
          ? r
          : { ...r, values: { ...r.values, [field]: value }, edited: { ...r.edited, [field]: true } },
      );
    },
    [patchRow],
  );

  const removeRow = useCallback((key: string) => {
    queue.current = queue.current.filter((p) => pageKey(p) !== key);
    setRows((prev) => prev.filter((r) => r.key !== key));
  }, []);

  const stats = useMemo(() => {
    const done = rows.filter((r) => r.status === "done");
    const needReview = done.filter((r) => FIELDS.some((f) => cellState(r, f.key) !== "ok")).length;
    const pending = rows.filter((r) => r.status === "queued" || r.status === "extracting").length;
    return {
      total: rows.length,
      done: done.length,
      needReview,
      pending,
      awaiting: rows.filter((r) => r.status === "pending").length,
      failed: rows.filter((r) => r.status === "error").length,
      secondsSaved: done.length * MANUAL_SECONDS_PER_FORM,
    };
  }, [rows]);

  /** Generates the Excel file from the table as it is now, hand edits included. */
  const exportRows = useCallback(async () => {
    setExporting(true);
    setMessage(null);
    try {
      const blob = await exportExcel(
        rows
          .filter((r) => r.status === "done")
          .map((r) => ({ values: r.values, link: r.page.image_url })),
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "commitments.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "הייצוא נכשל");
    } finally {
      setExporting(false);
    }
  }, [rows]);

  return { rows, phase, uploadErrors, message, exporting, stats, addFiles, extractAll, retry, editCell, removeRow, exportRows };
}
