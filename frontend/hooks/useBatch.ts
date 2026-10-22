"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { exportExcel, extractPage, saveCalibrationRun, uploadFiles, type PageInfo, type UploadError } from "@/lib/api";
import { calibrate, fieldOutcome } from "@/lib/calibration";
import { findConflicts, type RowConflicts } from "@/lib/crossCheck";
import { cellId, pickSpotChecks } from "@/lib/spotCheck";
import { FIELDS, emptyValues, idLengthError, parseExtraction, type BarcodeSuggestion, type Box, type CellState, type FieldKey, type FieldValues } from "@/lib/fields";

// "pending": uploaded, waiting for the user to press extract. "queued": waiting for a free slot.
export type RowStatus = "pending" | "queued" | "extracting" | "done" | "error";

export interface Row {
  key: string;
  page: PageInfo;
  status: RowStatus;
  error?: string;
  values: FieldValues;
  /** The values as the model extracted them, before any hand edit. */
  original: FieldValues;
  /** The backend's CONF_OK_ABOVE (a score above it passes with no review). */
  okAbove: number;
  /** Where on the page each field was read, for the review mode. */
  regions: Partial<Record<FieldKey, Box[]>>;
  barcodes: string[];
  barcodeConfirmed: FieldKey[];
  barcodeSuggestions: BarcodeSuggestion[];
  confidence: Partial<Record<FieldKey, number>>;
  states: Record<FieldKey, CellState>;
  /** Cells a technician typed into or confirmed. A human check clears the flag. */
  edited: Partial<Record<FieldKey, true>>;
}

/** One cell waiting for a human look: flagged by the model or the validators, or in conflict with another row. */
export interface ReviewItem {
  rowKey: string;
  field: FieldKey;
  /** Not flagged by anything: picked at random so the calibration also measures the high scores. */
  spot?: boolean;
}

export type UploadPhase = "idle" | "uploading";

const EXTRACT_CONCURRENCY = 2;
// A rough, adjustable estimate of typing one form by hand - only used for the "time saved" badge.
export const MANUAL_SECONDS_PER_FORM = 90;

// One id per working session: exporting twice updates the same saved run instead of adding another.
// randomUUID only exists in secure contexts (https or localhost), hence the fallback.
const newRunId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

const pageKey = (p: PageInfo) => `${p.file_id}-${p.page_number}`;
const okStates = () => Object.fromEntries(FIELDS.map((f) => [f.key, "ok"])) as Record<FieldKey, CellState>;

const newRow = (page: PageInfo): Row => ({
  key: pageKey(page),
  page,
  status: "pending",
  values: emptyValues(),
  original: emptyValues(),
  okAbove: 70,
  regions: {},
  barcodes: [],
  barcodeConfirmed: [],
  barcodeSuggestions: [],
  confidence: {},
  states: okStates(),
  edited: {},
});

/** Who vouched for a field's final value, in words for the audit sheet. */
function auditStatus(row: Row, key: FieldKey): string {
  const outcome = fieldOutcome(row, key);
  if (outcome === "typed") return "תוקן ידנית";
  if (outcome === "confirmed") return "אושר ידנית";
  if (row.barcodeConfirmed.includes(key)) return "אושר על ידי ברקוד";
  return row.states[key] === "ok" ? "אוטומטי" : "לא נבדק (מסומן לבדיקה)";
}

export const cellState = (row: Row, key: FieldKey): CellState => (row.edited[key] ? "ok" : row.states[key]);

export function useBatch() {
  const [rows, setRows] = useState<Row[]>([]);
  const [phase, setPhase] = useState<UploadPhase>("idle");
  const [uploadErrors, setUploadErrors] = useState<UploadError[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  // Created on first use (an export click), not during render, so the prerender never sees a random value.
  const runIdRef = useRef<string | null>(null);
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

  /** The technician looked at the cell against the page and it is right: clears its flag. */
  const confirmCell = useCallback(
    (key: string, field: FieldKey) => patchRow(key, (r) => ({ ...r, edited: { ...r.edited, [field]: true } })),
    [patchRow],
  );

  const removeRow = useCallback((key: string) => {
    queue.current = queue.current.filter((p) => pageKey(p) !== key);
    setRows((prev) => prev.filter((r) => r.key !== key));
  }, []);

  // Recomputed from the live values, so fixing a cell clears its own warning.
  const conflicts = useMemo<Record<string, RowConflicts>>(
    () => findConflicts(rows.map((r) => ({ key: r.key, values: r.values }))),
    [rows],
  );

  // The spot-check sample is drawn from every field the model read and left unflagged, whether or
  // not it has been looked at yet (so a checked field does not shift the draw), and is shown only
  // while it is still unchecked.
  const spotChecks = useMemo(
    () =>
      pickSpotChecks(
        rows.flatMap((r) =>
          r.status !== "done"
            ? []
            : FIELDS.filter((f) => r.states[f.key] === "ok" && !conflicts[r.key]?.[f.key] && r.original[f.key].trim()).map((f) => ({
                rowKey: r.key,
                field: f.key,
              })),
        ),
      ),
    [rows, conflicts],
  );

  const reviewQueue = useMemo<ReviewItem[]>(
    () =>
      rows.flatMap((r) =>
        r.status !== "done"
          ? []
          : FIELDS.flatMap((f): ReviewItem[] => {
              if (cellState(r, f.key) !== "ok" || conflicts[r.key]?.[f.key]) return [{ rowKey: r.key, field: f.key }];
              const sampled = spotChecks.has(cellId({ rowKey: r.key, field: f.key })) && !r.edited[f.key];
              return sampled ? [{ rowKey: r.key, field: f.key, spot: true }] : [];
            }),
      ),
    [rows, conflicts, spotChecks],
  );

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
      conflictRows: Object.keys(conflicts).length,
      failed: rows.filter((r) => r.status === "error").length,
      secondsSaved: done.length * MANUAL_SECONDS_PER_FORM,
    };
  }, [rows, conflicts]);

  // The end of a run: what the technicians confirmed and corrected is written to the history file.
  // A failure here must not undo an export that already succeeded, so it only warns.
  const saveCalibration = useCallback(async () => {
    const c = calibrate(rows, FIELDS.map((f) => f.key));
    if (c.reviewed === 0) return;
    try {
      runIdRef.current ??= newRunId();
      await saveCalibrationRun({
        run_id: runIdRef.current,
        forms: rows.filter((r) => r.status === "done").length,
        reviewed: c.reviewed,
        correct: c.correct,
        ok_above: rows.find((r) => r.status === "done")?.okAbove ?? 70,
        bands: c.bands.map((b) => ({ low: b.from, high: b.to, reviewed: b.reviewed, correct: b.correct })),
        fields: c.fields,
        suggestion: { ...c.suggestion },
      });
    } catch {
      setMessage("הקובץ יוצא, אך שמירת נתוני הכיול נכשלה.");
    }
  }, [rows]);

  /** Generates the Excel file from the table as it is now, hand edits included. */
  const exportRows = useCallback(async () => {
    setMessage(null);
    // An ID number that is not exactly 9 digits blocks the export until it is fixed or emptied.
    const badRows = rows.flatMap((r, i) => (r.status === "done" && idLengthError(r.values.id_number) ? [i + 1] : []));
    if (badRows.length) {
      setMessage(`לא ניתן לייצא: תעודת הזהות ב${badRows.length === 1 ? "שורה" : "שורות"} ${badRows.join(", ")} אינה בת 9 ספרות. יש לתקן אותה או לרוקן את התא.`);
      return;
    }
    setExporting(true);
    try {
      const blob = await exportExcel(
        rows
          .filter((r) => r.status === "done")
          .map((r) => ({
            values: r.values,
            link: r.page.image_url,
            number: rows.indexOf(r) + 1,
            source: `${r.page.file_name} · עמוד ${r.page.page_number}`,
            audit: FIELDS.map((f) => ({
              field: f.label,
              original: r.original[f.key],
              final: r.values[f.key],
              confidence: r.confidence[f.key] ?? null,
              status: auditStatus(r, f.key),
            })),
          })),
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "commitments.xlsx";
      a.click();
      URL.revokeObjectURL(url);
      await saveCalibration();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : "הייצוא נכשל");
    } finally {
      setExporting(false);
    }
  }, [rows, saveCalibration]);

  return { rows, phase, uploadErrors, message, exporting, stats, addFiles, extractAll, retry, editCell, confirmCell, conflicts, reviewQueue, removeRow, exportRows };
}
